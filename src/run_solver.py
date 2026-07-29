"""
Run Solver
==========

Publication-oriented command-line runner for the Academic Timetabling MILP
repository.

This module validates model-ready JSON instances, optionally preprocesses
instance data, and executes the MILP core solver implemented in
``src/core/milp_core.py``.

The runner does not access the institutional database directly. Input data are
expected to be provided as anonymized JSON files under ``data/samples/`` or as
user-configured JSON files through environment variables.

Main responsibilities
---------------------
- Load local configuration from ``.env`` when available.
- Locate one or more JSON input instances.
- Optionally run the instance generator when input JSON files are missing.
- Optionally preprocess time slots, day sets, room types, and reduced room
  whitelists.
- Run preventive consistency checks before optimization.
- Execute the MILP solver core.
- Print a concise post-solve summary from exported CSV files.

Environment variables
---------------------
DATA_JSON or DATOS_JSON:
    Path, directory, or wildcard pattern for JSON input instances.

EXPORT_PREFIX:
    Output file prefix. Default: ``outputs/example_run``.

MODEL_SOLVER:
    Solver backend name. Default: ``cplex``.

SOLVER_TIME_LIMIT:
    Solver time limit in seconds. Default: 600.

AUTO_BUILD_INSTANCE:
    If enabled, runs ``src/utils/instance_generator.py`` when no JSON input is
    found. Default: disabled.

Example
-------
python src/run_solver.py
"""

from __future__ import annotations

import csv
import glob
import importlib.util
import json
import os
import re
import runpy
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import find_dotenv, load_dotenv
except ImportError:  # pragma: no cover
    find_dotenv = None
    load_dotenv = None


# =============================================================================
# Project paths
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
CORE_DIR = SRC_DIR / "core"
UTILS_DIR = SRC_DIR / "utils"
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


# =============================================================================
# Environment helpers
# =============================================================================

TRUE_VALUES = {"1", "true", "True", "YES", "yes", "Y", "y"}
FALSE_VALUES = {"0", "false", "False", "NO", "no", "N", "n"}


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    """Parse a boolean-like value."""
    if value is None:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def _as_int(value: Optional[str], default: int) -> int:
    """Parse an integer value with fallback."""
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _clean_path(value: Optional[str]) -> str:
    """Normalize a path-like string without requiring the path to exist."""
    return (value or "").strip().strip('"').strip("'")


def _resolve_project_path(path_value: str) -> str:
    """Resolve relative paths against the repository root."""
    path = Path(path_value)
    if path.is_absolute():
        return str(path)
    return str((PROJECT_ROOT / path).resolve())


def _load_environment() -> Optional[str]:
    """
    Load configuration from a local ``.env`` file.

    The real ``.env`` file is intentionally not tracked by Git because it may
    contain local paths, database credentials, or solver-specific settings.
    """
    if load_dotenv is None:
        return None

    candidates: List[str] = []

    env_hint = _clean_path(os.getenv("ENV_FILE"))
    if env_hint:
        candidates.append(env_hint)

    project_hint = _clean_path(os.getenv("PROJECT_ROOT"))
    if project_hint:
        candidates.append(str(Path(project_hint) / ".env"))

    candidates.append(str(PROJECT_ROOT / ".env"))
    candidates.append(str(Path.cwd() / ".env"))

    if find_dotenv is not None:
        auto_env = find_dotenv(usecwd=True)
        if auto_env:
            candidates.append(auto_env)

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            load_dotenv(candidate, override=True)
            return candidate

    load_dotenv(override=True)
    return None


LOADED_ENV_PATH = _load_environment()


# =============================================================================
# Configuration
# =============================================================================

JSON_PATH = (
    os.getenv("DATA_JSON")
    or os.getenv("DATOS_JSON")
    or str(DATA_DIR / "samples" / "isc_20251_sample.json")
)

RUN_SOLVER = _as_bool(os.getenv("RUN_SOLVER"), True)
INCLUDE_TIME = _as_bool(os.getenv("MODEL_INCLUDE_TIME"), True)
TIME_LIMIT_SEC = _as_int(os.getenv("SOLVER_TIME_LIMIT"), 600)
MODEL_SOLVER = os.getenv("MODEL_SOLVER", "cplex")
EXPORT_PREFIX = os.getenv("EXPORT_PREFIX", str(OUTPUTS_DIR / "example_run"))
MULTI_PERIODS = (os.getenv("MULTI_PERIODS") or "").strip()

RAW_CPLEX_BIN = os.getenv("CPLEX_BIN", "") or ""
CPLEX_BIN = _clean_path(RAW_CPLEX_BIN)

AUTO_BUILD_INSTANCE = _as_bool(os.getenv("AUTO_BUILD_INSTANCE"), False)
INSTANCE_GENERATOR_PATH = _clean_path(
    os.getenv("INSTANCE_GENERATOR_PATH")
    or str(UTILS_DIR / "instance_generator.py")
)

FAST_MODE = _as_bool(os.getenv("FAST_MODE"), True)
RELAX_CONSTRAINTS = _as_bool(os.getenv("RELAX_CONSTRAINTS"), False)
AUTO_RELAX = _as_bool(os.getenv("AUTO_RELAX"), False)
AUTO_ULTRA_RELAX = _as_bool(os.getenv("AUTO_ULTRA_RELAX"), False)
ASSIGN_ROOMS = _as_bool(os.getenv("ASSIGN_ROOMS"), True)
SINGLE_ROOM_PER_COURSE = _as_bool(os.getenv("SINGLE_ROOM_PER_COURSE"), True)

LAB_COURSE_REGEX = os.getenv(
    "LAB_COURSE_REGEX",
    r"(?i)\b(LAB|LABORATORY|WORKSHOP|PRACTICE|PRACTICAL|MANUFACTURING|CAD|CIM)\b",
)
SEM1_GROUP_REGEX = os.getenv(
    "SEM1_GROUP_REGEX",
    r"(?i)^(1([A-Z]|$)|.*(^|[^0-9])1([^0-9]|$))",
)

ALLOW_THEORY_IN_LABS = _as_bool(os.getenv("ALLOW_THEORY_IN_LABS"), False)
MAX_ROOMS_PER_GROUP = _as_int(os.getenv("MAX_ROOMS_PER_GROUP"), 10)
MAX_PROF_PER_COURSE = _as_int(os.getenv("MAX_PROF_PER_COURSE"), 8)

STRICT_ROOM_SET = _as_bool(os.getenv("STRICT_ROOM_SET"), False)
REQUIRE_CAPACITY_FOR_ROOM = _as_bool(os.getenv("REQUIRE_CAPACITY_FOR_ROOM"), True)
BYPASS_PREFLIGHT = _as_bool(os.getenv("BYPASS_PREFLIGHT"), False)

