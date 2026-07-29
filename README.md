# An Exact Mixed-Integer Linear Programming Approach for Large-Scale University Timetabling: A Case Study

Replication package for the *Journal of the Operational Research Society* (JORS) case-oriented paper by
José Ángel Pérez Vázquez, Marco A. Aguirre L., Fausto Antonio Balderas-Jaramillo, Nelson Rangel Valdez,
Claudia Guadalupe Gómez Santillán, and María Lucila Morales Rodríguez —
Tecnológico Nacional de México / Instituto Tecnológico de Ciudad Madero (ITCM).

This repository contains the exact Mixed-Integer Linear Programming (MILP) model, the anonymized
institutional instance data, the raw CPLEX solver logs, and the figure-generation scripts needed to
independently verify every quantitative claim made in the paper.

**Corresponding author:** Marco A. Aguirre L. — `marco.al@cdmadero.tecnm.mx`

---

## 1. What this repository verifies

Every figure and table in the paper that reports a computational result is traceable to a file in this
repository. In particular:

| Claim in the paper | Verifiable from |
|---|---|
| CSE instance solved in 37.47 s, 10.73% relative gap, objective 14,442.05 | `logs/isc_20251_cplex_log.txt` |
| IE instance solved in 600.34 s (time limit), 84.88% relative gap, objective 72,950.00 | `logs/industrial_20251_cplex_log.txt` |
| Instance dimensionality (Table 4): raw and presolve-reduced variable/constraint counts | Both files above (CPLEX presolve summary) |
| Institutional cardinalities ($\lvert P\rvert$, $\lvert A\rvert$, $\lvert M\rvert$, $\lvert G\rvert$, $\lvert MG\rvert$) | `data/isc_20251_instance.json`, `data/industrial_20251_instance.json` |
| Laboratory utilization and teaching-workload distribution (Figures 4–5) | Computed directly from the CPLEX logs by `scripts/generate_fig4_utilizacion.py` and `scripts/generate_fig5_carga.py` |
| Every other figure in the paper | Regenerable from `scripts/` (see §5) |

We deliberately did **not** hand-transcribe any number from the logs into the paper's tables without a
script or a direct `grep`-able line in the corresponding log file, precisely so that a reviewer can repeat
that verification independently.

---

## 2. Repository structure

```
.
├── README.md                    <- this file
├── LICENSE                      <- MIT (see note on anonymized data below)
├── requirements.txt
├── paper/
│   ├── main.tex                 <- full LaTeX source (Taylor & Francis "interact" class)
│   ├── interactapasample.bib    <- bibliography (APA style via apacite), 38 entries
│   ├── main.pdf                 <- compiled manuscript
│   └── figures/                 <- the 6 figures used in the paper (PDF), each reproducible (see scripts/)
├── src/
│   ├── run_solver.py            <- CLI entry point: builds the model, calls CPLEX, exports results
│   └── core/
│       └── milp_core.py         <- MILP model: sets, parameters, variables, constraints, objective
├── data/
│   ├── isc_20251_instance.json         <- CSE instance input (anonymized), matches Table 4
│   └── industrial_20251_instance.json  <- IE instance input (anonymized), matches Table 4
├── logs/
│   ├── isc_20251_cplex_log.txt         <- full CPLEX solver log for the CSE run (anonymized)
│   └── industrial_20251_cplex_log.txt  <- full CPLEX solver log for the IE run (anonymized)
├── scripts/
│   ├── generate_fig1_workflow.py       <- Figure 1: system architecture (conceptual)
│   ├── generate_fig2_escalabilidad.py  <- Figure 2: scalability (dimensionality vs. CPU time)
│   ├── generate_fig3_horario.py        <- Figure 3: schedule fragment (from the CSE solved timetable)
│   ├── generate_fig4_utilizacion.py    <- Figure 4: laboratory utilization (computed from logs/data)
│   ├── generate_fig5_carga.py          <- Figure 5: teaching workload distribution (computed from logs/data)
│   └── generate_fig_jerarquia.py       <- Figure: three-level decision-variable hierarchy (conceptual)
├── docs/
│   └── CHECKSUMS.sha256         <- SHA-256 checksums for every file in data/ and logs/
└── anonymization_map_LOCAL_DO_NOT_PUBLISH.csv   <- NOT included in this repository (see §4)
```

---

## 3. Quick start

