# run_modelo_isc.py
# ==========================================================================================
# Runner de validación y resolución para JSONs generados por build_datos_modelo_isc.py.
# - No accede a la base de datos.
# - Puede preprocesar el JSON con Fase 2 (whitelist reducida desde calendario fuente).
# - Importa model_solver.py desde ruta exacta para evitar ambigüedades de importación.
# - Muestra di
# agnóstico explícito de warm start / fallback.
# - Evita que model_solver.py vuelva a aplicar Fase 2 si el runner ya la aplicó.
# ==========================================================================================

import os
import sys
import re
import json
import glob
import csv
import time
import runpy
import subprocess
import importlib
import importlib.util
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Any, Tuple, List, Optional

from dotenv import load_dotenv, find_dotenv


def _load_env_robust():
    candidates = []
    env_hint = (os.getenv("ENV_FILE") or "").strip()
    if env_hint:
        candidates.append(env_hint)
    proj = (os.getenv("PROJECT_ROOT") or "").strip().strip("\"'")
    if proj:
        candidates.append(os.path.join(proj, ".env"))
    candidates.append(os.path.join(os.getcwd(), ".env"))
    auto = find_dotenv(usecwd=True)
    if auto:
        candidates.append(auto)
    for p in candidates:
        if p and os.path.isfile(p):
            load_dotenv(p, override=True)
            return p
    load_dotenv(override=True)
    return None


_LOADED_ENV_PATH = _load_env_robust()

JSON_PATH = os.getenv("DATOS_JSON", "salidas/datos_modelo_*.json")
RUN_SOLVER = os.getenv("RUN_SOLVER", "1") in ("1", "true", "True", "YES", "yes")
INCLUDE_TIME = os.getenv("MODEL_INCLUDE_TIME", "0") in ("1", "true", "True", "YES", "yes")
TIME_LIMIT_SEC = int(os.getenv("SOLVER_TIME_LIMIT", "600") or "600")
MODEL_SOLVER = os.getenv("MODEL_SOLVER", "auto")
EXPORT_PREFIX = os.getenv("EXPORT_PREFIX", "salidas/{base}")
MULTI_PERIODS = (os.getenv("MULTI_PERIODS") or "").strip()
RAW_CPLEX_BIN = os.getenv("CPLEX_BIN", "") or ""
CPLEX_BIN = RAW_CPLEX_BIN.strip().strip('"').strip("'")
BUILDER_PATH = (os.getenv("BUILDER_PATH") or "").strip().strip('"').strip("'")
PROJECT_ROOT = (os.getenv("PROJECT_ROOT") or "").strip().strip('"').strip("'")

FAST_MODE = os.getenv("FAST_MODE", "1") in ("1", "true", "True", "YES", "yes")
RELAX_CONSTRAINTS = os.getenv("RELAX_CONSTRAINTS", "0") in ("1", "true", "True", "YES", "yes")
AUTO_RELAX = os.getenv("AUTO_RELAX", "1") in ("1", "true", "True", "YES", "yes")
AUTO_ULTRA_RELAX = os.getenv("AUTO_ULTRA_RELAX", "1") in ("1", "true", "True", "YES", "yes")
ASSIGN_ROOMS = os.getenv("ASSIGN_ROOMS", "0") in ("1", "true", "True", "YES", "yes")
SINGLE_ROOM_PER_COURSE = os.getenv("SINGLE_ROOM_PER_COURSE", "0") in ("1", "true", "True", "YES", "yes")

LAB_COURSE_REGEX = os.getenv("LAB_COURSE_REGEX", r"(?i)\b(LAB|LABORATORI|TALLER|PR(A|Á)CTIC)\b")
SEM1_GROUP_REGEX = os.getenv("SEM1_GROUP_REGEX", r"(?i)^(1([A-Z]|$)|.*(^|[^0-9])1([^0-9]|$))")
ALLOW_THEORY_IN_LABS = os.getenv("ALLOW_THEORY_IN_LABS", "1") in ("1", "true", "True", "YES", "yes")
MAX_ROOMS_PER_GROUP = int(os.getenv("MAX_ROOMS_PER_GROUP", "10") or "10")
MAX_PROF_PER_COURSE = int(os.getenv("MAX_PROF_PER_COURSE", "8") or "8")

STRICT_ROOM_SET = os.getenv("STRICT_ROOM_SET", "0") in ("1", "true", "True", "YES", "yes")
REQUIRE_CAPACITY_FOR_ROOM = os.getenv("REQUIRE_CAPACITY_FOR_ROOM", "0") in ("1", "true", "True", "YES", "yes")
BYPASS_PREFLIGHT = os.getenv("BYPASS_PREFLIGHT", "0") in ("1", "true", "True", "YES", "yes")

PREPROCESS_JSON = os.getenv("PREPROCESS_JSON", "1") in ("1", "true", "True", "YES", "yes")
FORCE_H_FROM_ENV = (os.getenv("FORCE_H_FROM_ENV") or "").strip()
FORCE_D_FROM_ENV = (os.getenv("FORCE_D_FROM_ENV") or "").strip()
FILL_A_TIPO_FROM_AT_AL = os.getenv("FILL_A_TIPO_FROM_AT_AL", "1") in ("1", "true", "True", "YES", "yes")
POST_SUMMARY = os.getenv("POST_SUMMARY", "1") in ("1", "true", "True", "YES", "yes")
FAIL_EARLY_IF_ROOM_CAP_INSUFF = os.getenv("FAIL_EARLY_IF_ROOM_CAP_INSUFF", "0") in ("1", "true", "True", "YES", "yes")

PHASE2_FROM_BLOCK_SOLUTION = os.getenv("PHASE2_FROM_BLOCK_SOLUTION", "1") in ("1", "true", "True", "YES", "yes")
PHASE2_SOURCE_PREFIX = (os.getenv("PHASE2_SOURCE_PREFIX") or "").strip()
PHASE2_SOURCE_CALENDAR = (os.getenv("PHASE2_SOURCE_CALENDAR") or "").strip()
PHASE2_TOPK_ROOMS = int(os.getenv("PHASE2_TOPK_ROOMS", "3") or "3")
PHASE2_MIN_ROOM_USES = int(os.getenv("PHASE2_MIN_ROOM_USES", "1") or "1")
PHASE2_STRICT_MERGE = os.getenv("PHASE2_STRICT_MERGE", "0") in ("1", "true", "True", "YES", "yes")