PREPROCESS_JSON = _as_bool(os.getenv("PREPROCESS_JSON"), True)
FORCE_H_FROM_ENV = (os.getenv("FORCE_H_FROM_ENV") or "").strip()
FORCE_D_FROM_ENV = (os.getenv("FORCE_D_FROM_ENV") or "").strip()
FILL_A_TIPO_FROM_AT_AL = _as_bool(os.getenv("FILL_A_TIPO_FROM_AT_AL"), True)
POST_SUMMARY = _as_bool(os.getenv("POST_SUMMARY"), True)
FAIL_EARLY_IF_ROOM_CAP_INSUFF = _as_bool(
    os.getenv("FAIL_EARLY_IF_ROOM_CAP_INSUFF"), False
)

PHASE2_FROM_BLOCK_SOLUTION = _as_bool(os.getenv("PHASE2_FROM_BLOCK_SOLUTION"), False)
PHASE2_SOURCE_PREFIX = (os.getenv("PHASE2_SOURCE_PREFIX") or "").strip()
PHASE2_SOURCE_CALENDAR = (os.getenv("PHASE2_SOURCE_CALENDAR") or "").strip()
PHASE2_TOPK_ROOMS = _as_int(os.getenv("PHASE2_TOPK_ROOMS"), 3)
PHASE2_MIN_ROOM_USES = _as_int(os.getenv("PHASE2_MIN_ROOM_USES"), 1)
PHASE2_STRICT_MERGE = _as_bool(os.getenv("PHASE2_STRICT_MERGE"), False)

PREFERRED_AT_LIST = [
    item.strip().upper()
    for item in (os.getenv("PREFERRED_AT_LIST", "")).replace(";", ",").split(",")
    if item.strip()
]
PHASE2_ADD_PREFERRED_AT = _as_bool(os.getenv("PHASE2_ADD_PREFERRED_AT"), True)
PHASE2_PREFERRED_AT_TOPK = _as_int(os.getenv("PHASE2_PREFERRED_AT_TOPK"), 2)
PHASE2_KEEP_SOURCE_ROOMS = _as_bool(os.getenv("PHASE2_KEEP_SOURCE_ROOMS"), True)

TIME_PENALTY_EARLY_SLOTS = os.getenv("FRANJA_BAD_EARLY", "07,08")
TIME_PENALTY_LATE_SLOTS = os.getenv("FRANJA_BAD_LATE", "19,20")
TIME_PENALTY_EARLY_WEIGHT = os.getenv("FRANJA_WEIGHT_EARLY", "50")
TIME_PENALTY_LATE_WEIGHT = os.getenv("FRANJA_WEIGHT_LATE", "80")


# =============================================================================
# Basic I/O utilities
# =============================================================================


def title(text: str) -> None:
    """Print a section title."""
    print("\n" + "=" * len(text))
    print(text)
    print("=" * len(text))


