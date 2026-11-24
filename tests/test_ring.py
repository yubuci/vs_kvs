"""Unit tests for common/ring.py (Level 3: placement of keys on servers).

No servers and no network: everything here is pure computation.

Run (from the repository root):
    python -m unittest tests.test_ring -v
"""

import itertools
import unittest

from common.ring import (
    DEFAULT_VNODES, HashRing, make_placement, modn_node_for, node_id, stable_hash,
)

N3 = ["127.0.0.1:5000", "127.0.0.1:5001", "127.0.0.1:5002"]
N4 = N3 + ["127.0.0.1:5003"]
KEYS = [f"key{i}" for i in range(10_000)]


def expected_owner(ring, key):
    """Reference implementation by linear scan: first point strictly greater than the hash, else the first."""
    h = stable_hash(key)
    for position, owner in zip(ring._hashes, ring._owners):
        if position > h:
            return owner
    return ring._owners[0]                         # past the last point: wrap around


class TestNodeIdUndHash(unittest.TestCase):
    def test_node_id(self):
        self.assertEqual(node_id("127.0.0.1", 5000), "127.0.0.1:5000")

    def test_stable_hash_sabit_degerler(self):
        # Fixed values: if these change, client, server and measurements would disagree
        self.assertEqual(stable_hash("k1"), 13130158380201725656)
        self.assertEqual(stable_hash("127.0.0.1:5000#0"), 7220134451442776037)
        self.assertEqual(stable_hash(""), 15284527576400310788)
        self.assertEqual(stable_hash("şehir"), 10271886199675276094)   # UTF-8, not the platform encoding

    def test_stable_hash_aralik(self):
        for key in KEYS[:1000]:
            h = stable_hash(key)
            self.assertIsInstance(h, int)
            self.assertTrue(0 <= h < 2 ** 64)      # unsigned 64-bit


class TestHashRingKurulum(unittest.TestCase):
    def test_gecersiz_girdiler(self):
        for nodes, vnodes in [([], 50), (N3, 0), (N3, -1), (N3 + [N3[0]], 50)]:
            with self.subTest(nodes=nodes, vnodes=vnodes):
                with self.assertRaises(ValueError):
                    HashRing(nodes, vnodes)

    def test_nokta_sayisi_ve_nodes(self):
        ring = HashRing(N3, vnodes=7)
        self.assertEqual(len(ring._hashes), 3 * 7)
        self.assertEqual(ring._hashes, sorted(ring._hashes))
        self.assertEqual(ring.nodes, N3)            # the order it was given
        self.assertEqual(HashRing(N3).vnodes, DEFAULT_VNODES)

    def test_nodes_kopyadir(self):
        ring = HashRing(N3)
        ring.nodes.append("x")                      # changing the returned list must not change the ring
        self.assertEqual(ring.nodes, N3)

    def test_tek_dugum_her_seyi_alir(self):
        ring = HashRing(["127.0.0.1:5000"])
        self.assertEqual({ring.node_for(k) for k in KEYS[:1000]}, {"127.0.0.1:5000"})


class TestNodeFor(unittest.TestCase):
    def test_referans_ile_ayni(self):
        # Catches off-by-one mistakes in the bisect / wrap-around logic
        for vnodes in (1, 10, 50):
            ring = HashRing(N3, vnodes)
            for key in KEYS[:2000]:
                self.assertEqual(ring.node_for(key), expected_owner(ring, key), (vnodes, key))

    def test_ayni_key_ayni_dugum(self):
        a, b = HashRing(N3), HashRing(N3)           # two separate rings, like client and server
        for key in KEYS[:1000]:
            self.assertEqual(a.node_for(key), b.node_for(key))

    def test_dugum_sirasi_onemsiz(self):
        base = HashRing(N3)
        for order in itertools.permutations(N3):
            ring = HashRing(list(order))
            for key in KEYS[:500]:
                self.assertEqual(ring.node_for(key), base.node_for(key))

    def test_sardirma(self):
        ring = HashRing(N3, vnodes=1)
        last = ring._hashes[-1]
        # Find a key whose hash lies after the last point: it must go to the first point
        key = next(k for k in (f"w{i}" for i in range(100_000)) if stable_hash(k) > last)
        self.assertEqual(ring.node_for(key), ring._owners[0])
        # A key just before the first point also goes to the first point (no wrap needed)
        key = next(k for k in (f"w{i}" for i in range(100_000)) if stable_hash(k) < ring._hashes[0])
        self.assertEqual(ring.node_for(key), ring._owners[0])

    def test_noktaya_esit_hash_sonrakine_gider(self):
        # bisect_right: a hash equal to a point's position belongs to the NEXT point
        ring = HashRing(N3, vnodes=1)
        point_text = f"{ring._owners[0]}#0"         # this text hashes exactly to the first point
        self.assertEqual(stable_hash(point_text), ring._hashes[0])
        self.assertEqual(ring.node_for(point_text), ring._owners[1])


