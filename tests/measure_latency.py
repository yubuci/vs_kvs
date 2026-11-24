"""
Gecikme ölçümü: replikasyonun SET süresine etkisi (Level 2, rapor bölüm 5).

Üç düzen ölçülür; her biri için yeni bir küme başlatılır:
    1 kopya  tek sunucu, rolsüz (Level 1 davranışı, replikasyon yok)
    2 kopya  primary + 1 backup
    3 kopya  primary + 2 backup

Tek bir client, tek bir bağlantı üzerinden SET'leri SIRAYLA gönderir ve her birinin
gidiş-dönüş süresini ölçer. Replikasyon senkron olduğu için primary, OK'yi ancak
backup'lar REPL'e cevap verdikten sonra döner; fark doğrudan bu süreye yansır.

Çalıştırma (depo kökünden):
    python -m tests.measure_latency
    python -m tests.measure_latency --n 2000 --value-size 4080 --repeat 3
    python -m tests.measure_latency --csv sonuc.csv

Sonuçlar makineye ve işletim sistemine bağlıdır; rapora yazarken ölçümün yapıldığı
sistem (çıktının başında yazar) de belirtilmeli.
"""

import argparse
import csv
import platform
import socket
import statistics
import sys
import time

from client.client import KVClient
from common.protocol import MAX_KEYS, RESP_OK
from scripts.start_cluster import start_cluster, stop_cluster

HOST = "127.0.0.1"
KEY_POOL = 200      # key'ler bu havuzda döner (256 sınırının altında); sonrakiler güncellemedir


def free_ports(n):
    """İşletim sisteminden n tane boş port alır (açık bir kümeyle çakışmasın)."""
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind((HOST, 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def build_servers(copies):
    """copies=1: tek rolsüz sunucu; copies>=2: 1 primary + (copies-1) backup."""
    ports = free_ports(copies)
    if copies == 1:
        return [{"host": HOST, "port": ports[0]}], False
    servers = [{"host": HOST, "port": ports[0], "role": "primary"}]
    servers += [{"host": HOST, "port": p, "role": "backup"} for p in ports[1:]]
    return servers, True


def measure(port, n, warmup, value):
    """Aynı bağlantı üzerinden warmup + n SET gönderir; ölçülen n sürenin listesini (ms) döndürür.

    Isınma (warmup) SET'leri ölçülmez: ilk istekler bağlantı kurulumu, Python'un
    ilk çağrıları ve backup bağlantılarının tembel açılması yüzünden yavaştır.
    """
    times = []
    with KVClient(HOST, port, timeout=10) as c:
        for i in range(warmup + n):
            line = f"SET k{i % KEY_POOL:03d} {value}"
            t0 = time.perf_counter()
            response = c.send_command(line)
            dt = (time.perf_counter() - t0) * 1000.0
            if response != RESP_OK:
                raise RuntimeError(f"beklenmeyen cevap: {response!r}")
            if i >= warmup:
                times.append(dt)
    return times


def summarize(times):
    """Ortalama, medyan, p95, en küçük ve en büyük (ms)."""
    s = sorted(times)
    p95 = s[min(len(s) - 1, int(round(0.95 * len(s))) - 1)]
    return {
        "mean": statistics.fmean(s),
        "median": statistics.median(s),
        "p95": p95,
        "min": s[0],
        "max": s[-1],
    }


def main():
    parser = argparse.ArgumentParser(description="SET gecikmesini 1, 2 ve 3 kopyayla ölçer")
    parser.add_argument("--n", type=int, default=1000, help="düzen başına ölçülen SET sayısı (varsayılan 1000)")
    parser.add_argument("--warmup", type=int, default=50, help="ölçülmeyen ısınma SET'leri (varsayılan 50)")
    parser.add_argument("--value-size", type=int, default=100, help="value uzunluğu, karakter (varsayılan 100)")
    parser.add_argument("--repeat", type=int, default=1, help="her düzen kaç kez ölçülsün (yeni kümeyle)")
    parser.add_argument("--csv", default=None, help="ham özetleri bu CSV dosyasına da yaz")
    args = parser.parse_args()
    if KEY_POOL > MAX_KEYS:
        raise SystemExit("KEY_POOL 256'yı aşmamalı")
    value = "x" * args.value_size

    print(f"Sistem: {platform.system()} {platform.release()}, Python {platform.python_version()}, "
          f"{platform.machine()}")
    print(f"Düzen başına {args.n} SET (+{args.warmup} ısınma), value {args.value_size} karakter, "
          f"{args.repeat} tekrar\n")

    rows = []
    for copies in (1, 2, 3):
        for rep in range(1, args.repeat + 1):
            servers, replication = build_servers(copies)
            running = start_cluster(servers, replication=replication, quiet=True)
            try:
                times = measure(servers[0]["port"], args.n, args.warmup, value)
            finally:
                stop_cluster(running)
            stats = summarize(times)
            rows.append({"copies": copies, "repeat": rep, **stats})
            print(f"  {copies} kopya, tekrar {rep}: ortalama {stats['mean']:.3f} ms, "
                  f"medyan {stats['median']:.3f} ms", file=sys.stderr)

    # Tekrarlar varsa düzen başına medyanların medyanı alınır (tek bir yavaş çalıştırma sonucu bozmasın)
    print("\n| Kopya | Ortalama (ms) | Medyan (ms) | p95 (ms) | Min (ms) | Max (ms) | Medyan / 1 kopya |")
    print("|---|---|---|---|---|---|---|")
    base = None
    for copies in (1, 2, 3):
        group = [r for r in rows if r["copies"] == copies]
        agg = {k: statistics.median(r[k] for r in group) for k in ("mean", "median", "p95", "min", "max")}
        if base is None:
            base = agg["median"]
        print(f"| {copies} | {agg['mean']:.3f} | {agg['median']:.3f} | {agg['p95']:.3f} | "
              f"{agg['min']:.3f} | {agg['max']:.3f} | {agg['median'] / base:.2f}× |")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["copies", "repeat", "mean", "median", "p95", "min", "max"])
            w.writeheader()
            w.writerows(rows)
        print(f"\nCSV: {args.csv}")


if __name__ == "__main__":
    main()
