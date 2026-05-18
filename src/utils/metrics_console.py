"""
Metrics Console
===============

Read-only console utility for inspecting database-level metrics used by the
Academic Timetabling MILP repository.

This script summarizes institutional source data, room catalogs, configured
room sets, course groups, and validation checks before generating model-ready
instances.

Main responsibilities
---------------------
- Load local configuration from `.env`.
- Connect to the configured MySQL database in read-only diagnostic mode.
- Report high-level database metrics.
- Validate configured theory and laboratory room sets.
- Export optional diagnostic CSV files.

Notes
-----
This script may refer to database table and column names such as `aulas`,
`horarios`, `grupos`, and `materias`. These names are preserved because they
belong to the source database schema and should not be translated in SQL code.
"""

from __future__ import annotations

import csv
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import mysql.connector
from dotenv import load_dotenv


# -----------------------------------------------------------------------------
# Environment loading and helper functions
# -----------------------------------------------------------------------------

load_dotenv()


def get_env(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """Read an environment variable with optional required validation."""
    value = os.getenv(key, default)
    if required and (value is None or str(value).strip() == ""):
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def parse_bool(key: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    raw = os.getenv(key)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "si", "s"}


def parse_env_list(key: str, default_list: Optional[Sequence[str]] = None) -> List[str]:
    """Parse a comma- or semicolon-separated environment list."""
    raw = (os.getenv(key) or "").strip()
    if raw:
        parts = re.split(r"[;,]", raw)
        return [part.strip().upper() for part in parts if part.strip()]
    return [str(item).strip().upper() for item in (default_list or []) if str(item).strip()]


def parse_json_env(key: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Parse a JSON dictionary stored in an environment variable."""
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default if default is not None else {}

    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _norm_room(value: Any) -> str:
    """
    Normalize room identifiers for comparison.

    The normalization removes surrounding spaces, converts text to uppercase,
    and removes spaces and hyphens.

    Examples
    --------
    'F-19' -> 'F19'
    ' ff1 ' -> 'FF1'
    """
    if value is None:
        return ""

    text = str(value).strip().upper()
    text = text.replace(" ", "").replace("-", "")
    return text


def _norm_period(value: Any) -> str:
    """Normalize period identifiers by keeping only digits."""
    return re.sub(r"\D", "", str(value or "").strip())


def _like_value(value: Any) -> str:
    """Build a SQL LIKE value in uppercase."""
    return f"%{str(value).strip().upper()}%"


def _nonnull_trim_sql(alias: str, col: str) -> str:
    """
    Return a safe MySQL expression for non-empty trimmed values.

    Example
    -------
    NULLIF(TRIM(CAST(alias.`col` AS CHAR)), '')
    """
    return f"NULLIF(TRIM(CAST({alias}.`{col}` AS CHAR)), '')"


def print_title(text: str) -> None:
    """Print a formatted console title."""
    print("\n" + "=" * len(text))
    print(text)
    print("=" * len(text))


def print_line(label: str, value: Any, width: int = 42) -> None:
    """Print a formatted key-value line."""
    print(f"{label:>{width}}: {value}")


def qual(schema: str, table: str) -> str:
    """Return a fully qualified MySQL table name."""
    return f"`{schema}`.`{table}`"


def has_table(cur, schema: str, name: str) -> bool:
    """Return True when a table exists in the selected schema."""
    cur.execute(
        """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        """,
        (schema, name),
    )
    return cur.fetchone()[0] > 0


def table_columns(cur, schema: str, table: str) -> List[str]:
    """Return the column names of a MySQL table."""
    cur.execute(f"DESCRIBE {qual(schema, table)}")
    return [row[0] for row in cur.fetchall()]


def pick_column(cols: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    """Pick the first available column from a candidate list."""
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return None


def run_scalar(cur, sql: str, params: Optional[Sequence[Any]] = None) -> Any:
    """Run a SQL query and return the first scalar value."""
    cur.execute(sql, params or ())
    row = cur.fetchone()
    return row[0] if row else None


def fetchall(cur, sql: str, params: Optional[Sequence[Any]] = None) -> List[Tuple[Any, ...]]:
    """Run a SQL query and return all rows."""
    cur.execute(sql, params or ())
    return cur.fetchall()


def _export_csv(path: str, header: Optional[Sequence[str]], rows: Iterable[Any]) -> None:
    """Export rows to CSV when diagnostic export is enabled."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if header:
                writer.writerow(header)
            for row in rows:
                if isinstance(row, (list, tuple)):
                    writer.writerow(list(row))
                else:
                    writer.writerow([row])
        print_line("CSV exported", path)
    except Exception as exc:
        print_line("CSV export error", str(exc))


def _where_clause(parts: Sequence[str]) -> str:
    """Build a SQL WHERE clause from a list of conditions."""
    return ("WHERE " + " AND ".join(parts)) if parts else ""


# -----------------------------------------------------------------------------
# Room helper functions for exact AT/AL sets
# -----------------------------------------------------------------------------

def _is_at_room(room: str, at_exact: set[str]) -> bool:
    """Return True when a room belongs to the theory-room set."""
    return room in at_exact


def _is_al_room(room: str, al_exact: set[str]) -> bool:
    """Return True when a room belongs to the laboratory-room set."""
    return room in al_exact


def _room_allowed(room: str, at_exact: set[str], al_exact: set[str]) -> bool:
    """Return True when a room is allowed by either exact room set."""
    return room in at_exact or room in al_exact


def _catalog_rooms_in_at(room_set: set[str], at_exact: set[str]) -> List[str]:
    """Return catalog rooms included in the theory-room set."""
    return sorted([room for room in room_set if room in at_exact])


def _catalog_rooms_in_al(room_set: set[str], al_exact: set[str]) -> List[str]:
    """Return catalog rooms included in the laboratory-room set."""
    return sorted([room for room in room_set if room in al_exact])


def _catalog_rooms_outside(room_set: set[str], at_exact: set[str], al_exact: set[str]) -> List[str]:
    """Return catalog rooms outside the configured theory/lab room sets."""
    return sorted([room for room in room_set if room not in at_exact and room not in al_exact])


def _dict_reverse_unique(alias_to_real: Dict[str, str]) -> Dict[str, str]:
    """Build a real-room to visual-alias dictionary without duplicates."""
    output = {}
    for alias, real in alias_to_real.items():
        real_normalized = _norm_room(real)
        alias_normalized = str(alias).strip().upper()
        if real_normalized and alias_normalized and real_normalized not in output:
            output[real_normalized] = alias_normalized
    return output


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DB_CFG = {
    "host": get_env("DB_HOST", "localhost"),
    "port": int(get_env("DB_PORT", "3306")),
    "user": get_env("DB_USER", required=True),
    "password": get_env("DB_PASSWORD", required=True),
    "database": get_env("DB_NAME", required=True),
}

EXPORT_DIR = str(get_env("EXPORT_DIR", "outputs") or "outputs").strip()
EXPORT_CSV = parse_bool("EXPORT_CSV", False)
os.makedirs(EXPORT_DIR, exist_ok=True)

TARGET_PERIOD = _norm_period(get_env("TARGET_PERIOD", get_env("MODEL_PERIODO", "20251")))
if not TARGET_PERIOD:
    TARGET_PERIOD = "20251"

ONLY_TARGET_PERIOD = parse_bool("ONLY_TARGET_PERIOD", True)
MULTI_PERIODS_RAW = (os.getenv("MULTI_PERIODS") or "").strip() if not ONLY_TARGET_PERIOD else ""

MODEL_CARRERA_LIKE = str(get_env("MODEL_CARRERA_LIKE", "SISTEM") or "SISTEM").strip().upper()
MODEL_NIVEL = str(get_env("MODEL_NIVEL", "L") or "L").strip().upper()
EXCLUDE_CARRERA_LIKE = str(get_env("EXCLUDE_CARRERA_LIKE", "NO ESCOLARIZADA") or "NO ESCOLARIZADA").strip().upper()

DEFAULT_AT = [
    "FF1",
    "FF2",
    "FF3",
    "FF4",
    "FF5",
    "FF6",
    "FF7",
    "FF8",
    "FF9",
    "FFA",
    "FFB",
    "FFC",
    "FFD",
]

AT_LIST = [_norm_room(item) for item in parse_env_list("MODEL_AT_LIST", default_list=DEFAULT_AT)]

AL_REAL_RAW = parse_env_list("MODEL_AL_LIST_REAL", default_list=[])
if not AL_REAL_RAW:
    AL_REAL_RAW = parse_env_list("MODEL_AL_LIST", default_list=[])

AL_REAL_LIST = [_norm_room(item) for item in AL_REAL_RAW]

LAB_ALIAS_MAP = parse_json_env("LAB_ALIAS_MAP_JSON", default={})
LAB_ALIAS_MAP = {
    str(key).strip().upper(): _norm_room(value)
    for key, value in LAB_ALIAS_MAP.items()
    if str(key).strip() and str(value).strip()
}
REAL_TO_ALIAS = _dict_reverse_unique(LAB_ALIAS_MAP)

STRICT_ROOMSETS = parse_bool("STRICT_ROOMSETS", True)
SEM1_EXTRAS = parse_env_list("SEM1_EXTRAS", default_list=[])


# -----------------------------------------------------------------------------
# Multi-period helper functions
# -----------------------------------------------------------------------------

def _detect_period_column(cur, schema: str, table: str = "grupos") -> Optional[str]:
    """Detect the period column in a table when available."""
    if not has_table(cur, schema, table):
        return None
    cols = table_columns(cur, schema, table)
    return pick_column(cols, ["periodo"])


def _fetch_periods_from_db(cur, schema: str, limit: Optional[int] = None) -> List[str]:
    """Fetch available period identifiers from the groups table."""
    colp = _detect_period_column(cur, schema, "grupos")
    if not colp:
        return []

    sql = f"SELECT DISTINCT `{colp}` AS p FROM {qual(schema, 'grupos')} ORDER BY `{colp}` DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    try:
        return [str(row[0]) for row in fetchall(cur, sql) if row and row[0] is not None]
    except Exception:
        return []


def _parse_multi_periods(cur, schema: str, raw: str) -> List[str]:
    """Parse MULTI_PERIODS configuration values."""
    if not raw:
        return []

    upper_value = raw.upper().strip()
    if upper_value == "ALL":
        return [_norm_period(period) for period in _fetch_periods_from_db(cur, schema, None) if _norm_period(period)]

    if upper_value.startswith("ALL_LAST:"):
        try:
            limit = int(upper_value.split(":", 1)[1])
        except Exception:
            limit = 6
        return [_norm_period(period) for period in _fetch_periods_from_db(cur, schema, limit) if _norm_period(period)]

    output = []
    for period in raw.split(","):
        normalized_period = _norm_period(period)
        if normalized_period:
            output.append(normalized_period)
    return output


# -----------------------------------------------------------------------------
# Group-scope helper functions
# -----------------------------------------------------------------------------

def _build_group_scope(cur, schema: str, g_alias: str = "g") -> Dict[str, Any]:
    """Build the SQL join and filter scope for the configured academic program."""
    if not has_table(cur, schema, "grupos"):
        return {"ok": False, "reason": "Table `grupos` does not exist."}
    if not has_table(cur, schema, "carreras"):
        return {"ok": False, "reason": "Table `carreras` does not exist."}

    g_cols = table_columns(cur, schema, "grupos")
    c_cols = table_columns(cur, schema, "carreras")

    c_name = pick_column(c_cols, ["nombre_carrera", "nombre", "desc_carrera", "descripcion", "nombre_largo"])
    c_level = pick_column(c_cols, ["nivel_escolar", "nivel", "nivel_academico"])
    c_key = pick_column(c_cols, ["carrera", "id_carrera", "clave_carrera"])
    c_ret = pick_column(c_cols, ["reticula", "id_reticula", "plan"])

    if not (c_name and c_level and c_key and c_ret):
        return {"ok": False, "reason": "Table `carreras` does not expose enough columns."}

    g_exc_carr = pick_column(g_cols, ["exclusivo_carrera"])
    g_exc_ret = pick_column(g_cols, ["exclusivo_reticula"])
    g_carr = pick_column(g_cols, ["carrera", "id_carrera", "clave_carrera"])
    g_ret = pick_column(g_cols, ["reticula", "id_reticula", "plan"])

    direct_join = None
    direct_strategy = None

    if (g_exc_carr or g_carr) and (g_exc_ret or g_ret):
        carr_parts = []
        ret_parts = []

        if g_exc_carr:
            carr_parts.append(_nonnull_trim_sql(g_alias, g_exc_carr))
        if g_carr and g_carr != g_exc_carr:
            carr_parts.append(_nonnull_trim_sql(g_alias, g_carr))

        if g_exc_ret:
            ret_parts.append(_nonnull_trim_sql(g_alias, g_exc_ret))
        if g_ret and g_ret != g_exc_ret:
            ret_parts.append(_nonnull_trim_sql(g_alias, g_ret))

        carr_expr = f"COALESCE({', '.join(carr_parts)})" if carr_parts else None
        ret_expr = f"COALESCE({', '.join(ret_parts)})" if ret_parts else None

        if carr_expr and ret_expr:
            direct_join = (
                f"JOIN {qual(schema, 'carreras')} c "
                f"ON TRIM(CAST(c.`{c_key}` AS CHAR)) = {carr_expr} "
                f"AND TRIM(CAST(c.`{c_ret}` AS CHAR)) = {ret_expr}"
            )
            direct_strategy = "g->c using exclusive/program fields"

    where = []
    params = []

    if MODEL_CARRERA_LIKE:
        where.append(f"UPPER(TRIM(c.`{c_name}`)) LIKE %s")
        params.append(_like_value(MODEL_CARRERA_LIKE))

    if EXCLUDE_CARRERA_LIKE:
        where.append(f"UPPER(TRIM(c.`{c_name}`)) NOT LIKE %s")
        params.append(_like_value(EXCLUDE_CARRERA_LIKE))

    if MODEL_NIVEL:
        where.append(f"UPPER(TRIM(c.`{c_level}`)) = %s")
        params.append(MODEL_NIVEL)

    if direct_join:
        return {
            "ok": True,
            "joins": direct_join,
            "where": where,
            "params": params,
            "strategy": direct_strategy,
            "c_name": c_name,
            "c_level": c_level,
            "source": "direct",
        }

    if has_table(cur, schema, "materias_carreras"):
        mc_cols = table_columns(cur, schema, "materias_carreras")
        g_materia = pick_column(g_cols, ["materia", "id_materia", "clave_materia"])
        mc_materia = pick_column(mc_cols, ["materia", "id_materia", "clave_materia"])
        mc_key = pick_column(mc_cols, ["carrera", "id_carrera", "clave_carrera"])
        mc_ret = pick_column(mc_cols, ["reticula", "id_reticula", "plan"])

        if g_materia and mc_materia and mc_key and mc_ret:
            joins = "\n".join(
                [
                    f"JOIN {qual(schema, 'materias_carreras')} mc "
                    f"ON TRIM(CAST(mc.`{mc_materia}` AS CHAR)) = TRIM(CAST({g_alias}.`{g_materia}` AS CHAR))",
                    f"JOIN {qual(schema, 'carreras')} c "
                    f"ON TRIM(CAST(c.`{c_key}` AS CHAR)) = TRIM(CAST(mc.`{mc_key}` AS CHAR)) "
                    f"AND TRIM(CAST(c.`{c_ret}` AS CHAR)) = TRIM(CAST(mc.`{mc_ret}` AS CHAR))",
                ]
            )
            return {
                "ok": True,
                "joins": joins,
                "where": where,
                "params": params,
                "strategy": "g->mc->c fallback",
                "c_name": c_name,
                "c_level": c_level,
                "source": "mc",
            }

    return {"ok": False, "reason": "Could not build the group scope from `grupos`."}


def _group_period_filter(cur, schema: str, g_alias: str = "g", target_period: Optional[str] = None) -> Dict[str, Any]:
    """Build a period filter for the groups table."""
    if not has_table(cur, schema, "grupos"):
        return {"ok": False, "reason": "Table `grupos` does not exist."}

    g_cols = table_columns(cur, schema, "grupos")
    g_period = pick_column(g_cols, ["periodo"])

    if not g_period:
        return {"ok": False, "reason": "Table `grupos` does not expose a `periodo` column."}

    if not target_period:
        return {"ok": True, "where": [], "params": [], "g_period": g_period}

    return {
        "ok": True,
        "where": [f"{g_alias}.`{g_period}` = %s"],
        "params": [target_period],
        "g_period": g_period,
    }


# -----------------------------------------------------------------------------
# Timetable-scope helper functions linked to group records
# -----------------------------------------------------------------------------

def _build_horarios_scope(cur, schema: str, h_alias: str = "h", g_alias: str = "g", target_period: Optional[str] = None) -> Dict[str, Any]:
    """Build the SQL join and filter scope for timetable records."""
    if not has_table(cur, schema, "horarios"):
        return {"ok": False, "reason": "Table `horarios` does not exist."}
    if not has_table(cur, schema, "grupos"):
        return {"ok": False, "reason": "Table `grupos` does not exist."}

    h_cols = table_columns(cur, schema, "horarios")
    g_cols = table_columns(cur, schema, "grupos")

    h_period = pick_column(h_cols, ["periodo"])
    g_period = pick_column(g_cols, ["periodo"])
    h_materia = pick_column(h_cols, ["materia", "id_materia", "clave_materia"])
    g_materia = pick_column(g_cols, ["materia", "id_materia", "clave_materia"])
    h_grupo = pick_column(h_cols, ["grupo"])
    g_grupo = pick_column(g_cols, ["grupo"])

    join_on = []
    if h_period and g_period:
        join_on.append(f"TRIM(CAST({g_alias}.`{g_period}` AS CHAR)) = TRIM(CAST({h_alias}.`{h_period}` AS CHAR))")
    if h_materia and g_materia:
        join_on.append(f"TRIM(CAST({g_alias}.`{g_materia}` AS CHAR)) = TRIM(CAST({h_alias}.`{h_materia}` AS CHAR))")
    if h_grupo and g_grupo:
        join_on.append(f"TRIM(CAST({g_alias}.`{g_grupo}` AS CHAR)) = TRIM(CAST({h_alias}.`{h_grupo}` AS CHAR))")

    if len(join_on) < 2:
        return {
            "ok": False,
            "reason": "Could not link `horarios` with `grupos` with sufficient precision.",
        }

    g_scope = _build_group_scope(cur, schema, g_alias=g_alias)
    if not g_scope["ok"]:
        return g_scope

    where = list(g_scope["where"])
    params = list(g_scope["params"])

    if target_period:
        if h_period:
            where.append(f"{h_alias}.`{h_period}` = %s")
            params.append(target_period)
        elif g_period:
            where.append(f"{g_alias}.`{g_period}` = %s")
            params.append(target_period)
        else:
            return {
                "ok": False,
                "reason": "Neither `horarios` nor `grupos` exposes a `periodo` column.",
            }

    joins = [
        f"JOIN {qual(schema, 'grupos')} {g_alias} ON " + " AND ".join(join_on),
        g_scope["joins"],
    ]

    return {
        "ok": True,
        "joins": "\n".join(joins),
        "where": where,
        "params": params,
        "strategy": f"h->g ({', '.join(join_on)}) + {g_scope['strategy']}",
    }


# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------

try:
    print(
        f"Connecting to {DB_CFG['host']}:{DB_CFG['port']} "
        f"database={DB_CFG['database']} as user={DB_CFG['user']} ..."
    )
    cn = mysql.connector.connect(**DB_CFG)
    cur = cn.cursor()
    schema = str(DB_CFG["database"])

    print_title("Execution mode")
    print_line("Target period", TARGET_PERIOD)
    print_line("Only target period", "Yes" if ONLY_TARGET_PERIOD else "No")
    print_line("Program LIKE filter", MODEL_CARRERA_LIKE or "-")
    print_line("Excluded program LIKE filter", EXCLUDE_CARRERA_LIKE or "-")
    print_line("Academic level", MODEL_NIVEL or "-")
    print_line("Exact AT rooms", ", ".join(AT_LIST) if AT_LIST else "-")
    print_line("Exact AL rooms", ", ".join(AL_REAL_LIST) if AL_REAL_LIST else "-")
    print_line("STRICT_ROOMSETS", "Yes" if STRICT_ROOMSETS else "No")
    print_line("SEM1_EXTRAS", ", ".join(SEM1_EXTRAS) if SEM1_EXTRAS else "-")
    print_line("Laboratory aliases", json.dumps(LAB_ALIAS_MAP, ensure_ascii=False) if LAB_ALIAS_MAP else "-")

    g_scope = _build_group_scope(cur, schema, g_alias="g")
    g_period_scope = _group_period_filter(cur, schema, g_alias="g", target_period=TARGET_PERIOD)
    h_scope = _build_horarios_scope(cur, schema, h_alias="h", g_alias="g", target_period=TARGET_PERIOD)

    print_title("Program-scope detection")
    print_line("Group scope", g_scope["strategy"] if g_scope.get("ok") else g_scope.get("reason"))
    print_line("Timetable scope", h_scope["strategy"] if h_scope.get("ok") else h_scope.get("reason"))

    # -------------------------------------------------------------------------
    # 1. Global table totals
    # -------------------------------------------------------------------------
    print_title("Database metrics")
    metrics_sql = {
        "Total academic levels": f"SELECT COUNT(*) FROM {qual(schema, 'nivel_escolar')}" if has_table(cur, schema, "nivel_escolar") else None,
        "Total student statuses": f"SELECT COUNT(*) FROM {qual(schema, 'estatus_alumno')}" if has_table(cur, schema, "estatus_alumno") else None,
        "Total study plans": f"SELECT COUNT(*) FROM {qual(schema, 'planes_de_estudio')}" if has_table(cur, schema, "planes_de_estudio") else None,
        "Total programs": f"SELECT COUNT(*) FROM {qual(schema, 'carreras')}" if has_table(cur, schema, "carreras") else None,
        "Total courses": f"SELECT COUNT(*) FROM {qual(schema, 'materias')}" if has_table(cur, schema, "materias") else None,
        "Total students": f"SELECT COUNT(*) FROM {qual(schema, 'alumnos')}" if has_table(cur, schema, "alumnos") else None,
        "Total groups (global)": f"SELECT COUNT(*) FROM {qual(schema, 'grupos')}" if has_table(cur, schema, "grupos") else None,
        "Total timetables (global)": f"SELECT COUNT(*) FROM {qual(schema, 'horarios')}" if has_table(cur, schema, "horarios") else None,
        "Total rooms": f"SELECT COUNT(*) FROM {qual(schema, 'aulas')}" if has_table(cur, schema, "aulas") else None,
    }

    metrics_out = []
    for metric_name, query in metrics_sql.items():
        if not query:
            print_line(metric_name, "N/A (missing table)")
            metrics_out.append((metric_name, "N/A"))
            continue
        try:
            value = run_scalar(cur, query)
            print_line(metric_name, value)
            metrics_out.append((metric_name, value))
        except mysql.connector.Error as exc:
            message = getattr(exc, "msg", str(exc))
            print_line(metric_name, f"ERROR ({message})")
            metrics_out.append((metric_name, f"ERROR: {message}"))

    try:
        if has_table(cur, schema, "grupos") and g_period_scope["ok"]:
            g_cols = table_columns(cur, schema, "grupos")
            gp = pick_column(g_cols, ["periodo"])
            total = run_scalar(
                cur,
                f"SELECT COUNT(*) FROM {qual(schema, 'grupos')} WHERE `{gp}`=%s",
                (TARGET_PERIOD,),
            )
            print_line("Total groups (target period)", total)
            metrics_out.append(("Total groups (target period)", total))
        else:
            print_line("Total groups (target period)", "N/A")
            metrics_out.append(("Total groups (target period)", "N/A"))

        if has_table(cur, schema, "horarios"):
            h_cols = table_columns(cur, schema, "horarios")
            hp = pick_column(h_cols, ["periodo"])
            if hp:
                total = run_scalar(
                    cur,
                    f"SELECT COUNT(*) FROM {qual(schema, 'horarios')} WHERE `{hp}`=%s",
                    (TARGET_PERIOD,),
                )
                print_line("Total timetables (target period)", total)
                metrics_out.append(("Total timetables (target period)", total))
            else:
                print_line("Total timetables (target period)", "N/A")
                metrics_out.append(("Total timetables (target period)", "N/A"))
    except mysql.connector.Error as exc:
        print_line("Target-period totals", f"ERROR ({getattr(exc, 'msg', str(exc))})")

    # -------------------------------------------------------------------------
    # 2. Target program totals
    # -------------------------------------------------------------------------
    print_title("Target-program totals")
    try:
        if g_scope["ok"] and g_period_scope["ok"]:
            g_cols = table_columns(cur, schema, "grupos")
            g_materia = pick_column(g_cols, ["materia", "id_materia", "clave_materia"])
            g_grupo = pick_column(g_cols, ["grupo"])
            gp = pick_column(g_cols, ["periodo"])

            if g_materia and g_grupo and gp:
                where = list(g_scope["where"]) + list(g_period_scope["where"])
                params = list(g_scope["params"]) + list(g_period_scope["params"])
                distinct_key = f"CONCAT(g.`{gp}`,'|',g.`{g_materia}`,'|',g.`{g_grupo}`)"
                sql = f"""
                    SELECT COUNT(DISTINCT {distinct_key})
                    FROM {qual(schema, 'grupos')} g
                    {g_scope['joins']}
                    {_where_clause(where)}
                """
                total_groups = run_scalar(cur, sql, params)
                print_line("Target-program groups", total_groups)
                metrics_out.append(("Target-program groups", total_groups))
            else:
                print_line("Target-program groups", "N/A (missing key columns)")
                metrics_out.append(("Target-program groups", "N/A"))
        else:
            print_line("Target-program groups", g_scope.get("reason", "N/A"))
            metrics_out.append(("Target-program groups", g_scope.get("reason", "N/A")))

        if h_scope["ok"]:
            sql = f"""
                SELECT COUNT(*)
                FROM {qual(schema, 'horarios')} h
                {h_scope['joins']}
                {_where_clause(h_scope['where'])}
            """
            total_timetables = run_scalar(cur, sql, h_scope["params"])
            print_line("Target-program timetable rows", total_timetables)
            metrics_out.append(("Target-program timetable rows", total_timetables))
        else:
            print_line("Target-program timetable rows", h_scope.get("reason", "N/A"))
            metrics_out.append(("Target-program timetable rows", h_scope.get("reason", "N/A")))
    except mysql.connector.Error as exc:
        print_line("Target-program totals", f"ERROR ({getattr(exc, 'msg', str(exc))})")

    if EXPORT_CSV:
        _export_csv(
            os.path.join(EXPORT_DIR, f"metrics_totals_{TARGET_PERIOD}.csv"),
            ["metric", "value"],
            metrics_out,
        )

    # -------------------------------------------------------------------------
    # 3. Detected programs within scope
    # -------------------------------------------------------------------------
    print_title("Programs detected within the target scope")
    try:
        if g_scope["ok"] and g_period_scope["ok"]:
            c_name = g_scope.get("c_name")
            if c_name:
                where = list(g_scope["where"]) + list(g_period_scope["where"])
                params = list(g_scope["params"]) + list(g_period_scope["params"])
                sql = f"""
                    SELECT UPPER(TRIM(c.`{c_name}`)) AS program_name, COUNT(*) AS total
                    FROM {qual(schema, 'grupos')} g
                    {g_scope['joins']}
                    {_where_clause(where)}
                    GROUP BY UPPER(TRIM(c.`{c_name}`))
                    ORDER BY total DESC, program_name
                """
                rows = fetchall(cur, sql, params)
                if rows:
                    for name, total in rows:
                        print(f"{str(name):<60}: {total}")
                else:
                    print("(no data for the configured scope)")
            else:
                print("(no recognized program-name column)")
        else:
            print(g_scope.get("reason", "(no target scope)"))
    except mysql.connector.Error as exc:
        print(f"ERROR while listing detected programs: {getattr(exc, 'msg', str(exc))}")

    # -------------------------------------------------------------------------
    # 4. Group distribution by period
    # -------------------------------------------------------------------------
    print_title("Target-program group distribution by period")
    try:
        if g_scope["ok"] and has_table(cur, schema, "grupos"):
            g_cols = table_columns(cur, schema, "grupos")
            gp = pick_column(g_cols, ["periodo"])
            gm = pick_column(g_cols, ["materia", "id_materia", "clave_materia"])
            gg = pick_column(g_cols, ["grupo"])

            if gp and gm and gg:
                distinct_key = f"CONCAT(g.`{gp}`,'|',g.`{gm}`,'|',g.`{gg}`)"
                where = list(g_scope["where"])
                params = list(g_scope["params"])

                if ONLY_TARGET_PERIOD:
                    where.append(f"g.`{gp}` = %s")
                    params.append(TARGET_PERIOD)
                else:
                    selected = _parse_multi_periods(cur, schema, MULTI_PERIODS_RAW)
                    if selected:
                        placeholders = ",".join(["%s"] * len(selected))
                        where.append(f"g.`{gp}` IN ({placeholders})")
                        params.extend(selected)

                sql = f"""
                    SELECT g.`{gp}` AS period, COUNT(DISTINCT {distinct_key}) AS total
                    FROM {qual(schema, 'grupos')} g
                    {g_scope['joins']}
                    {_where_clause(where)}
                    GROUP BY g.`{gp}`
                    ORDER BY g.`{gp}`
                """
                rows = fetchall(cur, sql, params)
                if rows:
                    for period, total in rows:
                        print(f"{period}\t{total}")
                else:
                    print("(no data)")
            else:
                print("(missing required group columns for this metric)")
        else:
            print(g_scope.get("reason", "(no target scope)"))
    except mysql.connector.Error as exc:
        print(f"ERROR while listing group distribution by period: {getattr(exc, 'msg', str(exc))}")

    # -------------------------------------------------------------------------
    # 5. Sample of filtered programs
    # -------------------------------------------------------------------------
    print_title("Sample of filtered programs")
    try:
        if g_scope["ok"] and g_period_scope["ok"]:
            c_name = g_scope.get("c_name")
            c_level = g_scope.get("c_level")
            if c_name:
                where = list(g_scope["where"]) + list(g_period_scope["where"])
                params = list(g_scope["params"]) + list(g_period_scope["params"])
                extra_level = f", UPPER(TRIM(c.`{c_level}`)) AS level" if c_level else ""
                sql = f"""
                    SELECT DISTINCT
                        UPPER(TRIM(c.`{c_name}`)) AS program_name
                        {extra_level}
                    FROM {qual(schema, 'grupos')} g
                    {g_scope['joins']}
                    {_where_clause(where)}
                    ORDER BY program_name
                    LIMIT 50
                """
                rows = fetchall(cur, sql, params)
                if rows:
                    if c_level:
                        print(f"{'level':<6} program")
                        print("-" * 90)
                        for name, level in rows:
                            print(f"{str(level):<6} {str(name)}")
                    else:
                        for (name,) in rows:
                            print(name)
                else:
                    print("(no data)")
            else:
                print("(no program-name column)")
        else:
            print(g_scope.get("reason", "(no target scope)"))
    except mysql.connector.Error as exc:
        print(f"ERROR while listing the program sample: {getattr(exc, 'msg', str(exc))}")

    # -------------------------------------------------------------------------
    # 6. Sample of target-period groups
    # -------------------------------------------------------------------------
    print_title("Sample of 20 target-program groups in the target period")
    try:
        if g_scope["ok"] and g_period_scope["ok"]:
            g_cols = table_columns(cur, schema, "grupos")
            gp = pick_column(g_cols, ["periodo"])
            gm = pick_column(g_cols, ["materia", "id_materia", "clave_materia"])
            gg = pick_column(g_cols, ["grupo"])
            gi = pick_column(g_cols, ["alumnos_inscritos", "inscritos", "matriculados"])
            gc = pick_column(g_cols, ["capacidad_grupo", "cupo", "capacidad"])
            gr = pick_column(g_cols, ["rfc", "id_personal", "profesor", "nombre"])
            gexc = pick_column(g_cols, ["exclusivo_carrera"])
            gexr = pick_column(g_cols, ["exclusivo_reticula"])
            gcar = pick_column(g_cols, ["carrera", "id_carrera", "clave_carrera"])
            gret = pick_column(g_cols, ["reticula", "id_reticula", "plan"])

            select_cols = []
            headers = []

            def add_col(col: Optional[str], alias_name: str) -> None:
                if col:
                    select_cols.append(f"g.`{col}`")
                    headers.append(alias_name)

            add_col(gp, "period")
            add_col(gm, "course")
            add_col(gg, "group")
            add_col(gi, "enrolled")
            add_col(gc, "capacity")
            add_col(gr, "teacher")
            add_col(gexc, "exclusive_program")
            add_col(gexr, "exclusive_plan")
            add_col(gcar, "program")
            add_col(gret, "plan")

            if not select_cols:
                print("(not enough columns for the sample)")
            else:
                where = list(g_scope["where"]) + list(g_period_scope["where"])
                params = list(g_scope["params"]) + list(g_period_scope["params"])
                order_cols = []
                if gm:
                    order_cols.append(f"g.`{gm}`")
                if gg:
                    order_cols.append(f"g.`{gg}`")
                if gp:
                    order_cols.append(f"g.`{gp}`")

                sql = f"""
                    SELECT {", ".join(select_cols)}
                    FROM {qual(schema, 'grupos')} g
                    {g_scope['joins']}
                    {_where_clause(where)}
                    ORDER BY {", ".join(order_cols) if order_cols else "1"}
                    LIMIT 20
                """
                rows = fetchall(cur, sql, params)
                if rows:
                    print("Target period =", TARGET_PERIOD)
                    print("\t".join(headers))
                    for row in rows:
                        print("\t".join("" if item is None else str(item) for item in row))
                else:
                    print("(no target-program groups in the target period)")
        else:
            print(g_scope.get("reason", "(no target scope)"))
    except mysql.connector.Error as exc:
        print(f"ERROR while listing target-period groups: {getattr(exc, 'msg', str(exc))}")

    # -------------------------------------------------------------------------
    # 7. Room catalog and exact AT/AL validation
    # -------------------------------------------------------------------------
    print_title("Room catalog and exact AT/AL validation")
    try:
        print_line("Exact AT rooms", ", ".join(AT_LIST) if AT_LIST else "-")
        print_line("Exact AL rooms", ", ".join(AL_REAL_LIST) if AL_REAL_LIST else "-")
        print_line("Target period", TARGET_PERIOD)

        room_catalog = []
        capacity_map: Dict[str, Optional[int]] = {}
        type_map: Dict[str, str] = {}
        building_map: Dict[str, str] = {}

        if has_table(cur, schema, "aulas"):
            aq = qual(schema, "aulas")
            a_cols = table_columns(cur, schema, "aulas")
            col_aula = pick_column(a_cols, ["aula", "clave", "id_aula", "codigo"])
            col_cap = pick_column(a_cols, ["capacidad", "capacidad_aula", "cupo"])
            col_tipo = pick_column(a_cols, ["tipo_aula"])
            col_edif = pick_column(a_cols, ["edificio"])

            if col_aula:
                select_cols = [f"`{col_aula}`"]
                if col_cap:
                    select_cols.append(f"`{col_cap}`")
                if col_tipo:
                    select_cols.append(f"`{col_tipo}`")
                if col_edif:
                    select_cols.append(f"`{col_edif}`")

                rows = fetchall(cur, f"SELECT {', '.join(select_cols)} FROM {aq}")

                for row in rows:
                    index = 0
                    room = _norm_room(row[index])
                    index += 1
                    if not room:
                        continue
                    room_catalog.append(room)

                    if col_cap:
                        try:
                            capacity_map[room] = int(row[index]) if row[index] is not None else None
                        except Exception:
                            capacity_map[room] = None
                        index += 1
                    else:
                        capacity_map[room] = None

                    if col_tipo:
                        type_map[room] = str(row[index]).strip().upper() if row[index] is not None else ""
                        index += 1
                    else:
                        type_map[room] = ""

                    if col_edif:
                        building_map[room] = str(row[index]).strip() if row[index] is not None else ""
                    else:
                        building_map[room] = ""
            else:
                print("(table `aulas` does not expose a recognized room-code column)")
        else:
            print("(table `aulas` does not exist)")

        room_set = set(room_catalog)
        at_set = set(AT_LIST)
        al_set = set(AL_REAL_LIST)

        in_at = _catalog_rooms_in_at(room_set, at_set)
        in_al = _catalog_rooms_in_al(room_set, al_set)
        outside = _catalog_rooms_outside(room_set, at_set, al_set)

        print_line("|A| database catalog", len(room_set))
        print_line("|AT| configured", len(at_set))
        print_line("|AL| configured", len(al_set))
        print_line("Database rooms in AT", len(in_at))
        print_line("Database rooms in AL", len(in_al))
        print_line("Database rooms outside AT/AL", len(outside))

        at_not_in_catalog = sorted(list(at_set - room_set))
        al_not_in_catalog = sorted(list(al_set - room_set))
        if at_not_in_catalog:
            print_line("AT outside database catalog", ", ".join(at_not_in_catalog[:10]))
        if al_not_in_catalog:
            print_line("AL outside database catalog", ", ".join(al_not_in_catalog[:10]))
        if outside[:10]:
            print_line("Examples outside AT/AL", ", ".join(outside[:10]))

        at_wrong_type = sorted([room for room in in_at if type_map.get(room, "") and type_map.get(room, "") != "A"])
        al_wrong_type = sorted([room for room in in_al if type_map.get(room, "") and type_map.get(room, "") != "L"])

        print_line("AT rooms with type != 'A'", len(at_wrong_type))
        if at_wrong_type[:10]:
            print_line("Examples of unexpected AT type", ", ".join(at_wrong_type[:10]))

        print_line("AL rooms with type != 'L'", len(al_wrong_type))
        if al_wrong_type[:10]:
            print_line("Examples of unexpected AL type", ", ".join(al_wrong_type[:10]))

        if REAL_TO_ALIAS:
            print_title("Laboratory aliases")
            for real_room in sorted(al_set):
                print_line(real_room, REAL_TO_ALIAS.get(real_room, "-"))

        known_capacities = [(room, cap) for room, cap in capacity_map.items() if isinstance(cap, int)]
        if known_capacities:
            top_capacities = sorted(known_capacities, key=lambda item: (item[1], item[0]), reverse=True)[:10]
            print("\nTop 10 rooms by known capacity:")
            for room, capacity in top_capacities:
                print(f"  {room:>6s}: {capacity}")
        else:
            print("(no capacity column in `aulas`, or capacity values are unavailable)")

        if type_map:
            print_title("Catalog summary by tipo_aula")
            type_counts: Dict[str, int] = {}
            for _room, room_type in type_map.items():
                type_key = room_type if room_type else "NO_TYPE"
                type_counts[type_key] = type_counts.get(type_key, 0) + 1
            for room_type, total in sorted(type_counts.items(), key=lambda item: item[0]):
                print_line(f"tipo_aula={room_type}", total)

        if not al_set:
            print_title("Warning: laboratory rooms are not configured")
            print("No real laboratory rooms were defined in MODEL_AL_LIST_REAL or MODEL_AL_LIST.")
            print("Therefore, laboratory validation cannot be completed yet.")

            lab_candidates = sorted([room for room in room_set if type_map.get(room, "") == "L"])
            print_line("Laboratory candidates in catalog", len(lab_candidates))
            if lab_candidates:
                print("Candidates with tipo_aula='L':")
                for room in lab_candidates:
                    alias = REAL_TO_ALIAS.get(room, "")
                    extra = f" -> alias {alias}" if alias else ""
                    capacity = capacity_map.get(room)
                    capacity_text = f", cap={capacity}" if isinstance(capacity, int) else ""
                    building = building_map.get(room, "")
                    building_text = f", building={building}" if building else ""
                    print(f"  {room}{extra}{building_text}{capacity_text}")

        # ---------------------------------------------------------------------
        # Real timetable usage, restricted to the configured scope when possible
        # ---------------------------------------------------------------------
        print_title("Real timetable usage validation with exact AT/AL sets")
        if has_table(cur, schema, "horarios"):
            hq = qual(schema, "horarios")
            h_cols = table_columns(cur, schema, "horarios")
            col_aula_h = pick_column(h_cols, ["aula", "salon", "aula_id", "clave_aula"])

            if not col_aula_h:
                print("(cannot verify: `horarios` does not expose a recognized room column)")
            else:
                if h_scope["ok"]:
                    sql = f"""
                        SELECT DISTINCT UPPER(REPLACE(TRIM(h.`{col_aula_h}`), '-', '')) AS aula
                        FROM {hq} h
                        {h_scope['joins']}
                        {_where_clause(h_scope['where'])}
                    """
                    used = fetchall(cur, sql, h_scope["params"])
                    used_set = {_norm_room(row[0]) for row in used if row and row[0]}
                    used_outside = sorted([room for room in used_set if room and not _room_allowed(room, at_set, al_set)])

                    print_line("Rooms used in target scope", len(used_set))
                    print_line("Rooms outside whitelist", len(used_outside))
                    if used_outside[:10]:
                        print_line("Examples outside", ", ".join(used_outside[:10]))

                    if EXPORT_CSV and used_outside:
                        _export_csv(
                            os.path.join(EXPORT_DIR, f"rooms_outside_whitelist_scope_{TARGET_PERIOD}.csv"),
                            ["room"],
                            [(room,) for room in used_outside],
                        )
                else:
                    col_periodo_h = pick_column(h_cols, ["periodo"])
                    if col_periodo_h:
                        used = fetchall(
                            cur,
                            f"""
                            SELECT DISTINCT UPPER(REPLACE(TRIM(`{col_aula_h}`), '-', '')) AS aula
                            FROM {hq}
                            WHERE `{col_periodo_h}`=%s
                            """,
                            (TARGET_PERIOD,),
                        )
                        used_set = {_norm_room(row[0]) for row in used if row and row[0]}
                        used_outside = sorted([room for room in used_set if room and not _room_allowed(room, at_set, al_set)])

                        print_line("Notice", "Could not restrict to the target scope; using global period data.")
                        print_line("Rooms used in global period", len(used_set))
                        print_line("Rooms outside whitelist", len(used_outside))
                        if used_outside[:10]:
                            print_line("Examples outside", ", ".join(used_outside[:10]))

                        if EXPORT_CSV and used_outside:
                            _export_csv(
                                os.path.join(EXPORT_DIR, f"rooms_outside_whitelist_global_{TARGET_PERIOD}.csv"),
                                ["room"],
                                [(room,) for room in used_outside],
                            )
                    else:
                        print("(cannot verify: `horarios` does not expose a period column)")
        else:
            print("(table `horarios` does not exist)")

    except mysql.connector.Error as exc:
        print(f"ERROR in room metrics: {getattr(exc, 'msg', str(exc))}")
    except Exception as exc:
        print(f"General error in room metrics: {exc}")

    # -------------------------------------------------------------------------
    # 8. Multi-period information
    # -------------------------------------------------------------------------
    if MULTI_PERIODS_RAW:
        print_title("MULTI_PERIODS information")
        periods = _parse_multi_periods(cur, schema, MULTI_PERIODS_RAW)
        if periods:
            print_line("Selected periods", ", ".join(periods))
        else:
            print("(MULTI_PERIODS did not return valid periods)")

    try:
        cur.close()
        cn.close()
    except Exception:
        pass

    print("\nMetrics completed.")

# -----------------------------------------------------------------------------
# High-level error handling
# -----------------------------------------------------------------------------

except mysql.connector.Error as exc:
    print(f"MySQL error: {getattr(exc, 'msg', str(exc))}")
except Exception as exc:
    print(f"General error: {exc}")