def load_json(path: str) -> Dict[str, Any]:
    """Load a UTF-8 JSON file."""
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path: str, data: Dict[str, Any]) -> None:
    """Save a UTF-8 JSON file."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def _read_csv_safe(path: str) -> Optional[List[List[str]]]:
    """Read a CSV file and return rows, or None when unavailable."""
    if not Path(path).is_file():
        return None
    with open(path, "r", encoding="utf-8") as file:
        return [row for row in csv.reader(file)]


# =============================================================================
# Solver and generator loading
# =============================================================================


def _import_solver_function():
    """
    Import the MILP solver entry point from ``src/core/milp_core.py``.

    Returns
    -------
    Callable or None
        The ``solve_one`` function when available.
    """
    core_path = CORE_DIR / "milp_core.py"

    if not core_path.is_file():
        print("ERROR: MILP core module was not found.")
        print(f"Expected path: {core_path}")
        return None

    try:
        spec = importlib.util.spec_from_file_location("milp_core_runtime", str(core_path))
        if spec is None or spec.loader is None:
            raise ImportError("Could not create an import specification for MILP core.")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover
        print("ERROR: Could not import the MILP core module.")
        print(f"Details: {exc}")
        return None

    if hasattr(module, "solve_one"):
        print(f"MILP core loaded from: {core_path}")
        print("Using solve_one() from MILP core.")
        return module.solve_one

    if hasattr(module, "main"):
        print(f"MILP core loaded from: {core_path}")
        print("Using main() from MILP core.")
        return module.main

    print("ERROR: MILP core does not expose solve_one() or main().")
    return None


SOLVER_FUNCTION = _import_solver_function()

if SOLVER_FUNCTION is None:
    raise SystemExit(1)


def _run_instance_generator() -> bool:
    """
    Run the configured instance generator when automatic generation is enabled.

    Returns
    -------
    bool
        True when the generator finishes successfully, False otherwise.
    """
    generator_path = Path(_resolve_project_path(INSTANCE_GENERATOR_PATH))

    if not generator_path.is_file():
        print("ERROR: instance generator was not found.")
        print(f"Configured path: {generator_path}")
        return False

    title("Running instance generator")
    print(f"Generator path: {generator_path}")

    try:
        runpy.run_path(str(generator_path), run_name="__main__")
        print("Instance generator completed successfully.")
        return True
    except Exception as runpy_error:
        print(f"Generator execution with runpy failed: {runpy_error}")

    try:
        result = subprocess.run(
            [sys.executable or "python", str(generator_path)],
            capture_output=True,
            text=True,
            cwd=str(generator_path.parent),
            check=False,
        )
        if result.returncode == 0:
            print("Instance generator completed successfully using subprocess.")
            if result.stdout and result.stdout.strip():
                print(result.stdout.strip())
            return True

        print("Instance generator failed using subprocess.")
        if result.stderr and result.stderr.strip():
            print(result.stderr.strip())
        return False
    except Exception as subprocess_error:
        print(f"Generator execution with subprocess failed: {subprocess_error}")
        return False


# =============================================================================
# Preflight and validation helpers
# =============================================================================


def preflight_solver_info() -> None:
    """Print solver and configuration information before optimization."""
    title("Solver preflight")

    if LOADED_ENV_PATH:
        print(f"Loaded environment file : {LOADED_ENV_PATH}")

    print(f"MODEL_SOLVER            : {MODEL_SOLVER}")
    print(f"TIME_LIMIT_SEC          : {TIME_LIMIT_SEC}")
    print(f"MODEL_INCLUDE_TIME      : {int(INCLUDE_TIME)}")
    print(f"CPLEX_BIN raw           : {RAW_CPLEX_BIN!r}")
    print(f"CPLEX_BIN normalized    : {CPLEX_BIN!r}")

    cplex_is_file = Path(CPLEX_BIN).is_file() if CPLEX_BIN else False
    cplex_is_dir = Path(CPLEX_BIN).is_dir() if CPLEX_BIN else False

    print(f"CPLEX_BIN is file       : {cplex_is_file}")
    print(f"CPLEX_BIN is directory  : {cplex_is_dir}")

    has_cplex_python_api = importlib.util.find_spec("cplex") is not None
    print(f"CPLEX Python API found  : {has_cplex_python_api}")

    print("\nModel options")
    print(f"STRICT_ROOM_SET              : {int(STRICT_ROOM_SET)}")
    print(f"REQUIRE_CAPACITY_FOR_ROOM    : {int(REQUIRE_CAPACITY_FOR_ROOM)}")
    print(f"BYPASS_PREFLIGHT             : {int(BYPASS_PREFLIGHT)}")
    print(f"ALLOW_THEORY_IN_LABS         : {int(ALLOW_THEORY_IN_LABS)}")
    print(f"MAX_ROOMS_PER_GROUP          : {MAX_ROOMS_PER_GROUP}")
    print(f"MAX_PROF_PER_COURSE          : {MAX_PROF_PER_COURSE}")
    print(f"FAST_MODE                    : {int(FAST_MODE)}")
    print(f"RELAX_CONSTRAINTS            : {int(RELAX_CONSTRAINTS)}")
    print(f"AUTO_RELAX                   : {int(AUTO_RELAX)}")
    print(f"AUTO_ULTRA_RELAX             : {int(AUTO_ULTRA_RELAX)}")
    print(f"ASSIGN_ROOMS                 : {int(ASSIGN_ROOMS)}")
    print(f"SINGLE_ROOM_PER_COURSE       : {int(SINGLE_ROOM_PER_COURSE)}")

    print("\nClassification patterns")
    print(f"LAB_COURSE_REGEX             : {LAB_COURSE_REGEX}")
    print(f"SEM1_GROUP_REGEX             : {SEM1_GROUP_REGEX}")

    if ASSIGN_ROOMS and SINGLE_ROOM_PER_COURSE:
        print("\nPhase-2 room whitelist options")
        print(f"PHASE2_FROM_BLOCK_SOLUTION   : {int(PHASE2_FROM_BLOCK_SOLUTION)}")
        print(f"PHASE2_TOPK_ROOMS            : {PHASE2_TOPK_ROOMS}")
        print(f"PHASE2_MIN_ROOM_USES         : {PHASE2_MIN_ROOM_USES}")
        print(f"PHASE2_SOURCE_PREFIX         : {PHASE2_SOURCE_PREFIX or '(auto)'}")
        print(f"PHASE2_SOURCE_CALENDAR       : {PHASE2_SOURCE_CALENDAR or '(auto)'}")
        print(f"PHASE2_STRICT_MERGE          : {int(PHASE2_STRICT_MERGE)}")
        print(f"PHASE2_ADD_PREFERRED_AT      : {int(PHASE2_ADD_PREFERRED_AT)}")
        print(f"PHASE2_PREFERRED_AT_TOPK     : {PHASE2_PREFERRED_AT_TOPK}")
        print(f"PHASE2_KEEP_SOURCE_ROOMS     : {int(PHASE2_KEEP_SOURCE_ROOMS)}")
        print(
            "PREFERRED_AT_LIST            : "
            + (", ".join(PREFERRED_AT_LIST) if PREFERRED_AT_LIST else "(empty)")
        )


def _compile_regex(pattern: str, default_pattern: str) -> re.Pattern:
    """Compile a regex pattern and fall back to a default when invalid."""
    raw_pattern = pattern or default_pattern
    try:
        return re.compile(raw_pattern)
    except re.error:
        print(f"WARNING: invalid regex pattern. Using default: {default_pattern}")
        return re.compile(default_pattern)


LAB_RE = _compile_regex(
    LAB_COURSE_REGEX,
    r"(?i)\b(LAB|LABORATORY|WORKSHOP|PRACTICE|PRACTICAL)\b",
)
SEM1_RE = _compile_regex(
    SEM1_GROUP_REGEX,
    r"(?i)^(1([A-Z]|$)|.*(^|[^0-9])1([^0-9]|$))",
)


def _normalize_code(value: Any) -> str:
    """Normalize institutional codes as uppercase strings."""
    return str(value or "").strip().upper()


def _course_maps(data: Dict[str, Any]) -> Tuple[Dict[str, bool], Dict[str, str]]:
    """Return course laboratory flags and descriptive text maps."""
    course_is_lab = {
        _normalize_code(key): bool(value)
        for key, value in (data.get("M_is_lab", {}) or {}).items()
    }
    course_text = {
        _normalize_code(key): str(value or "")
        for key, value in (data.get("M_text", {}) or {}).items()
    }
    return course_is_lab, course_text


def _is_lab_course(
    course: str,
    data: Optional[Dict[str, Any]] = None,
    course_is_lab: Optional[Dict[str, bool]] = None,
    course_text: Optional[Dict[str, str]] = None,
) -> bool:
    """Return True when a course is classified as laboratory/practical."""
    key = _normalize_code(course)

    if course_is_lab is None or course_text is None:
        if data is not None:
            course_is_lab, course_text = _course_maps(data)
        else:
            course_is_lab, course_text = {}, {}

    if bool(course_is_lab.get(key, False)):
        return True

    text = str(course_text.get(key, "") or "")
    if text and LAB_RE.search(text):
        return True

    return bool(LAB_RE.search(str(course)))


def _is_first_semester_group(group: str) -> bool:
    """Return True when a group matches the configured first-semester pattern."""
    return bool(SEM1_RE.search(str(group)))


def _candidate_rooms_for(course: str, group: str, data: Dict[str, Any]) -> List[str]:
    """Build a candidate room list for one course-group pair."""
    rooms = [str(room) for room in (data.get("A") or [])]
    room_capacity = {
        str(key): int(value) for key, value in (data.get("cap_A") or {}).items()
    }
    group_size = {
        str(key): int(value) for key, value in (data.get("size_G") or {}).items()
    }
    room_type = {
        str(key): (str(value) if value is not None else "")
        for key, value in (data.get("A_tipo") or {}).items()
    }
    extra_lab_rooms = set(str(item) for item in (data.get("AL_sem1_extras") or []))
    course_is_lab, course_text = _course_maps(data)
    required_capacity = int(group_size.get(str(group), 0))

    def capacity_ok(room: str) -> bool:
        capacity = int(room_capacity.get(str(room), 0))
        if REQUIRE_CAPACITY_FOR_ROOM:
            return capacity >= required_capacity
        return capacity >= max(int(required_capacity * 0.8), required_capacity - 5)

    def type_of(room: str) -> str:
        return (room_type.get(str(room)) or "").upper() or "T"

    is_lab = _is_lab_course(
        course, course_is_lab=course_is_lab, course_text=course_text
    )
    is_first_semester = _is_first_semester_group(group)

    if is_lab:
        feasible = [
            room for room in rooms if type_of(room) == "L" and capacity_ok(room)
        ]
        if not feasible and ALLOW_THEORY_IN_LABS:
            feasible = [
                room for room in rooms if type_of(room) == "T" and capacity_ok(room)
            ]
    else:
        feasible_theory = [
            room for room in rooms if type_of(room) == "T" and capacity_ok(room)
        ]
        feasible = list(feasible_theory)

        if is_first_semester:
            feasible_extra = [
                room
                for room in rooms
                if str(room) in extra_lab_rooms and capacity_ok(room)
            ]
            for room in feasible_extra:
                if room not in feasible:
                    feasible.append(room)

        if ALLOW_THEORY_IN_LABS:
            feasible_lab = [
                room for room in rooms if type_of(room) == "L" and capacity_ok(room)
            ]
            for room in feasible_lab:
                if room not in feasible:
                    feasible.append(room)

    if not feasible:
        feasible = [room for room in rooms if capacity_ok(room)]

    if not feasible:
        feasible = sorted(
            rooms,
            key=lambda room: int(room_capacity.get(str(room), 0)),
            reverse=True,
        )[:10]

    feasible.sort(
        key=lambda room: int(room_capacity.get(str(room), 10**9)) - required_capacity
    )

    if MAX_ROOMS_PER_GROUP > 0 and len(feasible) > MAX_ROOMS_PER_GROUP:
        feasible = feasible[:MAX_ROOMS_PER_GROUP]

    return feasible


def _basic_validations(data: Dict[str, Any]) -> None:
    """Run minimum structural validations for a model-ready JSON instance."""
    title("Minimum instance validations")

    room_capacity = data.get("cap_A", {}) or {}
    group_size = data.get("size_G", {}) or {}
    required_sessions = data.get("Hreq", {}) or {}

    course_group_pairs = data.get("MG", []) or []
    groups = data.get("G", []) or []
    rooms = data.get("A", []) or []
    courses = data.get("M", []) or []
    days = data.get("D", []) or []
    hours = data.get("H", []) or []
    teachers = data.get("P", []) or []

    print(
        f"|P|={len(teachers)} |A|={len(rooms)} |D|={len(days)} "
        f"|H|={len(hours)} |M|={len(courses)} |G|={len(groups)} "
        f"|MG|={len(course_group_pairs)}"
    )

    mg_keys = {f"{course}|{group}" for course, group in course_group_pairs}
    missing_hreq = mg_keys - set(required_sessions.keys())

    if not missing_hreq:
        print("Hreq covers all course-group pairs.")
    else:
        print(
            f"WARNING: missing Hreq values for {len(missing_hreq)} "
            f"course-group pairs. Examples: {sorted(list(missing_hreq))[:10]}"
        )

    groups_without_capacity = []
    for group in groups:
        required_capacity = int(str(group_size.get(str(group), 0)))
        if required_capacity and not any(
            int(str(room_capacity.get(str(room), 0)))
            >= max(int(required_capacity * 0.8), required_capacity - 5)
            for room in rooms
        ):
            groups_without_capacity.append(group)

    if not groups_without_capacity:
        print("All groups have at least one globally capacity-feasible room.")
    else:
        print(
            f"WARNING: {len(groups_without_capacity)} groups have no globally "
            f"capacity-feasible room. Examples: {groups_without_capacity[:5]}"
        )

    title("Room-type and candidate-room validation")

    room_type = data.get("A_tipo", {}) or {}
    extra_lab_rooms = data.get("AL_sem1_extras", []) or []
    course_is_lab, course_text = _course_maps(data)

    print(f"Rooms with declared type : {len(room_type)} / {len(rooms)}")
    if not room_type:
        print("WARNING: A_tipo was not found. Room type defaults may be used.")

    print(f"Additional first-semester lab rooms : {len(extra_lab_rooms)}")
    print(f"Laboratory regex                   : {LAB_RE.pattern}")
    print(f"First-semester group regex         : {SEM1_RE.pattern}")
    print(f"ALLOW_THEORY_IN_LABS               : {int(ALLOW_THEORY_IN_LABS)}")
    print(f"MAX_ROOMS_PER_GROUP                : {MAX_ROOMS_PER_GROUP}")
    print(
        "Current configuration              : "
        f"STRICT_ROOM_SET={int(STRICT_ROOM_SET)}, "
        f"REQUIRE_CAPACITY_FOR_ROOM={int(REQUIRE_CAPACITY_FOR_ROOM)}"
    )

    pairs_without_candidates = []
    lab_count = 0
    theory_count = 0

    for course, group in course_group_pairs:
        if _is_lab_course(course, course_is_lab=course_is_lab, course_text=course_text):
            lab_count += 1
        else:
            theory_count += 1

        feasible_rooms = _candidate_rooms_for(str(course), str(group), data)
        if not feasible_rooms:
            required_capacity = int(group_size.get(str(group), 0))
            pairs_without_candidates.append((str(course), str(group), required_capacity))

    print(f"Course totals: laboratory={lab_count} | theory={theory_count}")

    if not pairs_without_candidates:
        print("OK: all course-group pairs have at least one candidate room.")
        return

    print(
        f"WARNING: {len(pairs_without_candidates)} course-group pairs have no "
        "candidate room under current type/capacity rules."
    )

    for index, (course, group, required_capacity) in enumerate(
        pairs_without_candidates[:20], start=1
    ):
        print(
            f"  {index:02d}) course={course}, group={group}, "
            f"size={required_capacity} -> 0 candidates"
        )

    if len(pairs_without_candidates) > 20:
        print("  ...")


def _theoretical_capacity_check(data: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Check aggregate weekly demand against teacher and room capacities."""
    hours = data.get("H", []) or []
    days = data.get("D", []) or []
    rooms = data.get("A", []) or []
    teachers = data.get("P", []) or []
    required_sessions = data.get("Hreq", {}) or {}
    course_group_pairs = data.get("MG", []) or []

    weekly_blocks = len(hours) * len(days)
    total_demand = sum(
        int(required_sessions.get(f"{course}|{group}", 0))
        for course, group in course_group_pairs
    )
    teacher_capacity = len(teachers) * weekly_blocks
    room_capacity = len(set(rooms)) * weekly_blocks if ASSIGN_ROOMS else 10**9

    title("Aggregate theoretical capacity check")
    print(f"Hours={len(hours)} Days={len(days)} -> weekly_blocks={weekly_blocks}")
    print(f"Total demand (sum Hreq)       : {total_demand}")
    print(f"Teacher capacity              : {teacher_capacity}")

    if ASSIGN_ROOMS:
        print(f"Room capacity                 : {room_capacity}")
        if total_demand > room_capacity:
            print("WARNING: aggregate room capacity is insufficient.")
        else:
            print("OK: aggregate room capacity is sufficient.")
    else:
        print("Room assignment is disabled; room capacity check is not applied.")

    return total_demand, teacher_capacity, room_capacity, weekly_blocks


