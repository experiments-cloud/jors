"""
generate_fig3_horario.py

Reproduces Figure `fig:horario` (fig3.pdf) of the JORS manuscript:
"Fragment of the feasible weekly assignment obtained for the CSE
instance" — a weekly Gantt-style chart illustrating the structural
segregation between theory (standard classroom) and practice/laboratory
(specialized lab) components, for a small, real, verifiable fragment of
the 645 events scheduled in the CSE run.

Data provenance (IMPORTANT — read before editing EVENTS below)
----------------------------------------------------------------
The five events below are NOT illustrative placeholders. Each one is a
verbatim row extracted from `outputs/isc_20251_timetable.csv`
(columns: materia, grupo, hora, dia, aula, profesor), taken from the
GitHub replication repository. That CSV was cross-validated against the
checksummed CPLEX log delivered in
`ENTREGA_BDTEC_CORRIDAS/ISC_37.47s/ISC_37.47s_CPLEX_log.txt`: both
report the identical objective value (14,442.051), MIP gap (10.73%),
and solution time (37.47 s) for the CSE instance, confirming the CSV
is the solved schedule of that exact, verified run.

Course codes ("materia") are the institution's anonymized numeric
identifiers (e.g. "3502"); no human-readable course-name catalog
(e.g. "Physics I") was provided in either delivery, so this script
deliberately does NOT invent course titles. If a name catalog becomes
available, only the `course_label()` function below needs to change.

Each event spans exactly one 1-hour time block, matching the true
|H| = 15 daily blocks (07:00-21:00) of the model — unlike the original
placeholder figure, which showed illustrative 2-hour contiguous blocks
not derived from an actual solved instance.

Room-type classification (Theory "T" / Laboratory "L") comes from the
`A_tipo` dictionary of the same verified input JSON
(`ISC_37.47s_datos_modelo.json`).

Usage
-----
    python3 generate_fig3_horario.py [output_path]

Default output_path is ../fig3.pdf (relative to this script), matching
the \\includegraphics{fig3} reference in main.tex.
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ---------------------------------------------------------------------
# 1. Verified real events (materia, grupo, hora, dia, aula, profesor, tipo)
#    Source: outputs/isc_20251_timetable.csv, rows verified individually
#    against the same run's checksummed CPLEX log (see docstring).
#    dia codes follow the institution's convention: L=Mon, M=Tue,
#    X=Wed, J=Thu, V=Fri. hora is the 1-hour block starting at that time.
# ---------------------------------------------------------------------
DAY_CODE_TO_LABEL = {"L": "Monday", "M": "Tuesday", "X": "Wednesday", "J": "Thursday", "V": "Friday"}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

RAW_EVENTS = [
    dict(materia="3502", grupo="D", hora=8,  dia="L", aula="EE5", profesor="P000145", tipo="T"),
    dict(materia="2503", grupo="D", hora=19, dia="M", aula="FFB", profesor="P000012", tipo="T"),
    dict(materia="3506", grupo="D", hora=8,  dia="X", aula="LCC", profesor="P000058", tipo="L"),
    dict(materia="4504", grupo="D", hora=8,  dia="J", aula="L14", profesor="P000138", tipo="L"),
    dict(materia="4501", grupo="D", hora=13, dia="V", aula="FF4", profesor="P000100", tipo="T"),
]

COLOR_THEORY = "#AED6F1"
COLOR_PRACTICE = "#F5B041"
EDGE_COLOR = "black"

Y_MIN, Y_MAX = 7, 21  # true daily range: 07:00 to 21:00, 1-hour blocks


def course_label(materia: str) -> str:
    """No human-readable course-name catalog was provided; report the
    institution's own anonymized course code instead of inventing a title."""
    return f"Course {materia}"


def build_figure(output_path: Path) -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 11,
    })

    fig, ax = plt.subplots(figsize=(11, 7.5), dpi=300)

    day_x = {d: i for i, d in enumerate(DAYS)}
    bar_width = 0.72
    block_hours = 1  # true model granularity: |H| = 15 one-hour blocks/day

    for ev in RAW_EVENTS:
        day_label = DAY_CODE_TO_LABEL[ev["dia"]]
        x = day_x[day_label]
        y0 = ev["hora"]
        color = COLOR_THEORY if ev["tipo"] == "T" else COLOR_PRACTICE
        comp = "Theory" if ev["tipo"] == "T" else "Practice"

        rect = mpatches.Rectangle(
            (x - bar_width / 2, y0), bar_width, block_hours,
            facecolor=color, edgecolor=EDGE_COLOR, linewidth=1.3, zorder=3,
        )
        ax.add_patch(rect)

        ax.text(
            x, y0 + block_hours / 2, f"{course_label(ev['materia'])}\n({comp})",
            ha="center", va="center", fontsize=9.5, fontweight="bold", zorder=4,
        )
        ax.text(
            x, y0 + block_hours + 0.28,
            f"Room: {ev['aula']}" if ev["tipo"] == "T" else f"Lab: {ev['aula']}",
            ha="center", va="top", fontsize=9, zorder=4,
        )
        ax.text(
            x, y0 - 0.18, f"Group {ev['grupo']} \u2013 {ev['profesor']}",
            ha="center", va="bottom", fontsize=7.5, color="dimgray", zorder=4,
        )

    ax.set_xlim(-0.6, len(DAYS) - 0.4)
    ax.set_ylim(Y_MAX + 0.6, Y_MIN - 0.9)  # inverted: 07:00 at top
    ax.set_xticks(range(len(DAYS)))
    ax.set_xticklabels(DAYS, fontsize=12, fontweight="bold")
    ax.set_yticks(range(Y_MIN, Y_MAX + 1))
    ax.set_yticklabels([f"{h:02d}:00" for h in range(Y_MIN, Y_MAX + 1)], fontsize=8)

    ax.grid(axis="y", which="major", linestyle=":", color="gray", alpha=0.4, zorder=0)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    legend_handles = [
        mpatches.Patch(facecolor=COLOR_THEORY, edgecolor=EDGE_COLOR,
                        label="Theory component (Standard room)"),
        mpatches.Patch(facecolor=COLOR_PRACTICE, edgecolor=EDGE_COLOR,
                        label="Practice component (Specialized lab)"),
    ]
    ax.legend(
        handles=legend_handles, loc="upper center",
        bbox_to_anchor=(0.5, 1.08), ncol=2, frameon=False, fontsize=11,
    )

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="pdf")
    plt.close(fig)


if __name__ == "__main__":
    default_out = Path(__file__).resolve().parent.parent / "fig3.pdf"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default_out
    build_figure(out)
    print(f"Figure written to: {out}")
