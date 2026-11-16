"""Unit tests for server/replication.py.

The backups are fake: small in-process TCP servers that record every line they
receive and answer with a fixed response. No real server process is needed.

Run (from the repository root):
    python -m unittest tests.test_replication -v
"""

import socket
import threading
import time
import unittest

from server.replication import Replicator

HOST = "127.0.0.1"


class FakeBackup:
    """A minimal backup: records received lines and answers each one with `response`.

    response=None means "never answer" (used to test the timeout).
    """

    def __init__(self, response="OK"):
        self.response = response
        self.lines = []                       # every line received, in arrival order
        self._lock = threading.Lock()         # protects lines and _conns
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
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            reader = conn.makefile("r", encoding="utf-8", newline="\n")
            for line in reader:
                with self._lock:
                    self.lines.append(line.rstrip("\r\n"))
                if self.response is not None:
                    conn.sendall((self.response + "\n").encode("utf-8"))
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


class TestReplicator(unittest.TestCase):
    def backup(self, response="OK"):
        """Start a fake backup that is closed automatically when the test ends."""
        b = FakeBackup(response)
        self.addCleanup(b.close)
        return b

    def replicator(self, peers):
        """Create a Replicator whose connections are closed when the test ends."""
        r = Replicator(peers)
        self.addCleanup(r.close)
        return r

    def test_baslangicta_hepsi_alive(self):
        r = self.replicator([(HOST, 5001), (HOST, 5002)])   # nothing is sent, so no server is needed
        self.assertEqual(r.status(), {(HOST, 5001): "alive", (HOST, 5002): "alive"})

    def test_iki_backup_da_repl_alir(self):
        b1, b2 = self.backup(), self.backup()
        r = self.replicator([b1.addr, b2.addr])
        r.replicate(7, "k1", "merhaba")
        self.assertEqual(b1.received(), ["REPL 7 k1 merhaba"])
        self.assertEqual(b2.received(), ["REPL 7 k1 merhaba"])
        self.assertEqual(r.status(), {b1.addr: "alive", b2.addr: "alive"})

    def test_bosluklu_value_bozulmaz(self):
        b = self.backup()
        r = self.replicator([b.addr])
        r.replicate(3, "k1", "merhaba güzel dünya ")
        self.assertEqual(b.received(), ["REPL 3 k1 merhaba güzel dünya "])

    def test_ulasilamayan_backup_behind_olur(self):
        addr = (HOST, closed_port())
        r = self.replicator([addr])
        r.replicate(1, "k1", "v")                      # must not raise
        self.assertEqual(r.status(), {addr: "behind"})

    def test_behind_backup_tekrar_denenmez(self):
        addr = (HOST, closed_port())
        r = self.replicator([addr])
        r.replicate(1, "k1", "v")                      # first call: finds out the backup is dead
        t0 = time.perf_counter()
        r.replicate(2, "k2", "v")                      # second call: skipped, no connection attempt
        self.assertLess(time.perf_counter() - t0, 0.2)

    def test_olu_backup_digerini_etkilemez(self):
        b = self.backup()
        dead = (HOST, closed_port())
        r = self.replicator([dead, b.addr])                 # the dead one comes first in the list
        r.replicate(1, "k1", "v")
        r.replicate(2, "k2", "v")
        self.assertEqual(b.received(), ["REPL 1 k1 v", "REPL 2 k2 v"])
        self.assertEqual(r.status(), {dead: "behind", b.addr: "alive"})

    def test_ok_olmayan_cevap_behind_yapar(self):
        b = self.backup(response="ERR FULL")           # answered, but did not apply the write
        r = self.replicator([b.addr])
        r.replicate(1, "k1", "v")
        self.assertEqual(r.status(), {b.addr: "behind"})
        r.replicate(2, "k2", "v")                      # behind: gets nothing more
        self.assertEqual(b.received(), ["REPL 1 k1 v"])

    def test_cevap_vermeyen_backup_timeout_ile_behind(self):
        b = self.backup(response=None)                 # accepts the connection, never answers
        r = self.replicator([b.addr])
        t0 = time.perf_counter()
        r.replicate(1, "k1", "v")
        sure = time.perf_counter() - t0
        self.assertEqual(r.status(), {b.addr: "behind"})
        self.assertGreater(sure, 0.8)                  # waited for the 1 s timeout ...
        self.assertLess(sure, 3.0)                     # ... but not forever

    def test_mark_alive_sonrasi_yeniden_repl_alir(self):
        b = self.backup(response="ERR FULL")
        r = self.replicator([b.addr])
        r.replicate(1, "k1", "v")                      # -> behind
        b.response = "OK"                              # the backup "recovers"
        r.replicate(2, "k2", "v")                      # still behind: skipped
        r.mark_alive(HOST, b.port)
        r.replicate(3, "k3", "v")                      # alive again: delivered
        self.assertEqual(b.received(), ["REPL 1 k1 v", "REPL 3 k3 v"])
        self.assertEqual(r.status(), {b.addr: "alive"})

    def test_mark_alive_bilinmeyen_adres_yok_sayilir(self):
        b = self.backup()
        r = self.replicator([b.addr])
        r.mark_alive(HOST, 1)                          # not in the peer list: must not raise
        r.mark_alive("localhost", b.port)              # different spelling of the host: no match
        self.assertEqual(r.status(), {b.addr: "alive"})

    def test_kopan_baglanti_yeniden_kurulur(self):
        # KVClient reconnects once on a broken connection; REPL is safe to resend (same seq)
        b = self.backup()
        r = self.replicator([b.addr])
        r.replicate(1, "k1", "v")
        b.drop_connections()
        r.replicate(2, "k2", "v")
        self.assertEqual(r.status(), {b.addr: "alive"})
        self.assertIn("REPL 2 k2 v", b.received())

    def test_eszamanli_replicate_satirlari_karistirmaz(self):
        # 50 threads share one connection per backup; the per-backup lock keeps lines intact
        b = self.backup()
        r = self.replicator([b.addr])
        n = 50
        bariyer = threading.Barrier(n)

        def calis(i):
            bariyer.wait()
            r.replicate(i, f"k{i}", f"deger {i}")

        threadler = [threading.Thread(target=calis, args=(i,)) for i in range(n)]
        for t in threadler:
            t.start()
        for t in threadler:
            t.join()

        beklenen = sorted(f"REPL {i} k{i} deger {i}" for i in range(n))
        self.assertEqual(sorted(b.received()), beklenen)
        self.assertEqual(r.status(), {b.addr: "alive"})


if __name__ == "__main__":
    unittest.main()