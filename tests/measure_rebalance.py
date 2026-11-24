"""
Yer değiştirme ve dağılım ölçümü (Level 3, raporun ana ölçümü).

Sunucu GEREKMEZ: yerleştirme deterministik bir hesap olduğu için yalnızca
common/ring.py kullanılır. Bu yüzden sonuçlar platformdan bağımsızdır
(Windows ve Linux'ta aynı sayılar çıkar).

Ölçülenler:
  1) Yer değiştiren key oranı: sunucu sayısı N'den M'ye değişince (varsayılan 3 -> 4
     ve 4 -> 3) key'lerin yüzde kaçının sahibi değişiyor.
     Ayrıca: yer değiştiren key'lerden kaçı ESKİ sunucular arasında taşındı?
     Consistent hashing'de bu sayı 0 olmalı (key'ler yalnızca yeni sunucuya gider /
     yalnızca kaldırılan sunucudan gelir). mod N'de büyük olur.
  2) Dağılım: N sunucuda her sunucuya kaç key düşüyor; en büyük / en küçük,
     ideal paya oranı, standart sapma.

Düzenler: mod N ve ring (sanal düğüm sayısı 1, 10, 50, 200).

Sunucu kimlikleri ring'in sonucunu etkiler (sanal noktalar "127.0.0.1:5000#i"
gibi adların hash'iyle yerleşir). Bu yüzden portlar sabittir; --port-sets ile
ölçüm farklı port kümelerinde tekrarlanır ve en küçük-en büyük aralığı gösterilir.

Çalıştırma (depo kökünden; birkaç saniye sürer):
    python -m tests.measure_rebalance
    python -m tests.measure_rebalance --port-sets 5
    python -m tests.measure_rebalance --keys 20000 --vnodes 1 10 50 200 500 --csv rebalance.csv
"""

import argparse
import csv
import statistics

from common.ring import make_placement, node_id

HOST = "127.0.0.1"


def make_keys(n):
    """n deterministik key; en uzunu 9 karakter (16 sınırının altında)."""
    return [f"key{i:06d}" for i in range(n)]


def make_nodes(count, base_port):
    return [node_id(HOST, base_port + i) for i in range(count)]


def owners(mode, nodes, vnodes, keys):
    place = make_placement(mode, nodes, vnodes)
    return [place(k) for k in keys]


def movement(mode, vnodes, keys, nodes_before, nodes_after):
    """İki küme arasında yer değiştiren key'ler.

    Dönüş: (yer değiştiren oranı, yer değiştirenlerden eski sunucular arasında taşınanların sayısı)
    "Eski sunucular arasında": key'in hem önceki hem sonraki sahibi iki kümede de var olan bir sunucu.
    """
    before = owners(mode, nodes_before, vnodes, keys)
    after = owners(mode, nodes_after, vnodes, keys)
    common_nodes = set(nodes_before) & set(nodes_after)
    moved = 0
    between_old = 0
    for b, a in zip(before, after):
        if b != a:
            moved += 1
            if b in common_nodes and a in common_nodes:
                between_old += 1
    return moved / len(keys), between_old


def distribution(mode, vnodes, keys, nodes):
    """Sunucu başına key sayıları ve özetleri."""
    counts = {n: 0 for n in nodes}
    for o in owners(mode, nodes, vnodes, keys):
        counts[o] += 1
    values = [counts[n] for n in nodes]
    ideal = len(keys) / len(nodes)
    return {
        "counts": values,
        "min": min(values),
        "max": max(values),
        "max_ideal": max(values) / ideal,          # 1.00 = tamamen dengeli
        "stdev_pct": statistics.pstdev(values) / ideal * 100,
    }


def configs(vnode_list):
    """[(etiket, mode, vnodes), ...]; mod N için vnodes kullanılmaz."""
    return [("mod N", "modn", 1)] + [(f"ring, vnode {v}", "ring", v) for v in vnode_list]


def theoretical_move(mode, n_from, n_to):
    """Beklenen yer değiştirme oranı (ideal durumda)."""
    if mode == "ring":
        # Yalnızca eklenen sunucunun payı gelir / kaldırılan sunucunun payı gider
        return abs(n_to - n_from) / max(n_from, n_to)
    # mod N: key, hash % n_from == hash % n_to ise yerinde kalır.
    # Ardışık lcm(n_from, n_to) kalanın yalnızca min(n_from, n_to) tanesinde ikisi eşittir.
    from math import lcm
    return 1 - min(n_from, n_to) / lcm(n_from, n_to)


