"""
generate_fig1_workflow.py

Reproduces Figure `fig:workflow` (fig1.pdf) of the JORS manuscript:
"System architecture and workflow of the proposed MILP model."

Data provenance
----------------
Like `fig_jerarquia`, this figure is conceptual rather than data-driven:
it illustrates the four-stage system pipeline described in Section 3
(institutional data -> preprocessing -> exact optimization engine ->
optimized output), not a specific experimental result. It is generated
by this script, rather than kept as a static PDF with no source, for
the same reproducibility reason as every other figure in the article
(Section 5): every visual artifact should be regenerable from a single,
version-controlled source rather than an opaque binary file.

Usage
-----
    python3 generate_fig1_workflow.py [output_path]

Default output_path is ../fig1.pdf (relative to this script), matching
the \\includegraphics{fig1} reference in main.tex.
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch

STAGES = [
    ("1. Institutional data", "(Courses, professors,\nrooms, availability)"),
    ("2. Data preprocessing\n& parameterization", "(Python framework)"),
    ("3. Exact optimization\nMILP engine", "(IBM ILOG CPLEX)"),
    ("4. Optimized output", "(Feasible timetables\n& Capacity reports)"),
]

BOX_WIDTH = 2.6
BOX_HEIGHT = 1.5
GAP = 1.1
FACE_COLOR = "#EAF2FB"
EDGE_COLOR = "#1f6fb2"


def build_figure(output_path: Path) -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10.5,
    })

    n = len(STAGES)
    total_width = n * BOX_WIDTH + (n - 1) * GAP
    fig, ax = plt.subplots(figsize=(11, 2.6), dpi=300)

    centers_x = []
    x = -total_width / 2
    for _ in STAGES:
        centers_x.append(x)
        x += BOX_WIDTH + GAP

    for (title, desc), x0 in zip(STAGES, centers_x):
        box = mpatches.FancyBboxPatch(
            (x0, -BOX_HEIGHT / 2), BOX_WIDTH, BOX_HEIGHT,
            boxstyle="round,pad=0.02,rounding_size=0.08",
            linewidth=1.6, edgecolor=EDGE_COLOR, facecolor=FACE_COLOR, zorder=3,
        )
        ax.add_patch(box)
        cx = x0 + BOX_WIDTH / 2
        ax.text(cx, 0.22, title, ha="center", va="center",
                fontsize=10, fontweight="bold", zorder=4)
        ax.text(cx, -0.38, desc, ha="center", va="center",
                fontsize=8.8, zorder=4)

    for i in range(n - 1):
        x_from = centers_x[i] + BOX_WIDTH
        x_to = centers_x[i + 1]
        arrow = FancyArrowPatch(
            (x_from, 0), (x_to, 0),
            arrowstyle="-|>", mutation_scale=18,
            linewidth=1.8, color="black", zorder=2,
        )
        ax.add_patch(arrow)

    ax.set_xlim(centers_x[0] - 0.4, centers_x[-1] + BOX_WIDTH + 0.4)
    ax.set_ylim(-BOX_HEIGHT / 2 - 0.3, BOX_HEIGHT / 2 + 0.3)
    ax.axis("off")

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="pdf")
    plt.close(fig)


if __name__ == "__main__":
    default_out = Path(__file__).resolve().parent.parent / "fig1.pdf"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default_out
    build_figure(out)
    print(f"Figure written to: {out}")
