# model_solver.py
# ==========================================================================================
# Solver de horarios ISC para ITCM
#
# Mejoras principales de esta versión:
#   1) Mantiene la ruta clásica (sin fijaciones) y la ruta de Fase 2 real.
#   2) Soporta Fase 2.5:
#      - fija profesor desde un calendario fuente
#      - fija tiempo para teoría desde el calendario fuente
#      - permite mover laboratorios en el tiempo para recuperar factibilidad de aulas
#      - penaliza mover laboratorios fuera de sus bloques originales
#   3) Mantiene preferencia institucional por aulas FF frente a E/EE/F.
#   4) Mantiene CPLEX forzado (CPLEX_PY o CPLEX_CMD) sin fallback automático a CBC.
# ==========================================================================================

from __future__ import annotations

import csv
import glob
import importlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import pulp as pl
from dotenv import find_dotenv, load_dotenv
from pulp import constants
from pulp.apis.core import PulpSolverError, subprocess as pulp_subprocess
from pulp.apis.cplex_api import CPLEX_CMD as PULP_CPLEX_CMD


# ------------------------------------------------------------------------------------------
# Utilidades base
# ------------------------------------------------------------------------------------------

def _friendly() -> bool:
    return os.getenv("FRIENDLY_OUTPUT", "1") in ("1", "true", "True", "yes", "YES")


def _title(t: str) -> None:
    print("\n" + t)
    print("-" * max(8, len(t)))


def _tip(msg: str) -> None:
    if _friendly():
        print(msg)


def _ok(msg: str) -> None:
    if _friendly():
        print(msg)


def _warn(msg: str) -> None:
    print(msg)


def _err(msg: str) -> None:
    print(f"❌ {msg}")


def _k_pm(p: str, m: str) -> str:
    return f"{p}|{m}"


def _k_mg(m: str, g: str) -> str:
    return f"{m}|{g}"


def _safe_int(x, default=None):
    try:
        return int(x) if x is not None else default
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x) if x is not None else float(default)
    except Exception:
        return float(default)


def _env_bool(key: str, default: str = "0") -> bool:
    return (os.getenv(key, default) or default) in ("1", "true", "True", "yes", "YES")


def _env_list(key: str) -> List[str]:
    raw = os.getenv(key, "") or ""
    if not raw.strip():
        return []
    toks = re.split(r"[,;\s]+", raw.strip())
    return [str(t).strip().upper() for t in toks if str(t).strip()]


def _get_env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    try:
        return int(v) if v is not None else default
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default


_DAY_MAP = {
    "LUNES": "L", "L": "L", "MON": "L",
    "MARTES": "M", "M": "M", "TUE": "M",
    "MIERCOLES": "X", "MIÉRCOLES": "X", "X": "X", "WED": "X",
    "JUEVES": "J", "J": "J", "THU": "J",
    "VIERNES": "V", "V": "V", "FRI": "V",
}


def _norm_code(s: Any) -> str:
    return str(s or "").strip().upper()


def _canon_day(s: Any) -> str:
    z = _norm_code(s)
    return _DAY_MAP.get(z, z[:1] if z else "")


def _canon_hour(s: Any) -> str:
    z = str(s or "").strip()
    m = re.match(r"^(\d{1,2})", z)
    return f"{int(m.group(1)):02d}" if m else z


def _lp_safe_token(x: Any) -> str:
    s = str(x or "").strip()
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "X"


def _lp_name(prefix: str, *parts: Any) -> str:
    tokens = [_lp_safe_token(prefix)]
    tokens.extend(_lp_safe_token(p) for p in parts)
    return "_".join(tokens)


# ------------------------------------------------------------------------------------------
# Carga de .env
# ------------------------------------------------------------------------------------------
_LOADED_ENV_PATH = None


def _load_env():
    global _LOADED_ENV_PATH
    candidates = []

    env_hint = os.getenv("ENV_FILE", "").strip()
    if env_hint:
        candidates.append(env_hint)

    proj = os.getenv("PROJECT_ROOT", "").strip()
    if proj:
        candidates.append(os.path.join(proj, ".env"))

    candidates.append(os.path.join(os.getcwd(), ".env"))

    auto = find_dotenv(usecwd=True)
    if auto:
        candidates.append(auto)

    chosen = None
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                load_dotenv(p, override=True)
                chosen = p
                break
            except Exception:
                continue

    if chosen is None:
        load_dotenv(override=True)
        _warn("No se encontró .env explícito; usando variables del proceso.")
    else:
        _warn(f".env cargado: {os.path.abspath(chosen)}")

    _LOADED_ENV_PATH = chosen


def _echo_effective_env():
    keys = [
        "MODEL_PERIODO", "DATOS_JSON", "EXPORT_PREFIX",
        "MODEL_INCLUDE_TIME", "ASSIGN_ROOMS", "SINGLE_ROOM_PER_COURSE",
        "FAST_MODE", "SOLVER_TIME_LIMIT", "MODEL_SOLVER",
        "PREFER_CPLEX_PY", "CPLEX_CMD_ONLY", "DISABLE_CBC_FALLBACK",
        "AT_HARD", "AL_HARD", "REQUIRE_CAPACITY_FOR_ROOM",
        "ALLOW_THEORY_IN_LABS", "STRICT_WHITELIST_ENFORCEMENT",
        "BYPASS_PREFLIGHT", "FAIL_IF_OVERLAPS",
        "MAX_PROF_PER_COURSE", "MAX_ROOMS_PER_GROUP",
        "ROOM_WHITELIST_JSON", "CPLEX_THREADS", "CPLEX_MIP_GAP",
        "CPLEX_MIP_GAP_FINAL", "CPLEX_EXTRA_CMDS",
        "PREFERRED_AT_LIST", "BACKUP_AT_LIST", "ROOM_PENALTY_BACKUP_AT",
        "WARM_START_FROM_SOURCE_CALENDAR", "WARM_START_SOURCE_CALENDAR",
        "FALLBACK_USE_SOURCE_CALENDAR",
        "FIX_TIME_PROF_FROM_CALENDAR", "FIX_SOURCE_CALENDAR", "FIX_TIME_PROF_STRICT",
        "RELAX_LAB_TIME", "FIX_THEORY_TIME_FROM_CALENDAR", "LAB_TIME_MOVE_PENALTY",
        "ENABLE_ROOM_BALANCE", "ROOM_BALANCE_WEIGHT", "ROOM_BALANCE_TOL_FACTOR", "ROOM_BALANCE_BY_TYPE",
    ]
    _title("Config .env efectiva (solo lectura)")
    if _LOADED_ENV_PATH:
        print(f"(Origen .env) {_LOADED_ENV_PATH}")
    shown = set()
    for k in keys:
        if k in shown:
            continue
        shown.add(k)
        val = os.getenv(k, "")
        if k in ("ROOM_WHITELIST_JSON",) and len(val) > 120:
            val = val[:120] + "...(trunc)"
        print(f"  {k}={val}")


# ------------------------------------------------------------------------------------------
# CPLEX
# ------------------------------------------------------------------------------------------

def _has_cplex_py() -> bool:
    try:
        return importlib.util.find_spec("cplex") is not None
    except Exception:
        return False


def _try_import_cplex_module():
    try:
        if not _has_cplex_py():
            return None
        return importlib.import_module("cplex")
    except Exception:
        return None


def _norm_path(p: Optional[str]) -> str:
    if not p:
        return ""
    return os.path.normpath(str(p).strip().strip('"').strip("'"))


def _find_cplex_cmd_from_env() -> Optional[str]:
    p = _norm_path(os.getenv("CPLEX_BIN", "") or "")
    if not p:
        return None
    if os.path.isfile(p):
        return p
    if os.path.isdir(p):
        cand = os.path.join(p, "cplex.exe")
        if os.path.isfile(cand):
            return cand
    return None


def _find_cplex_cmd_from_path() -> Optional[str]:
    hit = shutil.which("cplex")
    return _norm_path(hit) if hit else None


def _find_cplex_cmd_common_places() -> Optional[str]:
    bases = [
        r"C:\Program Files\IBM\ILOG",
        r"C:\Program Files (x86)\IBM\ILOG",
        r"D:\Program Files\IBM\ILOG",
        r"E:\Program Files\IBM\ILOG",
    ]
    patterns = [
        r"CPLEX_Studio*\cplex\bin\x64_win64\cplex.exe",
        r"CPLEX_Studio*\cplex\bin\x86_win32\cplex.exe",
        r"CPLEX_Studio*\cplex\bin\*\cplex.exe",
    ]
    for base in bases:
        if not os.path.exists(base):
            continue
        for pat in patterns:
            for path in glob.glob(os.path.join(base, pat)):
                if os.path.isfile(path):
                    return _norm_path(path)
    return None


class CPLEX_CMD_SAFE(PULP_CPLEX_CMD):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_cplex_text = ""
        self._last_status_label = ""

    def _parse_cplex_status_label(self, text: str) -> str:
        z = text or ""
        if re.search(r"MIP\s*-\s*Integer optimal solution", z, flags=re.I):
            return "Optimal"
        if re.search(r"Time limit exceeded,\s*integer feasible", z, flags=re.I):
            return "Feasible (TimeLimit)"
        if re.search(r"Time limit exceeded,\s*no integer solution", z, flags=re.I):
            return "No feasible solution (TimeLimit)"
        if re.search(r"MIP\s*-\s*Integer infeasible", z, flags=re.I):
            return "Infeasible"
        if re.search(r"No solution exists", z, flags=re.I):
            return "No solution"
        return ""

    def actualSolve(self, lp, **kwargs):
        if not self.executable(self.path):
            raise PulpSolverError("PuLP: cannot execute " + str(self.path))

        tmpLp, tmpSol, tmpMst = self.create_tmp_files(lp.name, "lp", "sol", "mst")
        vs = lp.writeLP(tmpLp, writeSOS=1)

        try:
            os.remove(tmpSol)
        except Exception:
            pass

        cmd = [self.path]
        cplex = pulp_subprocess.Popen(
            cmd,
            stdin=pulp_subprocess.PIPE,
            stdout=pulp_subprocess.PIPE,
            stderr=pulp_subprocess.PIPE,
        )

        cplex_cmds = "read " + tmpLp + "\n"
        if self.optionsDict.get("warmStart", False):
            self.writesol(filename=tmpMst, vs=vs)
            cplex_cmds += "read " + tmpMst + "\n"
            cplex_cmds += "set advance 1\n"

        if self.timeLimit is not None:
            cplex_cmds += "set timelimit " + str(self.timeLimit) + "\n"

        options = self.options + self.getOptions()
        for option in options:
            cplex_cmds += option + "\n"

        if lp.isMIP():
            if self.mip:
                cplex_cmds += "mipopt\n"
                cplex_cmds += "change problem fixed\n"
            else:
                cplex_cmds += "change problem lp\n"

        cplex_cmds += "optimize\n"
        cplex_cmds += "write " + tmpSol + "\n"
        cplex_cmds += "quit\n"

        out, err = cplex.communicate(cplex_cmds.encode("utf-8"))
        out_txt = (out or b"").decode("utf-8", errors="ignore")
        err_txt = (err or b"").decode("utf-8", errors="ignore")
        full_txt = (out_txt + ("\n" + err_txt if err_txt else "")).strip()
        self._last_cplex_text = full_txt
        self._last_status_label = self._parse_cplex_status_label(full_txt)

        if self.msg and full_txt:
            print(full_txt)

        if cplex.returncode != 0:
            raise PulpSolverError(
                "PuLP: Error while trying to execute "
                + str(self.path)
                + (f"\nSTDERR:\n{err_txt}" if err_txt else "")
            )

        if not os.path.exists(tmpSol):
            status = constants.LpStatusInfeasible if self._last_status_label == "Infeasible" else constants.LpStatusNotSolved
            values = reducedCosts = shadowPrices = slacks = solStatus = None
        else:
            status, values, reducedCosts, shadowPrices, slacks, solStatus = self.readsol(tmpSol)
            if self._last_status_label == "Feasible (TimeLimit)":
                status = constants.LpStatusNotSolved

        self.delete_tmp_files(tmpLp, tmpSol, tmpMst)

        if values is not None:
            lp.assignVarsVals(values)
        if reducedCosts is not None:
            lp.assignVarsDj(reducedCosts)
        if shadowPrices is not None:
            lp.assignConsPi(shadowPrices)
        if slacks is not None:
            lp.assignConsSlack(slacks)
        lp.assignStatus(status, solStatus)
        return status


