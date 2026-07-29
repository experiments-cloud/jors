"""
generate_fig2_escalabilidad.py

Reproduces Figure `fig:escalabilidad` (fig2.pdf) of the JORS manuscript:
"Scalability analysis of the exact MILP solver" — a dual-axis chart that
compares reduced-model matrix dimensionality (decision variables and
logical constraints, log scale) against CPU resolution time (linear scale)
for the CSE (medium-scale) and IE (massive-scale) instances.

Data source
-----------
All values are taken directly from Table 4 (`tab:instancias`, reduced /
post-presolve block) and Table 5 (`tab:rendimiento`) of the manuscript,
which were in turn verified against the CPLEX solver logs delivered in
ENTREGA_BDTEC_CORRIDAS (ISC_37.47s_CPLEX_log.txt and
Industrial_600.34s_CPLEX_log.txt). Do not hand-edit the numbers below;
regenerate them from the logs if the underlying experiments change.

Usage
-----
    python3 generate_fig2_escalabilidad.py [output_path]

Default output_path is ../fig2.pdf (relative to this script), matching
the \\includegraphics{fig2} reference in main.tex.
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

# ---------------------------------------------------------------------
# 1. Verified data (Table 4 reduced-model block + Table 5)
# ---------------------------------------------------------------------
INSTANCES = ["CSE\n(Medium Scale)", "IE\n(Large Scale)"]

DECISION_VARIABLES = [15_611, 533_893]     # reduced binaries (post-presolve)
LOGICAL_CONSTRAINTS = [28_285, 835_657]    # reduced rows (post-presolve)
CPU_TIME_SECONDS = [37.47, 600.34]         # solver "Solution time" from logs

# ---------------------------------------------------------------------
# 2. Figure construction
# ---------------------------------------------------------------------
def build_figure(output_path: Path) -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "axes.labelsize": 11,
        "axes.labelweight": "bold",
    })

    fig, ax_left = plt.subplots(figsize=(8.5, 5.2), dpi=300)

    x = np.arange(len(INSTANCES))
    bar_width = 0.32

    color_vars = "#1f6fb2"       # decision variables (darker blue)
    color_cons = "#a9cce3"       # logical constraints (lighter blue)
    color_time = "#c0392b"       # CPU time line (red)

    bars_vars = ax_left.bar(
        x - bar_width / 2, DECISION_VARIABLES, width=bar_width,
        label="Decision Variables", color=color_vars,
        edgecolor="black", linewidth=0.8, zorder=3,
    )
    bars_cons = ax_left.bar(
        x + bar_width / 2, LOGICAL_CONSTRAINTS, width=bar_width,
        label="Logical Constraints", color=color_cons,
        edgecolor="black", linewidth=0.8, zorder=3,
    )

    ax_left.set_yscale("log")
    ax_left.set_ylim(1e4, 1.2e6)
    ax_left.set_ylabel("Matrix Dimension (Count \u2013 Log Scale)")
    ax_left.set_xticks(x)
    ax_left.set_xticklabels(INSTANCES)
    ax_left.set_xlabel("Academic Instances")
    ax_left.yaxis.set_major_formatter(mticker.LogFormatterSciNotation())
    ax_left.grid(axis="y", which="major", linestyle="--", alpha=0.4, zorder=0)
    ax_left.set_axisbelow(True)

    # Right axis: CPU resolution time (linear scale)
    ax_right = ax_left.twinx()
    line_time, = ax_right.plot(
        x, CPU_TIME_SECONDS, color=color_time, marker="o", markersize=8,
        linewidth=2.5, label="CPU Time (s)", zorder=5,
    )
    ax_right.set_ylabel("Processing Time (Seconds)", color=color_time)
    ax_right.tick_params(axis="y", colors=color_time)
    ax_right.set_ylim(0, 700)

    for xi, t in zip(x, CPU_TIME_SECONDS):
        ax_right.annotate(
            f"{t:.2f} s", xy=(xi, t), xytext=(0, 14),
            textcoords="offset points", ha="center",
            fontsize=11, fontweight="bold", color=color_time,
        )

    ax_left.set_title("Scalability Analysis: Matrix Dimensionality vs. CPU Time")

    handles = [bars_vars, bars_cons, line_time]
    labels = [h.get_label() for h in handles]
    ax_left.legend(handles, labels, loc="upper left", frameon=True, fontsize=9)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="pdf")
    plt.close(fig)


if __name__ == "__main__":
    default_out = Path(__file__).resolve().parent.parent / "fig2.pdf"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default_out
    build_figure(out)
    print(f"Figure written to: {out}")
