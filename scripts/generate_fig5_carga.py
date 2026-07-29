"""
generate_fig5_carga.py

Reproduces Figure `fig:carga` (fig5.pdf) of the JORS manuscript:
"Academic workload distribution per lecturer" for the CSE instance.

Data provenance (IMPORTANT)
----------------------------
Workload is computed directly from the real solved schedule
(`outputs/isc_20251_timetable.csv`, the same file verified in
generate_fig3_horario.py / generate_fig4_utilizacion.py against the
checksummed CPLEX log for this run), NOT from hand-picked illustrative
numbers.

For each lecturer p, weekly workload is the count of 1-hour session
rows assigned to p across the 645 scheduled CSE events. Lecturers with
zero assigned sessions are, by definition, absent from the solved
schedule and are excluded from this histogram (of the |P| = 97
lecturers available to the instance, 83 received at least one
session).

This computation supersedes the previous inline TikZ/pgfplots figure
embedded directly in main.tex, whose bin counts — (5, 8), (10, 15),
(15, 12), (20, 7), (25, 5), summing to exactly 47 lecturers — matched
the earlier, incorrect |P| = 47 figure for CSE rather than the
verified value of 97 available / 83 assigned lecturers, and could not
be traced to any computation. The real distribution is, in fact, more
useful for the paper's labor-equity narrative: it is strongly
right-skewed (most lecturers carry a light load; a small minority
carries a disproportionately heavy one), a sharper argument for the
managerial point Section 5.5 makes than a moderate, near-symmetric
distribution would have been.

Usage
-----
    python3 generate_fig5_carga.py [output_path]

Default output_path is ../fig5.pdf (relative to this script). Using
this figure in main.tex requires replacing the inline tikzpicture
block for fig:carga with \\includegraphics{fig5} (done separately
once this script's output is approved).
"""

import csv
import statistics
import sys
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

TIMETABLE_CSV = Path("/home/claude/jors/jors-main/outputs/isc_20251_timetable.csv")

# 5-block-wide bins over the observed range of weekly workload (3 to 24 blocks)
BIN_EDGES = [(1, 5), (6, 10), (11, 15), (16, 20), (21, 25)]
BIN_LABELS = [f"{lo}\u2013{hi}" for lo, hi in BIN_EDGES]


def compute_workload_histogram():
    with open(TIMETABLE_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    workload = Counter(r["profesor"] for r in rows)
    values = list(workload.values())

    counts = []
    for lo, hi in BIN_EDGES:
        counts.append(sum(1 for v in values if lo <= v <= hi))

    stats = {
        "n_lecturers": len(values),
        "min": min(values),
        "max": max(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
    }
    return counts, stats


def build_figure(output_path: Path) -> None:
    counts, stats = compute_workload_histogram()

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "axes.labelsize": 11,
        "axes.labelweight": "bold",
    })

    fig, ax = plt.subplots(figsize=(8.5, 5.0), dpi=300)
    bars = ax.bar(BIN_LABELS, counts, color="#82c785", edgecolor="black", linewidth=0.8, zorder=3)

    for bar, c in zip(bars, counts):
        ax.annotate(
            str(c), xy=(bar.get_x() + bar.get_width() / 2, c),
            xytext=(0, 4), textcoords="offset points",
            ha="center", fontsize=10, fontweight="bold",
        )

    ax.set_ylim(0, max(counts) + 6)
    ax.set_xlabel("Academic Workload (Weekly blocks)")
    ax.set_ylabel("Number of Lecturers")
    ax.set_title("Teaching workload distribution (CSE instance)")
    ax.grid(axis="y", which="major", linestyle="--", alpha=0.4, zorder=0)
    ax.set_axisbelow(True)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="pdf")
    plt.close(fig)

    print(f"Lecturers with >=1 assigned session: {stats['n_lecturers']}")
    print(f"Workload range: {stats['min']}-{stats['max']} weekly blocks "
          f"(mean={stats['mean']:.2f}, median={stats['median']:.1f})")
    for label, c in zip(BIN_LABELS, counts):
        print(f"  {label} blocks: {c} lecturers")


if __name__ == "__main__":
    default_out = Path(__file__).resolve().parent.parent / "fig5.pdf"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default_out
    build_figure(out)
    print(f"Figure written to: {out}")
