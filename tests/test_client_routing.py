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

from client.client import ClusterClient, KVClient, KVError, KVTimeout, ShardedClient, format_stats_report
from common.config import ConfigError
from common.ring import make_placement, node_id

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


class TestKVClientClose(FakeServerTestCase):
    def test_close_releases_the_file_descriptor(self):
        # Regression: close() used to close only the socket; the makefile() reader kept the fd open
        client = KVClient(*self.fake("OK").addr)
        client.send_command("GET k1")
        sock, reader = client._sock, client._reader
        client.close()
        self.assertTrue(reader.closed)
        self.assertEqual(sock.fileno(), -1)          # -1 = the descriptor is really gone

    def test_close_twice_and_before_connect_is_harmless(self):
        client = KVClient(HOST, closed_port())
        client.close()
        client.close()


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
        self.assertIn("--port, --servers ya da --cluster", result.stderr)


# ---------------------------------------------------------------------------
# Level 3: ShardedClient
# ---------------------------------------------------------------------------

def shard_entry(addr):
    return {"host": addr[0], "port": addr[1]}


def key_owned_by(place, node, prefix="k"):
    """Find a key that the placement function gives to `node` (the tests never guess hash values)."""
    for i in range(10000):
        key = f"{prefix}{i}"
        if place(key) == node:
            return key
    raise AssertionError(f"no key found for {node}")


class ShardedTestCase(FakeServerTestCase):
    def three(self, response="OK"):
        """Three fake servers; returns (servers, entries) with entries in the same order."""
        servers = [self.fake(response) for _ in range(3)]
        return servers, [shard_entry(s.addr) for s in servers]

    def sharded(self, entries, placement="ring", vnodes=50, timeout=2.0):
        c = ShardedClient(entries, placement=placement, vnodes=vnodes, timeout=timeout)
        self.addCleanup(c.close)
        return c

    @staticmethod
    def place_for(servers, placement="ring", vnodes=50):
        """The expected owner, computed from the same single source of truth the client uses."""
        return make_placement(placement, [node_id(*s.addr) for s in servers], vnodes)


class TestShardedRouting(ShardedTestCase):
    def test_set_and_get_go_only_to_the_owner(self):
        servers, entries = self.three()
        c = self.sharded(entries)
        place = self.place_for(servers)
        for server in servers:
            key = key_owned_by(place, node_id(*server.addr))
            c.send_command(f"SET {key} merhaba dünya")
            c.send_command(f"GET {key}")
            self.assertEqual(server.received()[-2:], [f"SET {key} merhaba dünya", f"GET {key}"])
        # every server got exactly its own two commands, nobody got another server's
        self.assertEqual([len(s.received()) for s in servers], [2, 2, 2])

    def test_lowercase_commands_are_routed_too(self):
        servers, entries = self.three()
        c = self.sharded(entries)
        key = key_owned_by(self.place_for(servers), node_id(*servers[2].addr))
        c.send_command(f"get {key}")
        c.send_command(f"set {key} v")
        self.assertEqual(servers[2].received(), [f"get {key}", f"set {key} v"])

    def test_modn_placement_is_used_when_asked(self):
        servers, entries = self.three()
        c = self.sharded(entries, placement="modn")
        place = self.place_for(servers, "modn")
        for server in servers:
            key = key_owned_by(place, node_id(*server.addr))
            c.send_command(f"GET {key}")
            self.assertEqual(server.received(), [f"GET {key}"])

    def test_keys_are_spread_over_all_servers(self):
        # Guards against a client that always asks the same server
        servers, entries = self.three()
        c = self.sharded(entries)
        for i in range(300):
            c.send_command(f"SET key{i} v")
        counts = [len(s.received()) for s in servers]
        self.assertEqual(sum(counts), 300)
        self.assertTrue(all(count > 30 for count in counts), counts)

    def test_same_key_always_the_same_server(self):
        servers, entries = self.three()
        c = self.sharded(entries)
        for _ in range(5):
            c.send_command("GET abc")
        self.assertEqual(sorted(len(s.received()) for s in servers), [0, 0, 5])

    def test_connection_is_reused(self):
        servers, entries = self.three()
        c = self.sharded(entries)
        key = key_owned_by(self.place_for(servers), node_id(*servers[0].addr))
        c.send_command(f"GET {key}")
        c.send_command(f"GET {key}")
        self.assertEqual(servers[0].connection_count, 1)

    def test_server_answers_are_returned_unchanged(self):
        servers, entries = self.three("ERR FULL")
        c = self.sharded(entries)
        self.assertEqual(c.send_command("SET k1 v"), "ERR FULL")

    def test_invalid_key_is_still_routed_and_the_server_decides(self):
        # The client does not validate: a 17-character key goes to its owner, who says ERR KEY
        servers, entries = self.three("ERR KEY")
        c = self.sharded(entries)
        self.assertEqual(c.send_command("SET " + "a" * 17 + " v"), "ERR KEY")
        self.assertEqual(sum(len(s.received()) for s in servers), 1)

    def test_lines_without_a_key_go_to_the_first_server(self):
        servers, entries = self.three("ERR KEY")
        c = self.sharded(entries)
        for line in ("SET", "GET", "SET  v", "GET ", "FOO bar", "STATS extra", ""):
            with self.subTest(line=line):
                self.assertEqual(c.send_command(line), "ERR KEY")
        self.assertEqual(servers[0].received(), ["SET", "GET", "SET  v", "GET ", "FOO bar", "STATS extra", ""])
        self.assertEqual(servers[1].received() + servers[2].received(), [])

    def test_constructor_rejects_bad_input(self):
        _, entries = self.three()
        with self.assertRaises(ValueError):
            ShardedClient([])                                   # no servers
        with self.assertRaises(ValueError):
            ShardedClient(entries + [entries[0]])               # the same server twice
        with self.assertRaises(ValueError):
            ShardedClient(entries, placement="hash")            # unknown mode
        with self.assertRaises(ValueError):
            ShardedClient(entries, vnodes=0)