PREFERRED_AT_LIST = [s.strip().upper() for s in (os.getenv("PREFERRED_AT_LIST", "")).replace(";", ",").split(",") if s.strip()]
PHASE2_ADD_PREFERRED_AT = os.getenv("PHASE2_ADD_PREFERRED_AT", "1") in ("1", "true", "True", "YES", "yes")
PHASE2_PREFERRED_AT_TOPK = int(os.getenv("PHASE2_PREFERRED_AT_TOPK", "2") or "2")
PHASE2_KEEP_SOURCE_ROOMS = os.getenv("PHASE2_KEEP_SOURCE_ROOMS", "1") in ("1", "true", "True", "YES", "yes")

FRANJA_BAD_EARLY = os.getenv("FRANJA_BAD_EARLY", "07,08")
FRANJA_BAD_LATE = os.getenv("FRANJA_BAD_LATE", "19,20")
FRANJA_WEIGHT_EARLY = os.getenv("FRANJA_WEIGHT_EARLY", "50")
FRANJA_WEIGHT_LATE = os.getenv("FRANJA_WEIGHT_LATE", "80")


def title(t: str) -> None:
    print("\n" + "=" * len(t))
    print(t)
    print("=" * len(t))


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def preflight_solver_info() -> None:
    title("Preflight del solver (.env y detecciones)")
    if _LOADED_ENV_PATH:
        print(f"ENV_FILE            = {_LOADED_ENV_PATH}")
    print(f"MODEL_SOLVER        = {MODEL_SOLVER}")
    print(f"TIME_LIMIT_SEC      = {TIME_LIMIT_SEC}")
    print(f"MODEL_INCLUDE_TIME  = {int(INCLUDE_TIME)}")
    print(f"CPLEX_BIN (raw)     = {RAW_CPLEX_BIN!r}")
    print(f"CPLEX_BIN (normaliz)= {CPLEX_BIN!r}")

    exists = os.path.isfile(CPLEX_BIN) if CPLEX_BIN else False
    isdir = os.path.isdir(CPLEX_BIN) if CPLEX_BIN else False
    print(f"CPLEX_BIN isfile?   = {exists}")
    print(f"CPLEX_BIN isdir?    = {isdir}")

    has_cplex_py = importlib.util.find_spec("cplex") is not None
    print(f"Modulo 'cplex' (API Python) instalado? {has_cplex_py}")

    if MODEL_SOLVER.lower() == "cplex":
        if exists or isdir:
            print("Se intentara CPLEX_CMD (ejecutable).")
        elif has_cplex_py:
            print("Sin CPLEX_BIN pero con 'cplex' -> CPLEX_PY.")
        else:
            print("Sin CPLEX_BIN ni 'cplex' -> fallara con MODEL_SOLVER=cplex.")
    elif MODEL_SOLVER.lower() == "auto":
        if exists or isdir:
            print("AUTO: CPLEX_CMD; si no, CPLEX_PY; si no, CBC.")
        elif has_cplex_py:
            print("AUTO: CPLEX_PY; si no, CBC.")
        else:
            print("AUTO: CBC.")
    else:
        print("Seleccionado CBC explicitamente o modo no-CPLEX.")

    print("\nConfiguracion de tolerancia:")
    print(f"STRICT_ROOM_SET           = {int(STRICT_ROOM_SET)}")
    print(f"REQUIRE_CAPACITY_FOR_ROOM = {int(REQUIRE_CAPACITY_FOR_ROOM)}")
    print(f"BYPASS_PREFLIGHT          = {int(BYPASS_PREFLIGHT)}")
    print(f"ALLOW_THEORY_IN_LABS      = {int(ALLOW_THEORY_IN_LABS)}")
    print(f"MAX_ROOMS_PER_GROUP       = {MAX_ROOMS_PER_GROUP}")
    print(f"MAX_PROF_PER_COURSE       = {MAX_PROF_PER_COURSE}")

    print("\nEstrategias de relajacion:")
    print(f"FAST_MODE                 = {int(FAST_MODE)}")
    print(f"RELAX_CONSTRAINTS         = {int(RELAX_CONSTRAINTS)}")
    print(f"AUTO_RELAX                = {int(AUTO_RELAX)}")
    print(f"AUTO_ULTRA_RELAX          = {int(AUTO_ULTRA_RELAX)}")
    print(f"ASSIGN_ROOMS              = {int(ASSIGN_ROOMS)}")
    print(f"SINGLE_ROOM_PER_COURSE    = {int(SINGLE_ROOM_PER_COURSE)}")

    print("\nValidacion de tipos/regex:")
    print(f"LAB_COURSE_REGEX    = {LAB_COURSE_REGEX}")
    print(f"SEM1_GROUP_REGEX    = {SEM1_GROUP_REGEX}")

    if ASSIGN_ROOMS and SINGLE_ROOM_PER_COURSE:
        print("\nFase 2 (whitelist desde opcion 2):")
        print(f"PHASE2_FROM_BLOCK_SOLUTION = {int(PHASE2_FROM_BLOCK_SOLUTION)}")
        print(f"PHASE2_TOPK_ROOMS          = {PHASE2_TOPK_ROOMS}")
        print(f"PHASE2_MIN_ROOM_USES       = {PHASE2_MIN_ROOM_USES}")
        print(f"PHASE2_SOURCE_PREFIX       = {PHASE2_SOURCE_PREFIX or '(auto)'}")
        print(f"PHASE2_SOURCE_CALENDAR     = {PHASE2_SOURCE_CALENDAR or '(auto)'}")
        print(f"PHASE2_STRICT_MERGE        = {int(PHASE2_STRICT_MERGE)}")
        print(f"PHASE2_ADD_PREFERRED_AT    = {int(PHASE2_ADD_PREFERRED_AT)}")
        print(f"PHASE2_PREFERRED_AT_TOPK   = {PHASE2_PREFERRED_AT_TOPK}")
        print(f"PHASE2_KEEP_SOURCE_ROOMS   = {int(PHASE2_KEEP_SOURCE_ROOMS)}")
        print(f"PREFERRED_AT_LIST          = {', '.join(PREFERRED_AT_LIST) if PREFERRED_AT_LIST else '(vacia)'}")
        print(f"BACKUP_AT_LIST             = {os.getenv('BACKUP_AT_LIST', '')}")
        print(f"ROOM_PENALTY_BACKUP_AT     = {os.getenv('ROOM_PENALTY_BACKUP_AT', '0')}")
        print(f"WARM_START_FROM_SOURCE_CALENDAR = {os.getenv('WARM_START_FROM_SOURCE_CALENDAR', '')}")
        print(f"WARM_START_SOURCE_CALENDAR      = {os.getenv('WARM_START_SOURCE_CALENDAR', '')}")
        print(f"FALLBACK_USE_SOURCE_CALENDAR    = {os.getenv('FALLBACK_USE_SOURCE_CALENDAR', '')}")