```bash
git clone <this-repo-url>
cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Regenerate every figure from verified source data (no solver required)

```bash
for f in scripts/generate_fig*.py; do python3 "$f"; done
```

Each script documents its own data provenance in its module docstring — open any file in `scripts/` and
read the top comment block before editing it. Figures 4 and 5 recompute their numbers directly from
`logs/isc_20251_cplex_log.txt` and `data/isc_20251_instance.json`; they are not hand-entered.

### Re-solve an instance with CPLEX (requires a licensed CPLEX installation)

```bash
export SOLVER_TIME_LIMIT=600
export CPLEX_MIP_GAP=0.20
python3 src/run_solver.py --instance data/isc_20251_instance.json
```

See the header of `src/run_solver.py` for the full list of environment variables (thread count, MIP
emphasis, room/lecturer candidate-list size, etc.) used to produce the results reported in the paper.
Without a CPLEX license, `src/core/milp_core.py` can still be read and imported to inspect the model
formulation, and `logs/` already contains the full solver trace of the runs the paper reports.

---

## 4. Data anonymization

The original scheduling records identify lecturers by Mexican RFC-style tax identifiers. Every file in
`data/` and `logs/` has been anonymized by replacing each RFC with a synthetic ID (`P000001`, `P000002`,
…), using one consistent mapping applied across all four source files, so that the same real lecturer maps
to the same anonymized ID everywhere in this repository.

The mapping itself is **not** included in this repository and is retained only internally by the authors
under institutional access controls, consistent with the Data Availability Statement in the paper. No
other personal data (names, emails, student records) appears in any published file.

---

## 5. Figure-to-script correspondence

| Figure in paper | Script | Data-driven? |
|---|---|---|
| Fig. 1 — System workflow | `generate_fig1_workflow.py` | No (conceptual diagram) |
| Fig. 2 — Scalability analysis | `generate_fig2_escalabilidad.py` | Yes — Table 4/5 values, cross-checked against `logs/` |
| Fig. 3 — Weekly schedule fragment | `generate_fig3_horario.py` | Yes — 5 real events, verified against the CSE solved schedule |
| Fig. 4 — Laboratory utilization | `generate_fig4_utilizacion.py` | Yes — computed from `data/isc_20251_instance.json` + solved schedule |
| Fig. 5 — Teaching workload distribution | `generate_fig5_carga.py` | Yes — computed from the same solved schedule |
| Fig. (hierarchy diagram) | `generate_fig_jerarquia.py` | No (conceptual diagram) |

---

## 6. Key results at a glance

| Instance | Lecturers \|P\| | Venues \|A\| | Courses \|M\| | Cohorts \|G\| | Reduced binaries | Reduced constraints | CPU time | Relative gap | Objective $Z$ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **CSE** (Computer Systems Eng.) | 97 | 35 | 51 | 38 | 15,611 | 28,285 | 37.47 s | 10.73% | 14,442.05 |
| **IE** (Industrial Engineering) | 121 | 75 | 64 | 55 | 533,893 | 835,657 | 600.34 s | 84.88% | 72,950.00 |

Both instances were solved with IBM ILOG CPLEX Optimization Studio 22.1.0.0, MIP gap tolerance 20%, time
limit 600 s, 8 deterministic threads, on an Intel Core i5-1135G7 / 8 GB RAM machine (Windows 10 Pro). Full
configuration is recorded in the header of each file in `logs/`.

**A note on "gap":** CPLEX reports a status of *Integer optimal, tolerance* once the incumbent falls within
the configured 20% gap tolerance — this is not the same as a mathematically certified zero-gap global
optimum. The paper is explicit about this distinction throughout; see Section 5.3 of `paper/main.pdf`.

---

## 7. Checksums

`docs/CHECKSUMS.sha256` lists the SHA-256 hash of every file under `data/` and `logs/`. Verify with:

```bash
cd data && sha256sum -c ../docs/CHECKSUMS.sha256 --ignore-missing
cd ../logs && sha256sum -c ../docs/CHECKSUMS.sha256 --ignore-missing
```

---

## 8. Citing this work

If you use this model, data, or code, please cite the paper:

> Pérez Vázquez, J. A., Aguirre L., M. A., Balderas-Jaramillo, F. A., Rangel Valdez, N., Gómez Santillán,
> C. G., & Morales Rodríguez, M. L. (2026). *An Exact Mixed-Integer Linear Programming Approach for
> Large-Scale University Timetabling: A Case Study.* Journal of the Operational Research Society.

A BibTeX entry will be added here once the DOI is assigned.

---

## 9. License

Code is released under the MIT License (see `LICENSE`). The anonymized institutional data in `data/` and
`logs/` is released under the same terms for research and educational reuse; see the note at the bottom of
`LICENSE` for the specific conditions that apply to that data.

## 10. Acknowledgements

The authors thank the Tecnológico Nacional de México (TecNM) for institutional support and the
Laboratorio Nacional de Tecnologías de la Información (LaNTI) for computing infrastructure.