class TestShardedRedirect(ShardedTestCase):
    def test_wrong_node_is_followed_once_on_the_same_host(self):
        real = self.fake("OK")
        stale = self.fake(lambda line: f"ERR WRONG_NODE {real.port}")
        # a config whose ring points at `stale`, while the data lives on `real`
        c = self.sharded([shard_entry(stale.addr), shard_entry(real.addr)])
        key = key_owned_by(self.place_for([stale, real]), node_id(*stale.addr))
        self.assertEqual(c.send_command(f"SET {key} v"), "OK")
        self.assertEqual(stale.received(), [f"SET {key} v"])
        self.assertEqual(real.received(), [f"SET {key} v"])

    def test_redirect_target_need_not_be_in_the_list(self):
        real = self.fake("VALUE x")
        stale = self.fake(lambda line: f"ERR WRONG_NODE {real.port}")
        c = self.sharded([shard_entry(stale.addr)])             # one-server config: stale owns everything
        self.assertEqual(c.send_command("GET k1"), "VALUE x")

    def test_second_wrong_node_is_an_error_and_there_is_no_loop(self):
        a = self.fake()
        b = self.fake(lambda line: f"ERR WRONG_NODE {a.port}")
        a.response = lambda line: f"ERR WRONG_NODE {b.port}"
        c = self.sharded([shard_entry(a.addr), shard_entry(b.addr)])
        with self.assertRaises(KVError) as ctx:
            c.send_command("GET k1")
        self.assertIn("anlaşamıyor", str(ctx.exception))
        self.assertEqual(len(a.received()) + len(b.received()), 2)      # asked once each, then stopped

    def test_redirect_to_a_dead_port_raises(self):
        stale = self.fake(lambda line: f"ERR WRONG_NODE {closed_port()}")
        c = self.sharded([shard_entry(stale.addr)])
        with self.assertRaises(KVError):
            c.send_command("GET k1")

    def test_not_primary_is_not_followed(self):
        # NOT_PRIMARY is a Level 2 message; the sharded client must not treat it as a redirect
        other = self.fake("OK")
        odd = self.fake(lambda line: f"ERR NOT_PRIMARY {other.port}")
        c = self.sharded([shard_entry(odd.addr)])
        self.assertEqual(c.send_command("SET k1 v"), f"ERR NOT_PRIMARY {other.port}")
        self.assertEqual(other.received(), [])


