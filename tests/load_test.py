"""
Ağ üzerinden eşzamanlılık / yük testi (Seviye 1'in asıl kanıtı).

Her tekrarda temiz bir sunucu ayrı bir süreç olarak başlatılır, böylece depo boştur.

  Test A (kapasite yarışı): 300 client aynı anda farklı yeni key ekler.
                            Beklenen: tam 256 OK, 44 ERR FULL, STATS 256.
  Test B (aynı key):        50 client aynı key'e farklı value yazar.
                            Beklenen: 50 OK, STATS 1, GET yazılan değerlerden biri.

Çalıştırma (depo kökünden; sunucuyu elle başlatmaya gerek yok):
    python -m tests.load_test
    python -m tests.load_test --tekrar 20 --port 5100
"""

import argparse
import subprocess
import sys
import threading
import time

from client.client import KVClient, KVError
from common.protocol import ERR_FULL, MAX_KEYS, RESP_OK

HOST = "127.0.0.1"
CLIENT_TIMEOUT = 15.0     # yük altında cevap gecikebilir; normal 3 sn yerine geniş tutulur


def sunucu_baslat(port):
    """Sunucuyu ayrı bir süreç olarak başlatır ve bağlantı kabul edene kadar bekler."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.server", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):                       # en fazla ~5 sn bekle
        if proc.poll() is not None:
            raise RuntimeError(f"Sunucu başlamadı (port {port} dolu olabilir)")
        try:
            with KVClient(HOST, port, timeout=1.0) as c:
                c.send_command("STATS")
            return proc
        except KVError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"Sunucu {port} portunda 5 sn içinde hazır olmadı")


def sunucu_durdur(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def es_zamanli_gonder(port, komutlar):
    """Her komut için bir client açar; hepsi bağlandıktan sonra komutları AYNI ANDA gönderir.

    Dönüş: (cevap listesi, bağlantı yeniden deneme sayısı)
    Bariyer olmadan thread'ler sırayla çalışır ve yarış neredeyse hiç oluşmaz.
    """
    n = len(komutlar)
    bariyer = threading.Barrier(n)
    cevaplar = [None] * n
    yeniden = [0] * n

    def calis(i):
        c = KVClient(HOST, port, timeout=CLIENT_TIMEOUT)
        try:
            # Bağlantı reddedilirse kısa bekleyip tekrar dene (dinleme kuyruğu dolmuş olabilir)
            for deneme in range(4):
                try:
                    c.connect()
                    break
                except KVError:
                    if deneme == 3:
                        raise
                    yeniden[i] += 1
                    time.sleep(0.05)
            bariyer.wait(timeout=30)          # herkes bağlanana kadar bekle
            cevaplar[i] = c.send_command(komutlar[i])
        except threading.BrokenBarrierError:
            cevaplar[i] = "HATA bariyer"
        except KVError as e:
            cevaplar[i] = f"HATA {e}"
            bariyer.abort()                   # diğerleri sonsuza kadar beklemesin
        finally:
            c.close()

    threadler = [threading.Thread(target=calis, args=(i,)) for i in range(n)]
    for t in threadler:
        t.start()
    for t in threadler:
        t.join()
    return cevaplar, sum(yeniden)


def test_a(port, tekrar, n=300):
    # Key deseni 8 karakter: r07_k123 (16 karakter sınırının altında)
    komutlar = [f"SET r{tekrar:02d}_k{i:03d} v{i}" for i in range(n)]
    t0 = time.perf_counter()
    cevaplar, yeniden = es_zamanli_gonder(port, komutlar)
    sure = time.perf_counter() - t0

    ok = cevaplar.count(RESP_OK)
    full = cevaplar.count(ERR_FULL)
    diger = n - ok - full
    with KVClient(HOST, port) as c:
        stats = c.send_command("STATS")
    gecti = (ok == MAX_KEYS and full == n - MAX_KEYS and diger == 0 and stats == f"STATS {MAX_KEYS}")
    return gecti, f"OK={ok:3d} FULL={full:3d} diger={diger} {stats:9s} yeniden={yeniden} {sure:5.2f}s"


def test_b(port, n=50):
    degerler = [f"deger_{i}" for i in range(n)]
    cevaplar, yeniden = es_zamanli_gonder(port, [f"SET ortak {d}" for d in degerler])
    ok = cevaplar.count(RESP_OK)
    with KVClient(HOST, port) as c:
        stats = c.send_command("STATS")
        get = c.send_command("GET ortak")
    gecerli_deger = get.startswith("VALUE ") and get[len("VALUE "):] in degerler
    gecti = (ok == n and stats == "STATS 1" and gecerli_deger)
    return gecti, f"OK={ok:2d}/{n} {stats} {get}"


def main():
    parser = argparse.ArgumentParser(description="Eşzamanlılık / yük testi")
    parser.add_argument("--tekrar", type=int, default=20, help="tekrar sayısı (varsayılan: 20)")
    parser.add_argument("--port", type=int, default=5100, help="ilk port; her sunucu bir sonrakini kullanır")
    args = parser.parse_args()

    basarisiz = 0
    for testadi, port_kaydir in (("A", 0), ("B", args.tekrar)):
        print(f"\nTest {testadi}:")
        for tekrar in range(1, args.tekrar + 1):
            port = args.port + port_kaydir + tekrar - 1
            proc = sunucu_baslat(port)
            try:
                gecti, ozet = test_a(port, tekrar) if testadi == "A" else test_b(port)
            finally:
                sunucu_durdur(proc)
            if not gecti:
                basarisiz += 1
            print(f"  {tekrar:2d}  {'GECTI ' if gecti else 'KALDI '} {ozet}")

    toplam = 2 * args.tekrar
    print(f"\nSonuç: {toplam - basarisiz}/{toplam} geçti")
    raise SystemExit(1 if basarisiz else 0)


if __name__ == "__main__":
    main()
