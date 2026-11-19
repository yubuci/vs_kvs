"""server/sync.py için testler (gerçek soketlerle, server.py olmadan).

serve_sync, testin kendi küçük primary'si içinden doğrudan çağrılır; böylece
server.py'deki SYNC bağlantısı (Öğrenci1) olmadan da iki taraf birlikte denenir.

Çalıştırma (depo kökünden):
    python -m unittest tests.test_sync -v
"""

import logging
import socket
import threading
import time
import unittest

from common.protocol import ENCODING, MAX_KEYS, MAX_VALUE_LEN, encode_line, parse_line, read_line
from server.store import Store
from server.sync import pull_sync, serve_sync

HOST = "127.0.0.1"
BACKUP_PORT = 5999      # pull_sync'e verilen "benim portum"; gerçekte dinlenmez, yalnızca SYNC satırına yazılır


class FakeReplicator:
    """Replicator'ın serve_sync'in kullandığı kısmı: mark_alive ve status."""

    def __init__(self, peers, events=None):
        self._status = {p: "behind" for p in peers}
        self.calls = []
        self.events = events if events is not None else []

    def mark_alive(self, host, port):
        self.calls.append((host, port))
        self.events.append("mark_alive")
        if (host, port) in self._status:
            self._status[(host, port)] = "alive"

    def status(self):
        return dict(self._status)


class SpyStore(Store):
    """snapshot()'ın ne zaman çağrıldığını kaydeden Store (sıra testi için)."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def snapshot(self):
        self.events.append("snapshot")
        return super().snapshot()


class TinyServer:
    """Tek amaçlı test sunucusu: her bağlantıda handler(conn, addr) çağırır.

    Port 0 ile açılır, işletim sistemi boş bir port verir (testler çakışmaz).
    """

    def __init__(self, handler):
        self.handler = handler
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((HOST, 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.errors = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(5)
            try:
                self.handler(conn, addr)
            except Exception as e:            # test sunucusundaki hata testte görünsün
                self.errors.append(e)
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


def primary_handler(store, replicator):
    """Gerçek akışın aynısı: SYNC satırını oku, ayrıştır, serve_sync'i çağır."""
    def handler(conn, addr):
        reader = conn.makefile("r", encoding=ENCODING, newline="\n")
        req = parse_line(read_line(reader))
        assert req.ok and req.cmd == "SYNC", req
        serve_sync(conn, store, replicator, addr[0], req.port)
    return handler


def scripted_handler(lines, close_early=False):
    """SYNC satırını okur ve hazır satırları gönderir (bozuk primary taklidi)."""
    def handler(conn, addr):
        reader = conn.makefile("r", encoding=ENCODING, newline="\n")
        read_line(reader)
        for line in lines:
            conn.sendall(encode_line(line))
        if close_early:
            return                            # END göndermeden kapat
    return handler


def free_port():
    """Kimsenin dinlemediği bir port (bağlantı reddedilsin diye)."""
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)      # beklenen uyarılar test çıktısını kirletmesin
        self.servers = []

    def tearDown(self):
        logging.disable(logging.NOTSET)
        for s in self.servers:
            s.close()
            self.assertEqual(s.errors, [], "test sunucusunda hata")

    def start(self, handler):
        srv = TinyServer(handler)
        self.servers.append(srv)
        return srv


class TestSyncRoundTrip(SyncTestBase):
    """serve_sync + pull_sync birlikte."""

    def sync_into(self, primary_store, backup_store, replicator=None):
        srv = self.start(primary_handler(primary_store, replicator))
        return pull_sync(HOST, srv.port, BACKUP_PORT, backup_store)

    def test_bos_depo(self):
        backup = Store()
        self.assertEqual(self.sync_into(Store(), backup), 0)
        self.assertEqual(backup.count(), 0)

    def test_tam_depo_256_kayit(self):
        primary = Store()
        for i in range(MAX_KEYS):
            primary.set(f"k{i:03d}", f"değer {i} çğışöü  iki boşluk ")
        backup = Store()
        self.assertEqual(self.sync_into(primary, backup), MAX_KEYS)
        # seq'ler dahil birebir aynı
        self.assertEqual(backup.snapshot(), primary.snapshot())

    def test_en_uzun_value(self):
        primary = Store()
        primary.set("uzun", "x" * MAX_VALUE_LEN)
        backup = Store()
        self.assertEqual(self.sync_into(primary, backup), 1)
        self.assertEqual(backup.get("uzun"), "x" * MAX_VALUE_LEN)

    def test_backuptaki_daha_yeni_deger_ezilmez(self):
        primary = Store()
        primary.set("a", "eski")              # seq 1
        primary.set("b", "primary")           # seq 2
        backup = Store()
        backup.apply_replicated("a", "yeni", 10)     # REPL ile daha yeni bir değer gelmiş olsun
        backup.apply_replicated("b", "çok eski", 1)  # backup'taki b daha eski
        self.sync_into(primary, backup)
        self.assertEqual(backup.get("a"), "yeni")      # seq 10 > 1: korunur
        self.assertEqual(backup.get("b"), "primary")   # seq 2 > 1: güncellenir

    def test_iki_kez_sync_ayni_sonuc(self):
        primary = Store()
        for i in range(20):
            primary.set(f"k{i}", f"v{i}")
        backup = Store()
        self.sync_into(primary, backup)
        self.sync_into(primary, backup)
        self.assertEqual(backup.snapshot(), primary.snapshot())

    def test_replicator_none(self):
        # Rolsüz sunucu: replicator yok, serve_sync yine çalışmalı
        primary = Store()
        primary.set("k", "v")
        backup = Store()
        self.assertEqual(self.sync_into(primary, backup, replicator=None), 1)