def fmt_range(values, pct=True, digits=1):
    """Tek değer ya da "en küçük – en büyük" aralığı."""
    f = (lambda x: f"{x * 100:.{digits}f} %") if pct else (lambda x: f"{x:.{digits}f}")
    lo, hi = min(values), max(values)
    return f(lo) if abs(hi - lo) < 1e-12 else f"{f(lo)} – {f(hi)}"


def main():
    parser = argparse.ArgumentParser(description="Yer değiştirme ve dağılım ölçümü (sunucu gerekmez)")
    parser.add_argument("--keys", type=int, default=10000, help="key sayısı (varsayılan 10000)")
    parser.add_argument("--vnodes", type=int, nargs="+", default=[1, 10, 50, 200],
                        help="ring için sanal düğüm sayıları (varsayılan 1 10 50 200)")
    parser.add_argument("--base-port", type=int, default=5000, help="ilk port (varsayılan 5000)")
    parser.add_argument("--port-sets", type=int, default=1,
                        help="kaç farklı port kümesiyle tekrarlansın (base-port, +100, +200, ...)")
    parser.add_argument("--small", type=int, default=3, help="küçük küme (varsayılan 3)")
    parser.add_argument("--large", type=int, default=4, help="büyük küme (varsayılan 4)")
    parser.add_argument("--csv", default=None, help="ham sonuçları CSV olarak da yaz")
    args = parser.parse_args()

    keys = make_keys(args.keys)
    bases = [args.base_port + 100 * i for i in range(args.port_sets)]
    small, large = args.small, args.large
    rows = []

    for label, mode, v in configs(args.vnodes):
        for base in bases:
            n_small = make_nodes(small, base)
            n_large = make_nodes(large, base)          # aynı sunucular + yeni(ler)
            up, up_between = movement(mode, v, keys, n_small, n_large)
            down, down_between = movement(mode, v, keys, n_large, n_small)
            dist = distribution(mode, v, keys, n_small)
            rows.append({"label": label, "mode": mode, "vnodes": v if mode == "ring" else "",
                         "base_port": base, "up": up, "up_between": up_between,
                         "down": down, "down_between": down_between, **dist})

    print(f"{args.keys} key, sunucular {HOST}:{bases[0]}..."
          + (f", {len(bases)} port kümesi (aralık gösteriliyor)" if len(bases) > 1 else ""))

    # --- Tablo 1: yer değiştirme --------------------------------------------------------------
    print(f"\n### Yer değiştiren key oranı ({small} -> {large} ve {large} -> {small} sunucu)\n")
    print(f"| Düzen | Beklenen | {small} -> {large} | Eski sunucular arasında | "
          f"{large} -> {small} | Eski sunucular arasında |")
    print("|---|---|---|---|---|---|")
    for label, mode, v in configs(args.vnodes):
        g = [r for r in rows if r["label"] == label]
        exp = theoretical_move(mode, small, large)
        print(f"| {label} | ~{exp * 100:.0f} % | {fmt_range([r['up'] for r in g])} | "
              f"{fmt_range([r['up_between'] for r in g], pct=False, digits=0)} | "
              f"{fmt_range([r['down'] for r in g])} | "
              f"{fmt_range([r['down_between'] for r in g], pct=False, digits=0)} |")

    # --- Tablo 2: dağılım -------------------------------------------------------------------
    ideal = args.keys / small
    print(f"\n### Dağılım ({small} sunucu, ideal pay {ideal:.0f} key)\n")
    print("| Düzen | Sunucu başına key | En büyük / ideal | Std. sapma (idealin %'si) |")
    print("|---|---|---|---|")
    for label, _mode, _v in configs(args.vnodes):
        g = [r for r in rows if r["label"] == label]
        if len(g) == 1:
            per_server = " / ".join(str(c) for c in g[0]["counts"])
        else:
            per_server = f"en küçük {min(r['min'] for r in g)}, en büyük {max(r['max'] for r in g)}"
        print(f"| {label} | {per_server} | {fmt_range([r['max_ideal'] for r in g], pct=False, digits=2)} | "
              f"{fmt_range([r['stdev_pct'] for r in g], pct=False, digits=1)} |")

    if args.csv:
        fields = ["label", "mode", "vnodes", "base_port", "up", "up_between", "down", "down_between",
                  "min", "max", "max_ideal", "stdev_pct", "counts"]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({**{k: r[k] for k in fields if k != "counts"},
                            "counts": " ".join(map(str, r["counts"]))})
        print(f"\nCSV: {args.csv}")


if __name__ == "__main__":
    main()
