"""
Rapor grafikleri (Almanca etiketler): Level 2 gecikme, Level 3 yer değiştirme, dağılım, ilk ERR FULL.

Sistemin kendisi dış kütüphane kullanmaz; bu betik YALNIZCA rapor için matplotlib ister:
    pip install matplotlib

Level 3 verileri burada yeniden hesaplanır (yerleştirme deterministik; Windows ve Linux'ta
birebir aynı sonuç, bkz. notlar.md 2026-11-25). Level 2 gecikme verileri makineye bağlı
olduğu için Windows ölçümünden sabit değer olarak alınır (notlar.md, 2026-11-20).

Çalıştırma (depo kökünden):
    python -m tests.plot_figures                 # docs/figures/ altına PNG + PDF
    python -m tests.plot_figures --out baska_klasor
"""

import argparse
import os
import statistics

import matplotlib

matplotlib.use("Agg")                     # pencere açmadan dosyaya çiz
import matplotlib.pyplot as plt          # noqa: E402
from matplotlib.ticker import FuncFormatter, PercentFormatter  # noqa: E402

from tests.measure_first_full import key_stream, nodes_for, predict  # noqa: E402
from tests.measure_rebalance import distribution, make_keys, make_nodes, movement  # noqa: E402
from common.ring import make_placement   # noqa: E402

# --- Görünüm (dataviz referans paleti, açık mod; slot 1-2 doğrulandı) -------------------------
SERIES_1 = "#2a78d6"     # mavi
SERIES_2 = "#eb6834"     # turuncu
INK = "#0b0b0b"          # birincil metin
INK_2 = "#52514e"        # ikincil metin, eksenler
GRID = "#e4e3df"         # silik ızgara
SURFACE = "#ffffff"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10,
    "axes.edgecolor": INK_2, "axes.labelcolor": INK, "axes.titlecolor": INK,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True,
    "xtick.color": INK_2, "ytick.color": INK_2,
    "legend.frameon": False,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.dpi": 300, "savefig.bbox": "tight",
})

CONFIGS = [("mod N", "modn", 1), ("Ring, 1 VN", "ring", 1), ("Ring, 10 VN", "ring", 10),
           ("Ring, 50 VN", "ring", 50), ("Ring, 200 VN", "ring", 200)]
PORT_SETS = [5000 + 100 * i for i in range(5)]
KEY_SETS = 5


def de_num(x, digits=0):
    """Almanca sayı biçimi: ondalık virgül."""
    return f"{x:.{digits}f}".replace(".", ",")


def de_pct(digits=0):
    return FuncFormatter(lambda v, _pos: de_num(v * 100, digits) + " %")


def range_bar(ax, x, lo, hi, color=INK_2):
    """İnce min-max çizgisi (aralık)."""
    ax.plot([x, x], [lo, hi], color=color, linewidth=1.2, solid_capstyle="butt", zorder=3)
    for y in (lo, hi):
        ax.plot([x - 0.08, x + 0.08], [y, y], color=color, linewidth=1.2, zorder=3)


def save(fig, out, name):
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"))
    plt.close(fig)
    print(f"  {name}.png / .pdf")


# --- Abbildung 1: verschobene Schlüssel 3 -> 4 --------------------------------------------------