class TestShardedFailure(ShardedTestCase):
    def test_owner_down_fails_but_other_owners_still_work(self):
        servers, entries = self.three()
        place = self.place_for(servers)
        dead_id = node_id(*servers[1].addr)
        dead_key = key_owned_by(place, dead_id)
        live_key = key_owned_by(place, node_id(*servers[0].addr))
        servers[1].close()
        c = self.sharded(entries)
        with self.assertRaises(KVError) as ctx:
            c.send_command(f"GET {dead_key}")
        self.assertIn(dead_id, str(ctx.exception))                       # says WHICH server
        self.assertEqual(c.send_command(f"GET {live_key}"), "OK")

    def test_there_is_no_failover(self):
        # A dead owner must never make the client write the key to a different server
        servers, entries = self.three()
        dead_key = key_owned_by(self.place_for(servers), node_id(*servers[1].addr))
        servers[1].close()
        c = self.sharded(entries)
        with self.assertRaises(KVError):
            c.send_command(f"SET {dead_key} v")
        self.assertEqual(servers[0].received() + servers[2].received(), [])

    def test_connect_needs_only_one_reachable_server(self):
        live = self.fake()
        c = self.sharded([shard_entry((HOST, closed_port())), shard_entry(live.addr)])
        c.connect()                                                      # no exception

    def test_connect_fails_when_nobody_is_reachable(self):
        c = self.sharded([shard_entry((HOST, closed_port())), shard_entry((HOST, closed_port()))])
        with self.assertRaises(KVError):
            c.connect()


class TestShardedStats(ShardedTestCase):
    def test_total_is_the_sum_over_all_servers(self):
        servers = [self.fake("STATS 5"), self.fake("STATS 7"), self.fake("STATS 0")]    # 0 is a valid count
        c = self.sharded([shard_entry(s.addr) for s in servers])
        self.assertEqual(c.send_command("STATS"), "STATS 12")
        self.assertEqual(c.send_command("stats"), "STATS 12")
        for server in servers:
            # the client sends the canonical upper-case command, whatever case the user typed
            self.assertEqual(server.received(), ["STATS", "STATS"])

    def test_per_server_counts_keep_config_order(self):
        servers = [self.fake("STATS 5"), self.fake("STATS 7"), self.fake("STATS 0")]
        c = self.sharded([shard_entry(s.addr) for s in servers])
        counts = c.stats_per_server()
        self.assertEqual(list(counts), [node_id(*s.addr) for s in servers])
        self.assertEqual(list(counts.values()), [5, 7, 0])

    def test_dead_server_is_none_and_the_total_refuses_to_guess(self):
        live = self.fake("STATS 5")
        dead = (HOST, closed_port())
        c = self.sharded([shard_entry(live.addr), shard_entry(dead)])
        counts = c.stats_per_server()
        self.assertEqual(counts, {node_id(*live.addr): 5, node_id(*dead): None})
        with self.assertRaises(KVError) as ctx:
            c.send_command("STATS")
        self.assertIn(node_id(*dead), str(ctx.exception))

    def test_unusable_answers_count_as_none(self):
        for answer in ("ERR UNKNOWN", "STATS", "STATS abc", "STATS -1", "STATS 5 6", "OK", "STATS \u0665"):
            with self.subTest(answer=answer):
                server = self.fake(answer)
                c = self.sharded([shard_entry(server.addr)])
                self.assertEqual(c.stats_per_server(), {node_id(*server.addr): None})

    def test_report_text(self):
        full = format_stats_report({"127.0.0.1:5000": 5, "127.0.0.1:5001": 7})
        self.assertEqual(full, "STATS 12\n  127.0.0.1:5000: 5\n  127.0.0.1:5001: 7")
        partial = format_stats_report({"127.0.0.1:5000": 5, "127.0.0.1:5001": None})
        self.assertIn("STATS 5 (EKSİK: 1", partial)
        self.assertIn("127.0.0.1:5001: ulaşılamıyor", partial)