def _python_exe() -> str:
    return sys.executable or "python"


def _candidate_model_solver_files() -> List[Path]:
    out: List[Path] = []
    bases = []
    try:
        bases.append(Path.cwd())
    except Exception:
        pass
    try:
        bases.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    if PROJECT_ROOT:
        bases.append(Path(PROJECT_ROOT))

    seen = set()
    for base in bases:
        if not base:
            continue
        p = (base / "model_solver.py").resolve()
        if p.exists() and str(p) not in seen:
            out.append(p)
            seen.add(str(p))
    return out


def _import_solver_function():
    candidates = _candidate_model_solver_files()
    load_errors = []
    for idx, path in enumerate(candidates, 1):
        try:
            module_name = f"model_solver_runtime_{idx}"
            spec = importlib.util.spec_from_file_location(module_name, str(path))
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                print(f"model_solver cargado por ruta exacta: {path}")
                if hasattr(module, "solve_one"):
                    print("Usando solve_one() del model_solver")
                    return module.solve_one
                if hasattr(module, "main"):
                    print("Usando main() del model_solver")
                    return module.main
        except Exception as e:
            load_errors.append(f"{path}: {e}")

    try:
        import model_solver
        print(f"model_solver importado desde sys.path: {getattr(model_solver, '__file__', '(sin __file__)')}")
        if hasattr(model_solver, "solve_one"):
            print("Usando solve_one() del model_solver")
            return model_solver.solve_one
        if hasattr(model_solver, "main"):
            print("Usando main() del model_solver")
            return model_solver.main
    except Exception as e:
        load_errors.append(f"import model_solver: {e}")

    print("No se pudo encontrar ninguna funcion de solver en model_solver")
    if load_errors:
        print("Detalles de carga:")
        for err in load_errors:
            print(f"  - {err}")
    return None


SOLVER_FUNCTION = _import_solver_function()
if SOLVER_FUNCTION is None:
    print("ERROR: No se pudo importar el solver. Verifica que model_solver.py existe y tiene una funcion valida.")
    sys.exit(1)


def _resolve_export_prefix(export_prefix: Optional[str], periodo: Optional[str], json_path: Optional[str] = None) -> Optional[str]:
    if not export_prefix:
        return None
    result = str(export_prefix)
    base = None
    if json_path:
        base = os.path.splitext(os.path.basename(json_path))[0]
    if "{base}" in result:
        result = result.replace("{base}", base or "salida")
    if periodo:
        if "{periodo}" in result:
            result = result.replace("{periodo}", periodo)
        else:
            result = f"{result}_{periodo}"
    return result


def _expand_json_inputs(path_pattern: str) -> List[str]:
    if not path_pattern:
        return []
    path_pattern = path_pattern.strip()
    if os.path.isdir(path_pattern):
        return sorted([str(p) for p in Path(path_pattern).glob("*.json")])
    if any(sym in path_pattern for sym in ["*", "?", "["]):
        return sorted(glob.glob(path_pattern))
    if os.path.isfile(path_pattern):
        return [path_pattern]
    return []


def _list_possible_built_jsons(pattern: str) -> List[str]:
    if not pattern:
        return []
    pat = (pattern or "").strip()
    if "{periodo}" in pat:
        pat = pat.replace("{periodo}", "*")
    if any(sym in pat for sym in ["*", "?", "["]):
        return sorted(glob.glob(pat))
    if os.path.isdir(pat):
        return sorted(str(p) for p in Path(pat).glob("*.json"))
    if os.path.isfile(pat):
        return [pat]
    root, ext = os.path.splitext(pat or "datos_modelo.json")
    ext = ext or ".json"
    return sorted(glob.glob(f"{root}_*{ext}"))


def _print_inputs_help(pattern: str, periods_expected: Optional[List[str]] = None) -> None:
    title("Ayuda: No se encontraron JSONs que coincidan con DATOS_JSON")
    print(f"Patron recibido: {pattern!r}")
    print("Sugerencias:")
    print("  1) Ejecuta el builder: python build_datos_modelo_isc.py")
    print("  2) Revisa que DATOS_JSON apunte a un archivo existente o use {periodo} correctamente")
    if periods_expected:
        ej = ", ".join(periods_expected)
        print(f"  3) Ejemplo de nombres esperados (sufijos): _{{{ej}}}.json")
    print("  4) Tambien puedes usar un wildcard, p. ej.: salidas/datos_modelo_*.json")


def _find_builder_aggressive():
    possible_names = [
        "build_datos_modelo_isc.py",
        "build_datos_modelo.py",
        "builder_isc.py",
        "builder.py",
        "Datos_Modelo.py",
    ]
    search_dirs = [Path.cwd()]
    try:
        search_dirs.append(Path(__file__).parent)
    except Exception:
        pass
    if PROJECT_ROOT:
        search_dirs.append(Path(PROJECT_ROOT))
    if BUILDER_PATH and os.path.isfile(BUILDER_PATH):
        print(f"Builder encontrado via BUILDER_PATH: {BUILDER_PATH}")
        return Path(BUILDER_PATH)
    for search_dir in search_dirs:
        if not search_dir or not Path(search_dir).exists():
            continue
        for builder_name in possible_names:
            candidate = Path(search_dir) / builder_name
            if candidate.exists():
                print(f"Builder encontrado: {candidate}")
                return candidate
            for subdir in ["", "scripts", "src", "tools", "bin", "build"]:
                candidate = Path(search_dir) / subdir / builder_name
                if candidate.exists():
                    print(f"Builder encontrado en subdirectorio: {candidate}")
                    return candidate
    for search_dir in search_dirs:
        if search_dir and Path(search_dir).exists():
            for root, _, files in os.walk(search_dir):
                for builder_name in possible_names:
                    if builder_name in files:
                        found = Path(root) / builder_name
                        print(f"Builder encontrado (busqueda recursiva): {found}")
                        return found
    return None


