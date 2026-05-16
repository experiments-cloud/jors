# bdtec_metricas.py
# ==========================================================================================
# DASHBOARD DE MÉTRICAS (solo lectura, salida por consola + CSV opcional)
# ------------------------------------------------------------------------------------------
# Ajustado para:
#   - Periodo fijo 20251 (o TARGET_PERIOD / MODEL_PERIODO normalizado)
#   - Alcance ISC por grupos priorizando exclusivo_carrera / exclusivo_reticula
#   - Fallback a carrera / reticula
#   - Último fallback: materias_carreras
#   - Exclusión explícita de NO ESCOLARIZADA (EXCLUDE_CARRERA_LIKE)
#   - AT exactas reales desde MODEL_AT_LIST
#   - AL exactas reales desde MODEL_AL_LIST_REAL (o MODEL_AL_LIST como fallback)
#   - Alias visuales opcionales para laboratorios vía LAB_ALIAS_MAP_JSON
#   - Validación robusta contra catálogo de aulas y tipo_aula
#   - Advertencia explícita cuando AL no está configurada
#   - Lista de laboratorios candidatos del catálogo para apoyar la selección de las 9 AL de ISC
# ------------------------------------------------------------------------------------------

import os
import re
import csv
import json
import mysql.connector
from dotenv import load_dotenv

# ------------------------------------------------------------------------------------------
# 1) Cargar .env y helpers
# ------------------------------------------------------------------------------------------
load_dotenv()


def get_env(key, default=None, required=False):
    v = os.getenv(key, default)
    if required and (v is None or str(v).strip() == ""):
        raise RuntimeError(f"Variable de entorno faltante: {key}")
    return v


def parse_bool(key, default=False):
    raw = os.getenv(key)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "si", "sí")


def parse_env_list(key, default_list=None):
    raw = (os.getenv(key) or "").strip()
    if raw:
        parts = re.split(r"[;,]", raw)
        return [p.strip().upper() for p in parts if p.strip()]
    return [str(x).strip().upper() for x in (default_list or []) if str(x).strip()]