def fig_rebalance(out, keys):
    labels, to_new, between_old, totals_range = [], [], [], []
    for label, mode, v in CONFIGS:
        per_set = []
        for base in PORT_SETS:
            frac, between = movement(mode, v, keys, make_nodes(3, base), make_nodes(4, base))
            per_set.append((frac, between / len(keys)))
        frac0, betw0 = per_set[0]                     # Portsatz 5000 als Balken
        labels.append(label)
        between_old.append(betw0)
        to_new.append(frac0 - betw0)
        totals_range.append((min(f for f, _ in per_set), max(f for f, _ in per_set)))

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    xs = range(len(labels))
    w = 0.56
    ax.bar(xs, to_new, w, color=SERIES_1, label="zum neuen Knoten", zorder=2)
    ax.bar(xs, between_old, w, bottom=to_new, color=SERIES_2, label="zwischen alten Knoten",
           zorder=2, edgecolor=SURFACE, linewidth=1.5)
    for x, (lo, hi), a, b in zip(xs, totals_range, to_new, between_old):
        if hi - lo > 1e-9:
            range_bar(ax, x + w / 2 + 0.07, lo, hi)
        ax.text(x, a + b - 0.02, de_num((a + b) * 100, 1) + " %", ha="center", va="top",
                color="white", fontsize=9, fontweight="bold", zorder=5)
    ax.axhline(0.25, color=INK_2, linestyle=(0, (4, 3)), linewidth=1, label="Ideal (Ring): 1/4", zorder=4)
    ax.set_xticks(list(xs), labels)
    ax.set_ylim(0, 0.85)
    ax.yaxis.set_major_formatter(de_pct())
    ax.set_ylabel("Anteil verschobener Schlüssel")
    ax.set_title("Verschobene Schlüssel beim Hinzufügen eines 4. Knotens")
    ax.legend(loc="upper right", ncols=1)
    fig.text(0, -0.04, f"{len(keys):,} Schlüssel; Balken: Ports 5000–5002 (+5003); "
             "Linie rechts: Spannweite über 5 Portsätze.".replace(",", "."),
             color=INK_2, fontsize=8)
    save(fig, out, "abb_rebalance")


# --- Abbildung 2: Lastverteilung -------------------------------------------------------------

def fig_distribution(out, keys):
    labels, med, lo, hi = [], [], [], []
    for label, mode, v in CONFIGS:
        vals = [distribution(mode, v, keys, make_nodes(3, base))["max_ideal"] for base in PORT_SETS]
        labels.append(label)
        med.append(statistics.median(vals))
        lo.append(min(vals))
        hi.append(max(vals))

    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    xs = list(range(len(labels)))
    for x, a, b in zip(xs, lo, hi):
        if b - a > 1e-9:
            range_bar(ax, x, a, b, color=SERIES_1)
    ax.scatter(xs, med, s=46, color=SERIES_1, zorder=4, edgecolor=SURFACE, linewidth=1.5)
    for x, m, b in zip(xs, med, hi):
        ax.text(x, max(m, b) + 0.035, de_num(m, 2), ha="center", va="bottom", color=INK, fontsize=9)
    ax.axhline(1.0, color=INK_2, linestyle=(0, (4, 3)), linewidth=1)
    ax.text(1.5, 0.985, "ideal gleichmäßig (1,0×)", ha="center", va="top", color=INK_2, fontsize=8.5)
    ax.set_xticks(xs, labels)
    ax.set_xlim(-0.5, len(labels) - 0.5)
    ax.set_ylim(0.85, 2.35)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: de_num(v, 1) + "×"))
    ax.set_ylabel("vollster Knoten / Idealanteil")
    ax.set_title("Lastverteilung auf 3 Knoten")
    fig.text(0, -0.05, f"{len(keys):,} Schlüssel; Punkt: Median, Linie: Spannweite über 5 Portsätze."
             .replace(",", "."), color=INK_2, fontsize=8)
    save(fig, out, "abb_verteilung")


# --- Abbildung 3: erstes ERR FULL --------------------------------------------------------------

