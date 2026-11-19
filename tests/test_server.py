"""Unit tests for server/server.py: address parsing, execute() with roles, serve().

Run (from the repository root):
    python -m unittest tests.test_server -v
"""

import socket
import threading
import unittest

from client.client import KVClient
from common.protocol import (
    ERR_FULL, ERR_KEY, ERR_UNKNOWN, MAX_KEYS, RESP_END, encode_line, format_sync, parse_entry,
)
from server.server import (
    ROLE_BACKUP, ROLE_PRIMARY, Node, execute, handle_client, parse_addr, parse_peers, serve,
)
from server.store import Store

HOST = "127.0.0.1"


class FakeReplicator:
    """Records replicate() and mark_alive() calls instead of sending anything."""

    def __init__(self, peers=()):
        self.calls = []
        self.alive_calls = []
        self._peers = list(peers)

    def replicate(self, seq, key, value):
        self.calls.append((seq, key, value))

    def mark_alive(self, host, port):
        self.alive_calls.append((host, port))

    def status(self):
        return {addr: "alive" for addr in self._peers}


def free_port():
    """Return a port that is free right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestParseAddr(unittest.TestCase):
    def test_gecerli(self):
        self.assertEqual(parse_addr("127.0.0.1:5001"), ("127.0.0.1", 5001))   # port is an int
        self.assertEqual(parse_addr("localhost:80"), ("localhost", 80))

    def test_gecersiz(self):
        for metin in ["bozuk", "127.0.0.1", "127.0.0.1:", "127.0.0.1:abc", ""]:
            with self.subTest(metin=metin):
                with self.assertRaises(ValueError):
                    parse_addr(metin)

    def test_peers(self):
        self.assertEqual(parse_peers("127.0.0.1:5001,127.0.0.1:5002"),
                         [("127.0.0.1", 5001), ("127.0.0.1", 5002)])
        self.assertEqual(parse_peers(" 127.0.0.1:5001 , 127.0.0.1:5002, "),   # spaces, trailing comma
                         [("127.0.0.1", 5001), ("127.0.0.1", 5002)])
        self.assertEqual(parse_peers(""), [])
        with self.assertRaises(ValueError):
            parse_peers("127.0.0.1:5001,bozuk")


class TestExecuteRolsuz(unittest.TestCase):
    """Without a role the server behaves exactly like Level 1."""

    def setUp(self):
        self.node = Node(Store())

    def test_set_get_stats(self):
        self.assertEqual(execute(self.node, "SET k1 merhaba dünya"), "OK")
        self.assertEqual(execute(self.node, "GET k1"), "VALUE merhaba dünya")
        self.assertEqual(execute(self.node, "GET yok"), "NOT_FOUND")
        self.assertEqual(execute(self.node, "STATS"), "STATS 1")

    def test_hatalar(self):
        self.assertEqual(execute(self.node, "SET  bos_key"), ERR_KEY)
        self.assertEqual(execute(self.node, "HELLO"), ERR_UNKNOWN)
        self.assertEqual(execute(self.node, "STATS"), "STATS 0")     # nothing was written

    def test_dolu_depo(self):
        for i in range(MAX_KEYS):
            self.assertEqual(execute(self.node, f"SET k{i} v"), "OK")
        self.assertEqual(execute(self.node, "SET yeni v"), ERR_FULL)
        self.assertEqual(execute(self.node, "SET k0 guncel"), "OK")  # update is still allowed


class TestExecutePrimary(unittest.TestCase):
    def setUp(self):
        self.rep = FakeReplicator()
        self.node = Node(Store(), role=ROLE_PRIMARY, replicator=self.rep)

    def test_basarili_set_kopyalanir(self):
        self.assertEqual(execute(self.node, "SET k1 merhaba dünya"), "OK")
        self.assertEqual(execute(self.node, "SET k2 x"), "OK")
        self.assertEqual(execute(self.node, "SET k1 yeni"), "OK")
        # one REPL per successful SET, with the seq the store assigned
        self.assertEqual(self.rep.calls, [(1, "k1", "merhaba dünya"), (2, "k2", "x"), (3, "k1", "yeni")])

    def test_basarisiz_set_kopyalanmaz(self):
        self.assertEqual(execute(self.node, "SET  bos_key"), ERR_KEY)
        for i in range(MAX_KEYS):
            execute(self.node, f"SET k{i} v")
        self.rep.calls.clear()
        self.assertEqual(execute(self.node, "SET yeni v"), ERR_FULL)
        self.assertEqual(self.rep.calls, [])                         # nothing written, nothing replicated

    def test_okuma_kopyalanmaz(self):
        execute(self.node, "SET k1 v")
        self.rep.calls.clear()
        execute(self.node, "GET k1")
        execute(self.node, "STATS")
        self.assertEqual(self.rep.calls, [])


class TestExecuteBackup(unittest.TestCase):
    def setUp(self):
        self.node = Node(Store(), role=ROLE_BACKUP, primary=(HOST, 5000))

    def test_client_set_reddedilir(self):
        self.assertEqual(execute(self.node, "SET k1 v"), "ERR NOT_PRIMARY 5000")
        self.assertEqual(execute(self.node, "STATS"), "STATS 0")     # nothing was written

    def test_repl_uygulanir(self):
        self.assertEqual(execute(self.node, "REPL 7 k1 merhaba dünya"), "OK")
        self.assertEqual(execute(self.node, "GET k1"), "VALUE merhaba dünya")   # reads work on a backup
        self.assertEqual(self.node.store.snapshot(), [(7, "k1", "merhaba dünya")])

    def test_eski_repl_ok_ama_yazilmaz(self):
        execute(self.node, "REPL 8 k1 sekiz")
        self.assertEqual(execute(self.node, "REPL 7 k1 yedi"), "OK")  # old seq: ignored, still OK
        self.assertEqual(execute(self.node, "GET k1"), "VALUE sekiz")

    def test_dolu_backup_err_full(self):
        for i in range(MAX_KEYS):
            execute(self.node, f"REPL {i + 1} k{i} v")
        self.assertEqual(execute(self.node, "REPL 999 yeni v"), ERR_FULL)

    def test_bozuk_repl(self):
        self.assertEqual(execute(self.node, "REPL abc k1 v"), ERR_UNKNOWN)   # checked by parse_line
        self.assertEqual(execute(self.node, "REPL 7"), ERR_KEY)

    def test_sync_backupta_reddedilir(self):
        self.assertEqual(execute(self.node, "SYNC 5001"), ERR_UNKNOWN)


class TestYanlisRol(unittest.TestCase):
    """REPL is only for backups, SYNC only for the primary."""

    def test_rolsuz_sunucu(self):
        node = Node(Store())
        self.assertEqual(execute(node, "REPL 1 k1 v"), ERR_UNKNOWN)
        self.assertEqual(execute(node, "SYNC 5001"), ERR_UNKNOWN)
        self.assertEqual(execute(node, "STATS"), "STATS 0")

    def test_primary_repl_kabul_etmez(self):
        node = Node(Store(), role=ROLE_PRIMARY, replicator=FakeReplicator())
        self.assertEqual(execute(node, "REPL 1 k1 v"), ERR_UNKNOWN)
        self.assertEqual(execute(node, "STATS"), "STATS 0")


class TestSyncPrimary(unittest.TestCase):
    """handle_client sends SYNC to serve_sync on a primary (multi-line answer)."""

    def test_sync_tum_depoyu_gonderir_ve_mark_alive_cagirir(self):
        rep = FakeReplicator(peers=[(HOST, 5001)])
        node = Node(Store(), role=ROLE_PRIMARY, replicator=rep)
        execute(node, "SET k1 merhaba dünya")
        execute(node, "SET k2 x")

        server_side, client_side = socket.socketpair()
        t = threading.Thread(target=handle_client, args=(server_side, (HOST, 40000), node), daemon=True)
        t.start()
        self.addCleanup(client_side.close)

        client_side.sendall(encode_line(format_sync(5001)) + encode_line("STATS"))
        reader = client_side.makefile("r", encoding="utf-8", newline="\n")
        lines = []
        while True:
            line = reader.readline().rstrip("\n")
            lines.append(line)
            if line == RESP_END:
                break
        self.assertEqual([parse_entry(l) for l in lines[:-1]], [(1, "k1", "merhaba dünya"), (2, "k2", "x")])
        self.assertEqual(rep.alive_calls, [(HOST, 5001)])        # host from the connection, port from SYNC
        self.assertEqual(reader.readline().rstrip("\n"), "STATS 2")   # the connection stays usable


class TestServe(unittest.TestCase):
    def start(self, on_ready=None):
        """Run serve() in a background thread on a free port; stopped when the test ends."""
        port = free_port()
        stop = threading.Event()
        node = Node(Store())
        t = threading.Thread(target=serve, args=(HOST, port, node),
                             kwargs={"on_ready": on_ready, "stop_event": stop}, daemon=True)
        t.start()

        def durdur():
            stop.set()
            t.join(timeout=3)            # the accept loop notices stop_event within about 1 s

        self.addCleanup(durdur)
        return port

    def baglan(self, port):
        """Connect to the server, retrying briefly until it listens."""
        son_hata = None
        for _ in range(50):
            c = KVClient(HOST, port, timeout=2.0)
            try:
                c.connect()
                self.addCleanup(c.close)
                return c
            except Exception as e:       # not listening yet
                son_hata = e
                threading.Event().wait(0.05)
        self.fail(f"server did not start: {son_hata}")

    def test_ag_uzerinden_set_get(self):
        port = self.start()
        c = self.baglan(port)
        self.assertEqual(c.send_command("SET a 1"), "OK")
        self.assertEqual(c.send_command("GET a"), "VALUE 1")

    def test_on_ready_sirasinda_baglanti_kabul_edilir(self):
        # The start-up SYNC of a backup runs in on_ready. While it runs, the server must
        # already answer other connections (the primary sends REPL right after mark_alive).
        sonuc = {}
        bitti = threading.Event()
        port_kutusu = {}

        def on_ready():
            # Talk to OUR OWN server from inside on_ready, like the primary would
            with KVClient(HOST, port_kutusu["port"], timeout=2.0) as c:
                sonuc["cevap"] = c.send_command("STATS")
            bitti.set()

        port = free_port()
        port_kutusu["port"] = port
        stop = threading.Event()
        t = threading.Thread(target=serve, args=(HOST, port, Node(Store())),
                             kwargs={"on_ready": on_ready, "stop_event": stop}, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(timeout=3)))

        self.assertTrue(bitti.wait(timeout=5), "on_ready did not finish: server was not accepting")
        self.assertEqual(sonuc["cevap"], "STATS 0")

    def test_stop_event_donguyu_bitirir(self):
        port = free_port()
        stop = threading.Event()
        t = threading.Thread(target=serve, args=(HOST, port, Node(Store())),
                             kwargs={"stop_event": stop}, daemon=True)
        t.start()
        self.baglan(port)                # the server is up
        stop.set()
        t.join(timeout=3)                # the accept loop wakes up at least once per second
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()