# =============================================================================
# Preprocessing helpers
# =============================================================================


def _parse_hours_from_env(value: str) -> Optional[List[str]]:
    """Parse forced hour slots from environment configuration."""
    value = (value or "").strip()
    if not value:
        return None

    range_match = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", value)
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        if start > end:
            start, end = end, start
        return [f"{hour:02d}" for hour in range(start, end + 1)]

    tokens = [token.strip() for token in value.split(",") if token.strip()]
    hours = []
    for token in tokens:
        if not re.match(r"^\d{1,2}$", token):
            return None
        hours.append(f"{int(token):02d}")

    return hours or None


def _parse_days_from_env(value: str) -> Optional[List[str]]:
    """Parse forced day labels from environment configuration."""
    value = (value or "").strip()
    if not value:
        return None
    return [token.strip().upper() for token in value.split(",") if token.strip()]


def _calendar_path_from_prefix(prefix: Optional[str]) -> Optional[str]:
    """Return the timetable CSV path derived from an export prefix."""
    if not prefix:
        return None
    return f"{prefix}_timetable.csv"


def _derive_phase2_calendar_path(export_prefix: Optional[str]) -> Optional[str]:
    """Resolve the source timetable path for phase-2 room whitelisting."""
    if PHASE2_SOURCE_CALENDAR:
        return _resolve_project_path(PHASE2_SOURCE_CALENDAR)
    if PHASE2_SOURCE_PREFIX:
        return _calendar_path_from_prefix(_resolve_project_path(PHASE2_SOURCE_PREFIX))
    return _calendar_path_from_prefix(export_prefix)