def _cplex_options_from_env() -> List[str]:
    opts = []
    emph = (os.getenv("CPLEX_MIP_EMPHASIS", "feasibility") or "feasibility").lower()
    emph_code = {"feasibility": "1", "optimality": "2", "balance": "0"}.get(emph, "1")
    opts.append(f"set emphasis mip {emph_code}")

    gap = os.getenv("CPLEX_MIP_GAP", "")
    if gap:
        try:
            float(gap)
            opts.append(f"set mip tolerances mipgap {gap}")
        except Exception:
            pass

    th = os.getenv("CPLEX_THREADS", "")
    if str(th).isdigit():
        opts.append(f"set threads {th}")

    extra = (os.getenv("CPLEX_EXTRA_CMDS", "") or "").strip()
    if extra:
        for line in re.split(r"[;|]\s*", extra):
            if line:
                opts.append(line)
    return opts


def _cbc_from_env(time_limit_sec: int, msg: bool):
    threads = os.getenv("CBC_THREADS", "").strip()
    gap_rel = os.getenv("CBC_GAP_REL", "").strip()
    extra = (os.getenv("CBC_EXTRA_ARGS", "") or "").strip()
    kwargs = {"msg": msg, "timeLimit": time_limit_sec}
    if threads.isdigit():
        kwargs["threads"] = int(threads)
    try:
        if gap_rel:
            kwargs["gapRel"] = float(gap_rel)
    except Exception:
        pass
    if extra:
        kwargs["options"] = shlex.split(extra)
    return pl.PULP_CBC_CMD(**kwargs)


def _solver_label(solver_obj) -> str:
    try:
        return solver_obj.__class__.__name__
    except Exception:
        return str(type(solver_obj))


def _diagnose_cplex_installation():
    _title("Diagnóstico CPLEX")
    has_py = _has_cplex_py()
    if has_py:
        mod = _try_import_cplex_module()
        if mod is not None:
            _ok("CPLEX_PY: Disponible")
        else:
            _err("CPLEX_PY: Módulo detectado pero no se pudo importar correctamente")
    else:
        _warn("CPLEX_PY: No disponible")

    cplex_cmd = _find_cplex_cmd_from_env() or _find_cplex_cmd_from_path() or _find_cplex_cmd_common_places()
    if cplex_cmd:
        _ok(f"CPLEX_CMD: Disponible en '{cplex_cmd}'")
        try:
            proc = subprocess.run([cplex_cmd], input=b"quit\n", capture_output=True, timeout=5)
            if proc.returncode == 0:
                _ok("CPLEX_CMD: Ejecutable verificado correctamente")
            else:
                _warn(f"CPLEX_CMD: retorno no-cero en verificación ({proc.returncode})")
        except Exception as e:
            _err(f"CPLEX_CMD: Error al ejecutar - {e}")
    else:
        _err("CPLEX_CMD: No encontrado")
    return has_py, cplex_cmd


def _pick_solver(solver_name: Optional[str], time_limit_sec: int, msg: bool):
    name = (solver_name or os.getenv("MODEL_SOLVER", "cplex")).lower()
    prefer_py = _env_bool("PREFER_CPLEX_PY", "1")
    cmd_only = _env_bool("CPLEX_CMD_ONLY", "0")
    disable_cbc_fallback = _env_bool("DISABLE_CBC_FALLBACK", "1")
    warm_start = _env_bool("WARM_START_FROM_SOURCE_CALENDAR", "1") and not _env_bool("FIX_TIME_PROF_FROM_CALENDAR", "0")

    has_py, cplex_cmd = _diagnose_cplex_installation()
    cplex_opts = _cplex_options_from_env()
    _tip(f"[Solver-pick] name={name} prefer_py={prefer_py} cmd_only={cmd_only} warm_start={warm_start}")

    if name == "cplex":
        if cmd_only:
            if cplex_cmd:
                _ok("Usando CPLEX_CMD (forzado por CPLEX_CMD_ONLY=1)")
                return CPLEX_CMD_SAFE(path=cplex_cmd, msg=msg, timeLimit=time_limit_sec, options=cplex_opts, warmStart=warm_start)
            raise RuntimeError("CPLEX_CMD_ONLY=1 pero no se encontró cplex.exe")

        if prefer_py and has_py:
            _ok("Usando CPLEX_PY (preferido)")
            return pl.CPLEX_PY(msg=msg, timeLimit=time_limit_sec)

        if cplex_cmd:
            _ok("Usando CPLEX_CMD")
            return CPLEX_CMD_SAFE(path=cplex_cmd, msg=msg, timeLimit=time_limit_sec, options=cplex_opts, warmStart=warm_start)

        if has_py:
            _ok("Usando CPLEX_PY (última opción)")
            return pl.CPLEX_PY(msg=msg, timeLimit=time_limit_sec)

        raise RuntimeError("NO HAY CPLEX DISPONIBLE. Instala CPLEX_PY o configura CPLEX_CMD correctamente.")

    if name == "cbc" and disable_cbc_fallback:
        raise RuntimeError("CBC solicitado pero DISABLE_CBC_FALLBACK=1")
    if name == "cbc":
        return _cbc_from_env(time_limit_sec, msg)

    raise RuntimeError(f"Solver no reconocido: {name}")


def _solve_cplex_only(prob, time_limit_sec: int, fast_mode: bool):
    msg = not fast_mode
    used_solver_label = ""
    solver_meta = {"status_label": "", "raw_text": ""}

    solver = _pick_solver(os.getenv("MODEL_SOLVER"), time_limit_sec, msg=msg)
    used_solver_label = f"CPLEX solver: {_solver_label(solver)}"

    if isinstance(solver, pl.CPLEX_PY):
        _title("Ejecutando CPLEX_PY")
        status = prob.solve(solver)
        solver_meta["status_label"] = pl.LpStatus.get(status, str(status))
        return status, used_solver_label, solver_meta

    _title("Ejecutando CPLEX_CMD")
    status = prob.solve(solver)
    solver_meta["status_label"] = getattr(solver, "_last_status_label", "") or pl.LpStatus.get(status, str(status))
    solver_meta["raw_text"] = getattr(solver, "_last_cplex_text", "") or ""
    return status, used_solver_label, solver_meta


# ------------------------------------------------------------------------------------------
# Reglas de clasificación
# ------------------------------------------------------------------------------------------

def _compile_regex_env(key: str, default_pat: str) -> re.Pattern:
    raw = os.getenv(key, default_pat) or default_pat
    try:
        return re.compile(raw)
    except re.error:
        _warn(f"Patrón inválido en {key!r}. Se usa default.")
        return re.compile(default_pat)


LAB_RE = _compile_regex_env("LAB_COURSE_REGEX", r"(?i)\b(LAB|LABORATORIO|PR(A|Á)CTIC(A|AS))\b")
SEM1_RE = _compile_regex_env("SEM1_GROUP_REGEX", r"(?i)^1([A-Z]|$)|.*(^|[^0-9])1([^0-9]|$)")
SEM1_MAT_RE = _compile_regex_env("SEM1_MATERIA_REGEX", r"(?i)^0?1\d{2,3}$")
ALLOW_THEORY_IN_LABS = _env_bool("ALLOW_THEORY_IN_LABS", "0")


def _course_is_lab(m: str, M_is_lab: Dict[str, bool], M_text: Dict[str, str]) -> bool:
    m2 = _norm_code(m)
    if bool(M_is_lab.get(m2, False)):
        return True
    txt = str(M_text.get(m2, "") or "")
    return bool(txt and LAB_RE.search(txt))


def _preferred_backup_sets(valid_A: List[str]) -> Tuple[Set[str], Set[str], float]:
    preferred = set(_env_list("PREFERRED_AT_LIST"))
    backup = set(_env_list("BACKUP_AT_LIST"))
    valid = set(map(_norm_code, valid_A))
    preferred &= valid
    backup &= valid
    backup -= preferred
    penalty = _safe_float(os.getenv("ROOM_PENALTY_BACKUP_AT", "0"), 0.0)
    return preferred, backup, penalty


def _room_penalty_for_course_room(m: str, a: str, M_is_lab: Dict[str, bool], M_text: Dict[str, str], preferred_at: Set[str], backup_at: Set[str], penalty_backup: float) -> float:
    if penalty_backup <= 0:
        return 0.0
    if _course_is_lab(m, M_is_lab, M_text):
        return 0.0
    return float(penalty_backup) if _norm_code(a) in backup_at else 0.0


def _room_balance_config() -> Tuple[bool, float, float, bool]:
    enable = _env_bool("ENABLE_ROOM_BALANCE", "0")
    weight = _safe_float(os.getenv("ROOM_BALANCE_WEIGHT", "0"), 0.0)
    tol_factor = _safe_float(os.getenv("ROOM_BALANCE_TOL_FACTOR", "1.10"), 1.10)
    by_type = _env_bool("ROOM_BALANCE_BY_TYPE", "1")
    if tol_factor < 1.0:
        tol_factor = 1.0
    if weight <= 0:
        enable = False
    return enable, weight, tol_factor, by_type


# ------------------------------------------------------------------------------------------
# Carga de dataset
# ------------------------------------------------------------------------------------------

