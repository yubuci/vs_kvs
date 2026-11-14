"""server/store.py için birim testleri.

Çalıştırma (depo kökünden):
    python -m unittest tests.test_store -v
"""

import threading
import unittest

from common.protocol import ERR_FULL, ERR_KEY, ERR_VALUE, MAX_KEYS, MAX_KEY_LEN, MAX_VALUE_LEN
from server.store import Store


class TestStore(unittest.TestCase):
    def test_set_get(self):
        s = Store()
        self.assertIsNone(s.set("k1", "merhaba dünya"))
        self.assertEqual(s.get("k1"), "merhaba dünya")
        self.assertEqual(s.count(), 1)

    def test_olmayan_key(self):
        self.assertIsNone(Store().get("yok"))

    def test_guncelleme_sayiyi_artirmaz(self):
        s = Store()
        s.set("k1", "a")
        s.set("k1", "b")
        self.assertEqual(s.get("k1"), "b")
        self.assertEqual(s.count(), 1)

    def test_gecersiz_key_ve_value(self):
        s = Store()
        self.assertEqual(s.set("", "v"), ERR_KEY)
        self.assertEqual(s.set("a" * (MAX_KEY_LEN + 1), "v"), ERR_KEY)
        self.assertEqual(s.set("a b", "v"), ERR_KEY)
        self.assertEqual(s.set("k1", ""), ERR_VALUE)
        self.assertEqual(s.set("k1", "x" * (MAX_VALUE_LEN + 1)), ERR_VALUE)
        self.assertEqual(s.count(), 0)   # hatalı istek hiçbir şey yazmamalı

    def test_sinir_degerleri_gecerli(self):
        s = Store()
        self.assertIsNone(s.set("a" * MAX_KEY_LEN, "x" * MAX_VALUE_LEN))

    def test_256_siniri(self):
        s = Store()
        for i in range(MAX_KEYS):
            self.assertIsNone(s.set(f"k{i}", "v"))        # 256. kayıt dahil OK
        self.assertEqual(s.set("yeni", "v"), ERR_FULL)     # 257. yeni key reddedilir
        self.assertEqual(s.count(), MAX_KEYS)
        self.assertIsNone(s.get("yeni"))

    def test_doluyken_guncelleme(self):
        s = Store()
        for i in range(MAX_KEYS):
            s.set(f"k{i}", "eski")
        self.assertIsNone(s.set("k0", "yeni"))             # var olan key güncellenebilir
        self.assertEqual(s.get("k0"), "yeni")
        self.assertEqual(s.count(), MAX_KEYS)

    def test_snapshot_kopyadir(self):
        s = Store()
        s.set("k1", "a")
        kopya = s.snapshot()
        kopya["k2"] = "b"                                  # kopyayı değiştirmek depoyu etkilemez
        self.assertEqual(s.count(), 1)

    def test_eszamanli_kapasite(self):
        # 300 thread aynı anda farklı yeni key ekler: tam 256 OK, 44 ERR FULL olmalı
        for _ in range(20):                                # yarış her seferinde çıkmaz, tekrarla
            s = Store()
            n = 300
            bariyer = threading.Barrier(n)                 # hepsi hazır olunca birlikte başlasınlar
            sonuclar = [None] * n

            def calis(i):
                bariyer.wait()
                sonuclar[i] = s.set(f"k{i}", "v")

            threadler = [threading.Thread(target=calis, args=(i,)) for i in range(n)]
            for t in threadler:
                t.start()
            for t in threadler:
                t.join()

            self.assertEqual(s.count(), MAX_KEYS)
            self.assertEqual(sonuclar.count(None), MAX_KEYS)
            self.assertEqual(sonuclar.count(ERR_FULL), n - MAX_KEYS)


if __name__ == "__main__":
    unittest.main()