def parse_json_env(key, default=None):
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default if default is not None else {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _norm_room(x):
    """
    Normaliza aulas para comparación:
    - trim
    - upper
    - elimina espacios y guiones

    Ejemplos:
      'F-19' -> 'F19'
      ' ff1 ' -> 'FF1'
    """
    if x is None:
        return ""
    s = str(x).strip().upper()
    s = s.replace(" ", "").replace("-", "")
    return s


def _norm_period(s):
    return re.sub(r"\D", "", str(s or "").strip())


def _like_value(s):
    return f"%{str(s).strip().upper()}%"


def _nonnull_trim_sql(alias, col):
    """
    Regresa expresión SQL segura en MySQL:
        NULLIF(TRIM(CAST(alias.`col` AS CHAR)), '')
    """
    return f"NULLIF(TRIM(CAST({alias}.`{col}` AS CHAR)), '')"


def print_title(t):
    print("\n" + "=" * len(t))
    print(t)
    print("=" * len(t))


def print_line(label, value, width=42):
    print(f"{label:>{width}}: {value}")


def qual(schema, table):
    return f"`{schema}`.`{table}`"


def has_table(cur, schema, name):
    cur.execute(
        """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
        """,
        (schema, name),
    )
    return cur.fetchone()[0] > 0


def table_columns(cur, schema, table):
    cur.execute(f"DESCRIBE {qual(schema, table)}")
    return [r[0] for r in cur.fetchall()]


def pick_column(cols, candidates):
    for c in candidates:
        if c in cols:
            return c
    return None


def run_scalar(cur, sql, params=None):
    cur.execute(sql, params or ())
    row = cur.fetchone()
    return row[0] if row else None


def fetchall(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def _export_csv(path, header, rows):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if header:
                w.writerow(header)
            for r in rows:
                if isinstance(r, (list, tuple)):
                    w.writerow(list(r))
                else:
                    w.writerow([r])
        print_line("CSV exportado", path)
    except Exception as e:
        print_line("CSV error", str(e))


def _where_clause(parts):
    return ("WHERE " + " AND ".join(parts)) if parts else ""


# ------------------------------------------------------------------------------------------
# 2) Room helpers (AT/AL exactas reales)
# ------------------------------------------------------------------------------------------
def _is_at_room(room, at_exact):
    return room in at_exact


def _is_al_room(room, al_exact):
    return room in al_exact


def _room_allowed(room, at_exact, al_exact):
    return room in at_exact or room in al_exact


def _catalog_rooms_in_at(A_set, at_exact):
    return sorted([a for a in A_set if a in at_exact])


def _catalog_rooms_in_al(A_set, al_exact):
    return sorted([a for a in A_set if a in al_exact])


def _catalog_rooms_outside(A_set, at_exact, al_exact):
    return sorted([a for a in A_set if a not in at_exact and a not in al_exact])


def _dict_reverse_unique(alias_to_real):
    out = {}
    for alias, real in alias_to_real.items():
        real_n = _norm_room(real)
        alias_n = str(alias).strip().upper()
        if real_n and alias_n and real_n not in out:
            out[real_n] = alias_n
    return out


# ------------------------------------------------------------------------------------------
# 3) Configuración
# ------------------------------------------------------------------------------------------
DB_CFG = {
    "host": get_env("DB_HOST", "localhost"),
    "port": int(get_env("DB_PORT", "3306")),
    "user": get_env("DB_USER", required=True),
    "password": get_env("DB_PASSWORD", required=True),
    "database": get_env("DB_NAME", required=True),
}

EXPORT_DIR = get_env("EXPORT_DIR", "salidas").strip()
EXPORT_CSV = parse_bool("EXPORT_CSV", False)
os.makedirs(EXPORT_DIR, exist_ok=True)

TARGET_PERIOD = _norm_period(get_env("TARGET_PERIOD", get_env("MODEL_PERIODO", "20251")))
if not TARGET_PERIOD:
    TARGET_PERIOD = "20251"

ONLY_TARGET_PERIOD = parse_bool("ONLY_TARGET_PERIOD", True)
MULTI_PERIODS_RAW = (os.getenv("MULTI_PERIODS") or "").strip() if not ONLY_TARGET_PERIOD else ""

MODEL_CARRERA_LIKE = str(get_env("MODEL_CARRERA_LIKE", "SISTEM")).strip().upper()
MODEL_NIVEL = str(get_env("MODEL_NIVEL", "L")).strip().upper()
EXCLUDE_CARRERA_LIKE = str(get_env("EXCLUDE_CARRERA_LIKE", "NO ESCOLARIZADA")).strip().upper()

DEFAULT_AT = ["FF1", "FF2", "FF3", "FF4", "FF5", "FF6", "FF7", "FF8", "FF9", "FFA", "FFB", "FFC", "FFD"]

AT_LIST = [_norm_room(x) for x in parse_env_list("MODEL_AT_LIST", default_list=DEFAULT_AT)]

# Nuevo flujo: preferir MODEL_AL_LIST_REAL; fallback a MODEL_AL_LIST por compatibilidad
AL_REAL_RAW = parse_env_list("MODEL_AL_LIST_REAL", default_list=[])
if not AL_REAL_RAW:
    AL_REAL_RAW = parse_env_list("MODEL_AL_LIST", default_list=[])

AL_REAL_LIST = [_norm_room(x) for x in AL_REAL_RAW]

LAB_ALIAS_MAP = parse_json_env("LAB_ALIAS_MAP_JSON", default={})
LAB_ALIAS_MAP = {str(k).strip().upper(): _norm_room(v) for k, v in LAB_ALIAS_MAP.items() if str(k).strip() and str(v).strip()}
REAL_TO_ALIAS = _dict_reverse_unique(LAB_ALIAS_MAP)

STRICT_ROOMSETS = parse_bool("STRICT_ROOMSETS", True)
SEM1_EXTRAS = parse_env_list("SEM1_EXTRAS", default_list=[])


# ------------------------------------------------------------------------------------------
# 4) Multi-period helpers
# ------------------------------------------------------------------------------------------
def _detect_period_column(cur, schema, table="grupos"):
    if not has_table(cur, schema, table):
        return None
    cols = table_columns(cur, schema, table)
    return pick_column(cols, ["periodo"])


def _fetch_periods_from_db(cur, schema, limit=None):
    colp = _detect_period_column(cur, schema, "grupos")
    if not colp:
        return []
    sql = f"SELECT DISTINCT `{colp}` AS p FROM {qual(schema, 'grupos')} ORDER BY `{colp}` DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        return [str(r[0]) for r in fetchall(cur, sql) if r and r[0] is not None]
    except Exception:
        return []


def _parse_multi_periods(cur, schema, raw):
    if not raw:
        return []
    up = raw.upper().strip()
    if up == "ALL":
        return [_norm_period(p) for p in _fetch_periods_from_db(cur, schema, None) if _norm_period(p)]
    if up.startswith("ALL_LAST:"):
        try:
            n = int(up.split(":", 1)[1])
        except Exception:
            n = 6
        return [_norm_period(p) for p in _fetch_periods_from_db(cur, schema, n) if _norm_period(p)]
    out = []
    for p in raw.split(","):
        pp = _norm_period(p)
        if pp:
            out.append(pp)
    return out


# ------------------------------------------------------------------------------------------
# 5) Scope ISC por grupos
# ------------------------------------------------------------------------------------------
def _build_group_scope(cur, schema, g_alias="g"):
    """
    Prioridad de identificación de carrera/retícula del grupo:
      1) exclusivo_carrera / exclusivo_reticula
      2) carrera / reticula
      3) materias_carreras (fallback final)
    """
    if not has_table(cur, schema, "grupos"):
        return {"ok": False, "reason": "No existe tabla `grupos`."}
    if not has_table(cur, schema, "carreras"):
        return {"ok": False, "reason": "No existe tabla `carreras`."}

    g_cols = table_columns(cur, schema, "grupos")
    c_cols = table_columns(cur, schema, "carreras")

    c_name = pick_column(c_cols, ["nombre_carrera", "nombre", "desc_carrera", "descripcion", "nombre_largo"])
    c_level = pick_column(c_cols, ["nivel_escolar", "nivel", "nivel_academico"])
    c_key = pick_column(c_cols, ["carrera", "id_carrera", "clave_carrera"])
    c_ret = pick_column(c_cols, ["reticula", "id_reticula", "plan"])

    if not (c_name and c_level and c_key and c_ret):
        return {"ok": False, "reason": "La tabla `carreras` no tiene columnas suficientes."}

    # Intento directo con grupo
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
            direct_strategy = "g->c (exclusivo_carrera/exclusivo_reticula -> carrera/reticula)"

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

    # Fallback final por materias_carreras
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
                "strategy": "g->mc->c (fallback)",
                "c_name": c_name,
                "c_level": c_level,
                "source": "mc",
            }

    return {
        "ok": False,
        "reason": "No fue posible construir el alcance ISC desde `grupos`.",
    }


def _group_period_filter(cur, schema, g_alias="g", target_period=None):
    if not has_table(cur, schema, "grupos"):
        return {"ok": False, "reason": "No existe tabla `grupos`."}
    g_cols = table_columns(cur, schema, "grupos")
    g_period = pick_column(g_cols, ["periodo"])
    if not g_period:
        return {"ok": False, "reason": "La tabla `grupos` no tiene columna `periodo`."}
    if not target_period:
        return {"ok": True, "where": [], "params": [], "g_period": g_period}
    return {
        "ok": True,
        "where": [f"{g_alias}.`{g_period}` = %s"],
        "params": [target_period],
        "g_period": g_period,
    }


# ------------------------------------------------------------------------------------------
# 6) Scope horarios enlazado a grupos ISC
# ------------------------------------------------------------------------------------------
def _build_horarios_scope(cur, schema, h_alias="h", g_alias="g", target_period=None):
    if not has_table(cur, schema, "horarios"):
        return {"ok": False, "reason": "No existe tabla `horarios`."}
    if not has_table(cur, schema, "grupos"):
        return {"ok": False, "reason": "No existe tabla `grupos`."}

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
            "reason": "No fue posible enlazar `horarios` con `grupos` con suficiente precisión.",
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
            return {"ok": False, "reason": "Ni `horarios` ni `grupos` exponen `periodo`."}

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


# ------------------------------------------------------------------------------------------
# 7) MAIN
# ------------------------------------------------------------------------------------------
try:
    print(
        f"Conectando a {DB_CFG['host']}:{DB_CFG['port']} "
        f"BD={DB_CFG['database']} como {DB_CFG['user']} ..."
    )
    cn = mysql.connector.connect(**DB_CFG)
    cur = cn.cursor()
    schema = DB_CFG["database"]

    print_title("Modo de ejecución")
    print_line("Periodo objetivo (fijo)", TARGET_PERIOD)
    print_line("Solo periodo objetivo", "Sí" if ONLY_TARGET_PERIOD else "No")
    print_line("Carrera LIKE", MODEL_CARRERA_LIKE or "—")
    print_line("Excluir carrera LIKE", EXCLUDE_CARRERA_LIKE or "—")
    print_line("Nivel", MODEL_NIVEL or "—")
    print_line("AT exactas", ", ".join(AT_LIST) if AT_LIST else "—")
    print_line("AL reales exactas", ", ".join(AL_REAL_LIST) if AL_REAL_LIST else "—")
    print_line("STRICT_ROOMSETS", "Sí" if STRICT_ROOMSETS else "No")
    print_line("SEM1_EXTRAS", ", ".join(SEM1_EXTRAS) if SEM1_EXTRAS else "—")
    print_line("Alias labs (A..I)", json.dumps(LAB_ALIAS_MAP, ensure_ascii=False) if LAB_ALIAS_MAP else "—")

    g_scope = _build_group_scope(cur, schema, g_alias="g")
    g_period_scope = _group_period_filter(cur, schema, g_alias="g", target_period=TARGET_PERIOD)
    h_scope = _build_horarios_scope(cur, schema, h_alias="h", g_alias="g", target_period=TARGET_PERIOD)

    print_title("Detección del alcance ISC")
    print_line("Scope grupos", g_scope["strategy"] if g_scope.get("ok") else g_scope.get("reason"))
    print_line("Scope horarios", h_scope["strategy"] if h_scope.get("ok") else h_scope.get("reason"))

    # --------------------------------------------------------------------------------------
    # 7.1 Totales por tabla
    # --------------------------------------------------------------------------------------
    print_title("Métricas BD 'bdtec'")
    metrics_sql = {
        "Total_niveles":           f"SELECT COUNT(*) FROM {qual(schema, 'nivel_escolar')}" if has_table(cur, schema, "nivel_escolar") else None,
        "Total_estatus":           f"SELECT COUNT(*) FROM {qual(schema, 'estatus_alumno')}" if has_table(cur, schema, "estatus_alumno") else None,
        "Total_planes":            f"SELECT COUNT(*) FROM {qual(schema, 'planes_de_estudio')}" if has_table(cur, schema, "planes_de_estudio") else None,
        "Total_carreras":          f"SELECT COUNT(*) FROM {qual(schema, 'carreras')}" if has_table(cur, schema, "carreras") else None,
        "Total_materias":          f"SELECT COUNT(*) FROM {qual(schema, 'materias')}" if has_table(cur, schema, "materias") else None,
        "Total_alumnos":           f"SELECT COUNT(*) FROM {qual(schema, 'alumnos')}" if has_table(cur, schema, "alumnos") else None,
        "Total_grupos (GLOBAL)":   f"SELECT COUNT(*) FROM {qual(schema, 'grupos')}" if has_table(cur, schema, "grupos") else None,
        "Total_horarios (GLOBAL)": f"SELECT COUNT(*) FROM {qual(schema, 'horarios')}" if has_table(cur, schema, "horarios") else None,
        "Total_aulas":             f"SELECT COUNT(*) FROM {qual(schema, 'aulas')}" if has_table(cur, schema, "aulas") else None,
    }

    metrics_out = []
    for k, q in metrics_sql.items():
        if not q:
            print_line(k, "N/D (tabla ausente)")
            metrics_out.append((k, "N/D"))
            continue
        try:
            val = run_scalar(cur, q)
            print_line(k, val)
            metrics_out.append((k, val))
        except mysql.connector.Error as e:
            msg = getattr(e, "msg", str(e))
            print_line(k, f"ERROR ({msg})")
            metrics_out.append((k, f"ERROR: {msg}"))

    # Totales periodo objetivo
    try:
        if has_table(cur, schema, "grupos") and g_period_scope["ok"]:
            g_cols = table_columns(cur, schema, "grupos")
            gp = pick_column(g_cols, ["periodo"])
            n = run_scalar(cur, f"SELECT COUNT(*) FROM {qual(schema, 'grupos')} WHERE `{gp}`=%s", (TARGET_PERIOD,))
            print_line("Total_grupos (PERIODO OBJ)", n)
            metrics_out.append(("Total_grupos (PERIODO OBJ)", n))
        else:
            print_line("Total_grupos (PERIODO OBJ)", "N/D")
            metrics_out.append(("Total_grupos (PERIODO OBJ)", "N/D"))

        if has_table(cur, schema, "horarios"):
            h_cols = table_columns(cur, schema, "horarios")
            hp = pick_column(h_cols, ["periodo"])
            if hp:
                n = run_scalar(cur, f"SELECT COUNT(*) FROM {qual(schema, 'horarios')} WHERE `{hp}`=%s", (TARGET_PERIOD,))
                print_line("Total_horarios (PERIODO OBJ)", n)
                metrics_out.append(("Total_horarios (PERIODO OBJ)", n))
            else:
                print_line("Total_horarios (PERIODO OBJ)", "N/D")
                metrics_out.append(("Total_horarios (PERIODO OBJ)", "N/D"))
    except mysql.connector.Error as e:
        print_line("Totales periodo obj", f"ERROR ({getattr(e, 'msg', str(e))})")

    # --------------------------------------------------------------------------------------
    # 7.2 Totales ISC
    # --------------------------------------------------------------------------------------
    print_title("Totales ISC (periodo objetivo)")
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
                total_isc_grupos = run_scalar(cur, sql, params)
                print_line("Grupos ISC", total_isc_grupos)
                metrics_out.append(("Grupos ISC", total_isc_grupos))
            else:
                print_line("Grupos ISC", "N/D (faltan columnas clave)")
                metrics_out.append(("Grupos ISC", "N/D"))
        else:
            print_line("Grupos ISC", g_scope.get("reason", "N/D"))
            metrics_out.append(("Grupos ISC", g_scope.get("reason", "N/D")))

        if h_scope["ok"]:
            sql = f"""
                SELECT COUNT(*)
                FROM {qual(schema, 'horarios')} h
                {h_scope['joins']}
                {_where_clause(h_scope['where'])}
            """
            total_isc_horarios = run_scalar(cur, sql, h_scope["params"])
            print_line("Horarios ISC", total_isc_horarios)
            metrics_out.append(("Horarios ISC", total_isc_horarios))
        else:
            print_line("Horarios ISC", h_scope.get("reason", "N/D"))
            metrics_out.append(("Horarios ISC", h_scope.get("reason", "N/D")))
    except mysql.connector.Error as e:
        print_line("Totales ISC", f"ERROR ({getattr(e, 'msg', str(e))})")

    if EXPORT_CSV:
        _export_csv(
            os.path.join(EXPORT_DIR, f"metricas_totales_{TARGET_PERIOD}.csv"),
            ["metrica", "valor"],
            metrics_out,
        )

    # --------------------------------------------------------------------------------------
    # 7.3 Carreras detectadas en alcance ISC
    # --------------------------------------------------------------------------------------
    print_title("Carreras detectadas en el alcance ISC")
    try:
        if g_scope["ok"] and g_period_scope["ok"]:
            c_name = g_scope.get("c_name")
            if c_name:
                where = list(g_scope["where"]) + list(g_period_scope["where"])
                params = list(g_scope["params"]) + list(g_period_scope["params"])
                sql = f"""
                    SELECT UPPER(TRIM(c.`{c_name}`)) AS carrera_nom, COUNT(*) AS total
                    FROM {qual(schema, 'grupos')} g
                    {g_scope['joins']}
                    {_where_clause(where)}
                    GROUP BY UPPER(TRIM(c.`{c_name}`))
                    ORDER BY total DESC, carrera_nom
                """
                rows = fetchall(cur, sql, params)
                if rows:
                    for nom, total in rows:
                        print(f"{nom:<60}: {total}")
                else:
                    print("(sin datos para el alcance ISC)")
            else:
                print("(no existe columna de nombre de carrera reconocible)")
        else:
            print(g_scope.get("reason", "(sin alcance ISC)"))
    except mysql.connector.Error as e:
        print(f"ERROR al listar carreras ISC: {getattr(e, 'msg', str(e))}")

    # --------------------------------------------------------------------------------------
    # 7.4 Distribución de grupos ISC por periodo
    # --------------------------------------------------------------------------------------
    print_title("Distribución de grupos ISC por periodo")
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
                    SELECT g.`{gp}` AS periodo, COUNT(DISTINCT {distinct_key}) AS total
                    FROM {qual(schema, 'grupos')} g
                    {g_scope['joins']}
                    {_where_clause(where)}
                    GROUP BY g.`{gp}`
                    ORDER BY g.`{gp}`
                """
                rows = fetchall(cur, sql, params)
                if rows:
                    for per, total in rows:
                        print(f"{per}\t{total}")
                else:
                    print("(sin datos)")
            else:
                print("(faltan columnas clave de grupos para esta métrica)")
        else:
            print(g_scope.get("reason", "(sin alcance ISC)"))
    except mysql.connector.Error as e:
        print(f"ERROR al listar distribución ISC por periodo: {getattr(e, 'msg', str(e))}")

    # --------------------------------------------------------------------------------------
    # 7.5 Muestra de carreras filtradas por ISC
    # --------------------------------------------------------------------------------------
    print_title("Muestra: carreras filtradas por ISC")
    try:
        if g_scope["ok"] and g_period_scope["ok"]:
            c_name = g_scope.get("c_name")
            c_level = g_scope.get("c_level")
            if c_name:
                where = list(g_scope["where"]) + list(g_period_scope["where"])
                params = list(g_scope["params"]) + list(g_period_scope["params"])
                extra_level = f", UPPER(TRIM(c.`{c_level}`)) AS nivel" if c_level else ""
                sql = f"""
                    SELECT DISTINCT
                        UPPER(TRIM(c.`{c_name}`)) AS carrera_nom
                        {extra_level}
                    FROM {qual(schema, 'grupos')} g
                    {g_scope['joins']}
                    {_where_clause(where)}
                    ORDER BY carrera_nom
                    LIMIT 50
                """
                rows = fetchall(cur, sql, params)
                if rows:
                    if c_level:
                        print(f"{'nivel':<6} carrera")
                        print("-" * 90)
                        for nom, niv in rows:
                            print(f"{str(niv):<6} {str(nom)}")
                    else:
                        for (nom,) in rows:
                            print(nom)
                else:
                    print("(sin datos)")
            else:
                print("(sin columna de nombre de carrera)")
        else:
            print(g_scope.get("reason", "(sin alcance ISC)"))
    except mysql.connector.Error as e:
        print(f"ERROR al listar muestra de carreras: {getattr(e, 'msg', str(e))}")

    # --------------------------------------------------------------------------------------
    # 7.6 Muestra de grupos ISC del periodo objetivo
    # --------------------------------------------------------------------------------------
    print_title("Muestra: 20 grupos ISC del periodo objetivo")
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

            def add_col(col, alias_name):
                if col:
                    select_cols.append(f"g.`{col}`")
                    headers.append(alias_name)

            add_col(gp, "periodo")
            add_col(gm, "materia")
            add_col(gg, "grupo")
            add_col(gi, "inscritos")
            add_col(gc, "capacidad")
            add_col(gr, "rfc")
            add_col(gexc, "exclusivo_carrera")
            add_col(gexr, "exclusivo_reticula")
            add_col(gcar, "carrera")
            add_col(gret, "reticula")

            if not select_cols:
                print("(no hay columnas suficientes para la muestra)")
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
                    print("Periodo objetivo =", TARGET_PERIOD)
                    print("\t".join(headers))
                    for r in rows:
                        print("\t".join("" if x is None else str(x) for x in r))
                else:
                    print("(sin grupos ISC en el periodo objetivo)")
        else:
            print(g_scope.get("reason", "(sin alcance ISC)"))
    except mysql.connector.Error as e:
        print(f"ERROR al listar grupos ISC del periodo objetivo: {getattr(e, 'msg', str(e))}")

    # --------------------------------------------------------------------------------------
    # 7.7 Aulas ISC + validaciones exactas
    # --------------------------------------------------------------------------------------
    print_title("Aulas ISC – catálogo y validaciones (AT/AL exactas)")
    try:
        print_line("AT exactas", ", ".join(AT_LIST) if AT_LIST else "—")
        print_line("AL reales exactas", ", ".join(AL_REAL_LIST) if AL_REAL_LIST else "—")
        print_line("Periodo objetivo", TARGET_PERIOD)

        A_catalog = []
        cap_map = {}
        tipo_map = {}
        edif_map = {}

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

                for r in rows:
                    idx = 0
                    room = _norm_room(r[idx]); idx += 1
                    if not room:
                        continue
                    A_catalog.append(room)

                    if col_cap:
                        try:
                            cap_map[room] = int(r[idx]) if r[idx] is not None else None
                        except Exception:
                            cap_map[room] = None
                        idx += 1
                    else:
                        cap_map[room] = None

                    if col_tipo:
                        tipo_map[room] = str(r[idx]).strip().upper() if r[idx] is not None else ""
                        idx += 1
                    else:
                        tipo_map[room] = ""

                    if col_edif:
                        edif_map[room] = str(r[idx]).strip() if r[idx] is not None else ""
                    else:
                        edif_map[room] = ""

            else:
                print("(tabla `aulas` sin columna de clave reconocible)")
        else:
            print("(no existe tabla `aulas`)")

        A_set = set(A_catalog)
        AT_set = set(AT_LIST)
        AL_set = set(AL_REAL_LIST)

        in_AT = _catalog_rooms_in_at(A_set, AT_set)
        in_AL = _catalog_rooms_in_al(A_set, AL_set)
        outside = _catalog_rooms_outside(A_set, AT_set, AL_set)

        print_line("|A| catálogo BD", len(A_set))
        print_line("|AT| definidas", len(AT_set))
        print_line("|AL| definidas", len(AL_set))
        print_line("Aulas BD ∩ AT", len(in_AT))
        print_line("Aulas BD ∩ AL", len(in_AL))
        print_line("Aulas BD fuera de AT/AL", len(outside))

        at_not_in_A = sorted(list(AT_set - A_set))
        al_not_in_A = sorted(list(AL_set - A_set))
        if at_not_in_A:
            print_line("AT fuera de BD (ej.)", ", ".join(at_not_in_A[:10]))
        if al_not_in_A:
            print_line("AL fuera de BD (ej.)", ", ".join(al_not_in_A[:10]))
        if outside[:10]:
            print_line("Ejemplos fuera de AT/AL", ", ".join(outside[:10]))

        # Validar contra tipo_aula del catálogo
        at_wrong_tipo = sorted([r for r in in_AT if tipo_map.get(r, "") and tipo_map.get(r, "") != "A"])
        al_wrong_tipo = sorted([r for r in in_AL if tipo_map.get(r, "") and tipo_map.get(r, "") != "L"])

        print_line("AT con tipo != 'A'", len(at_wrong_tipo))
        if at_wrong_tipo[:10]:
            print_line("Ejemplos AT tipo raro", ", ".join(at_wrong_tipo[:10]))

        print_line("AL con tipo != 'L'", len(al_wrong_tipo))
        if al_wrong_tipo[:10]:
            print_line("Ejemplos AL tipo raro", ", ".join(al_wrong_tipo[:10]))

        # Alias de laboratorios
        if REAL_TO_ALIAS:
            print_title("Alias de laboratorios (visual)")
            for real in sorted(AL_set):
                print_line(real, REAL_TO_ALIAS.get(real, "—"))

        # Top capacidad
        known_caps = [(k, v) for k, v in cap_map.items() if isinstance(v, int)]
        if known_caps:
            top_caps = sorted(known_caps, key=lambda t: (t[1], t[0]), reverse=True)[:10]
            print("\nTop 10 aulas por capacidad conocida:")
            for a, c in top_caps:
                print(f"  {a:>6s}: {c}")
        else:
            print("(sin columna de capacidad en `aulas` o no disponible)")

        # Resumen por tipo_aula
        if tipo_map:
            print_title("Resumen de catálogo por tipo_aula")
            tipos = {}
            for _room, tipo in tipo_map.items():
                clave = tipo if tipo else "SIN_TIPO"
                tipos[clave] = tipos.get(clave, 0) + 1
            for tipo, total in sorted(tipos.items(), key=lambda x: x[0]):
                print_line(f"tipo_aula={tipo}", total)

        # NUEVO: advertencia y candidatos de laboratorios si AL está vacía
        if not AL_set:
            print_title("Advertencia: laboratorios ISC no configurados")
            print("No hay aulas de laboratorio reales definidas en MODEL_AL_LIST_REAL / AL_HARD.")
            print("Por eso, el dashboard reporta |AL|=0 y no puede validar laboratorios de ISC todavía.")

            lab_candidates = sorted([r for r in A_set if tipo_map.get(r, "") == "L"])
            print_line("Laboratorios candidatos en catálogo", len(lab_candidates))
            if lab_candidates:
                print("Candidatos (tipo_aula='L'):")
                for room in lab_candidates:
                    alias = REAL_TO_ALIAS.get(room, "")
                    extra = f" -> alias {alias}" if alias else ""
                    cap = cap_map.get(room)
                    cap_txt = f", cap={cap}" if isinstance(cap, int) else ""
                    edif = edif_map.get(room, "")
                    edif_txt = f", edificio={edif}" if edif else ""
                    print(f"  {room}{extra}{edif_txt}{cap_txt}")

        # ----------------------------------------------------------------------------------
        # Uso real en horarios, restringido a ISC cuando sea posible
        # ----------------------------------------------------------------------------------
        print_title("Validación uso real en HORARIOS (AT/AL exactas, alcance ISC)")
        if has_table(cur, schema, "horarios"):
            hq = qual(schema, "horarios")
            h_cols = table_columns(cur, schema, "horarios")
            col_aula_h = pick_column(h_cols, ["aula", "salon", "aula_id", "clave_aula"])

            if not col_aula_h:
                print("(no fue posible verificar: `horarios` sin columna de aula reconocible)")
            else:
                if h_scope["ok"]:
                    sql = f"""
                        SELECT DISTINCT UPPER(REPLACE(TRIM(h.`{col_aula_h}`), '-', '')) AS aula
                        FROM {hq} h
                        {h_scope['joins']}
                        {_where_clause(h_scope['where'])}
                    """
                    used = fetchall(cur, sql, h_scope["params"])
                    used_set = {_norm_room(u[0]) for u in used if u and u[0]}
                    used_outside = sorted([a for a in used_set if a and not _room_allowed(a, AT_set, AL_set)])

                    print_line("Aulas usadas ISC", len(used_set))
                    print_line("Aulas fuera de whitelist", len(used_outside))
                    if used_outside[:10]:
                        print_line("Ejemplos (fuera)", ", ".join(used_outside[:10]))

                    if EXPORT_CSV and used_outside:
                        _export_csv(
                            os.path.join(EXPORT_DIR, f"aulas_fuera_whitelist_isc_{TARGET_PERIOD}.csv"),
                            ["aula"],
                            [(a,) for a in used_outside],
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
                        used_set = {_norm_room(u[0]) for u in used if u and u[0]}
                        used_outside = sorted([a for a in used_set if a and not _room_allowed(a, AT_set, AL_set)])

                        print_line("Aviso", "No se pudo restringir a ISC; se muestra validación global del periodo.")
                        print_line("Aulas usadas (global periodo)", len(used_set))
                        print_line("Aulas fuera de whitelist", len(used_outside))
                        if used_outside[:10]:
                            print_line("Ejemplos (fuera)", ", ".join(used_outside[:10]))

                        if EXPORT_CSV and used_outside:
                            _export_csv(
                                os.path.join(EXPORT_DIR, f"aulas_fuera_whitelist_global_{TARGET_PERIOD}.csv"),
                                ["aula"],
                                [(a,) for a in used_outside],
                            )
                    else:
                        print("(no fue posible verificar: `horarios` sin columna de periodo reconocible)")
        else:
            print("(no existe tabla `horarios`)")

    except mysql.connector.Error as e:
        print(f"ERROR en métricas de aulas: {getattr(e, 'msg', str(e))}")
    except Exception as e:
        print(f"ERROR general en métricas de aulas: {e}")

    # --------------------------------------------------------------------------------------
    # 7.8 Multi-periodo (informativo)
    # --------------------------------------------------------------------------------------
    if MULTI_PERIODS_RAW:
        print_title("MULTI_PERIODS (informativo)")
        periods = _parse_multi_periods(cur, schema, MULTI_PERIODS_RAW)
        if periods:
            print_line("Periodos seleccionados", ", ".join(periods))
        else:
            print("(MULTI_PERIODS no arrojó periodos válidos)")

    try:
        cur.close()
        cn.close()
    except Exception:
        pass

    print("\nMétricas completadas.")

# ------------------------------------------------------------------------------------------
# 8) Manejo de errores de alto nivel
# ------------------------------------------------------------------------------------------
except mysql.connector.Error as e:
    print(f"Error de MySQL: {getattr(e, 'msg', str(e))}")
except Exception as e:
    print(f"Error general: {e}")