def load_dataset(json_path: str) -> Dict[str, Any]:
    with open(json_path, "r", encoding="utf-8") as f:
        d = json.load(f)

    d["P"] = [_norm_code(x) for x in d.get("P", [])]
    d["AT"] = [_norm_code(x) for x in d.get("AT", [])]
    d["AL"] = [_norm_code(x) for x in d.get("AL", [])]
    d["A"] = [_norm_code(x) for x in (d.get("A") or sorted(set(d["AT"]) | set(d["AL"]))) ]

    AT_HARD = set(_env_list("AT_HARD"))
    AL_HARD = set(_env_list("AL_HARD"))
    require_cap = _env_bool("REQUIRE_CAPACITY_FOR_ROOM", "1")

    if AT_HARD:
        d["AT"] = [a for a in d["AT"] if a in AT_HARD]
    if AL_HARD:
        d["AL"] = [a for a in d["AL"] if a in AL_HARD]
    if AT_HARD or AL_HARD:
        allowed = (AT_HARD if AT_HARD else set(d["AT"])) | (AL_HARD if AL_HARD else set(d["AL"]))
        d["A"] = [a for a in d["A"] if a in allowed]

    d["cap_A"] = {_norm_code(k): int(v) for k, v in (d.get("cap_A", {}) or {}).items()}
    if require_cap:
        A_with_cap = [a for a in d["A"] if a in d["cap_A"]]
        missing = sorted(set(d["A"]) - set(A_with_cap))
        if missing:
            _warn(f"Aulas removidas por falta de capacidad: {missing}")
        d["A"] = A_with_cap
        d["AT"] = [a for a in d["AT"] if a in d["A"]]
        d["AL"] = [a for a in d["AL"] if a in d["A"]]

    raw_A_tipo = d.get("A_tipo", {}) or {}
    A_tipo = {}
    for a in d["A"]:
        t = _norm_code(raw_A_tipo.get(a))
        A_tipo[a] = t if t in ("T", "L") else ("L" if a in d["AL"] else "T")
    d["A_tipo"] = A_tipo

    D_raw = d.get("D") or ["L", "M", "X", "J", "V"]
    H_raw = d.get("H") or [1, 2, 3, 4, 5, 6]
    d["D"] = [_canon_day(x) for x in D_raw]
    d["H"] = [_canon_hour(x) for x in H_raw]

    d["M"] = [_norm_code(x) for x in d.get("M", [])]
    d["G"] = [_norm_code(x) for x in d.get("G", [])]

    MG: List[Tuple[str, str]] = []
    for x in (d.get("MG") or []):
        if isinstance(x, (list, tuple)) and len(x) == 2:
            m, g = _norm_code(x[0]), _norm_code(x[1])
        else:
            m, g = map(_norm_code, str(x).split("|", 1))
        if (m, g) not in MG:
            MG.append((m, g))
    d["MG"] = MG

    d["size_G"] = {_norm_code(k): int(v) for k, v in (d.get("size_G", {}) or {}).items()}

    a_pm = {}
    for k, v in (d.get("a_pm", {}) or {}).items():
        if "|" in str(k):
            p, m = str(k).split("|", 1)
            a_pm[f"{_norm_code(p)}|{_norm_code(m)}"] = float(v)
    d["a_pm"] = a_pm

    Hreq = {}
    for k, v in (d.get("Hreq", {}) or {}).items():
        if "|" in str(k):
            m, g = str(k).split("|", 1)
            Hreq[f"{_norm_code(m)}|{_norm_code(g)}"] = int(v)
    d["Hreq"] = Hreq

    d["MinH"] = {_norm_code(k): int(v) for k, v in (d.get("MinH", {}) or {}).items()}
    d["MaxH"] = {_norm_code(k): int(v) for k, v in (d.get("MaxH", {}) or {}).items()}

    Uraw = {}
    for p, lst in (d.get("U", {}) or {}).items():
        p2 = _norm_code(p)
        S = set()
        for it in (lst or []):
            try:
                h, dday = it
                S.add((_canon_hour(h), _canon_day(dday)))
            except Exception:
                continue
        Uraw[p2] = sorted(S)
    d["U"] = Uraw

    d["WhitelistRooms"] = {_norm_code(k): [_norm_code(a) for a in (v or [])] for k, v in (d.get("WhitelistRooms", {}) or {}).items()}
    d["WhitelistRules"] = []
    for r in (d.get("WhitelistRules", []) or []):
        try:
            mg_regex = str(r.get("mg_regex", ""))
            allow = [_norm_code(a) for a in (r.get("allow") or [])]
            typ_raw = r.get("type")
            typ = None if typ_raw is None or str(typ_raw).strip() == "" else str(typ_raw).upper()
            if typ not in (None, "T", "L"):
                typ = None
            if mg_regex and allow:
                d["WhitelistRules"].append({"mg_regex": mg_regex, "allow": allow, "type": typ})
        except Exception:
            continue

    env_rules = os.getenv("ROOM_WHITELIST_JSON", "").strip()
    if env_rules:
        try:
            arr = json.loads(env_rules)
            for r in (arr or []):
                mg_regex = str(r.get("mg_regex", ""))
                allow = [_norm_code(a) for a in (r.get("allow") or [])]
                typ_raw = r.get("type")
                typ = None if typ_raw is None or str(typ_raw).strip() == "" else str(typ_raw).upper()
                if typ not in (None, "T", "L"):
                    typ = None
                if mg_regex and allow:
                    d["WhitelistRules"].append({"mg_regex": mg_regex, "allow": allow, "type": typ})
        except Exception:
            _warn("ROOM_WHITELIST_JSON inválido; se ignora.")

    d["M_is_lab"] = {_norm_code(k): bool(v) for k, v in (d.get("M_is_lab", {}) or {}).items()}
    d["M_text"] = {_norm_code(k): str(v or "") for k, v in (d.get("M_text", {}) or {}).items()}
    d["M_tipo"] = {_norm_code(k): str(v or "") for k, v in (d.get("M_tipo", {}) or {}).items()}
    d["M_area"] = {_norm_code(k): str(v or "") for k, v in (d.get("M_area", {}) or {}).items()}
    d["AL_sem1_extras"] = [_norm_code(x) for x in (d.get("AL_sem1_extras") or [])]
    return d


# ------------------------------------------------------------------------------------------
# Whitelist / fase 2
# ------------------------------------------------------------------------------------------

def _find_whitelist_for_mg(m: str, g: str, whitelist_rooms: Dict[str, List[str]], whitelist_rules: List[Dict[str, Any]], course_type: str, allowed_A: List[str]) -> Optional[List[str]]:
    key = _k_mg(m, g)
    Aset = set(map(_norm_code, allowed_A))

    if key in whitelist_rooms and whitelist_rooms[key]:
        allow = [_norm_code(a) for a in whitelist_rooms[key] if _norm_code(a) in Aset]
        return allow if allow else None

    for rule in (whitelist_rules or []):
        mg_regex = rule.get("mg_regex")
        allow = [_norm_code(a) for a in (rule.get("allow") or [])]
        rtype = rule.get("type")
        if not mg_regex or not allow:
            continue
        try:
            if re.search(mg_regex, key) and ((rtype is None) or (rtype == course_type)):
                allow = [a for a in allow if a in Aset]
                return allow if allow else None
        except re.error:
            continue
    return None


def _expand_extras_tokens(tokens: List[str], A: List[str]) -> List[str]:
    if not tokens:
        return []
    Aset = set(map(_norm_code, A))
    out: List[str] = []
    for tok in tokens:
        t = _norm_code(tok)
        if not t:
            continue
        if t in Aset:
            if t not in out:
                out.append(t)
            continue
        pre = t[:-1] if t.endswith("*") else t
        for a in A:
            a2 = _norm_code(a)
            if a2.startswith(pre) and (a2 in Aset) and (a2 not in out):
                out.append(a2)
    return out


def _rank_preferred_at_for_group(g: str, preferred_at: List[str], cap_A: Dict[str, int], size_G: Dict[str, int]) -> List[str]:
    need = int(_safe_int(size_G.get(_norm_code(g), 0), 0))
    ranked = []
    for a in preferred_at:
        a2 = _norm_code(a)
        cap = int(_safe_int(cap_A.get(a2, 0), 0))
        diff = abs(cap - need) if need > 0 and cap > 0 else 10**9
        ranked.append((diff, -cap, a2))
    ranked.sort()
    return [a for _, _, a in ranked]


