"""
"İlk ERR FULL" ölçümü (Level 3): 3 sunucu teoride 3 × 256 = 768 kayıt tutar, pratikte kaç?

Key'ler sunuculara eşit dağılmadığı için bir sunucu diğerlerinden önce 256'ya ulaşır;
o sunucuya düşen bir sonraki YENİ key, diğer sunucularda yer olsa bile ERR FULL alır.
Bu betik iki şeyi ölçer:
  - ilk ERR FULL kaçıncı key'de geliyor
  - ilk 600 key yazılınca kaçı ERR FULL alıyor (600 key demosu sığar mı?)

Her düzen ve her key kümesi için iki sayı yan yana konur:
  - TAHMİN: yerleştirme deterministik olduğu için ring.py ile önceden hesaplanır (sunucusuz)
  - ÖLÇÜM:  gerçek 3 sunucuya (ayrı süreçler, --cluster) key'ler tek tek yazılır
İkisi TAM OLARAK aynı olmalı. Farklıysa sunucunun 256 sınırında ya da yerleştirmede hata var.

Ölçüm Öğrenci2'nin ShardedClient'ını beklemez: key'in sahibi make_placement ile hesaplanıp
doğrudan o sunucuya KVClient ile gidilir (aynı kural; sunucu yanlış sunucuyu WRONG_NODE ile reddeder).

Portlar sabittir: sanal noktalar sunucu adının ("127.0.0.1:5000#i") hash'iyle yerleştiği için
dağılım portlara bağlıdır (bkz. notlar.md, 2026-11-21). --port-sets ile farklı port kümeleri denenir.

Çalıştırma (depo kökünden):
    python -m tests.measure_first_full                 # tahmin + ölçüm (birkaç dakika)
    python -m tests.measure_first_full --offline       # yalnızca tahmin (saniyeler)
    python -m tests.measure_first_full --key-sets 10 --port-sets 3 --csv first_full.csv
"""

import argparse
import csv
import os
import statistics
import tempfile

from client.client import KVClient
from common.protocol import ERR_FULL, MAX_KEYS, RESP_OK
from common.ring import make_placement, node_id
from scripts.start_cluster import ClusterError, start_cluster, stop_cluster, write_shard_config

HOST = "127.0.0.1"
N_SERVERS = 3
DEMO_KEYS = 600
# Güvercin yuvası: 3 × 256 + 1 = 769. key'de bir sunucunun dolmuş olması kesin
MAX_ATTEMPTS = N_SERVERS * MAX_KEYS + 1


def key_stream(key_set):
    """Bir key kümesinin key'leri; her küme farklı key'ler (en uzunu 9 karakter)."""
    return [f"s{key_set:02d}_{i:04d}" for i in range(MAX_ATTEMPTS)]


def nodes_for(base_port):
    return [node_id(HOST, base_port + i) for i in range(N_SERVERS)]


def predict(place, keys):
    """Sunucusuz tahmin. Dönüş: (ilk ERR FULL'un sırası (1'den), ilk 600'de ERR FULL sayısı, 600'deki dağılım)."""
    counts = {}
    first_full = None
    full_in_demo = 0
    at_demo = None
    for i, key in enumerate(keys, start=1):
        owner = place(key)
        if counts.get(owner, 0) >= MAX_KEYS:             # sahibi dolu: bu key ERR FULL alır
            if first_full is None:
                first_full = i
            if i <= DEMO_KEYS:
                full_in_demo += 1
        else:
            counts[owner] = counts.get(owner, 0) + 1
        if i == DEMO_KEYS:
            at_demo = dict(counts)
        if first_full is not None and i >= DEMO_KEYS:
            break
    return first_full, full_in_demo, at_demo


def measure(place, keys, base_port, mode, vnodes):
    """Gerçek kümede ölçüm; dönüş predict() ile aynı biçimde."""
    servers = [{"host": HOST, "port": base_port + i} for i in range(N_SERVERS)]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, "cluster.json")
        write_shard_config(cfg, servers, placement=mode, vnodes=vnodes)
        running = start_cluster(servers, quiet=True, cluster_file=cfg)
        clients = {}
        try:
            for s in servers:
                clients[node_id(HOST, s["port"])] = KVClient(HOST, s["port"], timeout=10)
            first_full = None
            full_in_demo = 0
            for i, key in enumerate(keys, start=1):
                response = clients[place(key)].send_command(f"SET {key} v")
                if response == ERR_FULL:
                    if first_full is None:
                        first_full = i
                    if i <= DEMO_KEYS:
                        full_in_demo += 1
                elif response != RESP_OK:
                    raise RuntimeError(f"{key}: beklenmeyen cevap {response!r}")
                if first_full is not None and i >= DEMO_KEYS:
                    break
            # Sunuculardan gerçek kayıt sayıları (yalnızca bilgi için)
            stats = {n: c.send_command("STATS") for n, c in clients.items()}
        finally:
            for c in clients.values():
                c.close()
            stop_cluster(running)
    return first_full, full_in_demo, stats


