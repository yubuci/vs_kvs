"""Unit tests for client/client.py: KVClient timeout rule, ClusterClient routing, command line.

The servers are fake: small in-process TCP servers that record every line they
receive and answer with a fixed (or computed) response, like FakeBackup in
tests/test_replication.py. No real server process is needed for the routing tests.

Run (from the repository root):
    python -m unittest tests.test_client_routing -v
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from client.client import ClusterClient, KVClient, KVError, KVTimeout
from common.config import ConfigError

HOST = "127.0.0.1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repository root


class FakeServer:
    """A minimal server: records received lines and answers each one.

    response may be
        a str        -> always this answer
        a callable   -> called with the line, returns the answer (str) or None for "stay silent"
    """

    def __init__(self, response="OK"):
        self.response = response
        self.lines = []                       # every line received, in arrival order
        self.connection_count = 0             # how many TCP connections were accepted
        self._lock = threading.Lock()         # protects lines, connection_count and _conns
        self._conns = []
        self._stop = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind((HOST, 0))             # port 0: the OS picks a free port
        self._srv.listen()
        self._srv.settimeout(0.1)             # lets the accept loop notice _stop
        self.port = self._srv.getsockname()[1]
        self.addr = (HOST, self.port)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break                         # listening socket was closed
            conn.settimeout(None)
            with self._lock:
                self._conns.append(conn)
                self.connection_count += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            reader = conn.makefile("r", encoding="utf-8", newline="\n")
            for line in reader:
                line = line.rstrip("\r\n")
                with self._lock:
                    self.lines.append(line)
                answer = self.response(line) if callable(self.response) else self.response
                if answer is not None:
                    conn.sendall((answer + "\n").encode("utf-8"))
        except OSError:
            pass                              # connection closed by the test

    def received(self):
        with self._lock:
            return list(self.lines)

    def drop_connections(self):
        """Close all open connections but keep listening (simulates a short network break)."""
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self):
        self._stop = True
        self.drop_connections()
        self._srv.close()


def closed_port():
    """Return a port on which nothing is listening."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def entry(addr, role):
    return {"host": addr[0], "port": addr[1], "role": role}


class FakeServerTestCase(unittest.TestCase):
    def fake(self, response="OK"):
        """Start a fake server that is closed automatically when the test ends."""
        s = FakeServer(response)
        self.addCleanup(s.close)
        return s

    def cluster(self, *servers, timeout=2.0):
        """Create a ClusterClient whose connections are closed when the test ends."""
        c = ClusterClient(list(servers), timeout=timeout)
        self.addCleanup(c.close)
        return c


class TestKVClientTimeout(FakeServerTestCase):
    def test_timeout_is_not_retried(self):
        # The server answers the first line and then goes silent (a "hung" server).
        answers = iter(["OK"])
        server = self.fake(lambda line: next(answers, None))
        c = KVClient(HOST, server.port, timeout=0.4)
        self.addCleanup(c.close)
        self.assertEqual(c.send_command("A"), "OK")          # now the connection is open

        t0 = time.perf_counter()
        with self.assertRaises(KVTimeout):
            c.send_command("B")
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 0.7)                        # one timeout (~0.4 s), not two (~0.8 s)
        self.assertEqual(server.received(), ["A", "B"])      # "B" was sent exactly once

    def test_kvtimeout_is_a_kverror(self):
        self.assertTrue(issubclass(KVTimeout, KVError))      # old "except KVError" code keeps working

    def test_broken_connection_is_still_retried_once(self):
        # The retry rule for a DROPPED connection must survive the timeout change
        server = self.fake("OK")
        c = KVClient(HOST, server.port, timeout=2.0)
        self.addCleanup(c.close)
        self.assertEqual(c.send_command("A"), "OK")
        server.drop_connections()
        self.assertEqual(c.send_command("B"), "OK")          # reconnected transparently
        self.assertEqual(server.connection_count, 2)