def _phase2_calendar_to_whitelist(base_json_path: str) -> str:
    if not _env_bool("PHASE2_FROM_BLOCK_SOLUTION", "0"):
        return base_json_path

    cal_path = _norm_path(os.getenv("PHASE2_SOURCE_CALENDAR", "") or "")
    if not cal_path:
        prefix = os.getenv("PHASE2_SOURCE_PREFIX", "").strip()
        if prefix:
            cal_path = _norm_path(prefix + "_calendario.csv")

    if not cal_path or not os.path.isfile(cal_path):
        _warn(f"PHASE2: no se encontró calendario fuente: {cal_path or '(vacío)'}")
        return base_json_path

    with open(base_json_path, "r", encoding="utf-8") as f:
        js = json.load(f)

    topk = _get_env_int("PHASE2_TOPK_ROOMS", 3)
    min_uses = _get_env_int("PHASE2_MIN_ROOM_USES", 1)
    strict_merge = _env_bool("PHASE2_STRICT_MERGE", "0")
    add_pref = _env_bool("PHASE2_ADD_PREFERRED_AT", "1")
    pref_topk = _get_env_int("PHASE2_PREFERRED_AT_TOPK", 0)
    keep_source = _env_bool("PHASE2_KEEP_SOURCE_ROOMS", "1")

    A = [_norm_code(x) for x in (js.get("A") or list(set((js.get("AT") or []) + (js.get("AL") or []))))]
    AT = [_norm_code(x) for x in (js.get("AT") or [])]
    AL = [_norm_code(x) for x in (js.get("AL") or [])]
    cap_A = {_norm_code(k): int(v) for k, v in (js.get("cap_A", {}) or {}).items()}
    size_G = {_norm_code(k): int(v) for k, v in (js.get("size_G", {}) or {}).items()}
    M_is_lab = {_norm_code(k): bool(v) for k, v in (js.get("M_is_lab", {}) or {}).items()}
    M_text = {_norm_code(k): str(v or "") for k, v in (js.get("M_text", {}) or {}).items()}

    existing = {_norm_code(k): [_norm_code(a) for a in (v or [])] for k, v in (js.get("WhitelistRooms", {}) or {}).items()}
    room_counts = defaultdict(Counter)
    with open(cal_path, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        for row in rd:
            m = _norm_code(row.get("materia"))
            g = _norm_code(row.get("grupo"))
            a = _norm_code(row.get("aula"))
            if m and g and a:
                room_counts[_k_mg(m, g)][a] += 1

    preferred_at = [a for a in _env_list("PREFERRED_AT_LIST") if a in set(AT)]
    result = {}
    courses_with_list = 0
    lens = []
    ff_added_count = 0
    ff_added_examples = []

    MG = []
    for x in (js.get("MG") or []):
        if isinstance(x, (list, tuple)) and len(x) == 2:
            MG.append((_norm_code(x[0]), _norm_code(x[1])))
        else:
            m, g = map(_norm_code, str(x).split("|", 1))
            MG.append((m, g))

    for (m, g) in MG:
        key = _k_mg(m, g)
        counts = room_counts.get(key, Counter())
        src_rooms = [a for a, c in counts.most_common() if c >= min_uses and a in set(A)]
        if topk > 0:
            src_rooms = src_rooms[:topk]

        built = []
        if keep_source:
            built.extend(src_rooms)

        added_pref = []
        if add_pref and (not _course_is_lab(m, M_is_lab, M_text)) and pref_topk > 0:
            ranked_pref = _rank_preferred_at_for_group(g, preferred_at, cap_A, size_G)
            for a in ranked_pref:
                if a not in built and a not in added_pref:
                    added_pref.append(a)
                if len(added_pref) >= pref_topk:
                    break
            built.extend(added_pref)

        built = [a for a in built if a in set(A)]
        seen = set()
        built = [a for a in built if not (a in seen or seen.add(a))]

        prev = [a for a in existing.get(key, []) if a in set(A)]
        if strict_merge:
            final = built
        else:
            seen = set()
            final = [a for a in (prev + built) if not (a in seen or seen.add(a))]

        if final:
            result[key] = final
            courses_with_list += 1
            lens.append(len(final))
        if added_pref:
            ff_added_count += 1
            if len(ff_added_examples) < 10:
                ff_added_examples.append((key, final if final else added_pref))

    js["WhitelistRooms"] = result
    base_dir = os.path.dirname(base_json_path) or "."
    base_name = os.path.basename(base_json_path)
    tmp_json = os.path.join(base_dir, f"_tmp_{base_name}")
    with open(tmp_json, "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=2)

    _title("PREPROCESS FASE2 - Whitelist reducida desde opcion 2")
    print(f"Calendario fuente : {cal_path}")
    print(f"Cursos con lista  : {courses_with_list}")
    avg = (sum(lens) / len(lens)) if lens else 0.0
    print(f"Tam promedio      : {avg:.2f}")
    print(f"Tam min/max       : {min(lens) if lens else 0} / {max(lens) if lens else 0}")
    print(f"Cursos teoricos con FF agregada: {ff_added_count}")
    for key, rooms in ff_added_examples:
        print(f"  {key} -> {rooms}")
    _warn(f"PREPROCESS: JSON temporal escrito en: {tmp_json}")
    return tmp_json


# ------------------------------------------------------------------------------------------
# Warm start y fijaciones
# ------------------------------------------------------------------------------------------

def _csv_row_pick(row: Dict[str, Any], *names: str) -> str:
    if not row:
        return ""
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        key = str(name).strip().lower()
        if key in lowered and lowered[key] is not None:
            return str(lowered[key]).strip()
    return ""


def _fallback_enabled() -> bool:
    return _env_bool("FALLBACK_USE_SOURCE_CALENDAR", "1")


def _resolve_warm_start_calendar_path(export_prefix: Optional[str] = None) -> str:
    cal_path = _norm_path(os.getenv("WARM_START_SOURCE_CALENDAR", "") or "")
    if not cal_path:
        cal_path = _norm_path(os.getenv("PHASE2_SOURCE_CALENDAR", "") or "")
    if not cal_path:
        prefix = (os.getenv("PHASE2_SOURCE_PREFIX", "") or "").strip()
        if prefix:
            cal_path = _norm_path(prefix + "_calendario.csv")
    if (not cal_path) and export_prefix:
        guess = _norm_path(str(export_prefix) + "_calendario.csv")
        if os.path.isfile(guess):
            cal_path = guess
    return cal_path


def _apply_calendar_warm_start(export_prefix: str, MG, P_by_mg, A_by_mg, slots, y_pmg, w_p, x_amg, z_mghd, y_pmghd, x_amghd, assign_rooms: bool, single_room: bool) -> int:
    if not _env_bool("WARM_START_FROM_SOURCE_CALENDAR", "1"):
        return 0
    if _env_bool("FIX_TIME_PROF_FROM_CALENDAR", "0"):
        _title("Warm start omitido")
        print("Tiempo/profesor se fijarán por restricción desde FIX_SOURCE_CALENDAR.")
        return 0

    cal_path = _resolve_warm_start_calendar_path(export_prefix)
    if not cal_path or not os.path.isfile(cal_path):
        _warn(f"Warm start: no se encontró calendario fuente: {cal_path or '(vacío)'}")
        return 0

    use_room_warm_start = assign_rooms and _env_bool("WARM_START_INCLUDE_ROOMS", "0")
    mg_set = set(MG)
    slot_set = set(slots)
    slot_info_by_mg = defaultdict(dict)
    prof_counter = defaultdict(Counter)
    room_counter = defaultdict(Counter)

    counts = {k: 0 for k in [
        "rows", "matched", "z", "yhd", "xhd", "ypmg", "xmg", "w",
        "skip_mg", "skip_slot", "skip_prof", "skip_room", "prof_default",
        "room_default", "room_mixed_course"
    ]}

    try:
        with open(cal_path, "r", encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            for row in rd:
                counts["rows"] += 1
                m = _norm_code(_csv_row_pick(row, "materia", "course", "m"))
                g = _norm_code(_csv_row_pick(row, "grupo", "group", "g"))
                h = _canon_hour(_csv_row_pick(row, "hora", "hour", "bloque", "timeslot", "h"))
                d = _canon_day(_csv_row_pick(row, "dia", "day", "d"))
                a = _norm_code(_csv_row_pick(row, "aula", "room", "salon", "classroom"))
                p = _norm_code(_csv_row_pick(row, "profesor", "teacher", "docente", "rfc"))
                mg = (m, g)
                hd = (h, d)
                if mg not in mg_set:
                    counts["skip_mg"] += 1
                    continue
                if hd not in slot_set:
                    counts["skip_slot"] += 1
                    continue
                counts["matched"] += 1
                valid_ps = set(P_by_mg.get(mg, []))
                p_ok = bool(p and p in valid_ps)
                if p and not p_ok:
                    counts["skip_prof"] += 1
                if p_ok:
                    prof_counter[mg][p] += 1
                info = slot_info_by_mg[mg].setdefault(hd, {"p": "", "a": ""})
                if p_ok and not info["p"]:
                    info["p"] = p
                if use_room_warm_start:
                    valid_as = set(A_by_mg.get(mg, []))
                    a_ok = bool(a and a in valid_as)
                    if a and not a_ok:
                        counts["skip_room"] += 1
                    if a_ok:
                        room_counter[mg][a] += 1
                        if not info["a"]:
                            info["a"] = a
    except Exception as e:
        _warn(f"Warm start: no se pudo leer {cal_path}: {e}")
        return 0

    used_w = set()
    for (m, g) in MG:
        mg = (m, g)
        slot_map = slot_info_by_mg.get(mg, {})
        if not slot_map:
            continue
        valid_ps = list(P_by_mg.get(mg, []))
        p_fix = ""
        p_ctr = prof_counter.get(mg, Counter())
        if p_ctr:
            p_fix = p_ctr.most_common(1)[0][0]
        elif valid_ps:
            p_fix = valid_ps[0]
            counts["prof_default"] += 1
        if p_fix:
            var = y_pmg.get((p_fix, m, g))
            if var is not None:
                var.setInitialValue(1)
                counts["ypmg"] += 1
            if p_fix in w_p and p_fix not in used_w:
                w_p[p_fix].setInitialValue(1)
                used_w.add(p_fix)
                counts["w"] += 1

        a_fix = ""
        if use_room_warm_start and single_room:
            a_ctr = room_counter.get(mg, Counter())
            if a_ctr:
                total_valid = sum(a_ctr.values())
                top_room, top_uses = a_ctr.most_common(1)[0]
                if top_uses == total_valid:
                    a_fix = top_room
                else:
                    counts["room_mixed_course"] += 1

        if use_room_warm_start and single_room and a_fix:
            var = x_amg.get((a_fix, m, g))
            if var is not None:
                var.setInitialValue(1)
                counts["xmg"] += 1

        for (h, d), info in slot_map.items():
            zv = z_mghd.get((m, g, h, d))
            if zv is not None:
                zv.setInitialValue(1)
                counts["z"] += 1
            p_use = p_fix or info.get("p", "")
            if p_use:
                yv = y_pmghd.get((p_use, m, g, h, d))
                if yv is not None:
                    yv.setInitialValue(1)
                    counts["yhd"] += 1
            if use_room_warm_start and assign_rooms:
                if single_room:
                    if a_fix:
                        xv = x_amghd.get((a_fix, m, g, h, d))
                        if xv is not None:
                            xv.setInitialValue(1)
                            counts["xhd"] += 1
                else:
                    a_use = info.get("a", "")
                    if a_use:
                        xv = x_amghd.get((a_use, m, g, h, d))
                        if xv is not None:
                            xv.setInitialValue(1)
                            counts["xhd"] += 1

    total = counts["z"] + counts["yhd"] + counts["xhd"] + counts["ypmg"] + counts["xmg"] + counts["w"]
    if total > 0:
        _title("Warm start desde calendario fuente")
        print(f"Calendario fuente : {cal_path}")
        print(f"Filas CSV         : {counts['rows']}  | coincidencias modelo={counts['matched']}")
        print(f"Inicializaciones  : total={total}  z={counts['z']}  yhd={counts['yhd']}  xhd={counts['xhd']}  y={counts['ypmg']}  x={counts['xmg']}  w={counts['w']}")
        print(f"Descartes         : mg={counts['skip_mg']}  slot={counts['skip_slot']}  prof={counts['skip_prof']}  room={counts['skip_room']}")
        print(f"Defaults usados   : prof={counts['prof_default']}  room={counts['room_default']}")
        print(f"Cursos con aulas mixtas en fuente (sin fijar x) : {counts['room_mixed_course']}")
        print(f"WARM_START_INCLUDE_ROOMS = {1 if use_room_warm_start else 0}")
    else:
        _warn(f"Warm start: {cal_path} leído, pero sin coincidencias útiles con el modelo actual.")
    return total


def _resolve_fix_calendar_path(export_prefix: Optional[str] = None) -> str:
    cal_path = _norm_path(os.getenv("FIX_SOURCE_CALENDAR", "") or "")
    if not cal_path:
        cal_path = _resolve_warm_start_calendar_path(export_prefix)
    return cal_path


def _load_fixed_calendar_info(export_prefix: str, MG, slots, P_candidates_by_mg, Hreq, M_is_lab, M_text) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "active": False,
        "path": "",
        "prof_by_mg": {},
        "slots_by_mg": {},
        "rows": 0,
        "matched": 0,
        "skip_mg": 0,
        "skip_slot": 0,
        "skip_prof": 0,
        "prof_multi": 0,
        "prof_default": 0,
        "hreq_mismatch": 0,
        "theory_fixed_slots": {},
        "lab_original_slots": {},
    }

    if not _env_bool("FIX_TIME_PROF_FROM_CALENDAR", "0"):
        return info

    strict = _env_bool("FIX_TIME_PROF_STRICT", "1")
    cal_path = _resolve_fix_calendar_path(export_prefix)
    info["path"] = cal_path
    if not cal_path or not os.path.isfile(cal_path):
        msg = f"No se encontró FIX_SOURCE_CALENDAR: {cal_path or '(vacío)'}"
        if strict:
            raise RuntimeError(msg)
        _warn(f"Fase 2: {msg}. Se omite fijación.")
        return info

    mg_set = set(MG)
    slot_set = set(slots)
    slot_map = defaultdict(set)
    prof_counter = defaultdict(Counter)

    with open(cal_path, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        for row in rd:
            info["rows"] += 1
            m = _norm_code(_csv_row_pick(row, "materia", "course", "m"))
            g = _norm_code(_csv_row_pick(row, "grupo", "group", "g"))
            h = _canon_hour(_csv_row_pick(row, "hora", "hour", "bloque", "timeslot", "h"))
            d = _canon_day(_csv_row_pick(row, "dia", "day", "d"))
            p = _norm_code(_csv_row_pick(row, "profesor", "teacher", "docente", "rfc"))
            mg = (m, g)
            hd = (h, d)
            if mg not in mg_set:
                info["skip_mg"] += 1
                continue
            if hd not in slot_set:
                info["skip_slot"] += 1
                continue
            info["matched"] += 1
            slot_map[mg].add(hd)
            if p and p in set(P_candidates_by_mg.get(mg, [])):
                prof_counter[mg][p] += 1
            elif p:
                info["skip_prof"] += 1

    prof_by_mg = {}
    slots_by_mg = {}
    theory_fixed_slots = {}
    lab_original_slots = {}
    issues = []
    relax_lab_time = _env_bool("RELAX_LAB_TIME", "0")
    fix_theory_time = _env_bool("FIX_THEORY_TIME_FROM_CALENDAR", "1")

    for (m, g) in MG:
        mg = (m, g)
        req = int(Hreq.get(_k_mg(m, g), 0))
        slots_fixed = set(slot_map.get(mg, set()))
        got = len(slots_fixed)
        if got != req:
            info["hreq_mismatch"] += 1
            issues.append(f"Hreq mismatch en {m}|{g}: esperado={req}, encontrado={got}")

        ctr = prof_counter.get(mg, Counter())
        valid_ps = list(P_candidates_by_mg.get(mg, []))
        p_fix = ""
        if ctr:
            if len(ctr) > 1:
                info["prof_multi"] += 1
            p_fix = ctr.most_common(1)[0][0]
        elif valid_ps and not strict:
            p_fix = valid_ps[0]
            info["prof_default"] += 1
        else:
            issues.append(f"Sin profesor válido para {m}|{g} en calendario fuente")

        if not slots_fixed and req > 0:
            issues.append(f"Sin slots válidos para {m}|{g} en calendario fuente")

        if p_fix:
            prof_by_mg[mg] = p_fix
        slots_by_mg[mg] = slots_fixed

        is_lab = _course_is_lab(m, M_is_lab, M_text)
        if is_lab and relax_lab_time:
            lab_original_slots[mg] = slots_fixed
        else:
            if fix_theory_time:
                theory_fixed_slots[mg] = slots_fixed

    if issues and strict:
        sample = " ; ".join(issues[:8])
        raise RuntimeError("FIX_TIME_PROF_FROM_CALENDAR inválido: " + sample)

    info["active"] = True
    info["prof_by_mg"] = prof_by_mg
    info["slots_by_mg"] = slots_by_mg
    info["theory_fixed_slots"] = theory_fixed_slots
    info["lab_original_slots"] = lab_original_slots

    title_txt = "Fase 2.5: fijando profesor y relajando tiempo de labs" if relax_lab_time else "Fase 2 real: fijando tiempo y profesor desde calendario"
    _title(title_txt)
    print(f"Calendario fuente : {cal_path}")
    print(f"Filas CSV         : {info['rows']}  | coincidencias modelo={info['matched']}")
    print(f"Descartes         : mg={info['skip_mg']}  slot={info['skip_slot']}  prof={info['skip_prof']}")
    print(f"Incidencias       : prof_multi={info['prof_multi']}  prof_default={info['prof_default']}  hreq_mismatch={info['hreq_mismatch']}")
    print(f"FIX_TIME_PROF_STRICT = {1 if strict else 0}")
    print(f"RELAX_LAB_TIME       = {1 if relax_lab_time else 0}")
    print(f"FIX_THEORY_TIME_FROM_CALENDAR = {1 if fix_theory_time else 0}")
    if issues and not strict:
        _warn("Fase 2: incidencias no estrictas detectadas. Se continúa con defaults controlados.")
        for msg in issues[:10]:
            print(f"  - {msg}")
    return info


# ------------------------------------------------------------------------------------------
# Diagnóstico / export de fallback
# ------------------------------------------------------------------------------------------

def _export_source_calendar_fallback(export_prefix: str, MG, P_by_mg, A_by_mg, slots, assign_rooms: bool, single_room: bool) -> bool:
    if not _fallback_enabled():
        return False
    cal_path = _resolve_warm_start_calendar_path(export_prefix)
    if not cal_path or not os.path.isfile(cal_path):
        return False

    mg_set = set(MG)
    slot_set = set(slots)
    rows_cal = []
    prof_counter = defaultdict(Counter)
    room_counter = defaultdict(Counter)

    try:
        with open(cal_path, "r", encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            for row in rd:
                m = _norm_code(_csv_row_pick(row, "materia", "course", "m"))
                g = _norm_code(_csv_row_pick(row, "grupo", "group", "g"))
                h = _canon_hour(_csv_row_pick(row, "hora", "hour", "bloque", "timeslot", "h"))
                d = _canon_day(_csv_row_pick(row, "dia", "day", "d"))
                a = _norm_code(_csv_row_pick(row, "aula", "room", "salon", "classroom"))
                p = _norm_code(_csv_row_pick(row, "profesor", "teacher", "docente", "rfc"))
                if (m, g) not in mg_set or (h, d) not in slot_set:
                    continue
                if p and p in set(P_by_mg.get((m, g), [])):
                    prof_counter[(m, g)][p] += 1
                else:
                    p = ""
                if assign_rooms and a and a in set(A_by_mg.get((m, g), [])):
                    room_counter[(m, g)][a] += 1
                elif assign_rooms:
                    a = ""
                rows_cal.append([m, g, h, d, a, p])
    except Exception as e:
        _warn(f"Fallback: no se pudo leer calendario fuente {cal_path}: {e}")
        return False

    if not rows_cal:
        return False

    dirn = os.path.dirname(export_prefix)
    if dirn:
        os.makedirs(dirn, exist_ok=True)

    with open(f"{export_prefix}_calendario.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["materia", "grupo", "hora", "dia", "aula", "profesor"], *rows_cal])

    rows_prof = []
    for (m, g) in MG:
        p = prof_counter[(m, g)].most_common(1)[0][0] if prof_counter.get((m, g)) else ""
        rows_prof.append([m, g, p])
    with open(f"{export_prefix}_profesores.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["materia", "grupo", "profesor"], *rows_prof])

    if assign_rooms and single_room:
        rows_room = []
        for (m, g) in MG:
            a = room_counter[(m, g)].most_common(1)[0][0] if room_counter.get((m, g)) else ""
            rows_room.append([m, g, a])
        with open(f"{export_prefix}_aulas.csv", "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["materia", "grupo", "aula"], *rows_room])

    note_path = f"{export_prefix}_fallback_source_calendar.txt"
    with open(note_path, "w", encoding="utf-8") as f:
        f.write("Se exportó el calendario fuente como fallback porque el solver no produjo incumbente exportable.\n")
        f.write(f"Fuente: {cal_path}\n")
    _warn(f"Fallback: exportado calendario fuente en prefijo {export_prefix}")
    return True


def _create_feasibility_report(analysis: Dict[str, Any], export_prefix: str):
    dirn = os.path.dirname(export_prefix)
    if dirn:
        os.makedirs(dirn, exist_ok=True)
    report_path = f"{export_prefix}_feasibility_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("REPORTE DE FACTIBILIDAD\n=======================\n\n")
        for k, v in analysis.items():
            f.write(f"[{k}]\n")
            if isinstance(v, dict):
                for kk, vv in v.items():
                    f.write(f"  {kk}: {vv}\n")
            elif isinstance(v, list):
                for x in v[:200]:
                    f.write(f"  {x}\n")
            else:
                f.write(f"  {v}\n")
            f.write("\n")
    _warn(f"Escrito: {report_path}")


def _create_time_limit_report(export_prefix: str, status_label: str, used_solver_label: str, solver_meta: Optional[Dict[str, Any]] = None):
    dirn = os.path.dirname(export_prefix)
    if dirn:
        os.makedirs(dirn, exist_ok=True)
    report_path = f"{export_prefix}_solver_status.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("ESTADO DEL SOLVER\n=================\n\n")
        f.write(f"Estado mostrado: {status_label}\n")
        f.write(f"Solver: {used_solver_label}\n")
        if solver_meta:
            raw_status = str(solver_meta.get("status_label", "") or "")
            if raw_status:
                f.write(f"Estado CPLEX interpretado: {raw_status}\n")
            raw_text = str(solver_meta.get("raw_text", "") or "")
            if raw_text:
                excerpt = raw_text[-4000:]
                f.write("\n--- Extracto CPLEX ---\n")
                f.write(excerpt + ("\n" if not excerpt.endswith("\n") else ""))
        f.write("\nObservación: el solver terminó sin solución entera exportable.\n")
    _warn(f"Escrito: {report_path}")


def _write_solver_used(export_prefix: str, label: str):
    try:
        dirn = os.path.dirname(export_prefix)
        if dirn:
            os.makedirs(dirn, exist_ok=True)
        with open(f"{export_prefix}_solver_used.txt", "w", encoding="utf-8") as f:
            f.write(label.strip() + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------------------------------
# Preflight
# ------------------------------------------------------------------------------------------

def _echo_solver_preflight(json_path: str, export_prefix: str):
    _title("Preflight del solver (.env y detecciones)")
    print(f"MODEL_SOLVER        = {os.getenv('MODEL_SOLVER', 'cplex')}")
    print(f"TIME_LIMIT_SEC      = {_get_env_int('SOLVER_TIME_LIMIT', 120)}")
    print(f"MODEL_INCLUDE_TIME  = {1 if _env_bool('MODEL_INCLUDE_TIME','1') else 0}")
    cplex_bin_raw = os.getenv('CPLEX_BIN', '')
    cplex_bin_norm = _norm_path(cplex_bin_raw)
    print(f"CPLEX_BIN (raw)     = {cplex_bin_raw!r}")
    print(f"CPLEX_BIN (normaliz)= {cplex_bin_norm!r}")
    print(f"CPLEX_BIN isfile?   = {os.path.isfile(cplex_bin_norm) if cplex_bin_norm else False}")
    print(f"CPLEX_BIN isdir?    = {os.path.isdir(cplex_bin_norm) if cplex_bin_norm else False}")
    print(f"Modulo 'cplex' (API Python) instalado? {_has_cplex_py()}")
    print("Se intentara CPLEX_CMD (ejecutable).")

    print("\nConfiguracion de tolerancia:")
    print(f"STRICT_ROOM_SET           = {1 if _env_bool('STRICT_ROOM_SET', os.getenv('STRICT_ROOMSETS', '0')) else 0}")
    print(f"REQUIRE_CAPACITY_FOR_ROOM = {1 if _env_bool('REQUIRE_CAPACITY_FOR_ROOM', '1') else 0}")
    print(f"BYPASS_PREFLIGHT          = {1 if _env_bool('BYPASS_PREFLIGHT', '0') else 0}")
    print(f"ALLOW_THEORY_IN_LABS      = {1 if _env_bool('ALLOW_THEORY_IN_LABS', '0') else 0}")
    print(f"MAX_ROOMS_PER_GROUP       = {_get_env_int('MAX_ROOMS_PER_GROUP', 0)}")
    print(f"MAX_PROF_PER_COURSE       = {_get_env_int('MAX_PROF_PER_COURSE', 0)}")

    print("\nEstrategias de relajacion:")
    for k in ['FAST_MODE','RELAX_CONSTRAINTS','AUTO_RELAX','AUTO_ULTRA_RELAX','ASSIGN_ROOMS','SINGLE_ROOM_PER_COURSE']:
        print(f"{k:<25} = {1 if _env_bool(k, '0' if k in ['FAST_MODE','RELAX_CONSTRAINTS','AUTO_RELAX','AUTO_ULTRA_RELAX'] else '1') else 0}")

    print("\nValidacion de tipos/regex:")
    print(f"LAB_COURSE_REGEX    = {os.getenv('LAB_COURSE_REGEX') or LAB_RE.pattern}")
    print(f"SEM1_GROUP_REGEX    = {os.getenv('SEM1_GROUP_REGEX') or SEM1_RE.pattern}")

    print("\nFase 2 (whitelist / fijaciones):")
    for k in [
        'PHASE2_FROM_BLOCK_SOLUTION','PHASE2_TOPK_ROOMS','PHASE2_MIN_ROOM_USES',
        'PHASE2_SOURCE_PREFIX','PHASE2_SOURCE_CALENDAR','PHASE2_STRICT_MERGE',
        'PHASE2_ADD_PREFERRED_AT','PHASE2_PREFERRED_AT_TOPK','PHASE2_KEEP_SOURCE_ROOMS',
        'PREFERRED_AT_LIST','BACKUP_AT_LIST','ROOM_PENALTY_BACKUP_AT',
        'WARM_START_FROM_SOURCE_CALENDAR','WARM_START_SOURCE_CALENDAR',
        'FALLBACK_USE_SOURCE_CALENDAR','FIX_TIME_PROF_FROM_CALENDAR','FIX_SOURCE_CALENDAR',
        'FIX_TIME_PROF_STRICT','RELAX_LAB_TIME','FIX_THEORY_TIME_FROM_CALENDAR','LAB_TIME_MOVE_PENALTY'
    ]:
        print(f"{k:<25} = {os.getenv(k, '')}")

    print("\n=========================")
    period = _infer_period_from_path_or_json(json_path) or os.getenv('MODEL_PERIODO', '') or 'UNK'
    print(f"EJECUTANDO SOLVER - {period}")
    print("=========================")
    print(f"JSON (solver): {json_path}")
    print(f"Export prefix: {export_prefix}")
    print("Modo: solve_one con parametros")


def _build_candidates(P, A, D, H, MG, sizeG, capA, a_pm, Hreq, Uraw, A_tipo, AL_sem1_extras, whitelist_rooms, whitelist_rules, max_prof_per_course, max_rooms_per_group, M_is_lab, M_text, fixed_prof_by_mg=None, verbose=True):
    tipo_by_a = {_norm_code(a): (A_tipo.get(_norm_code(a)) or "T") for a in A}
    extras_concretas = set(_expand_extras_tokens(AL_sem1_extras or [], A))
    slots = [(h, d) for d in D for h in H]
    n_slots = len(slots)
    if n_slots <= 0:
        _warn("No hay bloques disponibles (|D|*|H| = 0). Revisa D/H en el JSON.")
        return ({}, {}, {}, [])

    U = {_norm_code(p): {(_canon_hour(h), _canon_day(d)) for (h, d) in map(tuple, lst)} for p, lst in (Uraw or {}).items()}
    avail = {_norm_code(p): n_slots - len(U.get(_norm_code(p), set())) for p in P}

    P_by_mg: Dict[Tuple[str, str], List[str]] = {}
    fixed_prof_by_mg = fixed_prof_by_mg or {}
    for (m, g) in MG:
        if (m, g) in fixed_prof_by_mg:
            P_by_mg[(m, g)] = [_norm_code(fixed_prof_by_mg[(m, g)])]
            continue
        req = int(Hreq.get(_k_mg(m, g), 0))
        feasibles = [p for p in P if avail.get(_norm_code(p), 0) >= req]
        feasibles.sort(key=lambda p: float(a_pm.get(_k_pm(p, m), 1000.0)))
        if max_prof_per_course > 0 and len(feasibles) > max_prof_per_course:
            feasibles = feasibles[:max_prof_per_course]
        P_by_mg[(m, g)] = [_norm_code(p) for p in feasibles]

    A_by_mg: Dict[Tuple[str, str], List[str]] = {}
    STRICT = _env_bool("STRICT_WHITELIST_ENFORCEMENT", "1")
    for (m, g) in MG:
        need = int(_safe_int(sizeG.get(g, 0), 0))
        is_lab = _course_is_lab(m, M_is_lab, M_text)
        is_sem1 = bool(SEM1_RE.search(g)) or bool(SEM1_MAT_RE.search(m))
        ctype = "L" if is_lab else "T"

        def _cap_ok(a):
            return int(_safe_int(capA.get(a, 0), 0)) >= need

        if is_lab:
            feas = [a for a in A if (tipo_by_a.get(a, "T") == "L") and _cap_ok(a)]
        else:
            feas = [a for a in A if (tipo_by_a.get(a, "T") == "T") and _cap_ok(a)]
            if is_sem1 and extras_concretas:
                for a in extras_concretas:
                    if _cap_ok(a) and a not in feas:
                        feas.append(a)
            if ALLOW_THEORY_IN_LABS:
                for a in A:
                    if tipo_by_a.get(a, "T") == "L" and _cap_ok(a) and a not in feas:
                        feas.append(a)

        wl = _find_whitelist_for_mg(m, g, whitelist_rooms, whitelist_rules, ctype, A)
        if wl is not None:
            original = set(feas)
            feas = [a for a in feas if a in set(wl)]
            if not feas and not STRICT:
                feas = list(original)

        feas = [_norm_code(a) for a in feas]
        feas.sort(key=lambda a: int(_safe_int(capA.get(a, 10**9), 10**9)) - need)
        if max_rooms_per_group > 0 and len(feas) > max_rooms_per_group:
            feas = feas[:max_rooms_per_group]
        A_by_mg[(m, g)] = feas

    if verbose and P_by_mg and A_by_mg:
        avg_p = sum(len(P_by_mg[k]) for k in P_by_mg) / max(1, len(P_by_mg))
        avg_a = sum(len(A_by_mg[k]) for k in A_by_mg) / max(1, len(A_by_mg))
        _title("Candidatos recortados")
        print(f"Profesores por curso (prom): {avg_p:.1f}  (lim={max_prof_per_course})")
        print(f"Aulas por (m,g)     (prom): {avg_a:.1f}  (lim={max_rooms_per_group})")
    return P_by_mg, A_by_mg, U, slots


def _preflight_checks_with_candidates(data, P_by_mg, A_by_mg, slots, assign_rooms_flag: bool, M_is_lab, M_text) -> bool:
    if _env_bool("BYPASS_PREFLIGHT", "0"):
        _warn("BYPASS_PREFLIGHT=1 -> saltando prechequeos.")
        return True
    if not slots:
        _warn("No hay slots (D×H=0).")
        return False

    P, A, D, H = data["P"], data["A"], data["D"], data["H"]
    M, G, MG = data["M"], data["G"], data["MG"]
    Hreq, MinH, MaxH = data["Hreq"], data["MinH"], data["MaxH"]
    A_tipo = data["A_tipo"]

    _title("Chequeos preventivos")
    n_slots = len(slots)
    print(f"|P|={len(P)} |A|={len(A)} |M|={len(M)} |G|={len(G)} |MG|={len(MG)} |D|={len(D)} |H|={len(H)} -> bloques={n_slots}")

    if not A and assign_rooms_flag:
        _warn("Conjunto de aulas vacío. Carga aulas válidas.")
        return False

    bad_groups = []
    for g in G:
        total = sum(int(Hreq.get(_k_mg(m, g), 0)) for (m, gg) in MG if gg == g)
        if total > n_slots:
            bad_groups.append((g, total, n_slots))
    if bad_groups:
        _warn("Grupos con demanda semanal de bloques que excede la semana:")
        for g, tot, cap in bad_groups[:20]:
            print(f"  Grupo {g}: demanda={tot} > capacidad_semana={cap}")
        return False

    if assign_rooms_flag:
        cursos_sin_aula = [(m, g) for (m, g) in MG if len(A_by_mg.get((m, g), [])) == 0]
        if cursos_sin_aula:
            _warn("Cursos sin aula candidata (capacidad/tipo/whitelist):")
            for (m, g) in cursos_sin_aula[:20]:
                print(f"  (m={m}, g={g}) -> 0 aulas")
            print(f"Total: {len(cursos_sin_aula)}")
            return False

    AT = [a for a in A if A_tipo.get(a, "T") == "T"]
    AL = [a for a in A if A_tipo.get(a, "T") == "L"]
    demand_T = sum(int(Hreq.get(_k_mg(m, g), 0)) for (m, g) in MG if not _course_is_lab(m, M_is_lab, M_text))
    demand_L = sum(int(Hreq.get(_k_mg(m, g), 0)) for (m, g) in MG if _course_is_lab(m, M_is_lab, M_text))
    cap_T = len(AT) * n_slots
    cap_L = len(AL) * n_slots
    print(f"Capacidad T={cap_T} (|AT|={len(AT)})  vs demanda T={demand_T}")
    print(f"Capacidad L={cap_L} (|AL|={len(AL)})  vs demanda L={demand_L}")

    if assign_rooms_flag:
        if demand_T > cap_T:
            _warn("Infactible: Demanda de cursos teóricos > capacidad total AT.")
            return False
        if demand_L > cap_L:
            _warn("Infactible: Demanda de cursos de laboratorio > capacidad total AL.")
            return False

    dem_total = sum(int(Hreq.get(_k_mg(m, g), 0)) for (m, g) in MG)
    sum_min = sum(int(MinH.get(p, 0)) for p in P)
    sum_max = sum(int(MaxH.get(p, n_slots)) for p in P)
    print(f"Demanda total Sum Hreq = {dem_total}")
    print(f"Sum MinH (todos P) = {sum_min}   Sum MaxH = {sum_max}")
    cap_prof_total = len(P) * n_slots
    print(f"Capacidad total profes (bloques): {cap_prof_total}")
    if dem_total > cap_prof_total:
        _warn(f"Capacidad de profesores insuficiente: demanda={dem_total} > {cap_prof_total}.")
        return False

    if assign_rooms_flag:
        cap_aulas_total = len(set(A)) * n_slots
        print(f"Capacidad total aulas (bloques): {cap_aulas_total}")
        if dem_total > cap_aulas_total:
            _warn(f"Capacidad de aulas insuficiente: demanda={dem_total} > {cap_aulas_total}.")
            return False

    P_active = set()
    for (m, g), plist in P_by_mg.items():
        for p in plist:
            P_active.add(p)
    prof_sin_cursos = sorted([p for p in P if p not in P_active])
    if prof_sin_cursos:
        with_minH = [p for p in prof_sin_cursos if int(MinH.get(p, 0)) > 0]
        _warn(f"Profes SIN cursos candidatos: {len(prof_sin_cursos)}; con MinH>0 = {len(with_minH)}")

    _ok("Chequeos preventivos: OK (modelo estricto)")
    return True


def _analyze_infeasibility(data, P_by_mg, A_by_mg, slots) -> Dict[str, Any]:
    P, MG = data["P"], data["MG"]
    Hreq, MinH, MaxH = data["Hreq"], data["MinH"], data["MaxH"]
    n_slots = len(slots) if slots else 0
    analysis = {
        "cursos_sin_profesor": [],
        "cursos_sin_aula": [],
        "demanda_vs_capacidad": {},
        "prof_sin_cursos": [],
        "bad_groups": [],
    }

    for (m, g) in MG:
        if len(P_by_mg.get((m, g), [])) == 0:
            analysis["cursos_sin_profesor"].append((m, g, int(Hreq.get(_k_mg(m, g), 0))))
        if len(A_by_mg.get((m, g), [])) == 0:
            analysis["cursos_sin_aula"].append((m, g, 0))

    G = data.get("G", [])
    for g in G:
        total = sum(int(Hreq.get(_k_mg(m, g), 0)) for (m, gg) in MG if gg == g)
        if n_slots and total > n_slots:
            analysis["bad_groups"].append((g, total, n_slots))

    P_active = set()
    for (m, g), plist in P_by_mg.items():
        for p in plist:
            P_active.add(p)
    for p in P:
        if p not in P_active:
            analysis["prof_sin_cursos"].append((p, int(MinH.get(p, 0))))

    total_demand = sum(int(Hreq.get(_k_mg(m, g), 0)) for (m, g) in MG)
    analysis["demanda_vs_capacidad"] = {
        "total_demand": total_demand,
        "total_prof_capacity": len(P) * n_slots if n_slots else 0,
        "sum_minH": sum(int(MinH.get(p, 0)) for p in P),
        "sum_maxH": sum(int(MaxH.get(p, n_slots or 0)) for p in P),
    }
    return analysis


# ------------------------------------------------------------------------------------------
# Núcleo del modelo
# ------------------------------------------------------------------------------------------

def _build_and_solve(data: Dict[str, Any], assign_rooms: bool, single_room: bool, include_time: bool, TIME_LIMIT: int, fast_mode: bool, MAXP: int, MAXA: int, export_prefix: str, solver_name: Optional[str]) -> Tuple[bool, Optional[float], str]:
    P, A, D, H = data["P"], data["A"], data["D"], data["H"]
    MG = data["MG"]
    capA, sizeG = data["cap_A"], data["size_G"]
    a_pm, Hreq = data["a_pm"], data["Hreq"]
    A_tipo = data["A_tipo"]
    AL_sem1_extras = data.get("AL_sem1_extras", [])
    WL_rooms = data.get("WhitelistRooms", {})
    WL_rules = data.get("WhitelistRules", [])
    M_is_lab = data.get("M_is_lab", {}) or {}
    M_text = data.get("M_text", {}) or {}
    preferred_at, backup_at, penalty_backup_at = _preferred_backup_sets(A)

    # Primera pasada para poder validar calendario fuente contra candidatos de profesor.
    P_by_mg0, A_by_mg0, U, slots = _build_candidates(
        P, A, D, H, MG, sizeG, capA, a_pm, Hreq, data["U"], A_tipo,
        AL_sem1_extras, WL_rooms, WL_rules, MAXP, MAXA, M_is_lab, M_text,
        fixed_prof_by_mg=None, verbose=False,
    )
    if not slots:
        analysis = _analyze_infeasibility(data, P_by_mg0, A_by_mg0, slots)
        _create_feasibility_report(analysis, export_prefix)
        return (False, None, "noslots")

    fixed_info = _load_fixed_calendar_info(export_prefix, MG, slots, P_by_mg0, Hreq, M_is_lab, M_text)
    fixed_prof_by_mg = fixed_info.get("prof_by_mg", {}) if fixed_info.get("active") else {}

    P_by_mg, A_by_mg, U, slots = _build_candidates(
        P, A, D, H, MG, sizeG, capA, a_pm, Hreq, data["U"], A_tipo,
        AL_sem1_extras, WL_rooms, WL_rules, MAXP, MAXA, M_is_lab, M_text,
        fixed_prof_by_mg=fixed_prof_by_mg, verbose=True,
    )

    if assign_rooms and penalty_backup_at > 0:
        _title("Preferencia de aulas teóricas")
        print(f"Aulas preferidas FF: {len(preferred_at)}")
        print(f"Aulas de respaldo: {len(backup_at)}")
        print(f"Penalización respaldo AT: {penalty_backup_at}")

    if not _preflight_checks_with_candidates(data, P_by_mg, A_by_mg, slots, assign_rooms_flag=assign_rooms, M_is_lab=M_is_lab, M_text=M_text):
        analysis = _analyze_infeasibility(data, P_by_mg, A_by_mg, slots)
        _create_feasibility_report(analysis, export_prefix)
        return (False, None, "preflight")

    n_slots = len(slots)
    relax_lab_time = _env_bool("RELAX_LAB_TIME", "0")
    fix_theory_time = _env_bool("FIX_THEORY_TIME_FROM_CALENDAR", "1")
    lab_move_penalty = _safe_float(os.getenv("LAB_TIME_MOVE_PENALTY", "20"), 20.0)
    balance_enable, balance_weight, balance_tol_factor, balance_by_type = _room_balance_config()

    theory_fixed_slots = fixed_info.get("theory_fixed_slots", {}) if fixed_info.get("active") else {}
    lab_original_slots = fixed_info.get("lab_original_slots", {}) if fixed_info.get("active") else {}
    fixed_slots_all = fixed_info.get("slots_by_mg", {}) if fixed_info.get("active") else {}

    if assign_rooms and include_time and balance_enable:
        _title("Balance de aulas")
        print(f"ENABLE_ROOM_BALANCE = {1 if balance_enable else 0}")
        print(f"ROOM_BALANCE_WEIGHT = {balance_weight}")
        print(f"ROOM_BALANCE_TOL_FACTOR = {balance_tol_factor}")
        print(f"ROOM_BALANCE_BY_TYPE = {1 if balance_by_type else 0}")

    _title("Construyendo modelo")
    prob = pl.LpProblem("Timetabling_ISC", pl.LpMinimize)

    y_pmg = {}
    for (m, g) in MG:
        for p in P_by_mg[(m, g)]:
            y_pmg[(p, m, g)] = pl.LpVariable(_lp_name("y", p, m, g), 0, 1, cat="Binary")

    w_p = {p: pl.LpVariable(_lp_name("w", p), 0, 1, cat="Binary") for p in P}

    x_amg = {}
    if assign_rooms and single_room:
        for (m, g) in MG:
            for a in A_by_mg[(m, g)]:
                x_amg[(a, m, g)] = pl.LpVariable(_lp_name("x", a, m, g), 0, 1, cat="Binary")

    z_mghd, y_pmghd, x_amghd = {}, {}, {}
    if include_time:
        for (m, g) in MG:
            for (h, d) in slots:
                z_mghd[(m, g, h, d)] = pl.LpVariable(_lp_name("z", m, g, h, d), 0, 1, cat="Binary")
                for p in P_by_mg[(m, g)]:
                    y_pmghd[(p, m, g, h, d)] = pl.LpVariable(_lp_name("yhd", p, m, g, h, d), 0, 1, cat="Binary")
                if assign_rooms:
                    for a in A_by_mg[(m, g)]:
                        x_amghd[(a, m, g, h, d)] = pl.LpVariable(_lp_name("xhd", a, m, g, h, d), 0, 1, cat="Binary")

    room_over = {}
    room_target = {}
    if assign_rooms and include_time and balance_enable:
        AT_rooms = [a for a in A if A_tipo.get(a, "T") == "T"]
        AL_rooms = [a for a in A if A_tipo.get(a, "T") == "L"]
        demand_T = sum(int(Hreq.get(_k_mg(m, g), 0)) for (m, g) in MG if not _course_is_lab(m, M_is_lab, M_text))
        demand_L = sum(int(Hreq.get(_k_mg(m, g), 0)) for (m, g) in MG if _course_is_lab(m, M_is_lab, M_text))
        target_T = (float(demand_T) / len(AT_rooms)) if AT_rooms else 0.0
        target_L = (float(demand_L) / len(AL_rooms)) if AL_rooms else 0.0
        target_all = (float(demand_T + demand_L) / len(A)) if A else 0.0
        for a in A:
            if balance_by_type:
                base_target = target_L if A_tipo.get(a, "T") == "L" else target_T
            else:
                base_target = target_all
            room_target[a] = float(base_target) * balance_tol_factor
            room_over[a] = pl.LpVariable(_lp_name("over_room", a), lowBound=0, cat="Continuous")

    _apply_calendar_warm_start(
        export_prefix, MG, P_by_mg, A_by_mg, slots,
        y_pmg, w_p, x_amg, z_mghd, y_pmghd, x_amghd,
        assign_rooms=assign_rooms, single_room=single_room,
    )

    _title("Objetivo")
    obj_terms = [a_pm.get(_k_pm(p, m), 1000.0) * y_pmg[(p, m, g)] for (m, g) in MG for p in P_by_mg[(m, g)]]

    if assign_rooms:
        if single_room and penalty_backup_at > 0:
            for (m, g) in MG:
                for a in A_by_mg[(m, g)]:
                    pen = _room_penalty_for_course_room(m, a, M_is_lab, M_text, preferred_at, backup_at, penalty_backup_at)
                    if pen > 0:
                        obj_terms.append(pen * x_amg[(a, m, g)])
        elif include_time and penalty_backup_at > 0:
            for (m, g) in MG:
                for (h, d) in slots:
                    for a in A_by_mg[(m, g)]:
                        pen = _room_penalty_for_course_room(m, a, M_is_lab, M_text, preferred_at, backup_at, penalty_backup_at)
                        if pen > 0:
                            obj_terms.append(pen * x_amghd[(a, m, g, h, d)])

    if include_time and fixed_info.get("active") and relax_lab_time and lab_move_penalty > 0:
        for (m, g) in MG:
            if not _course_is_lab(m, M_is_lab, M_text):
                continue
            src_slots = set(lab_original_slots.get((m, g), set()))
            for (h, d) in slots:
                if (h, d) not in src_slots:
                    obj_terms.append(lab_move_penalty * z_mghd[(m, g, h, d)])

    if assign_rooms and include_time and balance_enable and balance_weight > 0:
        for a in A:
            obj_terms.append(balance_weight * room_over[a])

    prob += pl.lpSum(obj_terms), _lp_name("Minimize", "affinity_room_policy_lab_moves_and_room_balance")

    _title("Restricciones")
    # Unicidad de profesor
    for (m, g) in MG:
        prob += (pl.lpSum(y_pmg[(p, m, g)] for p in P_by_mg[(m, g)]) == 1), _lp_name("R1_one_prof", m, g)

    # Fijación de profesor desde calendario
    if fixed_info.get("active"):
        for (m, g) in MG:
            p_fix = fixed_prof_by_mg.get((m, g), "")
            if p_fix:
                for p in P_by_mg[(m, g)]:
                    rhs = 1 if p == p_fix else 0
                    prob += (y_pmg[(p, m, g)] == rhs), _lp_name("RFIX_prof", p, m, g)

    # Activación w_p
    if include_time:
        for p in P:
            prob += (
                pl.lpSum(y_pmghd[(p, m, g, h, d)] for (m, g) in MG if p in P_by_mg[(m, g)] for (h, d) in slots) <= n_slots * w_p[p]
            ), _lp_name("R0_use_link_hd", p)
    for (m, g) in MG:
        for p in P_by_mg[(m, g)]:
            prob += y_pmg[(p, m, g)] <= w_p[p], _lp_name("R0_use_link_pmg", p, m, g)

    # Carga docente
    if include_time:
        for p in P:
            workload = pl.lpSum(y_pmghd[(p, m, g, h, d)] for (m, g) in MG if p in P_by_mg[(m, g)] for (h, d) in slots)
            prob += workload >= data["MinH"].get(p, 0) * w_p[p], _lp_name("R10_minH", p)
            prob += workload <= data["MaxH"].get(p, n_slots) * w_p[p], _lp_name("R10_maxH", p)
    else:
        total_blocks = len(data["D"]) * len(data["H"])
        for p in P:
            assigned_hours = pl.lpSum(data["Hreq"].get(_k_mg(m, g), 0) * y_pmg[(p, m, g)] for (m, g) in MG if p in P_by_mg[(m, g)])
            prob += assigned_hours >= data["MinH"].get(p, 0) * w_p[p], _lp_name("R10_minH", p)
            prob += assigned_hours <= data["MaxH"].get(p, total_blocks) * w_p[p], _lp_name("R10_maxH", p)

    # Aula fija por curso si aplica
    if assign_rooms and single_room:
        for (m, g) in MG:
            feas = A_by_mg[(m, g)]
            if feas:
                prob += pl.lpSum(x_amg[(a, m, g)] for a in feas) == 1, _lp_name("R4_one_room", m, g)

    if include_time:
        # Horas requeridas y fijaciones de tiempo
        for (m, g) in MG:
            req = data["Hreq"].get(_k_mg(m, g), 0)
            prob += pl.lpSum(z_mghd[(m, g, h, d)] for (h, d) in slots) == req, _lp_name("R2_Hreq", m, g)

            if fixed_info.get("active"):
                if relax_lab_time and _course_is_lab(m, M_is_lab, M_text):
                    # Labs: se permite mover tiempo.
                    pass
                else:
                    fixed_slots = set(theory_fixed_slots.get((m, g), fixed_slots_all.get((m, g), set())))
                    for (h, d) in slots:
                        rhs = 1 if (h, d) in fixed_slots else 0
                        prob += (z_mghd[(m, g, h, d)] == rhs), _lp_name("RFIX_time", m, g, h, d)

        # Relación y/z y fijación de profesor por slot
        for (m, g) in MG:
            for (h, d) in slots:
                prob += (pl.lpSum(y_pmghd[(p, m, g, h, d)] for p in P_by_mg[(m, g)]) == z_mghd[(m, g, h, d)]), _lp_name("R1b_sumYeqZ", m, g, h, d)
                for p in P_by_mg[(m, g)]:
                    prob += (y_pmghd[(p, m, g, h, d)] <= y_pmg[(p, m, g)]), _lp_name("R1c_link_yhd_ypmg", p, m, g, h, d)
                if fixed_info.get("active") and (m, g) in fixed_prof_by_mg:
                    p_fix = fixed_prof_by_mg[(m, g)]
                    for p in P_by_mg[(m, g)]:
                        rhs_expr = z_mghd[(m, g, h, d)] if p == p_fix else 0
                        prob += (y_pmghd[(p, m, g, h, d)] == rhs_expr), _lp_name("RFIX_yhd_prof", p, m, g, h, d)

        # Solapes y disponibilidad de profesor
        for p in P:
            forb = set(tuple(t) for t in data["U"].get(p, []))
            for (h, d) in slots:
                prob += (pl.lpSum(y_pmghd[(p, m, g, h, d)] for (m, g) in MG if (p in P_by_mg[(m, g)])) <= 1), _lp_name("R8_nosolap", p, h, d)
                if (h, d) in forb:
                    for (m, g) in MG:
                        if p in P_by_mg[(m, g)]:
                            prob += (y_pmghd[(p, m, g, h, d)] == 0), _lp_name("R3_nodispo", p, m, g, h, d)

        # Aulas por slot
        if assign_rooms:
            for (m, g) in MG:
                feas = A_by_mg[(m, g)]
                req = int(data["Hreq"].get(_k_mg(m, g), 0))
                for (h, d) in slots:
                    prob += (pl.lpSum(x_amghd[(a, m, g, h, d)] for a in feas) == z_mghd[(m, g, h, d)]), _lp_name("R6a_sumXeqZ", m, g, h, d)
                    for a in feas:
                        prob += (x_amghd[(a, m, g, h, d)] <= z_mghd[(m, g, h, d)]), _lp_name("R6a2_xhd_le_z", a, m, g, h, d)
                        if single_room:
                            # Cierre fuerte de aula única:
                            # x_amghd = 1 sii el curso está activo en (h,d) y el aula fija elegida es a.
                            prob += (x_amghd[(a, m, g, h, d)] <= x_amg[(a, m, g)]), _lp_name("R6b1_xhd_le_x", a, m, g, h, d)
                            prob += (x_amghd[(a, m, g, h, d)] >= z_mghd[(m, g, h, d)] + x_amg[(a, m, g)] - 1), _lp_name("R6b2_xhd_ge_z_plus_x_minus_1", a, m, g, h, d)

                if single_room:
                    for a in feas:
                        # Si el aula fija del curso es a, entonces exactamente req sesiones del curso
                        # deben ocurrir en esa aula; en cualquier otra, cero.
                        prob += (
                            pl.lpSum(x_amghd[(a, m, g, h, d)] for (h, d) in slots) == req * x_amg[(a, m, g)]
                        ), _lp_name("R6b3_total_room_matches_course_hours", a, m, g)

            for a in A:
                for (h, d) in slots:
                    sum_room_use = pl.lpSum(x_amghd[(a, m, g, h, d)] for (m, g) in MG if a in A_by_mg.get((m, g), []))
                    prob += sum_room_use <= 1, _lp_name("R6c_noroom_overlap", a, h, d)

            if balance_enable and balance_weight > 0:
                for a in A:
                    usage_expr = pl.lpSum(
                        x_amghd[(a, m, g, h, d)]
                        for (m, g) in MG if a in A_by_mg.get((m, g), [])
                        for (h, d) in slots
                    )
                    prob += usage_expr - room_target[a] <= room_over[a], _lp_name("RBAL_overuse", a)

        # No solape de grupo
        for g in data.get("G", []):
            mg_list = [(m, g2) for (m, g2) in MG if g2 == g]
            if not mg_list:
                continue
            for (h, d) in slots:
                prob += (pl.lpSum(z_mghd[(m, gg, h, d)] for (m, gg) in mg_list) <= 1), _lp_name("R5_no_group_overlap", g, h, d)

        # Conflictos de estudiantes, si existen
        E_courses = data.get("E_courses", {})
        if E_courses:
            for s, courses in E_courses.items():
                courses_mg = []
                for mg_key in courses:
                    try:
                        m, g = mg_key.split("|", 1)
                        m, g = _norm_code(m), _norm_code(g)
                        if (m, g) in MG:
                            courses_mg.append((m, g))
                    except ValueError:
                        continue
                for (h, d) in slots:
                    prob += (pl.lpSum(z_mghd[(m, g, h, d)] for (m, g) in courses_mg) <= 1), _lp_name("R7_student_conflict", s, h, d)

    _title("Lanzando CPLEX (forzado)")
    if TIME_LIMIT > 60:
        _tip(f"Tiempo límite: {TIME_LIMIT}s")

    status, used_solver_label, solver_meta = _solve_cplex_only(prob, TIME_LIMIT, fast_mode)
    obj_value = pl.value(prob.objective)
    _write_solver_used(export_prefix, used_solver_label)
    _ok(used_solver_label)

    status_label = (solver_meta.get("status_label") or pl.LpStatus.get(status, str(status)) or "").strip()
    has_exportable_solution = obj_value is not None and any(v.value() is not None and v.value() > 0.5 for v in y_pmg.values())

    if not has_exportable_solution or status_label not in ("Optimal", "Feasible", "Feasible (TimeLimit)"):
        human_status = status_label or pl.LpStatus.get(status, str(status))
        _warn(f"Sin solución exportable. Estado={human_status}; obj={obj_value}")
        if human_status == "Infeasible":
            analysis = _analyze_infeasibility(data, P_by_mg, A_by_mg, slots)
            analysis["fixed_mode"] = {
                "active": bool(fixed_info.get("active")),
                "relax_lab_time": relax_lab_time,
                "single_room_per_course": single_room,
                "lab_time_move_penalty": lab_move_penalty,
            }
            _create_feasibility_report(analysis, export_prefix)
        else:
            _create_time_limit_report(export_prefix, human_status, used_solver_label, solver_meta)
            if _export_source_calendar_fallback(export_prefix, MG, P_by_mg, A_by_mg, slots, assign_rooms, single_room):
                _warn("Se exportó fallback desde calendario fuente; úsalo solo como solución de referencia.")
                return (True, None, "fallback_source_calendar")
        return (False, None, human_status)

    _ok(f"Status solver: {status_label}  (obj={obj_value:.3f})")
    dirn = os.path.dirname(export_prefix)
    if dirn:
        os.makedirs(dirn, exist_ok=True)

    rows_prof = []
    for (m, g) in MG:
        assigned_p = ""
        for p in P_by_mg[(m, g)]:
            if y_pmg[(p, m, g)].value() > 0.5:
                assigned_p = p
                break
        rows_prof.append([m, g, assigned_p])
    with open(f"{export_prefix}_profesores.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["materia", "grupo", "profesor"], *rows_prof])

    if assign_rooms and single_room:
        rows_room = []
        for (m, g) in MG:
            assigned_a = ""
            for a in A_by_mg[(m, g)]:
                if x_amg[(a, m, g)].value() > 0.5:
                    assigned_a = a
                    break
            rows_room.append([m, g, assigned_a])
        with open(f"{export_prefix}_aulas.csv", "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["materia", "grupo", "aula"], *rows_room])

    rows_cal = []
    if include_time:
        prof_by_mg = {(m, g): "" for (m, g) in MG}
        for (m, g) in MG:
            for p in P_by_mg[(m, g)]:
                if y_pmg[(p, m, g)].value() > 0.5:
                    prof_by_mg[(m, g)] = p
                    break

        room_fixed_by_mg = {(m, g): "" for (m, g) in MG}
        if assign_rooms and single_room:
            for (m, g) in MG:
                for a in A_by_mg[(m, g)]:
                    if x_amg[(a, m, g)].value() > 0.5:
                        room_fixed_by_mg[(m, g)] = a
                        break

        for (m, g) in MG:
            for (h, d) in slots:
                if z_mghd[(m, g, h, d)].value() > 0.5:
                    p = prof_by_mg[(m, g)]
                    a = ""
                    if assign_rooms:
                        if single_room:
                            a = room_fixed_by_mg[(m, g)]
                        else:
                            for aa in A_by_mg[(m, g)]:
                                v = x_amghd.get((aa, m, g, h, d))
                                if v is not None and v.value() > 0.5:
                                    a = aa
                                    break
                    rows_cal.append([m, g, h, d, a, p])

        with open(f"{export_prefix}_calendario.csv", "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["materia", "grupo", "hora", "dia", "aula", "profesor"], *rows_cal])

        if assign_rooms and single_room:
            rooms_seen_by_mg = defaultdict(set)
            for m, g, h, d, a, p in rows_cal:
                if a:
                    rooms_seen_by_mg[(m, g)].add(a)
            multi_room_courses = [
                (m, g, sorted(list(rooms_seen_by_mg[(m, g)])))
                for (m, g) in MG
                if len(rooms_seen_by_mg.get((m, g), set())) > 1
            ]
            report_path = f"{export_prefix}_single_room_check.txt"
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("CHEQUEO SINGLE_ROOM_PER_COURSE\n")
                f.write("=============================\n\n")
                if not multi_room_courses:
                    f.write("OK: todos los cursos usan una sola aula en el calendario exportado.\n")
                else:
                    f.write("Se detectaron cursos con más de una aula en calendario:\n")
                    for m, g, rooms in multi_room_courses:
                        f.write(f"  {m}|{g} -> {rooms}\n")
            if multi_room_courses:
                _warn(f"Single-room check: se detectaron {len(multi_room_courses)} cursos con múltiples aulas. Ver {report_path}")
            else:
                _ok(f"Single-room check OK: ver {report_path}")

    _ok(f"Exportado prefijo: {export_prefix}")
    return (True, obj_value, "ok")


# ------------------------------------------------------------------------------------------
# API pública
# ------------------------------------------------------------------------------------------

def solve_one(json_path: str, export_prefix: str, solver_name: Optional[str] = None) -> bool:
    json_path_eff = _phase2_calendar_to_whitelist(json_path)
    data = load_dataset(json_path_eff)

    include_time = _env_bool("MODEL_INCLUDE_TIME", "1")
    assign_rooms = _env_bool("ASSIGN_ROOMS", "1")
    single_room = _env_bool("SINGLE_ROOM_PER_COURSE", "1")
    fast_mode = _env_bool("FAST_MODE", "0")
    TIME_LIMIT = _get_env_int("SOLVER_TIME_LIMIT", 120)
    MAXP = _get_env_int("MAX_PROF_PER_COURSE", 8 if not fast_mode else 5)
    MAXA = _get_env_int("MAX_ROOMS_PER_GROUP", 10 if not fast_mode else 6)

    _title("Bandera de ejecución (efectivas)")
    print(f"  include_time={include_time}  assign_rooms={assign_rooms}  single_room={single_room}")
    print(f"  fast_mode={fast_mode}  TIME_LIMIT={TIME_LIMIT}  MAXP={MAXP}  MAXA={MAXA}")

    _echo_solver_preflight(json_path_eff, export_prefix)
    ok, obj, tag = _build_and_solve(
        data, assign_rooms, single_room, include_time, TIME_LIMIT, fast_mode,
        MAXP, MAXA, export_prefix, solver_name=(solver_name or os.getenv("MODEL_SOLVER")),
    )
    return ok


def _infer_period_from_path_or_json(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            js = json.load(f)
            per = js.get("PERIODO")
            if per:
                return str(per)
    except Exception:
        pass
    base = os.path.basename(path)
    m = re.search(r"(20\d{2}[12])", base) or re.search(r"(20\d{2})", base)
    return m.group(1) if m else None


if __name__ == "__main__":
    _load_env()
    _title("Model Solver ISC - CPLEX FORZADO")
    _echo_effective_env()

    periodo = os.getenv("MODEL_PERIODO", "") or ""
    raw_base_json = os.getenv("DATOS_JSON", "salidas/datos_modelo_{periodo}.json")
    base_json = raw_base_json.replace("{periodo}", periodo) if "{periodo}" in raw_base_json and periodo else raw_base_json
    raw_export = os.getenv("EXPORT_PREFIX", "salidas/isc_{periodo}")

    patterns = []
    if any(sym in base_json for sym in ["*", "?", "["]):
        patterns.append(base_json)
    elif "{periodo}" in raw_base_json and not periodo:
        patterns.append(raw_base_json.replace("{periodo}", "*"))
    else:
        patterns.append(base_json)

    json_files = sorted(set(sum([glob.glob(pat) for pat in patterns], [])))
    if not json_files:
        _warn(f"No se encontraron JSON de entrada con patrón: {patterns}")
        raise SystemExit(1)

    _title(f"Archivos a resolver: {len(json_files)}")
    for i, jf in enumerate(json_files, 1):
        print(f"[{i}/{len(json_files)}] {jf}")

    for i, jf in enumerate(json_files, 1):
        _title(f"[{i}/{len(json_files)}] Resolviendo {jf}")
        period_i = _infer_period_from_path_or_json(jf) or (periodo or "ALL")
        export_i = raw_export.replace("{periodo}", period_i) if "{periodo}" in raw_export else f"{raw_export}_{period_i}"
        try:
            ok = solve_one(jf, export_i, solver_name=os.getenv("MODEL_SOLVER"))
            if not ok:
                _warn(f"Archivo {jf}: sin solución viable (ver reportes).")
            else:
                _ok(f"Archivo {jf}: solución encontrada y exportada en prefijo {export_i}")
        except Exception as e:
            _err(f"Fallo al resolver {jf}: {e}")