class TestServeSync(SyncTestBase):
    """Primary tarafının kuralları."""

    def test_mark_alive_dogru_adresle(self):
        rep = FakeReplicator([(HOST, BACKUP_PORT)])
        srv = self.start(primary_handler(Store(), rep))
        pull_sync(HOST, srv.port, BACKUP_PORT, Store())
        # Bağlantının kaynak portu değil, SYNC satırındaki dinleme portu
        self.assertEqual(rep.calls, [(HOST, BACKUP_PORT)])
        self.assertEqual(rep.status()[(HOST, BACKUP_PORT)], "alive")

    def test_sira_once_mark_alive_sonra_snapshot(self):
        events = []
        rep = FakeReplicator([(HOST, BACKUP_PORT)], events)
        srv = self.start(primary_handler(SpyStore(events), rep))
        pull_sync(HOST, srv.port, BACKUP_PORT, Store())
        self.assertEqual(events, ["mark_alive", "snapshot"])

    def test_bilinmeyen_backup_uyari(self):
        logging.disable(logging.NOTSET)
        rep = FakeReplicator([(HOST, 6000)])          # BACKUP_PORT listede yok
        srv = self.start(primary_handler(Store(), rep))
        with self.assertLogs("sync", level="WARNING") as cm:
            pull_sync(HOST, srv.port, BACKUP_PORT, Store())
        self.assertTrue(any("--peers" in m for m in cm.output))

    def test_donus_degeri(self):
        primary = Store()
        for i in range(5):
            primary.set(f"k{i}", "v")
        a, b = socket.socketpair()
        try:
            self.assertEqual(serve_sync(a, primary, None, HOST, BACKUP_PORT), 5)
        finally:
            a.close()
            b.close()


class TestPullSyncHatalar(SyncTestBase):
    """Backup tarafı hiçbir durumda istisna fırlatmamalı."""

    def test_primary_kapali(self):
        # Linux reddedilen bağlantıyı anında bildirir; Windows ise birkaç kez yeniden
        # deneyip ~2 sn sonra bildirir. Bu yüzden "hemen döner" değil, "timeout'u
        # aşmadan, istisnasız 0 döner" kontrol edilir.
        timeout = 1.0
        t0 = time.monotonic()
        self.assertEqual(pull_sync(HOST, free_port(), BACKUP_PORT, Store(), timeout=timeout), 0)
        self.assertLess(time.monotonic() - t0, timeout + 1.0)

    def test_primary_sync_tanimiyor(self):
        # Ör. server.py'de SYNC bağlantısı henüz yok: sunucu ERR UNKNOWN döner
        srv = self.start(scripted_handler(["ERR UNKNOWN"]))
        backup = Store()
        self.assertEqual(pull_sync(HOST, srv.port, BACKUP_PORT, backup), 0)
        self.assertEqual(backup.count(), 0)

    def test_end_gelmeden_kopma(self):
        srv = self.start(scripted_handler(["ENTRY 1 a x", "ENTRY 2 b y"], close_early=True))
        backup = Store()
        self.assertEqual(pull_sync(HOST, srv.port, BACKUP_PORT, backup), 2)
        self.assertEqual(backup.get("b"), "y")        # gelenler uygulanmış olarak kalır

    def test_bozuk_satir_atlanir(self):
        lines = ["ENTRY 1 a x", "ENTRY abc b y", "ENTRY 3 bu_key_17_karakte z", "BOZUK", "ENTRY 4 d w", "END"]
        srv = self.start(scripted_handler(lines))
        backup = Store()
        self.assertEqual(pull_sync(HOST, srv.port, BACKUP_PORT, backup), 2)
        self.assertEqual(backup.get("a"), "x")
        self.assertEqual(backup.get("d"), "w")
        self.assertEqual(backup.count(), 2)

    def test_primary_cevap_vermiyor(self):
        def sessiz(conn, addr):
            time.sleep(1.0)                           # hiçbir şey göndermeden bekle
        srv = self.start(sessiz)
        t0 = time.monotonic()
        self.assertEqual(pull_sync(HOST, srv.port, BACKUP_PORT, Store(), timeout=0.3), 0)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_backup_dolu(self):
        # Backup'ta primary'de olmayan 256 key var: yeni key'ler ERR FULL, istisna yok
        backup = Store()
        for i in range(MAX_KEYS):
            backup.apply_replicated(f"eski{i}", "v", 1)
        srv = self.start(scripted_handler(["ENTRY 5 yeni x", "END"]))
        self.assertEqual(pull_sync(HOST, srv.port, BACKUP_PORT, backup), 0)
        self.assertIsNone(backup.get("yeni"))


if __name__ == "__main__":
    unittest.main()
