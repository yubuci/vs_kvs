"""Unit tests for server/server.py: address parsing, execute() with roles, serve().

Run (from the repository root):
    python -m unittest tests.test_server -v
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from client.client import KVClient
from common.protocol import (
    ERR_FULL, ERR_KEY, ERR_UNKNOWN, MAX_KEYS, RESP_END, encode_line, format_sync, parse_entry,
)
from common.config import ConfigError, load_shard_config
from common.ring import HashRing, modn_node_for
from server.server import (
    ROLE_BACKUP, ROLE_PRIMARY, Node, cluster_settings, execute, handle_client,
    parse_addr, parse_peers, serve,
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


# ---------------------------------------------------------------------------------------------
# Level 3: sharding (--cluster)
# ---------------------------------------------------------------------------------------------

SERVERS3 = [{"host": HOST, "port": p} for p in (5000, 5001, 5002)]
CONFIG3 = {"placement": "ring", "vnodes": 50, "servers": SERVERS3}


def sharded_node(port=5000, config=CONFIG3, **overrides):
    """A Node that belongs to the 3-server cluster in CONFIG3."""
    placement, self_id, ports, _mode, _vnodes = cluster_settings(config, HOST, port, **overrides)
    return Node(Store(), placement=placement, self_id=self_id, ports=ports)


def key_owned_by(node, owner_port, prefix="k"):
    """First key k0, k1, ... whose owner is the server on owner_port."""
    want = f"{HOST}:{owner_port}"
    return next(k for k in (f"{prefix}{i}" for i in range(10_000)) if node.placement(k) == want)


class TestShardingExecute(unittest.TestCase):
    def setUp(self):
        self.node = sharded_node(5000)

    def test_kendi_keyini_kabul_eder(self):
        key = key_owned_by(self.node, 5000)
        self.assertEqual(execute(self.node, f"SET {key} merhaba dünya"), "OK")
        self.assertEqual(execute(self.node, f"GET {key}"), "VALUE merhaba dünya")
        self.assertEqual(execute(self.node, "STATS"), "STATS 1")

    def test_baskasinin_keyi_wrong_node(self):
        for owner in (5001, 5002):
            key = key_owned_by(self.node, owner)
            self.assertEqual(execute(self.node, f"SET {key} v"), f"ERR WRONG_NODE {owner}")
            self.assertEqual(execute(self.node, f"GET {key}"), f"ERR WRONG_NODE {owner}")
        self.assertEqual(execute(self.node, "STATS"), "STATS 0")   # nothing was written

    def test_stats_kontrol_edilmez(self):
        # STATS has no key: every server answers with its own count
        for port in (5000, 5001, 5002):
            self.assertEqual(execute(sharded_node(port), "STATS"), "STATS 0")

    def test_bicim_hatasi_sahiplikten_once(self):
        key = key_owned_by(self.node, 5001)
        self.assertEqual(execute(self.node, f"SET {key}"), "ERR VALUE")          # missing value
        self.assertEqual(execute(self.node, "SET " + "x" * 17 + " v"), "ERR KEY")

    def test_her_key_tam_olarak_bir_sunucuda_kabul_edilir(self):
        nodes = {p: sharded_node(p) for p in (5000, 5001, 5002)}
        for i in range(300):
            accepted = [p for p, n in nodes.items() if execute(n, f"SET k{i} v") == "OK"]
            self.assertEqual(len(accepted), 1, f"k{i}")
            # the other two point to the one that accepted
            for p, n in nodes.items():
                if p != accepted[0]:
                    self.assertEqual(execute(n, f"GET k{i}"), f"ERR WRONG_NODE {accepted[0]}")
        self.assertEqual(sum(n.store.count() for n in nodes.values()), 300)

    def test_kumede_olmayan_sunucu_kontrol_yapmaz(self):
        node = Node(Store())                         # Level 1: no placement
        for i in range(50):
            self.assertEqual(execute(node, f"SET k{i} v"), "OK")


class TestClusterSettings(unittest.TestCase):
    def test_dosyadaki_ayarlar(self):
        placement, self_id, ports, mode, vnodes = cluster_settings(CONFIG3, HOST, 5001)
        self.assertEqual(self_id, "127.0.0.1:5001")
        self.assertEqual(ports, {"127.0.0.1:5000": 5000, "127.0.0.1:5001": 5001, "127.0.0.1:5002": 5002})
        self.assertEqual((mode, vnodes), ("ring", 50))
        ring = HashRing(list(ports))
        self.assertTrue(all(placement(f"k{i}") == ring.node_for(f"k{i}") for i in range(200)))

    def test_placement_ve_vnodes_dosyayi_gecersiz_kilar(self):
        placement, _, ports, mode, _ = cluster_settings(CONFIG3, HOST, 5000, placement="modn")
        self.assertEqual(mode, "modn")
        self.assertTrue(all(placement(f"k{i}") == modn_node_for(f"k{i}", list(ports)) for i in range(200)))

        placement, _, ports, _, vnodes = cluster_settings(CONFIG3, HOST, 5000, vnodes=1)
        self.assertEqual(vnodes, 1)
        ring = HashRing(list(ports), vnodes=1)
        self.assertTrue(all(placement(f"k{i}") == ring.node_for(f"k{i}") for i in range(200)))

    def test_kendi_adresi_listede_yok(self):
        with self.assertRaises(ValueError) as cm:
            cluster_settings(CONFIG3, HOST, 5009)
        self.assertIn("127.0.0.1:5009", str(cm.exception))

    def test_localhost_ayri_bir_dugumdur(self):
        # The node id is "host:port" as written; localhost is NOT the same text as 127.0.0.1
        with self.assertRaises(ValueError):
            cluster_settings(CONFIG3, "localhost", 5000)


class TestLoadShardConfig(unittest.TestCase):
    """load_shard_config as the server uses it (the cases --cluster relies on)."""

    def write(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        self.addCleanup(os.remove, path)
        return path

    def test_varsayilanlar(self):
        cfg = load_shard_config(self.write({"servers": SERVERS3}))
        self.assertEqual(cfg["placement"], "ring")
        self.assertEqual(cfg["vnodes"], 50)
        self.assertEqual(cfg["servers"], SERVERS3)

    def test_hatali_dosyalar(self):
        level2 = {"servers": [{"host": HOST, "port": 5000, "role": "primary"}]}
        for data in [level2, {"servers": []}, {"placement": "x", "servers": SERVERS3},
                     {"vnodes": 0, "servers": SERVERS3}, "{bozuk json"]:
            with self.subTest(data=data):
                with self.assertRaises(ConfigError):
                    load_shard_config(self.write(data))

    def test_dosya_yok(self):
        with self.assertRaises(ConfigError):
            load_shard_config(os.path.join(tempfile.gettempdir(), "yok_boyle_bir_dosya.json"))


class TestClusterKomutSatiri(unittest.TestCase):
    """The server must refuse to start with an invalid Level 3 command line (exit code 2)."""

    def write_config(self, data=CONFIG3):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.addCleanup(os.remove, path)
        return path

    def run_server(self, *args):
        # Each case fails while parsing arguments, before any socket is opened
        return subprocess.run([sys.executable, "-m", "server.server", *args],
                              capture_output=True, text=True, timeout=30)

    def test_cluster_ile_role_birlikte_hata(self):
        r = self.run_server("--port", "5000", "--cluster", self.write_config(), "--role", "primary")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--role", r.stderr)

    def test_kendi_adresi_listede_yoksa_acilmaz(self):
        r = self.run_server("--port", "5009", "--cluster", self.write_config())
        self.assertEqual(r.returncode, 2)
        self.assertIn("127.0.0.1:5009", r.stderr)

    def test_level2_dosyasi_reddedilir(self):
        level2 = {"servers": [{"host": HOST, "port": 5000, "role": "primary"}]}
        r = self.run_server("--port", "5000", "--cluster", self.write_config(level2))
        self.assertEqual(r.returncode, 2)
        self.assertIn("role", r.stderr)

    def test_cluster_olmadan_placement_hata(self):
        r = self.run_server("--port", "5000", "--placement", "modn")
        self.assertEqual(r.returncode, 2)
        r = self.run_server("--port", "5000", "--cluster", self.write_config(), "--vnodes", "0")
        self.assertEqual(r.returncode, 2)


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