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
        err, seq = s.set("k1", "merhaba dünya") 
        self.assertIsNone(err)                            
        self.assertEqual(seq, 1)                         
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
        err, seq = s.set("", "v")
        self.assertEqual(err, ERR_KEY)
        err, seq = s.set("a" * (MAX_KEY_LEN + 1), "v")
        self.assertEqual(err ,ERR_KEY)
        err, seq = s.set("a b", "v")
        self.assertEqual(err ,ERR_KEY)
        err, seq = s.set("k1", "")
        self.assertEqual(err, ERR_VALUE)
        err, seq = s.set("k1", "x" * (MAX_VALUE_LEN + 1))
        self.assertEqual(err, ERR_VALUE)
        self.assertEqual(s.count(), 0)   # hatalı istek hiçbir şey yazmamalı

    def test_sinir_degerleri_gecerli(self):
        s = Store()
        err, seq = s.set("a" * MAX_KEY_LEN, "x" * MAX_VALUE_LEN)
        self.assertIsNone(err)

    def test_256_siniri(self):
        s = Store()
        for i in range(MAX_KEYS):
            err, seq = s.set(f"k{i}", "v")
            self.assertIsNone(err)                            # all 256 writes succeed
        self.assertEqual(seq, MAX_KEYS)                       # the 256th write got seq 256
        self.assertEqual(s.set("yeni", "v"), (ERR_FULL, None))  # the 257th new key is rejected
        self.assertEqual(s.count(), MAX_KEYS)
        self.assertIsNone(s.get("yeni"))

    def test_doluyken_guncelleme(self):
        s = Store()
        for i in range(MAX_KEYS):
            s.set(f"k{i}", "eski")
        err, seq = s.set("k0", "yeni")
        self.assertIsNone(err)                             # an existing key can be updated when full
        self.assertEqual(seq, MAX_KEYS + 1)                # an update also gets a new seq
        self.assertEqual(s.get("k0"), "yeni")
        self.assertEqual(s.count(), MAX_KEYS)              # still 256: an update adds no entry

    def test_snapshot_kopyadir(self):
        s = Store()
        s.set("k1", "a")
        kopya = s.snapshot()                                       # [(1, "k1", "a")]
        kopya.append,(2, "k2", "b")                                # changing the copy must not affect the store
        self.assertEqual(s.count(), 1)
        self.assertIsNone(s.get("k2"))                             # the appended entry exists only in the copy

    def test_eszamanli_kapasite(self):
        # 300 threads add different new keys at the same time: exactly 256 OK and 44 ERR FULL
        for _ in range(20):                                # the race does not show every time, so repeat
            s = Store()
            n = 300
            bariyer = threading.Barrier(n)                 # release all threads together once all are ready
            sonuclar = [None] * n                          # each slot will hold an (err, seq) pair

            def calis(i):
                bariyer.wait()
                sonuclar[i] = s.set(f"k{i}", "v")

            threadler = [threading.Thread(target=calis, args=(i,)) for i in range(n)]
            for t in threadler:
                t.start()
            for t in threadler:
                t.join()

            # Split the (err, seq) pairs: all errors, and the seq of every successful write
            hatalar = [err for err, seq in sonuclar]
            numaralar = [seq for err, seq in sonuclar if err is None]

            self.assertEqual(s.count(), MAX_KEYS)
            self.assertEqual(hatalar.count(None), MAX_KEYS)                    # exactly 256 writes succeeded
            self.assertEqual(hatalar.count(ERR_FULL), n - MAX_KEYS)            # the other 44 were rejected
            self.assertEqual(sorted(numaralar), list(range(1, MAX_KEYS + 1)))  # every seq 1..256 exactly once


    # ------------------------------------------------------------------
    # Level 2: sequence numbers
    # ------------------------------------------------------------------

    def test_seq_her_sette_artar(self):
        s = Store()
        _, seq1 = s.set("k1", "a")
        _, seq2 = s.set("k2", "b")
        _, seq3 = s.set("k1", "c")                         # an update also gets a new seq
        self.assertEqual((seq1, seq2, seq3), (1, 2, 3))

    def test_basarisiz_set_seq_harcamaz(self):
        s = Store()
        self.assertEqual(s.set("", "v"), (ERR_KEY, None))  # invalid key: rejected, no seq
        self.assertEqual(s.set("k1", ""), (ERR_VALUE, None))
        err, seq = s.set("k1", "v")
        self.assertIsNone(err)
        self.assertEqual(seq, 1)                           # the failed writes did not use up a number

    def test_dolu_depoda_basarisiz_set_seq_harcamaz(self):
        s = Store()
        for i in range(MAX_KEYS):
            s.set(f"k{i}", "v")
        self.assertEqual(s.set("yeni", "v"), (ERR_FULL, None))
        _, seq = s.set("k0", "guncel")                     # next successful write
        self.assertEqual(seq, MAX_KEYS + 1)                # 257, not 258: ERR FULL used no number

    def test_snapshot_uclu_liste(self):
        s = Store()
        s.set("k1", "a")
        s.set("k2", "b")
        self.assertEqual(s.snapshot(), [(1, "k1", "a"), (2, "k2", "b")])   # (seq, key, value), ordered by seq

    # ------------------------------------------------------------------
    # Level 2: apply_replicated (used by a backup for REPL and SYNC)
    # ------------------------------------------------------------------

    def test_apply_yeni_key_yazilir(self):
        s = Store()
        self.assertIsNone(s.apply_replicated("a", "ilk", 5))
        self.assertEqual(s.get("a"), "ilk")
        self.assertEqual(s.snapshot(), [(5, "a", "ilk")])  # keeps the primary's seq, not a local one

    def test_apply_eski_seq_yok_sayilir(self):
        s = Store()
        s.apply_replicated("a", "ilk", 5)
        self.assertIsNone(s.apply_replicated("a", "eski", 3))   # ignored, but still not an error
        self.assertEqual(s.get("a"), "ilk")
        self.assertEqual(s.snapshot(), [(5, "a", "ilk")])

    def test_apply_ayni_seq_yok_sayilir(self):
        # The same message can arrive twice (REPL and SYNC, or a retried REPL)
        s = Store()
        s.apply_replicated("a", "ilk", 5)
        self.assertIsNone(s.apply_replicated("a", "ayni", 5))
        self.assertEqual(s.get("a"), "ilk")

    def test_apply_yeni_seq_uygulanir(self):
        s = Store()
        s.apply_replicated("a", "ilk", 5)
        self.assertIsNone(s.apply_replicated("a", "yeni", 9))
        self.assertEqual(s.get("a"), "yeni")
        self.assertEqual(s.snapshot(), [(9, "a", "yeni")])
        self.assertEqual(s.count(), 1)                     # an update adds no entry

    def test_apply_ters_sirada_gelen_mesajlar(self):
        # REPL 8 may overtake REPL 7 on the network; the newer value must win either way
        s = Store()
        s.apply_replicated("a", "sekiz", 8)
        s.apply_replicated("a", "yedi", 7)
        self.assertEqual(s.get("a"), "sekiz")

    def test_apply_dolu_depoda_yeni_key(self):
        s = Store()
        for i in range(MAX_KEYS):
            s.set(f"k{i}", "v")
        self.assertEqual(s.apply_replicated("yeni", "v", 1000), ERR_FULL)
        self.assertEqual(s.count(), MAX_KEYS)
        self.assertIsNone(s.get("yeni"))

    def test_apply_dolu_depoda_guncelleme(self):
        s = Store()
        for i in range(MAX_KEYS):
            s.set(f"k{i}", "eski")
        self.assertIsNone(s.apply_replicated("k0", "yeni", 1000))   # existing key: allowed when full
        self.assertEqual(s.get("k0"), "yeni")
        self.assertEqual(s.count(), MAX_KEYS)

if __name__ == "__main__":
    unittest.main()