class TestRouting(FakeServerTestCase):
    def test_set_goes_to_primary(self):
        primary, backup = self.fake("OK"), self.fake("OK")
        c = self.cluster(entry(primary.addr, "primary"), entry(backup.addr, "backup"))
        self.assertEqual(c.send_command("SET k1 merhaba dünya"), "OK")
        self.assertEqual(primary.received(), ["SET k1 merhaba dünya"])
        self.assertEqual(backup.received(), [])

    def test_set_finds_primary_by_role_not_by_position(self):
        backup, primary = self.fake("OK"), self.fake("OK")
        c = self.cluster(entry(backup.addr, "backup"), entry(primary.addr, "primary"))   # primary is second
        c.send_command("SET k1 v")
        self.assertEqual(primary.received(), ["SET k1 v"])
        self.assertEqual(backup.received(), [])

    def test_lowercase_set_is_a_write_too(self):
        primary, backup = self.fake("OK"), self.fake("OK")
        c = self.cluster(entry(primary.addr, "primary"), entry(backup.addr, "backup"))
        c.send_command("set k1 v")
        self.assertEqual(primary.received(), ["set k1 v"])

    def test_get_uses_first_reachable_server(self):
        first, second = self.fake("VALUE a"), self.fake("VALUE b")
        c = self.cluster(entry(first.addr, "primary"), entry(second.addr, "backup"))
        self.assertEqual(c.send_command("GET k1"), "VALUE a")
        self.assertEqual(second.received(), [])               # never contacted

    def test_get_skips_dead_first_server(self):
        dead = (HOST, closed_port())
        alive = self.fake("VALUE b")
        c = self.cluster(entry(dead, "primary"), entry(alive.addr, "backup"))
        self.assertEqual(c.send_command("GET k1"), "VALUE b")

    def test_stats_is_routed_like_get(self):
        dead = (HOST, closed_port())
        alive = self.fake("STATS 3")
        c = self.cluster(entry(dead, "primary"), entry(alive.addr, "backup"))
        self.assertEqual(c.send_command("STATS"), "STATS 3")

    def test_connection_is_reused(self):
        server = self.fake("VALUE x")
        c = self.cluster(entry(server.addr, "primary"))
        for _ in range(3):
            c.send_command("GET k1")
        self.assertEqual(server.connection_count, 1)           # one TCP connection, three commands

    def test_server_error_is_returned_as_it_is(self):
        server = self.fake("ERR KEY")
        c = self.cluster(entry(server.addr, "primary"))
        self.assertEqual(c.send_command("SET  k v"), "ERR KEY")   # the client does not judge, the server does


class TestNotPrimaryRedirect(FakeServerTestCase):
    def test_redirect_repeats_command_on_given_port(self):
        real = self.fake("OK")
        wrong = self.fake(lambda line: f"ERR NOT_PRIMARY {real.port}")     # a stale config points at the wrong node
        c = self.cluster(entry(wrong.addr, "primary"), entry(real.addr, "backup"))
        self.assertEqual(c.send_command("SET k1 v"), "OK")
        self.assertEqual(wrong.received(), ["SET k1 v"])
        self.assertEqual(real.received(), ["SET k1 v"])

    def test_redirect_target_need_not_be_in_the_list(self):
        real = self.fake("OK")
        wrong = self.fake(lambda line: f"ERR NOT_PRIMARY {real.port}")
        c = self.cluster(entry(wrong.addr, "primary"))                    # real is not listed at all
        self.assertEqual(c.send_command("SET k1 v"), "OK")

    def test_redirect_is_followed_only_once(self):
        # Two nodes that point at each other: without the "once" rule this would loop forever
        a = self.fake()
        b = self.fake(lambda line: f"ERR NOT_PRIMARY {a.port}")
        a.response = lambda line: f"ERR NOT_PRIMARY {b.port}"
        c = self.cluster(entry(a.addr, "primary"), entry(b.addr, "backup"))
        self.assertEqual(c.send_command("SET k1 v"), f"ERR NOT_PRIMARY {a.port}")   # returned, not followed
        self.assertEqual(len(a.received()), 1)
        self.assertEqual(len(b.received()), 1)

    def test_redirect_to_dead_port_raises(self):
        wrong = self.fake(lambda line: f"ERR NOT_PRIMARY {closed_port()}")
        c = self.cluster(entry(wrong.addr, "primary"))
        with self.assertRaises(KVError):
            c.send_command("SET k1 v")