def _run_builder_improved() -> bool:
    builder_path = _find_builder_aggressive()
    if builder_path is None:
        title("ERROR: BUILDER NO ENCONTRADO")
        print("No se pudo encontrar el archivo del builder.")
        return False

    title("EJECUTANDO BUILDER")
    print(f"Builder encontrado: {builder_path}")
    print(f"MULTI_PERIODS = {MULTI_PERIODS or '(vacio)'}")
    print(f"DATOS_JSON    = {JSON_PATH}")
    try:
        print("Metodo: runpy.run_path()")
        runpy.run_path(str(builder_path), run_name="__main__")
        print("Builder ejecutado exitosamente con runpy")
        return True
    except Exception as e:
        print(f"runpy fallo: {e}")
    try:
        print("Metodo: subprocess (fallback)")
        result = subprocess.run([_python_exe(), str(builder_path)], capture_output=True, text=True, cwd=os.path.dirname(builder_path))
        if result.returncode == 0:
            print("Builder ejecutado exitosamente con subprocess")
            if result.stdout and result.stdout.strip():
                print("Salida del builder:", result.stdout.strip())
            return True
        print(f"Builder fallo con subprocess: {result.stderr}")
        return False
    except Exception as e:
        print(f"subprocess fallo: {e}")
        return False


def _compile_regex(pat: str, default_pat: str) -> re.Pattern:
    raw = pat or default_pat
    try:
        return re.compile(raw)
    except re.error:
        print(f"Advertencia: patron invalido. Se usara default: {default_pat}")
        return re.compile(default_pat)


LAB_RE = _compile_regex(LAB_COURSE_REGEX, r"(?i)\b(LAB|LABORATORI|TALLER|PR(A|Á)CTIC)\b")
SEM1_RE = _compile_regex(SEM1_GROUP_REGEX, r"(?i)^(1([A-Z]|$)|.*(^|[^0-9])1([^0-9]|$))")


def _norm_code(s: Any) -> str:
    return str(s or "").strip().upper()


def _course_maps(data: dict) -> Tuple[Dict[str, bool], Dict[str, str]]:
    m_is_lab = {_norm_code(k): bool(v) for k, v in (data.get("M_is_lab", {}) or {}).items()}
    m_text = {_norm_code(k): str(v or "") for k, v in (data.get("M_text", {}) or {}).items()}
    return m_is_lab, m_text


def _is_lab_course(materia: str, data: Optional[dict] = None, m_is_lab: Optional[Dict[str, bool]] = None, m_text: Optional[Dict[str, str]] = None) -> bool:
    key = _norm_code(materia)
    if m_is_lab is None or m_text is None:
        if data is not None:
            m_is_lab, m_text = _course_maps(data)
        else:
            m_is_lab, m_text = {}, {}
    if bool(m_is_lab.get(key, False)):
        return True
    txt = str(m_text.get(key, "") or "")
    if txt and LAB_RE.search(txt):
        return True
    return bool(LAB_RE.search(str(materia)))


def _is_sem1_group(grupo: str) -> bool:
    return bool(SEM1_RE.search(str(grupo)))


def _candidate_rooms_for(m: str, g: str, data: dict) -> List[str]:
    A = [str(a) for a in (data.get("A") or [])]
    cap_A = {str(k): int(v) for k, v in (data.get("cap_A") or {}).items()}
    size_G = {str(k): int(v) for k, v in (data.get("size_G") or {}).items()}
    A_tipo = {str(k): (str(v) if v is not None else "") for k, v in (data.get("A_tipo") or {}).items()}
    extras = set(str(x) for x in (data.get("AL_sem1_extras") or []))
    M_is_lab, M_text = _course_maps(data)
    need = int(size_G.get(str(g), 0))

    def _cap_ok(a):
        capacity = int(cap_A.get(str(a), 0))
        if REQUIRE_CAPACITY_FOR_ROOM:
            return capacity >= need
        return capacity >= max(int(need * 0.8), need - 5)

    def _tipo(a):
        return (A_tipo.get(str(a)) or "").upper() or "T"

    is_lab = _is_lab_course(m, m_is_lab=M_is_lab, m_text=M_text)
    is_sem1 = _is_sem1_group(g)

    if is_lab:
        feas = [a for a in A if _tipo(a) == "L" and _cap_ok(a)]
        if not feas and ALLOW_THEORY_IN_LABS:
            feas = [a for a in A if _tipo(a) == "T" and _cap_ok(a)]
    else:
        feas_T = [a for a in A if _tipo(a) == "T" and _cap_ok(a)]
        feas = list(feas_T)
        if is_sem1:
            feas_extra = [a for a in A if (str(a) in extras) and _cap_ok(a)]
            for a in feas_extra:
                if a not in feas:
                    feas.append(a)
        if ALLOW_THEORY_IN_LABS:
            feas_L = [a for a in A if _tipo(a) == "L" and _cap_ok(a)]
            for a in feas_L:
                if a not in feas:
                    feas.append(a)

    if not feas:
        feas = [a for a in A if _cap_ok(a)]
    if not feas:
        feas = sorted(A, key=lambda a: int(cap_A.get(str(a), 0)), reverse=True)[:10]

    feas.sort(key=lambda a: int(cap_A.get(str(a), 10**9)) - need)
    if MAX_ROOMS_PER_GROUP > 0 and len(feas) > MAX_ROOMS_PER_GROUP:
        feas = feas[:MAX_ROOMS_PER_GROUP]
    return feas