class TestConsistentHashing(unittest.TestCase):
    def test_dugum_eklenince_tasinan_key_yeni_dugume_gider(self):
        old, new = HashRing(N3), HashRing(N4)
        moved = [k for k in KEYS if old.node_for(k) != new.node_for(k)]
        for key in moved:
            self.assertEqual(new.node_for(key), "127.0.0.1:5003")   # never between old nodes
        self.assertTrue(0.15 < len(moved) / len(KEYS) < 0.35, len(moved))   # about 1/4

    def test_dugum_cikarilinca_yalnizca_onun_keyleri_tasinir(self):
        old, new = HashRing(N4), HashRing(N3)
        for key in KEYS:
            if old.node_for(key) != "127.0.0.1:5003":
                self.assertEqual(new.node_for(key), old.node_for(key))  # untouched

    def test_modn_ise_cogunu_tasir(self):
        moved = sum(modn_node_for(k, N3) != modn_node_for(k, N4) for k in KEYS)
        self.assertTrue(0.70 < moved / len(KEYS) < 0.80, moved)   # about 3/4: the comparison

    def test_50_vnode_ile_dengeli(self):
        ring = HashRing(N3)
        counts = {n: 0 for n in N3}
        for key in KEYS:
            counts[ring.node_for(key)] += 1
        for node, count in counts.items():
            self.assertTrue(0.25 < count / len(KEYS) < 0.42, (node, count))


class TestNodesFor(unittest.TestCase):
    def setUp(self):
        self.ring = HashRing(N3)

    def test_iki_farkli_fiziksel_dugum(self):
        for key in KEYS[:500]:
            pair = self.ring.nodes_for(key, 2)
            self.assertEqual(len(pair), 2)
            self.assertEqual(len(set(pair)), 2)
            self.assertEqual(pair[0], self.ring.node_for(key))   # the first one is the owner

    def test_fazla_n_hepsini_verir(self):
        for key in KEYS[:200]:
            nodes = self.ring.nodes_for(key, 5)
            self.assertEqual(sorted(nodes), sorted(N3))

    def test_kucuk_n(self):
        self.assertEqual(self.ring.nodes_for("k1", 1), [self.ring.node_for("k1")])
        self.assertEqual(self.ring.nodes_for("k1", 0), [])

    def test_tek_vnode_ile_de_sonlanir(self):
        ring = HashRing(N3, vnodes=1)
        self.assertEqual(sorted(ring.nodes_for("k1", 3)), sorted(N3))


class TestModnUndPlacement(unittest.TestCase):
    def test_modn(self):
        for key in KEYS[:500]:
            self.assertEqual(modn_node_for(key, N3), N3[stable_hash(key) % 3])

    def test_modn_sirasi_onemli(self):
        # Unlike the ring, mod N depends on the order of the list
        reordered = list(reversed(N3))
        self.assertTrue(any(modn_node_for(k, N3) != modn_node_for(k, reordered) for k in KEYS[:100]))

    def test_make_placement_ring(self):
        place = make_placement("ring", N3)
        ring = HashRing(N3)
        for key in KEYS[:500]:
            self.assertEqual(place(key), ring.node_for(key))

    def test_make_placement_vnodes_iletilir(self):
        place = make_placement("ring", N3, vnodes=1)
        ring = HashRing(N3, vnodes=1)
        for key in KEYS[:500]:
            self.assertEqual(place(key), ring.node_for(key))

    def test_make_placement_modn(self):
        place = make_placement("modn", N3)
        for key in KEYS[:500]:
            self.assertEqual(place(key), modn_node_for(key, N3))

    def test_make_placement_hatalar(self):
        with self.assertRaises(ValueError):
            make_placement("bilinmeyen", N3)
        with self.assertRaises(ValueError):
            make_placement("modn", [])
        with self.assertRaises(ValueError):
            make_placement("ring", [])


if __name__ == "__main__":
    unittest.main()