def _best_preferred_theory_rooms(
    course: str,
    group: str,
    data: Dict[str, Any],
    candidate_set: set,
) -> List[str]:
    """Select preferred theory rooms compatible with capacity and room type."""
    if not PHASE2_ADD_PREFERRED_AT or not PREFERRED_AT_LIST:
        return []
    if _is_lab_course(course, data=data):
        return []

    room_capacity = {
        str(key): int(value) for key, value in (data.get("cap_A") or {}).items()
    }
    group_size = {
        str(key): int(value) for key, value in (data.get("size_G") or {}).items()
    }
    room_type = {
        str(key): (str(value) if value is not None else "")
        for key, value in (data.get("A_tipo") or {}).items()
    }

    required_capacity = int(group_size.get(str(group), 0))
    compatible = []

    for room in PREFERRED_AT_LIST:
        normalized_room = str(room).upper()
        if normalized_room not in candidate_set:
            continue
        if (room_type.get(normalized_room) or "T").upper() != "T":
            continue
        capacity = int(room_capacity.get(normalized_room, 0))
        if capacity < max(int(required_capacity * 0.8), required_capacity - 5):
            continue
        compatible.append((normalized_room, abs(capacity - required_capacity), capacity))

    compatible.sort(key=lambda item: (item[1], item[2], item[0]))
    return [room for room, _, _ in compatible[: max(0, PHASE2_PREFERRED_AT_TOPK)]]


