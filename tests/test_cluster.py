"""Level 2 entegrasyon testleri: gerçek 3 sunucu (1 primary, 2 backup), ayrı süreçler.

Birim testlerinden farkı: burada hiçbir şey taklit edilmez. Sunucular
scripts/start_cluster.py ile gerçekten başlatılır, kapatılır ve yeniden açılır;
client'lar gerçek TCP bağlantılarıyla konuşur.

Her test kendi boş portlarını kullanır (açık bir kümeyle çakışmaz).

Çalıştırma (depo kökünden; yaklaşık 15-30 sn sürer):
    python -m unittest tests.test_cluster -v
"""

import socket
import subprocess
import threading
import time
import unittest

from client.client import ClusterClient, KVClient, KVError
from common.protocol import RESP_OK, format_not_primary
from scripts.start_cluster import REPO_ROOT, build_command, start_cluster, stop_cluster, wait_ready

HOST = "127.0.0.1"
CONVERGE_TIMEOUT = 5.0   # saniye; backup'ların primary'ye yetişmesi için en fazla bekleme


def free_ports(n):
    """İşletim sisteminden n tane boş port alır."""
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind((HOST, 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def ask(port, line, timeout=3.0):
    """Tek bir sunucuya tek bir komut gönderir."""
    with KVClient(HOST, port, timeout=timeout) as c:
        return c.send_command(line)


def contents(port, keys):
    """Sunucudaki verilen key'lerin değerleri: {key: cevap satırı}. Tek bağlantı kullanır."""
    with KVClient(HOST, port) as c:
        return {k: c.send_command(f"GET {k}") for k in keys}


def wait_until(condition, timeout=CONVERGE_TIMEOUT, step=0.1):
    """condition() True olana kadar bekler. Süre dolarsa False döner (test kendisi assert eder)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


class ClusterTestBase(unittest.TestCase):
    """Her test için temiz bir küme kurar ve sonunda kapatır."""

    def setUp(self):
        p, b1, b2 = free_ports(3)
        self.primary, self.backup1, self.backup2 = p, b1, b2
        self.servers = [
            {"host": HOST, "port": p, "role": "primary"},
            {"host": HOST, "port": b1, "role": "backup"},
            {"host": HOST, "port": b2, "role": "backup"},
        ]
        self.running = start_cluster(self.servers, quiet=True)

    def tearDown(self):
        stop_cluster(self.running)

    # --- yardımcılar -----------------------------------------------------------

    def proc_of(self, port):
        for server, proc in reversed(self.running):    # yeniden başlatılmışsa en yenisi
            if server["port"] == port:
                return proc
        raise KeyError(port)

    def kill(self, port):
        """Sunucuyu kapatır (çökme gibi: süreç sonlandırılır, veri RAM'de olduğu için gider)."""
        proc = self.proc_of(port)
        proc.terminate()
        proc.wait(timeout=5)

    def restart(self, port):
        """Kapatılmış sunucuyu aynı rol ve parametrelerle yeniden başlatır.

        Backup ise açılışta primary'den SYNC çeker. wait_ready yalnızca "dinliyor mu"
        diye bakar; SYNC'in bitmesini testler wait_until ile ayrıca bekler.
        """
        server = next(s for s in self.servers if s["port"] == port)
        proc = subprocess.Popen(build_command(server, self.servers, True), cwd=REPO_ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.running.append((server, proc))
        wait_ready(proc, HOST, port)

    def write(self, items):
        """Primary'ye sırayla SET gönderir; hepsinin OK olduğunu doğrular."""
        with KVClient(HOST, self.primary) as c:
            for key, value in items:
                self.assertEqual(c.send_command(f"SET {key} {value}"), RESP_OK, key)

    def assert_same_as_primary(self, port, keys):
        """port'taki sunucu, verilen key'lerde primary ile aynı olana kadar bekler ve doğrular."""
        expected = contents(self.primary, keys)
        same = wait_until(lambda: contents(port, keys) == expected
                          and ask(port, "STATS") == ask(self.primary, "STATS"))
        if not same:
            # Farkı okunur biçimde göster
            got = contents(port, keys)
            diff = {k: (expected[k], got[k]) for k in keys if expected[k] != got[k]}
            self.fail(f"{port} primary'ye {CONVERGE_TIMEOUT:g} sn içinde yetişmedi. "
                      f"STATS {ask(port, 'STATS')} / {ask(self.primary, 'STATS')}, "
                      f"farklı {len(diff)} key, ör. {list(diff.items())[:3]}")


class TestReplikasyon(ClusterTestBase):

    def test_yazmalar_uc_sunucuya_kopyalaniyor(self):
        items = [(f"k{i:03d}", f"değer {i} çğışöü  iki boşluk") for i in range(100)]
        self.write(items)
        keys = [k for k, _ in items]
        for port in (self.backup1, self.backup2):
            self.assert_same_as_primary(port, keys)
        self.assertEqual(ask(self.backup1, "STATS"), "STATS 100")

    def test_guncelleme_de_kopyalaniyor(self):
        self.write([("k1", "eski"), ("k1", "yeni")])
        self.assert_same_as_primary(self.backup1, ["k1"])
        self.assertEqual(ask(self.backup2, "GET k1"), "VALUE yeni")


class TestRoller(ClusterTestBase):

    def test_backup_client_set_reddeder(self):
        self.assertEqual(ask(self.backup1, "SET x y"), format_not_primary(self.primary))
        self.assertEqual(ask(self.primary, "STATS"), "STATS 0")    # hiçbir yere yazılmadı

    def test_cluster_client_backuptan_primarye_yonlenir(self):
        # Liste bilerek backup ile başlıyor: GET oradan okunur, SET primary'ye gider
        servers = [self.servers[1], self.servers[2], self.servers[0]]
        with ClusterClient(servers) as c:
            self.assertEqual(c.send_command("SET a 1"), RESP_OK)
        self.assert_same_as_primary(self.backup1, ["a"])


class TestArizalar(ClusterTestBase):

    def test_primary_kapali_get_calisir_set_calismaz(self):
        self.write([(f"k{i}", f"v{i}") for i in range(20)])
        self.assert_same_as_primary(self.backup1, [f"k{i}" for i in range(20)])
        self.kill(self.primary)

        # OK almış hiçbir veri kaybolmadı: backup'lardan okunuyor
        self.assertEqual(ask(self.backup1, "GET k7"), "VALUE v7")
        self.assertEqual(ask(self.backup2, "GET k19"), "VALUE v19")

        with ClusterClient(self.servers) as c:
            self.assertEqual(c.send_command("GET k3"), "VALUE v3")   # primary atlanır, backup cevaplar
            with self.assertRaises(KVError):                         # failover yok (Level 5)
                c.send_command("SET yeni x")

    def test_backup_yeniden_baslayinca_sync_ile_yetisiyor(self):
        first = [(f"a{i}", f"v{i}") for i in range(30)]
        self.write(first)
        self.kill(self.backup1)

        # Backup kapalıyken: yeni key'ler ve var olan bir key'in güncellenmesi
        second = [(f"b{i}", f"w{i}") for i in range(50)] + [("a0", "güncel")]
        self.write(second)
        self.restart(self.backup1)

        keys = sorted({k for k, _ in first + second})
        self.assert_same_as_primary(self.backup1, keys)
        self.assertEqual(ask(self.backup1, "GET a0"), "VALUE güncel")

        # SYNC'ten sonra REPL de yeniden geliyor (backup tekrar "alive")
        self.write([("sonra", "x")])
        self.assert_same_as_primary(self.backup1, keys + ["sonra"])

    def test_sync_yazma_surerken_kayip_yok(self):
        # Backup kapalıyken ve yeniden açılırken arka planda sürekli yazılır.
        # serve_sync'teki sıra (önce mark_alive, sonra snapshot) yanlış olsaydı
        # aradaki yazmalar backup'a hiç ulaşmazdı.
        #
        # Her yazma BENZERSİZ bir key kullanır. İlk sürüm 150 key'i döngüyle tekrar
        # yazıyordu: pencerede kaybolan bir yazma, aynı key birkaç ms sonra yeniden
        # yazılınca REPL ile telafi ediliyor ve hata hiç görünmüyordu (bozuk sırayla
        # da test geçiyordu). Benzersiz key ile kayıp sonradan kapanamaz.
        max_keys = 220                      # 256 sınırının altında kal
        keys = []
        stop = threading.Event()
        errors = []

        def writer():
            try:
                with KVClient(HOST, self.primary) as c:
                    for i in range(max_keys):
                        if stop.is_set():
                            break
                        key = f"w{i:03d}"
                        if c.send_command(f"SET {key} v{i}") != RESP_OK:
                            errors.append(key)
                        keys.append(key)
                        time.sleep(0.01)    # ~2-3 sn boyunca yazsın, sınırı erken doldurmasın
            except KVError as e:
                errors.append(str(e))

        t = threading.Thread(target=writer)
        t.start()
        try:
            time.sleep(0.5)
            self.kill(self.backup1)
            time.sleep(0.5)
            self.restart(self.backup1)       # SYNC, yazma sürerken başlar
            time.sleep(1.0)
        finally:
            stop.set()
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assert_same_as_primary(self.backup1, keys)
        self.assert_same_as_primary(self.backup2, keys)    # hiç kapanmayan backup da tutarlı

    def test_iki_backup_kapali_primary_yazmaya_devam_eder(self):
        self.kill(self.backup1)
        self.kill(self.backup2)
        # İlk SET'te ölü backup'lar fark edilip "behind" işaretlenir. Windows'ta reddedilen
        # bağlantı geç bildirildiği için bu ilk SET ~2 sn sürebilir.
        self.assertEqual(ask(self.primary, "SET ilk x", timeout=10), RESP_OK)
        # Sonrakiler artık ölü backup'ları beklememeli
        t0 = time.monotonic()
        self.write([(f"k{i}", "v") for i in range(20)])
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(ask(self.primary, "STATS"), "STATS 21")


if __name__ == "__main__":
    unittest.main()