class TestFailure(FakeServerTestCase):
    def test_primary_down_set_fails_but_get_works_from_backup(self):
        # The Level 2 demo scenario: "kill the primary: GET works, what does SET return?"
        dead_primary = (HOST, closed_port())
        backup = self.fake("VALUE merhaba")
        c = self.cluster(entry(dead_primary, "primary"), entry(backup.addr, "backup"))
        self.assertEqual(c.send_command("GET k1"), "VALUE merhaba")
        with self.assertRaises(KVError) as ctx:
            c.send_command("SET k1 yeni")
        self.assertIn("Primary", str(ctx.exception))
        self.assertEqual(backup.received(), ["GET k1"])        # the SET was NOT sent to the backup

    def test_all_servers_down_gives_clear_error(self):
        dead1, dead2 = (HOST, closed_port()), (HOST, closed_port())
        c = self.cluster(entry(dead1, "primary"), entry(dead2, "backup"))
        with self.assertRaises(KVError) as ctx:
            c.send_command("GET k1")
        self.assertIn("Hiçbir sunucuya", str(ctx.exception))
        self.assertIn(str(dead1[1]), str(ctx.exception))       # the message names the addresses it tried

    def test_connect_fails_only_when_nobody_is_reachable(self):
        alive = self.fake("OK")
        dead = (HOST, closed_port())
        self.cluster(entry(dead, "primary"), entry(alive.addr, "backup")).connect()   # one is enough
        with self.assertRaises(KVError):
            self.cluster(entry(dead, "primary")).connect()

    def test_cluster_without_primary_is_rejected(self):
        with self.assertRaises(ConfigError):
            ClusterClient([entry((HOST, 5001), "backup")])


class TestCommandLine(FakeServerTestCase):
    """Runs `python -m client.client ...` as a real process (like a user would)."""

    def run_client(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")       # Turkish characters on Windows consoles
        return subprocess.run([sys.executable, "-m", "client.client", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=20)

    def servers_file(self, *servers):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "servers.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"servers": list(servers)}, f)
        return path

    def test_port_mode_is_unchanged_level_1_behaviour(self):
        server = self.fake("VALUE merhaba dünya")
        result = self.run_client("--port", str(server.port), "GET", "k1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "VALUE merhaba dünya")

    def test_servers_mode_routes_set_to_primary(self):
        primary, backup = self.fake("OK"), self.fake("OK")
        path = self.servers_file(entry(primary.addr, "primary"), entry(backup.addr, "backup"))
        result = self.run_client("--servers", path, "SET", "k1", "merhaba", "dünya")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "OK")
        self.assertEqual(primary.received(), ["SET k1 merhaba dünya"])   # arguments joined with one space
        self.assertEqual(backup.received(), [])

    def test_server_err_gives_exit_code_1(self):
        server = self.fake("ERR KEY")
        path = self.servers_file(entry(server.addr, "primary"))
        self.assertEqual(self.run_client("--servers", path, "SET", "k1", "v").returncode, 1)

    def test_all_down_gives_exit_code_2_and_no_traceback(self):
        path = self.servers_file(entry((HOST, closed_port()), "primary"))
        result = self.run_client("--servers", path, "GET", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Hata:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_servers_file_gives_clear_error(self):
        result = self.run_client("--servers", os.path.join(ROOT, "yok_servers.json"), "GET", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_port_and_servers_together_are_rejected(self):
        result = self.run_client("--port", "5000", "--servers", "servers.json", "GET", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("birlikte", result.stderr)

    def test_neither_port_nor_servers_is_rejected(self):
        result = self.run_client("GET", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--port ya da --servers", result.stderr)


if __name__ == "__main__":
    unittest.main()