def configs(vnode_list):
    return [("mod N", "modn", 1)] + [(f"ring, vnode {v}", "ring", v) for v in vnode_list]


def main():
    parser = argparse.ArgumentParser(description="İlk ERR FULL ölçümü: tahmin ve gerçek küme")
    parser.add_argument("--vnodes", type=int, nargs="+", default=[1, 10, 50, 200])
    parser.add_argument("--key-sets", type=int, default=5, help="kaç farklı key kümesi (varsayılan 5)")
    parser.add_argument("--base-port", type=int, default=5000, help="ilk port (varsayılan 5000)")
    parser.add_argument("--port-sets", type=int, default=1, help="kaç port kümesi (base, +100, ...)")
    parser.add_argument("--offline", action="store_true", help="yalnızca tahmin; sunucu başlatma")
    parser.add_argument("--csv", default=None)
    args = parser.parse_args()

    bases = [args.base_port + 100 * i for i in range(args.port_sets)]
    rows = []
    mismatches = 0
    for label, mode, v in configs(args.vnodes):
        for base in bases:
            place = make_placement(mode, nodes_for(base), v)
            for ks in range(args.key_sets):
                keys = key_stream(ks)
                p_first, p_demo, p_dist = predict(place, keys)
                row = {"label": label, "mode": mode, "vnodes": v if mode == "ring" else "",
                       "base_port": base, "key_set": ks,
                       "pred_first_full": p_first, "pred_full_in_600": p_demo,
                       "dist_at_600": " / ".join(str(p_dist.get(n, 0)) for n in nodes_for(base))}
                if not args.offline:
                    try:
                        m_first, m_demo, _stats = measure(place, keys, base, mode, v)
                    except ClusterError as e:
                        raise SystemExit(f"Hata: küme başlatılamadı: {e}")
                    row.update(meas_first_full=m_first, meas_full_in_600=m_demo,
                               match=(m_first, m_demo) == (p_first, p_demo))
                    if not row["match"]:
                        mismatches += 1
                        print(f"  UYUŞMAZLIK: {label}, port {base}, küme {ks}: tahmin {p_first}/{p_demo}, "
                              f"ölçüm {m_first}/{m_demo}")
                rows.append(row)
            print(f"  {label}, port {base}: bitti", flush=True)

    src = "tahmin" if args.offline else "ölçüm (tahminle karşılaştırmalı)"
    print(f"\n### İlk ERR FULL, {N_SERVERS} sunucu ({src}; {args.key_sets} key kümesi"
          + (f", {len(bases)} port kümesi" if len(bases) > 1 else f", portlar {bases[0]}...") + ")\n")
    head = "| Düzen | İlk ERR FULL (medyan) | En küçük – en büyük | 600 key'de ERR FULL (medyan) | 600 key sığdı |"
    sep = "|---|---|---|---|---|"
    if not args.offline:
        head += " Tahmin = ölçüm |"
        sep += "---|"
    print(head)
    print(sep)
    key_first = "pred_first_full" if args.offline else "meas_first_full"
    key_demo = "pred_full_in_600" if args.offline else "meas_full_in_600"
    for label, _mode, _v in configs(args.vnodes):
        g = [r for r in rows if r["label"] == label]
        firsts = [r[key_first] for r in g]
        demos = [r[key_demo] for r in g]
        fits = sum(1 for d in demos if d == 0)
        line = (f"| {label} | {statistics.median(firsts):.0f} | {min(firsts)} – {max(firsts)} | "
                f"{statistics.median(demos):.0f} | {fits}/{len(g)} |")
        if not args.offline:
            line += f" {sum(r['match'] for r in g)}/{len(g)} |"
        print(line)
    print(f"\nTeorik üst sınır: {N_SERVERS} × {MAX_KEYS} = {N_SERVERS * MAX_KEYS} kayıt.")
    if not args.offline:
        print("Tahmin ve ölçüm her çalıştırmada aynı." if mismatches == 0
              else f"DİKKAT: {mismatches} çalıştırmada tahmin ile ölçüm farklı.")

    if args.csv:
        fields = list(rows[0].keys())
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"CSV: {args.csv}")
    if mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
