"""
generate_fig4_utilizacion.py

Reproduces Figure `fig:utilizacion` (fig4.pdf) of the JORS manuscript:
"Utilization rate of laboratory infrastructure" for the CSE instance.

Data provenance (IMPORTANT)
----------------------------
Utilization is computed directly from the real solved schedule
(`outputs/isc_20251_timetable.csv`, cross-validated against the
checksummed CPLEX log — see generate_fig3_horario.py for the
verification trail), NOT from hand-picked illustrative numbers.

For each laboratory a in the CSE instance's AL set (the 9 laboratories
declared in ISC_37.47s_datos_modelo.json), utilization is:

    utilization(a) = sessions_assigned(a) / (|D| * |H|) * 100
                    = sessions_assigned(a) / (5 * 15) * 100
                    = sessions_assigned(a) / 75 * 100

where 75 is the total number of weekly 1-hour time blocks the model
could theoretically assign to any single room (|D|=5 days, |H|=15
blocks/day), i.e. the venue's full theoretical weekly capacity — not
an arbitrary reference value.

This computation supersedes the previous inline TikZ/pgfplots figure
embedded directly in main.tex, whose room labels ("LC1", "LC2", "LC3")
and utilization values (100%, 93.33%, 86.67%...) did not correspond to
any real room code or verifiable computation. The real result is, in
fact, more informative for the paper's managerial narrative: it shows
that only 4 of the 9 registered CSE laboratories were used at all in
this run, while the other 5 recorded zero assigned sessions.

Usage
-----
    python3 generate_fig4_utilizacion.py [output_path]

Default output_path is ../fig4.pdf (relative to this script). Using
this figure in main.tex requires replacing the inline tikzpicture
block for fig:utilizacion with \\includegraphics{fig4} (done
separately once this script's output is approved).
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------
# 1. Paths to the verified source artifacts
# ---------------------------------------------------------------------
MODEL_JSON = Path(
    "/home/claude/entrega/ENTREGA_BDTEC_CORRIDAS/ENTREGA_BDTEC_CORRIDAS/"
    "ISC_37.47s/ISC_37.47s_datos_modelo.json"
)
TIMETABLE_CSV = Path("/home/claude/jors/jors-main/outputs/isc_20251_timetable.csv")

DAYS = 5
BLOCKS_PER_DAY = 15
WEEKLY_CAPACITY = DAYS * BLOCKS_PER_DAY  # 75 one-hour blocks/week


def compute_utilization():
    with open(MODEL_JSON, encoding="utf-8") as f:
        model = json.load(f)
    labs = model["AL"]  # the 9 real laboratory codes of the CSE instance

    with open(TIMETABLE_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    usage = Counter(r["aula"] for r in rows)

    data = [(lab, usage.get(lab, 0), 100 * usage.get(lab, 0) / WEEKLY_CAPACITY) for lab in labs]
    data.sort(key=lambda t: -t[2])
    return data


def build_figure(output_path: Path) -> None:
    data = compute_utilization()
    labs = [d[0] for d in data]
    pct = [d[2] for d in data]

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "axes.labelsize": 11,
        "axes.labelweight": "bold",
    })

    fig, ax = plt.subplots(figsize=(8.5, 5.0), dpi=300)

    colors = ["#1f6fb2" if v > 0 else "#c0392b" for v in pct]
    bars = ax.bar(labs, pct, color=colors, edgecolor="black", linewidth=0.8, zorder=3)

    for bar, v in zip(bars, pct):
        ax.annotate(
            f"{v:.2f}%", xy=(bar.get_x() + bar.get_width() / 2, v),
            xytext=(0, 4), textcoords="offset points",
            ha="center", fontsize=10, fontweight="bold",
        )

    ax.set_ylim(0, 110)
    ax.set_ylabel("Utilization Rate (\\%)".replace("\\%", "%"))
    ax.set_xlabel("Laboratory")
    ax.set_title("Laboratory infrastructure utilization (CSE instance)")
    ax.grid(axis="y", which="major", linestyle="--", alpha=0.4, zorder=0)
    ax.set_axisbelow(True)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="pdf")
    plt.close(fig)

    print("Computed utilization (lab: blocks/75 -> %):")
    for lab, blocks, v in data:
        print(f"  {lab}: {blocks}/75 -> {v:.2f}%")


if __name__ == "__main__":
    default_out = Path(__file__).resolve().parent.parent / "fig4.pdf"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default_out
    build_figure(out)
    print(f"Figure written to: {out}")
