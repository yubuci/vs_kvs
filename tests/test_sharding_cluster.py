"""Level 3 entegrasyon testleri: gerçek 3 sunucu, sharding (--cluster), ayrı süreçler.

Her test kendi boş portlarını işletim sisteminden alır. Dağılım portlara bağlı olduğu için
(sanal noktalar sunucu adının hash'iyle yerleşir) testler sabit sayılar beklemez:
seçilen portlarla dağılımı make_placement ile ÖNCEDEN hesaplar ve sunucuların TAM OLARAK
buna uymasını bekler. Böylece şanssız bir port kümesinde (bir sunucu 256'yı aşıyor) de
doğru çalışırlar ve kontrol, "yaklaşık doğru" yerine "birebir doğru" olur.

Client olarak KVClient + make_placement kullanılır (Öğrenci2'nin ShardedClient'ını beklemez).
ShardedClient varsa onu sınayan test de çalışır, yoksa atlanır.

Çalıştırma (depo kökünden; 15-30 sn):
    python -m unittest tests.test_sharding_cluster -v
"""

import os
import socket
import tempfile
import unittest

from client.client import KVClient, KVError
from common.protocol import ERR_FULL, MAX_KEYS, RESP_OK
from common.ring import make_placement, node_id
from scripts.start_cluster import start_cluster, stop_cluster, write_shard_config

try:
    from client.client import ShardedClient        # Öğrenci2, Level 3 Aşama B
except ImportError:
    ShardedClient = None

HOST = "127.0.0.1"
VNODES = 50


def free_ports(n):
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind((HOST, 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def port_of(node):
    return int(node.rsplit(":", 1)[1])


class ShardingClusterBase:
    """Ortak testler; alt sınıflar MODE'u belirler ("ring" ya da "modn")."""

    MODE = None

    def setUp(self):
        self.ports = free_ports(3)
        self.servers = [{"host": HOST, "port": p} for p in self.ports]
        self.nodes = [node_id(HOST, p) for p in self.ports]
        self.place = make_placement(self.MODE, self.nodes, VNODES)
        self.tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(self.tmp.name, "cluster.json")
        write_shard_config(cfg, self.servers, placement=self.MODE, vnodes=VNODES)
        self.running = start_cluster(self.servers, quiet=True, cluster_file=cfg)
        self.clients = {}

    def tearDown(self):
        for c in self.clients.values():
            c.close()
        stop_cluster(self.running)
        self.tmp.cleanup()

    # --- yardımcılar ----------------------------------------------------------------------

    def client(self, port):
        """Sunucu başına tek, açık tutulan bağlantı."""
        if port not in self.clients:
            self.clients[port] = KVClient(HOST, port, timeout=5)
        return self.clients[port]

    def owner_port(self, key):
        return port_of(self.place(key))

    def others(self, key):
        return [p for p in self.ports if p != self.owner_port(key)]

    def expected(self, keys):
        """Sırayla yazılınca beklenen cevaplar ve sunucu başına kayıt sayısı (tahmin)."""
        counts = {p: 0 for p in self.ports}
        answers = []
        for k in keys:
            p = self.owner_port(k)
            if counts[p] >= MAX_KEYS:
                answers.append(ERR_FULL)
            else:
                counts[p] += 1
                answers.append(RESP_OK)
        return answers, counts

    def write_to_owners(self, keys):
        return [self.client(self.owner_port(k)).send_command(f"SET {k} değer {k}") for k in keys]

    def stats(self, port):
        return int(self.client(port).send_command("STATS").split()[1])

    # --- testler --------------------------------------------------------------------------

    def test_600_key_tahmine_birebir_uyuyor(self):
        keys = [f"k{i:04d}" for i in range(600)]
        exp_answers, exp_counts = self.expected(keys)
        self.assertEqual(self.write_to_owners(keys), exp_answers)
        self.assertEqual({p: self.stats(p) for p in self.ports}, exp_counts)
        # Level 3'ün amacı: toplam, tek sunucunun sınırını (256) aşıyor
        self.assertGreater(sum(exp_counts.values()), MAX_KEYS)

    def test_her_key_yalnizca_sahibinde(self):
        keys = [f"a{i:03d}" for i in range(100)]
        self.assertEqual(self.write_to_owners(keys), [RESP_OK] * len(keys))
        for k in keys:
            self.assertEqual(self.client(self.owner_port(k)).send_command(f"GET {k}"), f"VALUE değer {k}")
            for p in self.others(k):
                self.assertEqual(self.client(p).send_command(f"GET {k}"),
                                 f"ERR WRONG_NODE {self.owner_port(k)}", (k, p))

    def test_yanlis_sunucuya_set_yazilmaz(self):
        k = "yanlis1"
        wrong = self.others(k)[0]
        self.assertEqual(self.client(wrong).send_command(f"SET {k} x"), f"ERR WRONG_NODE {self.owner_port(k)}")
        self.assertEqual(self.stats(wrong), 0)
        self.assertEqual(self.client(self.owner_port(k)).send_command(f"GET {k}"), "NOT_FOUND")

    def test_bir_sunucu_kapali_yalnizca_onun_keyleri_okunamaz(self):
        keys = [f"b{i:03d}" for i in range(150)]
        self.assertEqual(self.write_to_owners(keys), [RESP_OK] * len(keys))
        dead = self.ports[0]
        proc = next(pr for s, pr in self.running if s["port"] == dead)
        proc.terminate()
        proc.wait(timeout=5)
        self.clients.pop(dead).close()

        readable = 0
        for k in keys:
            if self.owner_port(k) == dead:
                with self.assertRaises(KVError):          # sahibi kapalı: replikasyon yok (Level 4)
                    with KVClient(HOST, dead, timeout=1) as c:
                        c.send_command(f"GET {k}")
            else:
                self.assertEqual(self.client(self.owner_port(k)).send_command(f"GET {k}"), f"VALUE değer {k}")
                readable += 1
        self.assertEqual(readable, sum(1 for k in keys if self.owner_port(k) != dead))

    @unittest.skipIf(ShardedClient is None, "ShardedClient henüz yok (Öğrenci2, Level 3 Aşama B)")
    def test_sharded_client(self):
        keys = [f"c{i:03d}" for i in range(300)]
        exp_answers, exp_counts = self.expected(keys)
        with ShardedClient(self.servers, placement=self.MODE, vnodes=VNODES) as c:
            self.assertEqual([c.send_command(f"SET {k} v") for k in keys], exp_answers)
            self.assertEqual(c.send_command("STATS"), f"STATS {sum(exp_counts.values())}")
            self.assertEqual(c.send_command(f"GET {keys[0]}"), "VALUE v")


class TestShardingRing(ShardingClusterBase, unittest.TestCase):
    MODE = "ring"


class TestShardingModN(ShardingClusterBase, unittest.TestCase):
    MODE = "modn"


if __name__ == "__main__":
    unittest.main()