def _row_value(row: Dict[str, str], *names: str) -> str:
    """Read the first available non-empty value from a CSV row."""
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _build_whitelist_from_calendar(
    calendar_csv: str,
    data: Dict[str, Any],
) -> Dict[str, List[str]]:
    """Build reduced room whitelists from a previous timetable CSV."""
    if not calendar_csv or not Path(calendar_csv).is_file():
        return {}

    room_capacity = {
        str(key): int(value) for key, value in (data.get("cap_A") or {}).items()
    }
    group_size = {
        str(key): int(value) for key, value in (data.get("size_G") or {}).items()
    }
    counts: Dict[str, Counter] = defaultdict(Counter)

    with open(calendar_csv, "r", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            course = _row_value(row, "course", "materia")
            group = _row_value(row, "group", "grupo")
            room = _row_value(row, "room", "aula").upper()

            if not course or not group or not room:
                continue
            counts[f"{course}|{group}"][room] += 1

    whitelist: Dict[str, List[str]] = {}
    courses_with_preferred = 0

    for course, group in (data.get("MG") or []):
        pair_key = f"{course}|{group}"
        room_counter = counts.get(pair_key)
        candidate_list = _candidate_rooms_for(str(course), str(group), data)
        candidate_set = set(map(str, candidate_list))

        source_rooms: List[str] = []
        if room_counter:
            required_capacity = int(group_size.get(str(group), 0))
            ordered = []
            for room, uses in room_counter.most_common():
                if uses < PHASE2_MIN_ROOM_USES or room not in candidate_set:
                    continue
                capacity = int(room_capacity.get(room, 0))
                ordered.append((room, uses, capacity))
            ordered.sort(key=lambda item: (-item[1], abs(item[2] - required_capacity), item[0]))
            source_rooms = [room for room, _, _ in ordered[: max(1, PHASE2_TOPK_ROOMS)]]

        preferred_rooms = _best_preferred_theory_rooms(
            str(course), str(group), data, candidate_set
        )

        final_rooms: List[str] = []
        if PHASE2_KEEP_SOURCE_ROOMS:
            for room in source_rooms:
                if room not in final_rooms:
                    final_rooms.append(room)

        for room in preferred_rooms:
            if room not in final_rooms:
                final_rooms.append(room)

        if not final_rooms and candidate_list:
            final_rooms = candidate_list[: min(3, len(candidate_list))]

        if final_rooms:
            whitelist[pair_key] = final_rooms
            if preferred_rooms:
                courses_with_preferred += 1

    whitelist["_phase2_stats"] = {
        "courses_with_preferred_augmented": courses_with_preferred
    }
    return whitelist


def _merge_whitelist_rooms(data: Dict[str, Any], derived: Dict[str, List[str]]) -> bool:
    """Merge a derived room whitelist into the input data."""
    if not derived:
        return False

    derived = dict(derived)
    derived.pop("_phase2_stats", None)

    current = data.get("WhitelistRooms", {}) or {}
    merged = dict(current)

    for pair_key, rooms in derived.items():
        if pair_key in merged and merged[pair_key]:
            if PHASE2_STRICT_MERGE:
                merged[pair_key] = [str(room).upper() for room in rooms]
            else:
                base = [str(room).upper() for room in merged.get(pair_key, [])]
                for room in rooms:
                    normalized_room = str(room).upper()
                    if normalized_room not in base:
                        base.append(normalized_room)
                merged[pair_key] = base
        else:
            merged[pair_key] = [str(room).upper() for room in rooms]

    data["WhitelistRooms"] = merged
    return True


def _inject_phase2_whitelist(data: Dict[str, Any], export_prefix: Optional[str]) -> bool:
    """Inject a reduced room whitelist based on a previous timetable."""
    if not (ASSIGN_ROOMS and SINGLE_ROOM_PER_COURSE and PHASE2_FROM_BLOCK_SOLUTION):
        return False

    calendar_csv = _derive_phase2_calendar_path(export_prefix)
    if not calendar_csv or not Path(calendar_csv).is_file():
        print("PREPROCESS PHASE 2: source timetable was not found.")
        return False

    derived_all = _build_whitelist_from_calendar(calendar_csv, data)
    stats = derived_all.pop("_phase2_stats", {}) if isinstance(derived_all, dict) else {}

    if not derived_all:
        print("PREPROCESS PHASE 2: no room whitelist could be derived.")
        return False

    changed = _merge_whitelist_rooms(data, derived_all)

    if changed:
        title("PREPROCESS PHASE 2 - Reduced room whitelist")
        print(f"Source timetable        : {calendar_csv}")
        print(f"Courses with whitelist  : {len(derived_all)}")

        sizes = [len(value) for value in derived_all.values() if value]
        if sizes:
            print(f"Average whitelist size  : {sum(sizes) / len(sizes):.2f}")
            print(f"Min/max whitelist size  : {min(sizes)} / {max(sizes)}")

        if PHASE2_ADD_PREFERRED_AT:
            print(
                "Theory courses augmented with preferred rooms : "
                f"{int(stats.get('courses_with_preferred_augmented', 0))}"
            )

        for pair_key, rooms in list(derived_all.items())[:10]:
            print(f"  {pair_key} -> {rooms}")

    return changed


def _preprocess_json_if_needed(
    original_json_path: str,
    export_prefix: Optional[str] = None,
) -> str:
    """Apply optional preprocessing and return the JSON path to be solved."""
    if not PREPROCESS_JSON:
        return original_json_path

    try:
        data = load_json(original_json_path)
    except Exception as exc:
        print(f"Could not open {original_json_path} for preprocessing: {exc}")
        return original_json_path

    changed = False

    forced_hours = _parse_hours_from_env(FORCE_H_FROM_ENV)
    if forced_hours:
        data["H"] = forced_hours
        print(f"PREPROCESS: forced H from environment -> {forced_hours}")
        changed = True

    forced_days = _parse_days_from_env(FORCE_D_FROM_ENV)
    if forced_days:
        data["D"] = forced_days
        print(f"PREPROCESS: forced D from environment -> {forced_days}")
        changed = True

    if FILL_A_TIPO_FROM_AT_AL:
        room_type = data.get("A_tipo", {}) or {}
        theory_rooms = [str(room).strip().upper() for room in (data.get("AT") or [])]
        lab_rooms = [str(room).strip().upper() for room in (data.get("AL") or [])]

        if theory_rooms or lab_rooms:
            before = len(room_type)
            for room in theory_rooms:
                if room and room_type.get(room) not in ("T", "L"):
                    room_type[room] = "T"
            for room in lab_rooms:
                if room and room_type.get(room) not in ("T", "L"):
                    room_type[room] = "L"
            data["A_tipo"] = room_type
            after = len(room_type)
            if after > before:
                print(f"PREPROCESS: A_tipo completed from AT/AL -> {before} -> {after}")
                changed = True

    if _inject_phase2_whitelist(data, export_prefix):
        changed = True

    if not changed:
        print("PREPROCESS: no changes were applied to the JSON instance.")
        return original_json_path

    base_name = Path(original_json_path).stem
    temp_path = OUTPUTS_DIR / f"_tmp_{base_name}.json"
    save_json(str(temp_path), data)
    print(f"PREPROCESS: temporary JSON written to: {temp_path}")
    return str(temp_path)


# =============================================================================
# Input discovery and output summary
# =============================================================================


def _resolve_export_prefix(
    export_prefix: Optional[str],
    period: Optional[str],
    json_path: Optional[str] = None,
) -> Optional[str]:
    """Resolve export-prefix templates using base name and period."""
    if not export_prefix:
        return None

    resolved = str(export_prefix)
    base = Path(json_path).stem if json_path else "output"

    if "{base}" in resolved:
        resolved = resolved.replace("{base}", base)

    if period:
        if "{period}" in resolved:
            resolved = resolved.replace("{period}", period)
        elif not resolved.endswith(f"_{period}"):
            resolved = f"{resolved}_{period}"

    return _resolve_project_path(resolved)


def _expand_json_inputs(path_pattern: str) -> List[str]:
    """Expand a JSON path, directory, or wildcard pattern into file paths."""
    if not path_pattern:
        return []

    pattern = _resolve_project_path(path_pattern.strip())

    if Path(pattern).is_dir():
        return sorted(str(path) for path in Path(pattern).glob("*.json"))

    if any(symbol in pattern for symbol in ["*", "?", "["]):
        return sorted(glob.glob(pattern))

    if Path(pattern).is_file():
        return [pattern]

    return []


def _list_possible_built_jsons(pattern: str) -> List[str]:
    """Return likely JSON files matching a configured pattern."""
    if not pattern:
        return []

    candidate = _resolve_project_path(pattern.strip())

    if "{period}" in candidate:
        candidate = candidate.replace("{period}", "*")

    if any(symbol in candidate for symbol in ["*", "?", "["]):
        return sorted(glob.glob(candidate))

    if Path(candidate).is_dir():
        return sorted(str(path) for path in Path(candidate).glob("*.json"))

    if Path(candidate).is_file():
        return [candidate]

    root, extension = os.path.splitext(candidate or "instance.json")
    extension = extension or ".json"
    return sorted(glob.glob(f"{root}_*{extension}"))


def _print_inputs_help(
    pattern: str,
    periods_expected: Optional[List[str]] = None,
) -> None:
    """Print help when no input JSON instance is found."""
    title("Input JSON files were not found")
    print(f"Configured pattern: {pattern!r}")
    print("Suggestions:")
    print("  1) Check that DATA_JSON or DATOS_JSON points to an existing JSON file.")
    print("  2) Use a repository sample, e.g. data/samples/isc_20251_sample.json.")
    print("  3) Use a wildcard pattern if processing several instances.")
    if periods_expected:
        print(f"  4) Expected period suffix examples: {', '.join(periods_expected)}")


def _is_fresh_file(path: str, min_mtime: float) -> bool:
    """Return True when a file exists and was modified after a reference time."""
    try:
        return Path(path).is_file() and Path(path).stat().st_mtime >= (min_mtime - 0.25)
    except OSError:
        return False


def _post_summary(
    export_prefix: Optional[str],
    min_mtime: Optional[float] = None,
    only_if_fresh: bool = False,
) -> None:
    """Print a concise summary from exported timetable CSV files."""
    if not POST_SUMMARY or not export_prefix:
        return

    title("Post-solve summary")

    timetable_path = f"{export_prefix}_timetable.csv"
    soft_path = f"{export_prefix}_soft_violations.csv"
    room_overlap_path = f"{export_prefix}_postsolve_overlaps_rooms.csv"
    teacher_overlap_path = f"{export_prefix}_postsolve_overlaps_teachers.csv"
    group_overlap_path = f"{export_prefix}_postsolve_overlaps_groups.csv"

    def is_available(path: str) -> bool:
        if min_mtime is None:
            return Path(path).is_file()
        return _is_fresh_file(path, min_mtime)

    if only_if_fresh and not is_available(timetable_path):
        print("No new timetable file was found for this run.")
    else:
        timetable = _read_csv_safe(timetable_path) if is_available(timetable_path) else None

        if timetable and len(timetable) > 1:
            header = timetable[0]
            rows = timetable[1:]

            def column_index(*names: str) -> int:
                for name in names:
                    try:
                        return header.index(name)
                    except ValueError:
                        continue
                return -1

            time_index = column_index("time", "hora")
            room_index = column_index("room", "aula")
            teacher_index = column_index("teacher", "profesor")

            print(f"Scheduled events: {len(rows)}")

            if time_index >= 0:
                counts: Dict[str, int] = {}
                for row in rows:
                    if len(row) > time_index:
                        counts[row[time_index]] = counts.get(row[time_index], 0) + 1
                print("Usage by time slot:")
                for time_slot, count in sorted(
                    counts.items(),
                    key=lambda item: (
                        int(re.sub(r"[^0-9]", "", item[0]) or 0),
                        item[0],
                    ),
                ):
                    print(f"  {time_slot}: {count}")

            if room_index >= 0:
                room_counts: Dict[str, int] = {}
                for row in rows:
                    if len(row) > room_index and row[room_index]:
                        room_counts[row[room_index]] = room_counts.get(row[room_index], 0) + 1
                print("Top rooms by usage:")
                for room, count in sorted(room_counts.items(), key=lambda item: -item[1])[:10]:
                    print(f"  {room}: {count}")

            if teacher_index >= 0:
                teacher_counts: Dict[str, int] = {}
                for row in rows:
                    if len(row) > teacher_index and row[teacher_index]:
                        teacher_counts[row[teacher_index]] = (
                            teacher_counts.get(row[teacher_index], 0) + 1
                        )
                print("Top teachers by assigned blocks:")
                for teacher, count in sorted(teacher_counts.items(), key=lambda item: -item[1])[:10]:
                    print(f"  {teacher}: {count}")
        else:
            print("No timetable file was found, or it is empty.")

    for label, path in [
        ("room", room_overlap_path),
        ("teacher", teacher_overlap_path),
        ("group", group_overlap_path),
    ]:
        if is_available(path):
            print(f"{label.capitalize()} overlaps: see {path}")
        else:
            print(f"{label.capitalize()} overlaps: not detected or file not generated.")

    if is_available(soft_path):
        print(f"Soft violations: see {soft_path}")
    else:
        print("Soft violations: none detected or file not generated.")


# =============================================================================
# Solver execution
# =============================================================================


def _result_is_success(result: Any) -> bool:
    """Interpret solver return values as success/failure."""
    if isinstance(result, bool):
        return bool(result)

    if not isinstance(result, dict):
        return False

    status = str(result.get("status") or result.get("status_label") or "").strip().lower()
    if status in {
        "optimal",
        "feasible",
        "feasible (timelimit)",
        "success",
        "ok",
        "fallback_source_calendar",
    }:
        return True

    exportable = result.get("exportable")
    if isinstance(exportable, bool):
        return exportable

    has_exportable_solution = result.get("has_exportable_solution")
    if isinstance(has_exportable_solution, bool):
        return has_exportable_solution

    return False


def _result_status_text(result: Any) -> str:
    """Return a printable solver status text."""
    if isinstance(result, bool):
        return "SUCCESS" if result else "FAILURE"
    if isinstance(result, dict):
        return str(result.get("status") or result.get("status_label") or "Unknown")
    return str(type(result))


def _solve_single(
    json_path: str,
    export_prefix: Optional[str],
    inferred_period: Optional[str] = None,
) -> None:
    """Validate, preprocess, and solve one JSON input instance."""
    resolved_export_prefix = _resolve_export_prefix(export_prefix, inferred_period, json_path)
    run_started = time.time()

    path_to_solve = _preprocess_json_if_needed(
        _resolve_project_path(json_path),
        export_prefix=resolved_export_prefix,
    )

    original_phase2_env = os.environ.get("PHASE2_FROM_BLOCK_SOLUTION")
    solver_phase2_disabled = False

    if ASSIGN_ROOMS:
        os.environ["PHASE2_FROM_BLOCK_SOLUTION"] = "0"
        solver_phase2_disabled = True
        print("Runner patch: PHASE2_FROM_BLOCK_SOLUTION=0 inside solver execution.")

    try:
        try:
            data = load_json(path_to_solve)
        except Exception as exc:
            title("ERROR")
            print(f"Could not open JSON after preprocessing: {path_to_solve}")
            print(f"Details: {exc}")
            return

        _basic_validations(data)
        total_demand, _, room_capacity, _ = _theoretical_capacity_check(data)

        if ASSIGN_ROOMS and FAIL_EARLY_IF_ROOM_CAP_INSUFF and total_demand > room_capacity:
            title("Execution stopped by capacity preflight")
            print("Aggregate room capacity is insufficient under current configuration.")
            return

        preflight_solver_info()

        os.environ["SOLVER_TIME_LIMIT"] = str(TIME_LIMIT_SEC)
        os.environ["MODEL_INCLUDE_TIME"] = "1" if INCLUDE_TIME else "0"
        os.environ["ASSIGN_ROOMS"] = "1" if ASSIGN_ROOMS else "0"
        os.environ["SINGLE_ROOM_PER_COURSE"] = "1" if SINGLE_ROOM_PER_COURSE else "0"
        os.environ["MODEL_SOLVER"] = str(MODEL_SOLVER)
        os.environ["FRANJA_BAD_EARLY"] = TIME_PENALTY_EARLY_SLOTS
        os.environ["FRANJA_BAD_LATE"] = TIME_PENALTY_LATE_SLOTS
        os.environ["FRANJA_WEIGHT_EARLY"] = TIME_PENALTY_EARLY_WEIGHT
        os.environ["FRANJA_WEIGHT_LATE"] = TIME_PENALTY_LATE_WEIGHT

        if resolved_export_prefix:
            os.environ["EXPORT_PREFIX"] = resolved_export_prefix

        title(f"Running solver - {inferred_period or 'single instance'}")
        print(f"JSON input    : {path_to_solve}")
        print(f"Output prefix : {resolved_export_prefix}")

        try:
            import inspect

            signature = inspect.signature(SOLVER_FUNCTION)
            if "json_path" in signature.parameters:
                print("Execution mode: solve_one with explicit parameters")
                result = SOLVER_FUNCTION(
                    json_path=path_to_solve,
                    export_prefix=resolved_export_prefix,
                    solver_name=MODEL_SOLVER,
                )
            else:
                print("Execution mode: main with environment variables")
                original_data_json = os.getenv("DATA_JSON")
                original_legacy_json = os.getenv("DATOS_JSON")
                original_export = os.getenv("EXPORT_PREFIX")

                os.environ["DATA_JSON"] = path_to_solve
                os.environ["DATOS_JSON"] = path_to_solve
                if resolved_export_prefix:
                    os.environ["EXPORT_PREFIX"] = resolved_export_prefix

                result = SOLVER_FUNCTION()

                if original_data_json is not None:
                    os.environ["DATA_JSON"] = original_data_json
                else:
                    os.environ.pop("DATA_JSON", None)

                if original_legacy_json is not None:
                    os.environ["DATOS_JSON"] = original_legacy_json
                else:
                    os.environ.pop("DATOS_JSON", None)

                if original_export is not None:
                    os.environ["EXPORT_PREFIX"] = original_export
                else:
                    os.environ.pop("EXPORT_PREFIX", None)
        except Exception as exc:
            print(f"ERROR while executing the solver: {exc}")
            import traceback

            traceback.print_exc()
            return

        title("Solver results")
        success = _result_is_success(result)

        if isinstance(result, bool):
            print(f"Result: {'SUCCESS' if result else 'FAILURE'}")
        elif isinstance(result, dict):
            status = _result_status_text(result)
            objective = result.get("objective", result.get("obj", "N/A"))
            exportable = result.get(
                "exportable",
                result.get("has_exportable_solution", "N/A"),
            )
            print(f"Status          : {status}")
            print(f"Objective value : {objective}")
            print(f"Exportable      : {exportable}")
        else:
            print(f"Returned object type: {type(result)}")

        _post_summary(
            resolved_export_prefix,
            min_mtime=run_started,
            only_if_fresh=not success,
        )

    finally:
        if solver_phase2_disabled:
            if original_phase2_env is None:
                os.environ.pop("PHASE2_FROM_BLOCK_SOLUTION", None)
            else:
                os.environ["PHASE2_FROM_BLOCK_SOLUTION"] = original_phase2_env


def _infer_period_from_path(path: str) -> Optional[str]:
    """Infer period identifiers such as 20251 from a file name."""
    match = re.search(r"(20\d{2}[12])", Path(path).name) or re.search(
        r"(20\d{2})", Path(path).name
    )
    return match.group(1) if match else None


def _discover_json_inputs() -> List[str]:
    """Discover JSON inputs and optionally run the instance generator if needed."""
    discovered = _expand_json_inputs(JSON_PATH) or _list_possible_built_jsons(JSON_PATH)

    if discovered:
        return discovered

    if not AUTO_BUILD_INSTANCE:
        return []

    generated = _run_instance_generator()
    if not generated:
        return []

    return _expand_json_inputs(JSON_PATH) or _list_possible_built_jsons(JSON_PATH)


def main() -> int:
    """Command-line entry point."""
    if SOLVER_FUNCTION is None:
        print("ERROR: solver function could not be loaded.")
        return 1

    if MULTI_PERIODS:
        title("Batch mode")
        print(f"Configured MULTI_PERIODS: {MULTI_PERIODS}")

        discovered = _discover_json_inputs()
        if not discovered:
            _print_inputs_help(JSON_PATH, periods_expected=["20241", "20242", "20251"])
            return 1

        for json_file in discovered:
            period = _infer_period_from_path(json_file) or "unknown"
            title(f"Dataset period: {period}")
            _solve_single(json_file, EXPORT_PREFIX, period)

        print("\nBatch execution completed.")
        return 0

    inferred_period: Optional[str] = None
    path_to_use = JSON_PATH

    expanded = _discover_json_inputs()
    if not expanded and "{period}" in (JSON_PATH or ""):
        expanded = _expand_json_inputs(JSON_PATH.replace("{period}", "*"))

    if expanded:
        try:
            expanded.sort(key=lambda path: Path(path).stat().st_mtime, reverse=True)
        except OSError:
            expanded = sorted(expanded)

        path_to_use = expanded[0]
        inferred_period = _infer_period_from_path(path_to_use)

        print(
            f"[Single] Using most recent JSON: {path_to_use} "
            f"(period={inferred_period or 'N/A'})"
        )
    else:
        resolved_path = _resolve_project_path(path_to_use)
        if "{period}" in (JSON_PATH or "") and not Path(resolved_path).is_file():
            candidates = JSON_PATH.replace("{period}", "*")
            candidate_list = sorted(glob.glob(_resolve_project_path(candidates)))

            if not candidate_list:
                _print_inputs_help(JSON_PATH, periods_expected=["20241", "20242", "20251"])
                return 1

            try:
                candidate_list.sort(
                    key=lambda path: Path(path).stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                candidate_list = sorted(candidate_list)

            path_to_use = candidate_list[0]
            inferred_period = _infer_period_from_path(path_to_use)

            print(
                f"[Single] Fallback to most recent JSON: {path_to_use} "
                f"(period={inferred_period or 'N/A'})"
            )

    print(f"Using JSON: {path_to_use}")
    print(
        "Validate/Solve -> "
        f"RUN_SOLVER={int(RUN_SOLVER)} | "
        f"INCLUDE_TIME={int(INCLUDE_TIME)} | "
        f"SOLVER={MODEL_SOLVER}"
    )

    if not RUN_SOLVER:
        try:
            data_tmp = load_json(_preprocess_json_if_needed(_resolve_project_path(path_to_use)))
            _basic_validations(data_tmp)
            _theoretical_capacity_check(data_tmp)
        except Exception as exc:
            title("ERROR")
            print(f"Could not open the JSON instance: {exc}")
            return 1

        title("Validation mode")
        print("The solver was not called because RUN_SOLVER=0.")
        return 0

    _solve_single(path_to_use, EXPORT_PREFIX, inferred_period=inferred_period)
    print("\nExecution completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