def _basic_validations(data: dict):
    title("Validaciones minimas")
    cap_A = data.get("cap_A", {}) or {}
    size_G = data.get("size_G", {}) or {}
    Hreq = data.get("Hreq", {}) or {}
    MG = data.get("MG", []) or []
    G = data.get("G", []) or []
    A = data.get("A", []) or []
    M = data.get("M", []) or []
    D = data.get("D", []) or []
    H = data.get("H", []) or []
    M_is_lab, M_text = _course_maps(data)

    print(f"|P|={len(data.get('P', []) or [])} |A|={len(A)} |D|={len(D)} |H|={len(H)} |M|={len(M)} |G|={len(G)} |MG|={len(MG)}")
    mg_keys = {f"{m}|{g}" for (m, g) in MG}
    faltantes = mg_keys - set(Hreq.keys())
    print("Hreq cubre todos los (m,g)." if not faltantes else f"Faltan Hreq en {len(faltantes)} pares. Ejemplos: {sorted(list(faltantes))[:10]}")

    bad_g = []
    for g in G:
        need = int(str(size_G.get(str(g), 0)))
        if need and not any(int(str(cap_A.get(str(a), 0))) >= max(int(need * 0.8), need - 5) for a in A):
            bad_g.append(g)
    if not bad_g:
        print("Todos los grupos tienen al menos una aula con capacidad suficiente (chequeo global).")
    else:
        print(f"{len(bad_g)} grupos sin aula suficiente (chequeo global). Ejemplos: {bad_g[:5]}")

    title("Validacion de tipo de aula (AT/AL) y extras por curso (m,g)")
    A_tipo = data.get("A_tipo", {}) or {}
    extras = data.get("AL_sem1_extras", []) or []
    print(f"Aulas con tipo declarado (A_tipo): {len(A_tipo)} / {len(A)}")
    if not A_tipo:
        print("Aviso: no se encontro 'A_tipo' en el JSON -> se asumira 'T' por defecto en validacion.")
    print(f"AL_sem1_extras: {len(extras)} elementos")
    print(f"Regex LAB = {LAB_RE.pattern}")
    print(f"Regex SEM1= {SEM1_RE.pattern}")
    print(f"ALLOW_THEORY_IN_LABS = {int(ALLOW_THEORY_IN_LABS)}")
    print(f"MAX_ROOMS_PER_GROUP  = {MAX_ROOMS_PER_GROUP}")
    print(f"Configuracion actual: STRICT_ROOM_SET={int(STRICT_ROOM_SET)}, REQUIRE_CAPACITY_FOR_ROOM={int(REQUIRE_CAPACITY_FOR_ROOM)}")

    sin_candidatas = []
    n_lab = n_teo = 0
    for (m, g) in MG:
        if _is_lab_course(m, m_is_lab=M_is_lab, m_text=M_text):
            n_lab += 1
        else:
            n_teo += 1
        feas = _candidate_rooms_for(str(m), str(g), data)
        if not feas:
            need = int(size_G.get(str(g), 0))
            sin_candidatas.append((str(m), str(g), need))

    print(f"Totales: cursos LAB={n_lab}  |  cursos TEORIA={n_teo}")
    if not sin_candidatas:
        print("OK: Todos los (m,g) tienen al menos 1 aula candidata (tipo/capacidad/extras).")
    else:
        print(f"Atencion: {len(sin_candidatas)} cursos sin aula candidata considerando tipo/capacidad/extras.")
        for i, (m, g, need) in enumerate(sin_candidatas[:20], 1):
            print(f"  {i:02d}) (m={m}, g={g}) size_G={need} -> 0 candidatas")
        if len(sin_candidatas) > 20:
            print("  ...")


def _theoretical_capacity_check(data: dict) -> Tuple[int, int, int, int]:
    H = data.get("H", []) or []
    D = data.get("D", []) or []
    A = data.get("A", []) or []
    P = data.get("P", []) or []
    Hreq = data.get("Hreq", {}) or {}
    MG = data.get("MG", []) or []
    blocks = len(H) * len(D)
    demanda = sum(int(Hreq.get(f"{m}|{g}", 0)) for (m, g) in MG)
    cap_prof = len(P) * blocks
    cap_aulas = len(set(A)) * blocks if ASSIGN_ROOMS else 10**9

    title("Chequeo rapido de capacidad teorica")
    print(f"Bloques/dia={len(H)}  Dias={len(D)}  -> bloques_semana={blocks}")
    print(f"Demanda total (Sum Hreq) = {demanda}")
    print(f"Cap. profesores (|P|*bloques) = {cap_prof}")
    if ASSIGN_ROOMS:
        print(f"Cap. aulas (|A|*bloques) = {cap_aulas}")
        if demanda > cap_aulas:
            print("ATENCION: Capacidad de aulas insuficiente.")
        else:
            print("OK: Capacidad de aulas suficiente.")
    else:
        print("Asignacion de aulas DESACTIVADA -> no se aplica chequeo de capacidad de aulas.")
    return demanda, cap_prof, cap_aulas, blocks


def _parse_H_from_env(s: str) -> Optional[List[str]]:
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        return [f"{h:02d}" for h in range(a, b + 1)]
    toks = [t.strip() for t in s.split(",") if t.strip()]
    out = []
    for t in toks:
        if not re.match(r"^\d{1,2}$", t):
            return None
        out.append(f"{int(t):02d}")
    return out or None


def _parse_D_from_env(s: str) -> Optional[List[str]]:
    s = (s or "").strip()
    if not s:
        return None
    toks = [t.strip().upper() for t in s.split(",") if t.strip()]
    return toks or None


def _calendar_path_from_prefix(prefix: Optional[str]) -> Optional[str]:
    if not prefix:
        return None
    return f"{prefix}_calendario.csv"


def _derive_phase2_calendar_path(export_prefix: Optional[str]) -> Optional[str]:
    if PHASE2_SOURCE_CALENDAR:
        return PHASE2_SOURCE_CALENDAR
    if PHASE2_SOURCE_PREFIX:
        return _calendar_path_from_prefix(PHASE2_SOURCE_PREFIX)
    return _calendar_path_from_prefix(export_prefix)


def _best_preferred_theory_rooms(m: str, g: str, data: dict, candidate_set: set) -> List[str]:
    if not PHASE2_ADD_PREFERRED_AT or not PREFERRED_AT_LIST:
        return []
    if _is_lab_course(m, data=data):
        return []
    cap_A = {str(k): int(v) for k, v in (data.get("cap_A") or {}).items()}
    size_G = {str(k): int(v) for k, v in (data.get("size_G") or {}).items()}
    A_tipo = {str(k): (str(v) if v is not None else "") for k, v in (data.get("A_tipo") or {}).items()}
    need = int(size_G.get(str(g), 0))

    allowed_pref = []
    for room in PREFERRED_AT_LIST:
        rr = str(room).upper()
        if rr not in candidate_set:
            continue
        if (A_tipo.get(rr) or "T").upper() != "T":
            continue
        cap = int(cap_A.get(rr, 0))
        if cap < max(int(need * 0.8), need - 5):
            continue
        allowed_pref.append((rr, abs(cap - need), cap))
    allowed_pref.sort(key=lambda t: (t[1], t[2], t[0]))
    return [room for room, _, _ in allowed_pref[:max(0, PHASE2_PREFERRED_AT_TOPK)]]