class TestShardedCommandLine(FakeServerTestCase):
    """Runs `python -m client.client --cluster ...` as a real process."""

    def run_client(self, *args, stdin=None):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-m", "client.client", *args], cwd=ROOT, env=env, input=stdin,
                              capture_output=True, text=True, encoding="utf-8", timeout=20)

    def shard_file(self, *entries, **extra):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "servers_level3.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(extra, servers=list(entries)), f)
        return path

    def setup_three(self, **extra):
        servers = [self.fake("OK") for _ in range(3)]
        return servers, self.shard_file(*(shard_entry(s.addr) for s in servers), **extra)

    def test_cluster_mode_routes_to_the_owner(self):
        servers, path = self.setup_three(placement="ring", vnodes=50)
        place = make_placement("ring", [node_id(*s.addr) for s in servers], 50)
        key = key_owned_by(place, node_id(*servers[1].addr))
        result = self.run_client("--cluster", path, "SET", key, "merhaba", "dünya")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "OK"))
        self.assertEqual(servers[1].received(), [f"SET {key} merhaba dünya"])
        self.assertEqual(servers[0].received() + servers[2].received(), [])

    def test_placement_option_overrides_the_file(self):
        servers, path = self.setup_three(placement="ring", vnodes=50)
        ids = [node_id(*s.addr) for s in servers]
        ring, modn = make_placement("ring", ids, 50), make_placement("modn", ids)
        key = next(f"k{i}" for i in range(10000) if ring(f"k{i}") != modn(f"k{i}"))   # a key the modes disagree on
        self.run_client("--cluster", path, "--placement", "modn", "GET", key)
        owner = servers[ids.index(modn(key))]
        self.assertEqual(owner.received(), [f"GET {key}"])

    def test_wrong_node_is_corrected_end_to_end(self):
        real = self.fake("VALUE x")
        stale = self.fake(lambda line: f"ERR WRONG_NODE {real.port}")
        path = self.shard_file(shard_entry(stale.addr))
        result = self.run_client("--cluster", path, "GET", "k1")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "VALUE x"))

    def test_owner_down_gives_exit_code_2_and_no_traceback(self):
        path = self.shard_file(shard_entry((HOST, closed_port())))
        result = self.run_client("--cluster", path, "GET", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Hata:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_level2_file_is_rejected_with_a_clear_message(self):
        path = self.shard_file({"host": HOST, "port": 5000, "role": "primary"})
        result = self.run_client("--cluster", path, "GET", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Level 2", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_option_combinations_are_rejected(self):
        for args in (("--cluster", "a.json", "--port", "5000"),
                     ("--cluster", "a.json", "--servers", "b.json"),
                     ("--port", "5000", "--servers", "b.json")):
            with self.subTest(args=args):
                result = self.run_client(*args, "GET", "k1")
                self.assertEqual(result.returncode, 2)
                self.assertIn("birlikte", result.stderr)
        result = self.run_client("--placement", "modn", "--port", "5000", "GET", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--placement yalnızca --cluster", result.stderr)

    def test_unknown_placement_value_is_rejected(self):
        result = self.run_client("--cluster", "a.json", "--placement", "random", "GET", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_shell_stats_shows_total_and_per_server(self):
        servers = [self.fake("STATS 5"), self.fake("STATS 7"), self.fake("STATS 0")]
        path = self.shard_file(*(shard_entry(s.addr) for s in servers))
        result = self.run_client("--cluster", path, stdin="STATS\nexit\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("STATS 12", result.stdout)
        for server, count in zip(servers, (5, 7, 0)):
            self.assertIn(f"{node_id(*server.addr)}: {count}", result.stdout)

    def test_shell_stats_with_a_dead_server_shows_it(self):
        live = self.fake("STATS 5")
        dead = (HOST, closed_port())
        path = self.shard_file(shard_entry(live.addr), shard_entry(dead))
        result = self.run_client("--cluster", path, stdin="STATS\nexit\n")
        self.assertIn("EKSİK", result.stdout)
        self.assertIn(f"{node_id(*dead)}: ulaşılamıyor", result.stdout)


if __name__ == "__main__":
    unittest.main()