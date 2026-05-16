\# Reproduction Manual



This document describes how to install, configure, and reproduce the experiments included in this repository.



\## 1. Requirements



The following software is required:



\- Python 3.10

\- IBM ILOG CPLEX Optimization Studio 22.1

\- PuLP

\- pandas

\- Streamlit

\- Windows 10/11 recommended



CPLEX must be installed locally. This repository does not include IBM CPLEX.



\## 2. Clone the repository



```bash

git clone https://github.com/<repository-owner>/academic-timetabling-milp.git

cd academic-timetabling-milp

```



\## 3. Create a virtual environment



```bash

python -m venv .venv

```



Activate it on Windows:



```bash

.venv\\Scripts\\activate

```



\## 4. Install dependencies



```bash

pip install -r requirements.txt

```



\## 5. Configure the environment



Copy the example configuration file:



```bash

copy .env.example .env

```



Then edit `.env` and configure the local CPLEX executable path:



```env

CPLEX\_BIN=C:\\Program Files\\IBM\\ILOG\\CPLEX\_Studio221\\cplex\\bin\\x64\_win64\\cplex.exe

```



The `.env` file is intentionally excluded from the repository because it may contain local paths, database credentials, or machine-specific parameters.



\## 6. Input data



Anonymized sample instances are available in:



```text

data/samples/

```



The main sample files are:



```text

isc\_20251\_sample.json

industrial\_20251\_sample.json

```



\## 7. Run the solver



To execute the solver:



```bash

python src/run\_solver.py

```



The solver reads the input JSON file configured in `.env` and generates output files using the prefix defined by `EXPORT\_PREFIX`.



\## 8. Run the visualization dashboard



```bash

streamlit run src/ui/visualizer\_app.py

```



The dashboard can be used to inspect timetables, room usage, teacher workload, and feasibility checks.



\## 9. Expected outputs



Representative anonymized outputs are included in:



```text

outputs/

```



These files include:



\- generated timetables,

\- room assignment summaries,

\- teacher assignment summaries.



\## 10. Data anonymization



Sensitive teacher identifiers were replaced with generic labels such as:



```text

P000001, P000002, P000003

```



The mapping between original identifiers and anonymous identifiers is not included in the repository.



\## 11. Notes



The original institutional database is not included. The repository provides anonymized model-ready instances and representative outputs to support reproducibility.