def _build_whitelist_from_calendar(calendar_csv: str, data: dict) -> Dict[str, List[str]]:
    if not calendar_csv or not os.path.isfile(calendar_csv):
        return {}
    cap_A = {str(k): int(v) for k, v in (data.get("cap_A") or {}).items()}
    size_G = {str(k): int(v) for k, v in (data.get("size_G") or {}).items()}
    counts: Dict[str, Counter] = defaultdict(Counter)

    with open(calendar_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            m = str(row.get("materia", "")).strip()
            g = str(row.get("grupo", "")).strip()
            a = str(row.get("aula", "")).strip().upper()
            if not m or not g or not a:
                continue
            counts[f"{m}|{g}"][a] += 1

    whitelist = {}
    summary_added_pref = 0
    for m, g in (data.get("MG") or []):
        mg = f"{m}|{g}"
        room_counter = counts.get(mg)
        candidate_list = _candidate_rooms_for(str(m), str(g), data)
        candidate_set = set(map(str, candidate_list))

        source_rooms: List[str] = []
        if room_counter:
            need = int(size_G.get(str(g), 0))
            ordered = []
            for room, uses in room_counter.most_common():
                if uses < PHASE2_MIN_ROOM_USES or room not in candidate_set:
                    continue
                cap = int(cap_A.get(room, 0))
                ordered.append((room, uses, cap))
            ordered.sort(key=lambda t: (-t[1], abs(t[2] - need), t[0]))
            source_rooms = [room for room, _, _ in ordered[:max(1, PHASE2_TOPK_ROOMS)]]

        preferred_rooms = _best_preferred_theory_rooms(str(m), str(g), data, candidate_set)

        final_rooms: List[str] = []
        if PHASE2_KEEP_SOURCE_ROOMS:
            for room in source_rooms:
                if room not in final_rooms:
                    final_rooms.append(room)
        for room in preferred_rooms:
            if room not in final_rooms:
                final_rooms.append(room)

        # respaldo si la Fase 2 no dejó nada
        if not final_rooms and candidate_list:
            final_rooms = candidate_list[: min(3, len(candidate_list))]

        if final_rooms:
            whitelist[mg] = final_rooms
            if preferred_rooms:
                summary_added_pref += 1

    whitelist["_phase2_stats"] = {"courses_with_preferred_augmented": summary_added_pref}
    return whitelist


def _merge_whitelist_rooms(data: dict, derived: Dict[str, List[str]]) -> bool:
    if not derived:
        return False
    derived = dict(derived)
    derived.pop("_phase2_stats", None)

    current = data.get("WhitelistRooms", {}) or {}
    merged = dict(current)
    for mg, rooms in derived.items():
        if mg in merged and merged[mg]:
            if PHASE2_STRICT_MERGE:
                merged[mg] = [str(r).upper() for r in rooms]
            else:
                base = [str(r).upper() for r in merged.get(mg, [])]
                for r in rooms:
                    rr = str(r).upper()
                    if rr not in base:
                        base.append(rr)
                merged[mg] = base
        else:
            merged[mg] = [str(r).upper() for r in rooms]
    data["WhitelistRooms"] = merged
    return True


def _inject_phase2_whitelist(data: dict, export_prefix: Optional[str]) -> bool:
    if not (ASSIGN_ROOMS and SINGLE_ROOM_PER_COURSE and PHASE2_FROM_BLOCK_SOLUTION):
        return False
    calendar_csv = _derive_phase2_calendar_path(export_prefix)
    if not calendar_csv or not os.path.isfile(calendar_csv):
        print("PREPROCESS FASE2: no se encontro calendario fuente para construir whitelist reducida.")
        return False
    derived_all = _build_whitelist_from_calendar(calendar_csv, data)
    stats = derived_all.pop("_phase2_stats", {}) if isinstance(derived_all, dict) else {}
    if not derived_all:
        print("PREPROCESS FASE2: no se pudo derivar ninguna whitelist desde el calendario fuente.")
        return False
    changed = _merge_whitelist_rooms(data, derived_all)
    if changed:
        title("PREPROCESS FASE2 - Whitelist reducida desde opcion 2")
        print(f"Calendario fuente : {calendar_csv}")
        print(f"Cursos con lista  : {len(derived_all)}")
        sizes = [len(v) for v in derived_all.values() if v]
        if sizes:
            print(f"Tam promedio      : {sum(sizes)/len(sizes):.2f}")
            print(f"Tam min/max       : {min(sizes)} / {max(sizes)}")
        if PHASE2_ADD_PREFERRED_AT:
            print(f"Cursos teoricos con FF agregada: {int(stats.get('courses_with_preferred_augmented', 0))}")
        for mg, rooms in list(derived_all.items())[:10]:
            print(f"  {mg} -> {rooms}")
    return changed


def _preprocess_json_if_needed(original_json_path: str, export_prefix: Optional[str] = None) -> str:
    if not PREPROCESS_JSON:
        return original_json_path
    try:
        data = load_json(original_json_path)
    except Exception as e:
        print(f"No se pudo abrir {original_json_path} para preprocesar: {e}")
        return original_json_path

    changed = False
    H_forced = _parse_H_from_env(FORCE_H_FROM_ENV)
    if H_forced:
        data["H"] = H_forced
        print(f"PREPROCESS: H forzado desde ENV -> {H_forced}")
        changed = True
    D_forced = _parse_D_from_env(FORCE_D_FROM_ENV)
    if D_forced:
        data["D"] = D_forced
        print(f"PREPROCESS: D forzado desde ENV -> {D_forced}")
        changed = True

    if FILL_A_TIPO_FROM_AT_AL:
        A_tipo = data.get("A_tipo", {}) or {}
        AT_list = [str(a).strip().upper() for a in (data.get("AT") or [])]
        AL_list = [str(a).strip().upper() for a in (data.get("AL") or [])]
        if AT_list or AL_list:
            before = len(A_tipo)
            for a in AT_list:
                if a and A_tipo.get(a) not in ("T", "L"):
                    A_tipo[a] = "T"
            for a in AL_list:
                if a and A_tipo.get(a) not in ("T", "L"):
                    A_tipo[a] = "L"
            data["A_tipo"] = A_tipo
            after = len(A_tipo)
            if after > before:
                print(f"PREPROCESS: A_tipo completado con AT/AL -> {before} -> {after} claves")
                changed = True

    if _inject_phase2_whitelist(data, export_prefix):
        changed = True

    if not changed:
        print("PREPROCESS: no hubo cambios sobre el JSON.")
        return original_json_path

    base = os.path.splitext(os.path.basename(original_json_path))[0]
    temp_path = f"salidas/_tmp_{base}.json"
    save_json(temp_path, data)
    print(f"PREPROCESS: JSON temporal escrito en: {temp_path}")
    return temp_path


def _read_csv_safe(path: str) -> Optional[List[List[str]]]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return [row for row in csv.reader(f)]


def _is_fresh_file(path: str, min_mtime: float) -> bool:
    try:
        return os.path.isfile(path) and os.path.getmtime(path) >= (min_mtime - 0.25)
    except Exception:
        return False


def _post_summary(export_prefix: Optional[str], min_mtime: Optional[float] = None, only_if_fresh: bool = False) -> None:
    if not POST_SUMMARY or not export_prefix:
        return
    title("POST-SUMMARY")
    cal_path = f"{export_prefix}_calendario.csv"
    soft_path = f"{export_prefix}_soft_violations.csv"
    over_r = f"{export_prefix}_postsolve_overlaps_rooms.csv"
    over_p = f"{export_prefix}_postsolve_overlaps_prof.csv"
    over_g = f"{export_prefix}_postsolve_overlaps_groups.csv"

    def fresh(path: str) -> bool:
        if min_mtime is None:
            return os.path.isfile(path)
        return _is_fresh_file(path, min_mtime)

    if only_if_fresh and not fresh(cal_path):
        print("No se encontro calendario nuevo en esta corrida.")
    else:
        cal = _read_csv_safe(cal_path) if fresh(cal_path) else None
        if cal and len(cal) > 1:
            hdr = cal[0]
            rows = cal[1:]

            def idx(name, default=-1):
                try:
                    return hdr.index(name)
                except ValueError:
                    return default

            i_hora = idx("hora")
            i_aula = idx("aula")
            i_prof = idx("profesor")
            print(f"Eventos calendarizados: {len(rows)}")

            if i_hora >= 0:
                cnt = {}
                for r in rows:
                    if len(r) > i_hora:
                        cnt[r[i_hora]] = cnt.get(r[i_hora], 0) + 1
                print("Uso por hora (asc):")
                for h, c in sorted(cnt.items(), key=lambda t: (int(re.sub(r"[^0-9]", "", t[0]) or 0), t[0])):
                    print(f"  {h}: {c}")

            if i_aula >= 0:
                cnta = {}
                for r in rows:
                    if len(r) > i_aula and r[i_aula]:
                        cnta[r[i_aula]] = cnta.get(r[i_aula], 0) + 1
                print("Top aulas (uso):")
                for a, c in sorted(cnta.items(), key=lambda kv: -kv[1])[:10]:
                    print(f"  {a}: {c}")

            if i_prof >= 0:
                cntp = {}
                for r in rows:
                    if len(r) > i_prof and r[i_prof]:
                        cntp[r[i_prof]] = cntp.get(r[i_prof], 0) + 1
                print("Top profesores (bloques):")
                for p, c in sorted(cntp.items(), key=lambda kv: -kv[1])[:10]:
                    print(f"  {p}: {c}")
        else:
            print("No se encontro calendario o esta vacio.")

    for label, pth in [("AULA", over_r), ("PROFESOR", over_p), ("GRUPO", over_g)]:
        if fresh(pth):
            print(f"Solapes de {label}: ver {pth}")
        else:
            print(f"Solapes de {label}: no detectados (archivo no generado).")

    if fresh(soft_path):
        print(f"Relajaciones usadas: ver {soft_path}")
    else:
        print("Relajaciones usadas: ninguna (archivo no generado).")


def _result_is_success(res: Any) -> bool:
    if isinstance(res, bool):
        return bool(res)
    if not isinstance(res, dict):
        return False
    status = str(res.get("status") or res.get("status_label") or "").strip().lower()
    if status in {"optimal", "feasible", "feasible (timelimit)", "success", "ok", "fallback_source_calendar"}:
        return True
    exportable = res.get("exportable")
    if isinstance(exportable, bool):
        return exportable
    has_exportable_solution = res.get("has_exportable_solution")
    if isinstance(has_exportable_solution, bool):
        return has_exportable_solution
    return False


def _result_status_text(res: Any) -> str:
    if isinstance(res, bool):
        return "EXITO" if res else "FALLO"
    if isinstance(res, dict):
        return str(res.get("status") or res.get("status_label") or "Desconocido")
    return str(type(res))


def _solve_single(json_path: str, export_prefix: Optional[str], inferred_period: Optional[str] = None):
    if export_prefix:
        export_prefix = _resolve_export_prefix(export_prefix, inferred_period, json_path)

    run_started = time.time()
    path_to_solve = _preprocess_json_if_needed(json_path, export_prefix=export_prefix)

    # PATCH CLAVE:
    # El runner ya aplicó Fase 2 sobre path_to_solve.
    # Desactivamos temporalmente PHASE2_FROM_BLOCK_SOLUTION para que model_solver.py
    # no vuelva a correr Fase 2 y no genere _tmp__tmp_...
    orig_phase2_env = os.environ.get("PHASE2_FROM_BLOCK_SOLUTION")
    solver_phase2_disabled = False
    if ASSIGN_ROOMS:
        os.environ["PHASE2_FROM_BLOCK_SOLUTION"] = "0"
        solver_phase2_disabled = True
        print("PATCH: PHASE2_FROM_BLOCK_SOLUTION=0 temporalmente dentro del solver (evita doble Fase 2).")

    try:
        try:
            data = load_json(path_to_solve)
        except Exception as e:
            title("ERROR")
            print(f"No se pudo abrir el JSON ({path_to_solve}) tras preprocesado: {e}")
            return

        _basic_validations(data)
        demanda, _, cap_aulas, _ = _theoretical_capacity_check(data)
        if ASSIGN_ROOMS and FAIL_EARLY_IF_ROOM_CAP_INSUFF and demanda > cap_aulas:
            title("ABORTANDO (FAIL_EARLY_IF_ROOM_CAP_INSUFF)")
            print("La capacidad de aulas es insuficiente y se pidio cortar temprano.")
            return

        preflight_solver_info()

        os.environ["SOLVER_TIME_LIMIT"] = str(TIME_LIMIT_SEC)
        os.environ["MODEL_INCLUDE_TIME"] = "1" if INCLUDE_TIME else "0"
        os.environ["ASSIGN_ROOMS"] = "1" if ASSIGN_ROOMS else "0"
        os.environ["SINGLE_ROOM_PER_COURSE"] = "1" if SINGLE_ROOM_PER_COURSE else "0"
        os.environ["MODEL_SOLVER"] = str(MODEL_SOLVER)
        os.environ["FRANJA_BAD_EARLY"] = FRANJA_BAD_EARLY
        os.environ["FRANJA_BAD_LATE"] = FRANJA_BAD_LATE
        os.environ["FRANJA_WEIGHT_EARLY"] = FRANJA_WEIGHT_EARLY
        os.environ["FRANJA_WEIGHT_LATE"] = FRANJA_WEIGHT_LATE
        if export_prefix:
            os.environ["EXPORT_PREFIX"] = export_prefix

        title(f"EJECUTANDO SOLVER - {inferred_period or 'SINGLE'}")
        print(f"JSON (solver): {path_to_solve}")
        print(f"Export prefix: {export_prefix}")
        try:
            solver_module = importlib.import_module(getattr(SOLVER_FUNCTION, "__module__", "")) if getattr(SOLVER_FUNCTION, "__module__", "") else None
            if solver_module is not None:
                print(f"Solver function module = {getattr(solver_module, '__file__', '(sin __file__)')}")
        except Exception:
            pass

        try:
            import inspect
            sig = inspect.signature(SOLVER_FUNCTION)
            if "json_path" in sig.parameters:
                print("Modo: solve_one con parametros")
                res = SOLVER_FUNCTION(json_path=path_to_solve, export_prefix=export_prefix, solver_name=MODEL_SOLVER)
            else:
                print("Modo: main con variables de entorno")
                original_json = os.getenv("DATOS_JSON")
                original_export = os.getenv("EXPORT_PREFIX")
                os.environ["DATOS_JSON"] = path_to_solve
                if export_prefix:
                    os.environ["EXPORT_PREFIX"] = export_prefix
                res = SOLVER_FUNCTION()
                if original_json is not None:
                    os.environ["DATOS_JSON"] = original_json
                if original_export is not None:
                    os.environ["EXPORT_PREFIX"] = original_export
        except Exception as e:
            print(f"ERROR ejecutando el solver: {e}")
            import traceback
            traceback.print_exc()
            return

        title("RESULTADOS DEL SOLVER")
        success = _result_is_success(res)
        if isinstance(res, bool):
            print(f"Resultado: {'EXITO' if res else 'FALLO'}")
        elif isinstance(res, dict):
            status = _result_status_text(res)
            obj = res.get("objective", res.get("obj", "N/A"))
            exportable = res.get("exportable", res.get("has_exportable_solution", "N/D"))
            print(f"Estado           : {status}")
            print(f"Valor objetivo   : {obj}")
            print(f"Exportable       : {exportable}")
        else:
            print(f"Tipo de retorno: {type(res)}")

        _post_summary(export_prefix, min_mtime=run_started, only_if_fresh=not success)

    finally:
        if solver_phase2_disabled:
            if orig_phase2_env is None:
                os.environ.pop("PHASE2_FROM_BLOCK_SOLUTION", None)
            else:
                os.environ["PHASE2_FROM_BLOCK_SOLUTION"] = orig_phase2_env


if __name__ == "__main__":
    if SOLVER_FUNCTION is None:
        print("ERROR: No se pudo cargar la funcion del solver.")
        sys.exit(1)

    if MULTI_PERIODS:
        title("=== Modo batch multi-periodo ===")
        print("MULTI_PERIODS a procesar:", MULTI_PERIODS)
        ok_builder = _run_builder_improved()
        if not ok_builder:
            jsons_existentes = _expand_json_inputs(JSON_PATH) or _list_possible_built_jsons(JSON_PATH)
            if not jsons_existentes:
                print("Abortando batch: no se pudo ejecutar el builder y no hay JSONs existentes.")
                sys.exit(1)
            title("AVISO: Usando JSONs existentes")
            print(f"No se pudo ejecutar el builder, pero se encontraron {len(jsons_existentes)} JSONs existentes.")
            jsons_to_process = []
            for jp in jsons_existentes:
                base = os.path.basename(jp)
                m = re.search(r"(20\d{2}[12])", base) or re.search(r"(20\d{2})", base)
                per = m.group(1) if m else "DESCONOCIDO"
                jsons_to_process.append((per, jp))
        else:
            jsons_to_process = []
            discovered = _expand_json_inputs(JSON_PATH) or _list_possible_built_jsons(JSON_PATH)
            for jp in discovered:
                base = os.path.basename(jp)
                m = re.search(r"(20\d{2}[12])", base) or re.search(r"(20\d{2})", base)
                per = m.group(1) if m else "DESCONOCIDO"
                jsons_to_process.append((per, jp))
        if not jsons_to_process:
            title("ATENCION: No se encontraron JSONs")
            print("No se encontraron JSONs generados tras el builder.")
            sys.exit(1)
        for per, jp in jsons_to_process:
            title(f"--- Periodo {per}: dataset ---")
            try:
                exp = _resolve_export_prefix(EXPORT_PREFIX, per, jp) if EXPORT_PREFIX else None
                _solve_single(jp, exp, per)
            except Exception as e:
                print(f"Error con {jp}: {e}")
        print("\nBATCH COMPLETADO")
        sys.exit(0)

    inferred_period: Optional[str] = None
    path_to_use = JSON_PATH
    expanded = _expand_json_inputs(JSON_PATH)
    if not expanded and "{periodo}" in (JSON_PATH or ""):
        expanded = _expand_json_inputs(JSON_PATH.replace("{periodo}", "*"))

    if expanded:
        try:
            expanded.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        except Exception:
            expanded = sorted(expanded)
        path_to_use = expanded[0]
        m = re.search(r"(20\d{2}[12])", os.path.basename(path_to_use)) or re.search(r"(20\d{2})", os.path.basename(path_to_use))
        inferred_period = m.group(1) if m else None
        print(f"[Single] Usare el JSON mas reciente: {path_to_use} (periodo≈{inferred_period or 'N/D'})")
    else:
        if "{periodo}" in (JSON_PATH or "") and not os.path.isfile(path_to_use):
            candidates = JSON_PATH.replace("{periodo}", "*")
            cand_list = sorted(glob.glob(candidates))
            if not cand_list:
                _print_inputs_help(JSON_PATH, periods_expected=["20241", "20242", "20251"])
                sys.exit(1)
            try:
                cand_list.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            except Exception:
                cand_list = sorted(cand_list)
            path_to_use = cand_list[0]
            m = re.search(r"(20\d{2}[12])", os.path.basename(path_to_use)) or re.search(r"(20\d{2})", os.path.basename(path_to_use))
            inferred_period = m.group(1) if m else None
            print(f"[Single] (fallback) Usare el JSON mas reciente: {path_to_use} (periodo≈{inferred_period or 'N/D'})")

    print(f"Usando JSON   : {path_to_use}")
    print(f"Valida/Resuelve -> RUN_SOLVER={int(RUN_SOLVER)} | INCLUDE_TIME={int(INCLUDE_TIME)} | SOLVER={MODEL_SOLVER}")

    if not RUN_SOLVER:
        try:
            data_tmp = load_json(_preprocess_json_if_needed(path_to_use))
            _basic_validations(data_tmp)
            _theoretical_capacity_check(data_tmp)
        except Exception as e:
            title("ERROR")
            print(f"No se pudo abrir el JSON: {e}")
        title("Modo validacion: no se llamo al solver")
        print("Para resolver: establece RUN_SOLVER=1 en .env")
        sys.exit(0)

    _solve_single(path_to_use, EXPORT_PREFIX, inferred_period=inferred_period)
    print("\nEJECUCION COMPLETADA EXITOSAMENTE")