def fig_first_full(out):
    labels, med, lo, hi, fits = [], [], [], [], []
    for label, mode, v in CONFIGS:
        place = make_placement(mode, nodes_for(5000), v)
        res = [predict(place, key_stream(ks)) for ks in range(KEY_SETS)]
        firsts = [r[0] for r in res]
        labels.append(label)
        med.append(statistics.median(firsts))
        lo.append(min(firsts))
        hi.append(max(firsts))
        fits.append(sum(1 for r in res if r[1] == 0))

    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    xs = list(range(len(labels)))
    for x, a, b in zip(xs, lo, hi):
        range_bar(ax, x, a, b, color=SERIES_1)
    ax.scatter(xs, med, s=46, color=SERIES_1, zorder=4, edgecolor=SURFACE, linewidth=1.5)
    for x, m, f in zip(xs, med, fits):
        ax.text(x + 0.12, m, f"{m:.0f}", va="center", color=INK, fontsize=9)
        ax.text(x, 405, f"600 passen:\n{f}/{KEY_SETS}", ha="center", va="bottom", color=INK_2, fontsize=8)
    ax.axhline(768, color=INK_2, linestyle=(0, (4, 3)), linewidth=1)
    ax.text(len(labels) - 0.5, 771, "theoretische Obergrenze 3 × 256 = 768", ha="right", va="bottom",
            color=INK_2, fontsize=8.5)
    ax.axhline(600, color=SERIES_2, linestyle=(0, (1, 2)), linewidth=1.2)
    ax.text(-0.45, 603, "Demo: 600 Schlüssel", ha="left", va="bottom", color=INK_2, fontsize=8.5)
    ax.set_xticks(xs, labels)
    ax.set_xlim(-0.5, len(labels) - 0.5)
    ax.set_ylim(400, 800)
    ax.set_ylabel("Schlüssel bis zum ersten ERR FULL")
    ax.set_title("Erstes ERR FULL bei 3 Knoten (je 256 Einträge)")
    fig.text(0, -0.05, f"Ports 5000–5002; Punkt: Median, Linie: Spannweite über {KEY_SETS} Schlüsselmengen. "
             "Messung auf echten Servern = Vorhersage (25/25).", color=INK_2, fontsize=8)
    save(fig, out, "abb_erstes_err_full")


# --- Abbildung 4: Latenz (Level 2) -------------------------------------------------------------

# Windows 11, Python 3.13.15, 1.000 SET je Konfiguration, Median aus 3 Clustern (notlar.md, 2026-11-20).
# 100 Zeichen: zwei getrennte Läufe -> Balken = Mittelwert, Linie = beide Läufe.
LATENCY_100 = {1: (0.030, 0.028), 2: (0.063, 0.060), 3: (0.090, 0.098)}
LATENCY_4080 = {1: 0.032, 2: 0.064, 3: 0.103}


def fig_latency(out):
    copies = [1, 2, 3]
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    w = 0.34
    a = [statistics.fmean(LATENCY_100[c]) for c in copies]
    b = [LATENCY_4080[c] for c in copies]
    xa = [c - w / 2 - 0.02 for c in copies]
    xb = [c + w / 2 + 0.02 for c in copies]
    ax.bar(xa, a, w, color=SERIES_1, label="Value 100 Zeichen", zorder=2)
    ax.bar(xb, b, w, color=SERIES_2, label="Value 4.080 Zeichen", zorder=2)
    for x, c in zip(xa, copies):
        lo, hi = min(LATENCY_100[c]), max(LATENCY_100[c])
        range_bar(ax, x, lo, hi)
    tops = [max(LATENCY_100[c]) for c in copies] + b
    for x, v, t in zip(xa + xb, a + b, tops):
        ax.text(x, t + 0.003, de_num(v, 3), ha="center", va="bottom", color=INK, fontsize=8.5)
    ax.set_xticks(copies, ["1 Kopie\n(ohne Replikation)", "2 Kopien\n(1 Backup)", "3 Kopien\n(2 Backups)"])
    ax.set_ylim(0, 0.13)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: de_num(v, 2)))
    ax.set_ylabel("Median-Latenz pro SET [ms]")
    ax.set_title("Latenz von SET mit synchroner Replikation")
    ax.legend(loc="upper left")
    fig.text(0, -0.08, "Windows 11, ein Rechner (Loopback); 1.000 SET je Konfiguration, Median aus 3 Clustern. "
             "Linie: zwei getrennte Läufe.", color=INK_2, fontsize=8)
    save(fig, out, "abb_latenz")


def main():
    parser = argparse.ArgumentParser(description="Rapor grafikleri (PNG + PDF)")
    parser.add_argument("--out", default=os.path.join("docs", "figures"))
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    keys = make_keys(10000)
    print(f"Grafikler -> {args.out}")
    fig_rebalance(args.out, keys)
    fig_distribution(args.out, keys)
    fig_first_full(args.out)
    fig_latency(args.out)


if __name__ == "__main__":
    main()
