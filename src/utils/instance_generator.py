# build_datos_modelo_isc.py
# ==========================================================================================
# Constructor de datos para el modelo ISC
# ------------------------------------------------------------------------------------------
# Este script:
#   1. Lee la configuración desde .env.
#   2. Extrae información de la base de datos bdtec.
#   3. Filtra el alcance a Ingeniería en Sistemas Computacionales, nivel Licenciatura,
#      excluyendo explícitamente la modalidad no escolarizada.
#   4. Construye los conjuntos y parámetros requeridos por el modelo.
#   5. Emite un archivo JSON listo para el solver.
# ==========================================================================================

import os
import re
import json
import math
import hashlib
import pathlib
import shutil
from collections import Counter, defaultdict

import mysql.connector
from dotenv import load_dotenv

load_dotenv()


class Logger:
    def __init__(self):
        width = shutil.get_terminal_size(fallback=(110, 24)).columns
        self.width = max(80, min(140, width))
        self.sql_counter = 0
        self.debug_sql = str(os.getenv("DEBUG_SQL", "0")).strip().lower() in ("1", "true", "yes")

    def _rule(self, text=""):
        text = str(text or "").strip()
        if not text:
            print("-" * self.width)
            return
        prefix = f" {text} "
        n = self.width - len(prefix)
        if n < 1:
            n = 1
        print(prefix + ("-" * n))

    def section(self, title):
        self._rule(title)

    def kv(self, key, value):
        print(f"{str(key).strip():>30}: {value}")

    def info(self, msg):
        print(f"[INFO] {msg}")

    def warn(self, msg):
        print(f"[WARN] {msg}")

    def error(self, msg):
        print(f"[ERROR] {msg}")

    def ok(self, msg):
        print(f"[OK] {msg}")

    def sql(self, query, params=None):
        if not self.debug_sql:
            return
        self.sql_counter += 1
        self._rule(f"SQL #{self.sql_counter}")
        print(query)
        print(f"params={params}")

    def table(self, title, headers, rows, max_rows=25):
        rows = rows[:max_rows]
        widths = [len(str(h)) for h in headers]
        for row in rows:
            for j, value in enumerate(row):
                widths[j] = max(widths[j], len(str(value)))

        self._rule(title)
        sep = "+".join("-" * (w + 2) for w in widths)
        print("+" + sep + "+")
        print("|" + "|".join(f" {str(headers[i]).ljust(widths[i])} " for i in range(len(headers))) + "|")
        print("+" + sep + "+")
        for row in rows:
            print("|" + "|".join(f" {str(row[i]).ljust(widths[i])} " for i in range(len(headers))) + "|")
        print("+" + sep + "+")


log = Logger()


def get_env(key, default=None, required=False):
    value = os.getenv(key, default)
    if required and (value is None or str(value).strip() == ""):
        raise RuntimeError(f"Variable de entorno faltante: {key}")
    return value


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "si", "sí")


def parse_env_list(raw_value, default=None):
    raw = str(raw_value or "").strip()
    if not raw:
        return list(default or [])
    return [token.strip() for token in re.split(r"[\s,;]+", raw) if token.strip()]


def parse_json_dict(raw_value):
    raw = str(raw_value or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def parse_json_list(raw_value):
    raw = str(raw_value or "").strip()
    if not raw:
        return []
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, list) else []
    except Exception:
        return []


def norm_text(x):
    return str(x or "").strip()


def norm_upper(x):
    return str(x or "").strip().upper()


def norm_room(x):
    return norm_upper(x).replace(" ", "").replace("-", "")


def norm_period(x):
    return re.sub(r"\D", "", norm_text(x))


def safe_int(x, default=None):
    try:
        return int(x) if x is not None else default
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default


def parse_hours_env(raw):
    value = norm_text(raw)
    if not value:
        return None

    m = re.match(r"^(\d{1,2})\s*[-:]\s*(\d{1,2})$", value)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        return [f"{h:02d}" for h in range(a, b + 1)]

    if "," in value:
        hours = []
        for token in value.split(","):
            token = token.strip()
            if token.isdigit():
                hours.append(f"{int(token):02d}")
        return hours or None

    return None


def mk_gkey_cohort(grupo, carrera, reticula, semestre, turno, periodo):
    return "|".join([
        norm_upper(grupo),
        norm_upper(carrera),
        norm_upper(reticula),
        norm_upper(semestre),
        norm_upper(turno),
        norm_upper(periodo),
    ])


def mk_gkey_course(grupo, carrera, reticula, materia, turno, periodo):
    return "|".join([
        norm_upper(grupo),
        norm_upper(carrera),
        norm_upper(reticula),
        norm_upper(materia),
        norm_upper(turno),
        norm_upper(periodo),
    ])


def sql_nonnull_trim(alias, col):
    return f"NULLIF(TRIM(CAST({alias}.`{col}` AS CHAR)), '')"


def sql_coalesce_group_key_expr(alias, primary_col, fallback_col):
    parts = []
    if primary_col:
        parts.append(sql_nonnull_trim(alias, primary_col))
    if fallback_col and fallback_col != primary_col:
        parts.append(sql_nonnull_trim(alias, fallback_col))
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"


# ------------------------------------------------------------------------------------------
# Helpers para diversificar aulas teóricas por curso (sin tocar laboratorios)
# ------------------------------------------------------------------------------------------
def _stable_int_seed(*parts) -> int:
    txt = "|".join(str(x or "").strip().upper() for x in parts)
    return int(hashlib.md5(txt.encode("utf-8")).hexdigest()[:8], 16)


def _rotate_list(seq, k):
    seq = list(seq or [])
    if not seq:
        return seq
    k = k % len(seq)
    return seq[k:] + seq[:k]


def _dedup_keep_order(seq):
    out = []
    seen = set()
    for x in seq:
        x = norm_room(x)
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _build_diversified_theory_candidates(
    materia,
    grupo,
    ff_rooms,
    backup_rooms,
    target_total=12,
    n_ff=8,
    n_backup=4,
):
    ff_rooms = _dedup_keep_order(ff_rooms)
    backup_rooms = _dedup_keep_order(backup_rooms)

    target_total = max(1, int(target_total))
    n_ff = max(0, int(n_ff))
    n_backup = max(0, int(n_backup))

    seed = _stable_int_seed(materia, grupo)

    ff_rot = _rotate_list(ff_rooms, seed % max(1, len(ff_rooms))) if ff_rooms else []
    bk_rot = _rotate_list(backup_rooms, (seed // 7) % max(1, len(backup_rooms))) if backup_rooms else []

    picked_ff = ff_rot[:min(n_ff, len(ff_rot))]
    picked_bk = bk_rot[:min(n_backup, len(bk_rot))]

    # junta y deduplica
    combined = _dedup_keep_order(picked_ff + picked_bk)

    # >>> ESTA ES LA CORRECCIÓN CLAVE <<<
    # recorta SIEMPRE al límite total
    if len(combined) > target_total:
        combined = combined[:target_total]

    # si faltan candidatas, completa hasta target_total
    if len(combined) < target_total:
        remaining = _dedup_keep_order(ff_rot + bk_rot)
        for r in remaining:
            if r not in combined:
                combined.append(r)
            if len(combined) >= target_total:
                break

    return combined


DB_CFG = {
    "host": get_env("DB_HOST", "localhost"),
    "port": int(get_env("DB_PORT", "3306")),
    "user": get_env("DB_USER", required=True),
    "password": get_env("DB_PASSWORD", ""),
    "database": get_env("DB_NAME", required=True),
}

TARGET_PERIOD = norm_period(get_env("TARGET_PERIOD", get_env("MODEL_PERIODO", "20251"))) or "20251"
ONLY_TARGET_PERIOD = parse_bool(get_env("ONLY_TARGET_PERIOD", "1"), True)
MULTI_PERIODS_RAW = "" if ONLY_TARGET_PERIOD else norm_text(get_env("MULTI_PERIODS", ""))

MODEL_CARRERA_LIKE = norm_upper(get_env("MODEL_CARRERA_LIKE", "SISTEM"))
MODEL_NIVEL = norm_upper(get_env("MODEL_NIVEL", "L"))
EXCLUDE_CARRERA_LIKE = norm_upper(get_env("EXCLUDE_CARRERA_LIKE", "NO ESCOLARIZADA"))

DEFAULT_GROUP_SIZE = safe_int(get_env("DEFAULT_GROUP_SIZE", 30), 30)
DEFAULT_AULA_CAP = safe_int(get_env("DEFAULT_AULA_CAP", 35), 35)
DEFAULT_HREQ = safe_int(get_env("DEFAULT_HREQ", 3), 3)
DEFAULT_MINH = safe_int(get_env("DEFAULT_MINH", 0), 0)
DEFAULT_MAXH = safe_int(get_env("DEFAULT_MAXH", 25), 25)

A_PM_DIRECT = 10
A_PM_HIGH = 1000

G_KEY_MODE = norm_upper(get_env("G_KEY_MODE", "COHORT"))
AUTO_SPLIT_G = parse_bool(get_env("AUTO_SPLIT_G", "1"), True)
G_SPLIT_PREF = norm_upper(get_env("G_SPLIT_PREF", "RFC"))

STRICT_ROOMSETS = parse_bool(get_env("STRICT_ROOMSETS", "1"), True)
SEM1_EXTRAS = [norm_upper(x) for x in parse_env_list(get_env("SEM1_EXTRAS", ""))]

FORCE_D = [norm_upper(x) for x in parse_env_list(get_env("FORCE_D_FROM_ENV", ""))]
FORCE_H = parse_hours_env(get_env("FORCE_H_FROM_ENV", ""))
H_OVERRIDE = safe_int(get_env("H_OVERRIDE", None), None)

DATOS_JSON_TEMPLATE = get_env("DATOS_JSON", "salidas/datos_modelo_{periodo}.json")
LAB_COURSE_REGEX = re.compile(
    get_env("LAB_COURSE_REGEX", r"(?i)\b(LAB|LABORATORIO|PR(A|Á)CTIC(A|AS))\b"),
    re.IGNORECASE,
)

LAB_TIPO_MATERIA_VALUES = {norm_upper(x) for x in parse_env_list(get_env("LAB_TIPO_MATERIA_VALUES", ""))}
LAB_CLAVE_AREA_VALUES = {norm_upper(x) for x in parse_env_list(get_env("LAB_CLAVE_AREA_VALUES", ""))}
LAB_MATERIAS_SET = {norm_text(x) for x in parse_env_list(get_env("LAB_MATERIAS_LIST", ""))}

HREQ_MATERIAS_MAP = {
    norm_text(k): safe_int(v, None)
    for k, v in parse_json_dict(get_env("HREQ_MATERIAS_JSON", "{}")).items()
}
HREQ_REGEX_RULES_RAW = parse_json_list(get_env("HREQ_REGEX_RULES_JSON", "[]"))
HREQ_REGEX_RULES = []
for item in HREQ_REGEX_RULES_RAW:
    if not isinstance(item, dict):
        continue
    pattern = item.get("pattern")
    hours = safe_int(item.get("hours"), None)
    if pattern and hours is not None and hours > 0:
        try:
            HREQ_REGEX_RULES.append((re.compile(str(pattern), re.IGNORECASE), int(hours)))
        except re.error:
            pass

LOW_HREQ_PATTERNS = [
    re.compile(r"(?i)\bTUTOR(I|Í)A(S)?\b"),
    re.compile(r"(?i)\bRESIDENCIA PROFESIONAL\b"),
]

NONTECH_WORKSHOP_PATTERNS = [
    re.compile(r"(?i)\bTALLER DE [ÉE]TICA\b"),
    re.compile(r"(?i)\bTALLER DE ADMINISTRACI[ÓO]N\b"),
    re.compile(r"(?i)\bTALLER DE INVESTIGACI[ÓO]N I\b"),
    re.compile(r"(?i)\bTALLER DE INVESTIGACI[ÓO]N II\b"),
]

TECH_WORKSHOP_PATTERNS = [
    re.compile(r"(?i)\bTALLER DE SISTEMAS OPERATIVOS\b"),
    re.compile(r"(?i)\bTALLER DE BASE DE DATOS\b"),
]

DEFAULT_AT = ["FF1", "FF2", "FF3", "FF4", "FF5", "FF6", "FF7", "FF8", "FF9", "FFA", "FFB", "FFC", "FFD"]

AT_RAW = parse_env_list(get_env("AT_HARD", ""))
if not AT_RAW:
    AT_RAW = parse_env_list(get_env("MODEL_AT_LIST", ""), default=DEFAULT_AT)
AT_CONF = {norm_room(x) for x in AT_RAW if norm_room(x)}

AL_RAW = parse_env_list(get_env("AL_HARD", ""))
if not AL_RAW:
    AL_RAW = parse_env_list(get_env("MODEL_AL_LIST_REAL", ""))
if not AL_RAW:
    AL_RAW = parse_env_list(get_env("MODEL_AL_LIST", ""))
AL_CONF = {norm_room(x) for x in AL_RAW if norm_room(x)}

LAB_ALIAS_MAP = parse_json_dict(get_env("LAB_ALIAS_MAP_JSON", "{}"))
LAB_ALIAS_MAP = {
    norm_upper(alias): norm_room(real)
    for alias, real in LAB_ALIAS_MAP.items()
    if norm_upper(alias) and norm_room(real)
}
REAL_TO_ALIAS = {}
for alias, real in LAB_ALIAS_MAP.items():
    if real not in REAL_TO_ALIAS:
        REAL_TO_ALIAS[real] = alias


# ------------------------------------------------------------------------------------------
# Configuración de whitelist diversificada para teoría
# ------------------------------------------------------------------------------------------
ENABLE_DIVERSIFIED_THEORY_WHITELIST = parse_bool(
    get_env("ENABLE_DIVERSIFIED_THEORY_WHITELIST", "1"), True
)
THEORY_WL_FF_COUNT = safe_int(get_env("THEORY_WL_FF_COUNT", 8), 8) or 8
THEORY_WL_BACKUP_COUNT = safe_int(get_env("THEORY_WL_BACKUP_COUNT", 4), 4) or 4
THEORY_WL_TOTAL_LIMIT = safe_int(get_env("THEORY_WL_TOTAL_LIMIT", 12), 12) or 12

PREFERRED_AT_LIST = _dedup_keep_order(parse_env_list(get_env("PREFERRED_AT_LIST", "")))
BACKUP_AT_LIST = _dedup_keep_order(parse_env_list(get_env("BACKUP_AT_LIST", "")))

PREFERRED_AT_LIST = [r for r in PREFERRED_AT_LIST if r in AT_CONF]
BACKUP_AT_LIST = [r for r in BACKUP_AT_LIST if r in AT_CONF and r not in set(PREFERRED_AT_LIST)]


log.section("Builder ISC")
log.kv("Script", pathlib.Path(__file__).resolve())
log.kv("Python", os.sys.executable)
log.kv("Periodo objetivo", TARGET_PERIOD)
log.kv("Solo periodo objetivo", "SI" if ONLY_TARGET_PERIOD else "NO")
log.kv("Carrera LIKE", MODEL_CARRERA_LIKE)
log.kv("Excluir carrera LIKE", EXCLUDE_CARRERA_LIKE)
log.kv("Nivel", MODEL_NIVEL)
log.kv("G_KEY_MODE", G_KEY_MODE)
log.kv("AUTO_SPLIT_G", "SI" if AUTO_SPLIT_G else "NO")
log.kv("STRICT_ROOMSETS", "SI" if STRICT_ROOMSETS else "NO")
log.kv("|AT| configuradas", len(AT_CONF))
log.kv("|AL| configuradas", len(AL_CONF))
log.kv("Whitelist teoría diversificada", "SI" if ENABLE_DIVERSIFIED_THEORY_WHITELIST else "NO")
log.kv("THEORY_WL_FF_COUNT", THEORY_WL_FF_COUNT)
log.kv("THEORY_WL_BACKUP_COUNT", THEORY_WL_BACKUP_COUNT)
log.kv("THEORY_WL_TOTAL_LIMIT", THEORY_WL_TOTAL_LIMIT)

conn = mysql.connector.connect(**DB_CFG)
cur = conn.cursor()
SCHEMA = DB_CFG["database"]


def sql_fetchall(query, params=None):
    log.sql(query, params)
    cur.execute(query, params or ())
    return cur.fetchall()


def sql_scalar(query, params=None):
    log.sql(query, params)
    cur.execute(query, params or ())
    row = cur.fetchone()
    return row[0] if row else None


def has_relation(name):
    q = """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
    """
    cur.execute(q, (SCHEMA, name))
    return cur.fetchone()[0] > 0


def has_view(name):
    q = """
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.VIEWS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
    """
    cur.execute(q, (SCHEMA, name))
    return cur.fetchone()[0] > 0


def table_columns(name):
    cur.execute(f"DESCRIBE `{SCHEMA}`.`{name}`")
    return [r[0] for r in cur.fetchall()]


def pick_column(cols, candidates):
    for c in candidates:
        if c in cols:
            return c
    return None

A_DB = []
CAP_A_DB = {}
TIPO_A_DB = {}
EDIF_A_DB = {}
COURSE_INFO = {}
M_IS_LAB = {}
M_TEXT = {}
M_BASE_HREQ = {}
M_TIPO = {}
M_AREA = {}


def merge_course_info(code, text=None, hreq=None, ht=None, hp=None, tipo_materia=None, clave_area=None, source=None):
    code_n = norm_text(code)
    if not code_n:
        return

    info = COURSE_INFO.setdefault(code_n, {
        "text": "",
        "hreq": None,
        "ht": None,
        "hp": None,
        "tipo_materia": "",
        "clave_area": "",
        "is_lab": False,
        "sources": set(),
    })

    text_n = norm_text(text)
    if text_n and len(text_n) > len(info["text"]):
        info["text"] = text_n

    tipo_n = norm_upper(tipo_materia)
    area_n = norm_upper(clave_area)

    if tipo_n and not info["tipo_materia"]:
        info["tipo_materia"] = tipo_n
    if area_n and not info["clave_area"]:
        info["clave_area"] = area_n

    if hreq is not None and hreq > 0 and info["hreq"] is None:
        info["hreq"] = int(hreq)

    if ht is not None and ht >= 0 and info["ht"] is None:
        info["ht"] = int(ht)

    if hp is not None and hp >= 0 and info["hp"] is None:
        info["hp"] = int(hp)

    if info["hreq"] is None:
        ht_v = info["ht"] if info["ht"] is not None else 0
        hp_v = info["hp"] if info["hp"] is not None else 0
        if ht_v > 0 or hp_v > 0:
            info["hreq"] = ht_v + hp_v

    if info["text"] and LAB_COURSE_REGEX.search(info["text"]):
        info["is_lab"] = True
    if info["hp"] is not None and info["hp"] > 0 and (info["ht"] is None or info["ht"] == 0):
        info["is_lab"] = True
    if LAB_TIPO_MATERIA_VALUES and info["tipo_materia"] in LAB_TIPO_MATERIA_VALUES:
        info["is_lab"] = True
    if LAB_CLAVE_AREA_VALUES and info["clave_area"] in LAB_CLAVE_AREA_VALUES:
        info["is_lab"] = True
    if LAB_MATERIAS_SET and code_n in LAB_MATERIAS_SET:
        info["is_lab"] = True

    if source:
        info["sources"].add(source)


def load_room_catalog():
    if not has_relation("aulas"):
        log.warn("No existe la tabla `aulas`.")
        return

    cols = table_columns("aulas")
    col_room = pick_column(cols, ["aula", "clave", "id_aula", "codigo", "nombre_aula", "salon"])
    col_cap = pick_column(cols, ["capacidad_aula", "capacidad", "cupo", "aforo"])
    col_tipo = pick_column(cols, ["tipo_aula"])
    col_edif = pick_column(cols, ["edificio"])

    if not col_room:
        log.warn("La tabla `aulas` no tiene columna de nombre reconocible.")
        return

    select_cols = [f"`{col_room}`"]
    if col_cap:
        select_cols.append(f"`{col_cap}`")
    if col_tipo:
        select_cols.append(f"`{col_tipo}`")
    if col_edif:
        select_cols.append(f"`{col_edif}`")

    rows = sql_fetchall(f"SELECT {', '.join(select_cols)} FROM `{SCHEMA}`.`aulas`")
    for row in rows:
        idx = 0
        room = norm_room(row[idx])
        idx += 1
        if not room:
            continue

        if room not in A_DB:
            A_DB.append(room)

        if col_cap:
            CAP_A_DB[room] = safe_int(row[idx], DEFAULT_AULA_CAP)
            idx += 1
        else:
            CAP_A_DB[room] = DEFAULT_AULA_CAP

        if col_tipo:
            TIPO_A_DB[room] = norm_upper(row[idx])
            idx += 1
        else:
            TIPO_A_DB[room] = ""

        if col_edif:
            EDIF_A_DB[room] = norm_text(row[idx])
        else:
            EDIF_A_DB[room] = ""

    for room in A_DB:
        CAP_A_DB.setdefault(room, DEFAULT_AULA_CAP)
        TIPO_A_DB.setdefault(room, "")
        EDIF_A_DB.setdefault(room, "")

    log.info(f"Aulas leídas desde BD: {len(A_DB)}")


def build_a_tipo(aulas):
    result = {}
    for room in aulas:
        result[room] = "L" if room in AL_CONF else "T"
    return result


def load_course_catalog():
    if not has_relation("materias"):
        log.warn("No existe la tabla `materias`; se usará clasificación mínima por defecto.")
        return

    cols = table_columns("materias")
    col_code = pick_column(cols, ["materia", "id_materia", "clave_materia", "codigo"])
    col_name_full = pick_column(cols, ["nombre_completo_materia", "nombre_materia", "nombre", "descripcion"])
    col_name_short = pick_column(cols, ["nombre_abreviado_materia", "nombre_abreviado", "abreviatura"])
    col_tipo = pick_column(cols, ["tipo_materia"])
    col_area = pick_column(cols, ["clave_area"])
    col_hreq = pick_column(cols, ["horas_semana", "hreq", "horas", "horas_totales", "total_horas", "hrs"])
    col_ht = pick_column(cols, ["horas_teoricas", "ht", "horas_teoria", "hteoria"])
    col_hp = pick_column(cols, ["horas_practicas", "hp", "horas_practica", "horas_lab", "hlab"])

    if not col_code:
        log.warn("La tabla `materias` no tiene una columna de clave reconocible.")
        return

    select_cols = [f"`{col_code}`"]
    extra_cols = [col_name_full, col_name_short, col_tipo, col_area, col_hreq, col_ht, col_hp]
    for c in extra_cols:
        if c:
            select_cols.append(f"`{c}`")

    rows = sql_fetchall(f"SELECT {', '.join(select_cols)} FROM `{SCHEMA}`.`materias`")
    for row in rows:
        idx = 0
        code = norm_text(row[idx])
        idx += 1

        full_name = norm_text(row[idx]) if col_name_full else ""
        idx += 1 if col_name_full else 0

        short_name = norm_text(row[idx]) if col_name_short else ""
        idx += 1 if col_name_short else 0

        tipo_materia = norm_text(row[idx]) if col_tipo else ""
        idx += 1 if col_tipo else 0

        clave_area = norm_text(row[idx]) if col_area else ""
        idx += 1 if col_area else 0

        hreq = safe_int(row[idx], None) if col_hreq else None
        idx += 1 if col_hreq else 0

        ht = safe_int(row[idx], None) if col_ht else None
        idx += 1 if col_ht else 0

        hp = safe_int(row[idx], None) if col_hp else None
        idx += 1 if col_hp else 0

        text_parts = [full_name, short_name]
        if tipo_materia:
            text_parts.append(f"TIPO:{tipo_materia}")
        if clave_area:
            text_parts.append(f"AREA:{clave_area}")
        text = " | ".join([t for t in text_parts if t])

        merge_course_info(
            code,
            text=text,
            hreq=hreq,
            ht=ht,
            hp=hp,
            tipo_materia=tipo_materia,
            clave_area=clave_area,
            source="materias",
        )

    for code, info in COURSE_INFO.items():
        M_IS_LAB[code] = bool(info.get("is_lab", False))
        M_TEXT[code] = info.get("text", "")
        M_BASE_HREQ[code] = info.get("hreq", None)
        M_TIPO[code] = info.get("tipo_materia", "")
        M_AREA[code] = info.get("clave_area", "")


def infer_hreq_for_materia(materia):
    materia_n = norm_text(materia)

    base_h = M_BASE_HREQ.get(materia_n)
    if base_h is not None and base_h > 0:
        return int(base_h), "catalogo"

    if materia_n in HREQ_MATERIAS_MAP and HREQ_MATERIAS_MAP[materia_n] is not None and HREQ_MATERIAS_MAP[materia_n] > 0:
        return int(HREQ_MATERIAS_MAP[materia_n]), "override_clave"

    text = M_TEXT.get(materia_n, "")

    for pattern in LOW_HREQ_PATTERNS:
        if pattern.search(text):
            return 1, "heuristica_baja"

    for pattern in NONTECH_WORKSHOP_PATTERNS:
        if pattern.search(text):
            return 3, "taller_no_tecnico"

    for pattern, hours in HREQ_REGEX_RULES:
        if pattern.search(text):
            return int(hours), "regex"

    for pattern in TECH_WORKSHOP_PATTERNS:
        if pattern.search(text):
            return 4, "taller_tecnico"

    if M_IS_LAB.get(materia_n, False):
        return 4, "laboratorio"

    return DEFAULT_HREQ, "default"


def enrich_hreq_and_types():
    counters = Counter()
    lab_detected = 0

    for materia, g_key in MG:
        mg_key = f"{materia}|{g_key}"

        if mg_key not in HREQ_MAP:
            hreq_value, source = infer_hreq_for_materia(materia)
            HREQ_MAP[mg_key] = int(hreq_value)
            counters[source] += 1

        if M_IS_LAB.get(materia, False):
            lab_detected += 1

    log.kv("MG Hreq desde catalogo", counters.get("catalogo", 0))
    log.kv("MG Hreq desde override", counters.get("override_clave", 0))
    log.kv("MG Hreq heuristica baja", counters.get("heuristica_baja", 0))
    log.kv("MG Hreq taller no tecnico", counters.get("taller_no_tecnico", 0))
    log.kv("MG Hreq desde regex", counters.get("regex", 0))
    log.kv("MG Hreq taller tecnico", counters.get("taller_tecnico", 0))
    log.kv("MG Hreq por laboratorio", counters.get("laboratorio", 0))
    log.kv("MG Hreq por default", counters.get("default", 0))
    log.kv("MG clasificadas como laboratorio", lab_detected)


def build_days():
    if FORCE_D:
        return FORCE_D[:]

    if has_relation("dias"):
        cols = table_columns("dias")
        col_day = pick_column(cols, ["dia", "nombre", "clave", "id"])
        if col_day:
            raw = [norm_upper(r[0])[:1] for r in sql_fetchall(f"SELECT `{col_day}` FROM `{SCHEMA}`.`dias`")]
            cleaned = [d for d in raw if d in ("L", "M", "X", "J", "V", "S")]
            if cleaned:
                return cleaned

    return ["L", "M", "X", "J", "V"]


def build_hours():
    if FORCE_H:
        return FORCE_H[:]

    if H_OVERRIDE and H_OVERRIDE > 0:
        return [f"{i:02d}" for i in range(1, H_OVERRIDE + 1)]

    return [f"{h:02d}" for h in range(7, 22)]


load_room_catalog()
load_course_catalog()
D = build_days()
H = build_hours()


def detect_period_column():
    if not has_relation("grupos"):
        return None
    return pick_column(table_columns("grupos"), ["periodo"])


def periods_from_db(limit=None):
    period_col = detect_period_column()
    if not period_col:
        return []
    sql = f"SELECT DISTINCT `{period_col}` FROM `{SCHEMA}`.`grupos` ORDER BY `{period_col}` DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = sql_fetchall(sql)
    return [str(r[0]) for r in rows if r and r[0] is not None]


def parse_multi_periods(raw):
    raw = norm_text(raw)
    if not raw:
        return []

    up = raw.upper()
    if up == "ALL":
        return [norm_period(p) for p in periods_from_db() if norm_period(p)]

    if up.startswith("ALL_LAST:"):
        try:
            n = int(up.split(":", 1)[1])
        except Exception:
            n = 6
        return [norm_period(p) for p in periods_from_db(limit=n) if norm_period(p)]

    return [norm_period(p) for p in raw.split(",") if norm_period(p)]


period_list = []
if MULTI_PERIODS_RAW:
    period_list = parse_multi_periods(MULTI_PERIODS_RAW)
if ONLY_TARGET_PERIOD or not period_list:
    period_list = [TARGET_PERIOD]

    MG = []
G = []
SIZE_G = {}
P_OBS = set()
M_OBS = set()
SEEN_MG = set()
SEEN_G = set()
PM_DIRECT = set()
HREQ_MAP = {}
MG_RFC = {}
WRITTEN = []
SEMS_OBS = set()
TURNS_OBS = set()


def reset_accumulators():
    MG.clear()
    G.clear()
    SIZE_G.clear()
    P_OBS.clear()
    M_OBS.clear()
    SEEN_MG.clear()
    SEEN_G.clear()
    PM_DIRECT.clear()
    HREQ_MAP.clear()
    MG_RFC.clear()
    SEMS_OBS.clear()
    TURNS_OBS.clear()


def add_row(materia, grupo, capacidad, inscritos, rfc, carrera, reticula, semestre, turno, periodo, horas):
    materia_n = norm_text(materia)
    carrera_n = norm_text(carrera)
    reticula_n = norm_text(reticula)
    semestre_n = norm_text(semestre)
    turno_n = norm_text(turno)
    periodo_n = norm_text(periodo)

    if not materia_n or not grupo:
        return

    if G_KEY_MODE == "COURSE":
        g_key = mk_gkey_course(grupo, carrera_n, reticula_n, materia_n, turno_n, periodo_n)
    else:
        g_key = mk_gkey_cohort(grupo, carrera_n, reticula_n, semestre_n, turno_n, periodo_n)

    if not g_key:
        return

    pair = (materia_n, g_key)
    if pair not in SEEN_MG:
        MG.append([materia_n, g_key])
        SEEN_MG.add(pair)

    if g_key not in SEEN_G:
        G.append(g_key)
        SEEN_G.add(g_key)

    M_OBS.add(materia_n)

    cap = safe_int(capacidad, None)
    if cap is None:
        cap = safe_int(inscritos, None)
    if cap is None:
        cap = DEFAULT_GROUP_SIZE
    SIZE_G[g_key] = max(SIZE_G.get(g_key, 0), cap)

    rfc_n = norm_upper(rfc)
    if rfc_n:
        P_OBS.add(rfc_n)
        PM_DIRECT.add((rfc_n, materia_n))
        MG_RFC[f"{materia_n}|{g_key}"] = rfc_n

    if semestre_n:
        SEMS_OBS.add(semestre_n)
    if turno_n:
        TURNS_OBS.add(turno_n)

    h = safe_int(horas, None)
    if h is not None and h > 0:
        HREQ_MAP[f"{materia_n}|{g_key}"] = h


def route_from_view(periodo):
    view_name = "v_grupos_sistemas_actual"
    if not has_view(view_name):
        return False

    if not sql_scalar(f"SELECT COUNT(*) FROM `{SCHEMA}`.`{view_name}`"):
        return False

    cols = table_columns(view_name)
    cm = pick_column(cols, ["materia", "id_materia", "clave_materia"])
    cg = pick_column(cols, ["grupo"])
    cr = pick_column(cols, ["rfc", "profesor", "id_personal", "nombre"])
    cc = pick_column(cols, ["capacidad_grupo", "cupo", "capacidad"])
    ci = pick_column(cols, ["alumnos_inscritos", "inscritos", "matriculados"])
    ccarr = pick_column(cols, ["carrera", "id_carrera", "clave_carrera"])
    cret = pick_column(cols, ["reticula", "id_reticula", "plan"])
    csem = pick_column(cols, ["semestre", "sem", "grado"])
    cturn = pick_column(cols, ["turno", "turno_grupo", "jornada"])
    cper = pick_column(cols, ["periodo"])
    chrs = pick_column(cols, ["horas_semana", "hreq", "horas", "hrs", "horas_totales"])

    if not cm or not cg:
        return False

    sql = (
        f"SELECT `{cm}`, `{cg}`, "
        f"{('`' + cc + '`') if cc else 'NULL'}, "
        f"{('`' + ci + '`') if ci else 'NULL'}, "
        f"{('`' + cr + '`') if cr else 'NULL'}, "
        f"{('`' + ccarr + '`') if ccarr else 'NULL'}, "
        f"{('`' + cret + '`') if cret else 'NULL'}, "
        f"{('`' + csem + '`') if csem else 'NULL'}, "
        f"{('`' + cturn + '`') if cturn else 'NULL'}, "
        f"{('`' + cper + '`') if cper else 'NULL'}, "
        f"{('`' + chrs + '`') if chrs else 'NULL'} "
        f"FROM `{SCHEMA}`.`{view_name}`"
    )
    params = ()
    if periodo and cper:
        sql += f" WHERE TRIM(CAST(`{cper}` AS CHAR))=%s"
        params = (str(periodo),)

    rows = sql_fetchall(sql, params)
    if not rows:
        return False

    for m, g, cap, ins, rfc, carr, ret, sem, tur, per, hrs in rows:
        add_row(m, g, cap, ins, rfc, carr, ret, sem, tur, per if per is not None else periodo, hrs)

    log.ok(f"Extracción desde vista {view_name}: {len(rows)} filas")
    return True


def route_groups_to_carreras(periodo):
    if not (has_relation("grupos") and has_relation("carreras")):
        return False

    gcols = table_columns("grupos")
    ccols = table_columns("carreras")

    gm = pick_column(gcols, ["materia", "id_materia", "clave_materia"])
    gg = pick_column(gcols, ["grupo"])
    gr = pick_column(gcols, ["rfc", "id_personal", "profesor", "nombre"])
    gc = pick_column(gcols, ["capacidad_grupo", "cupo", "capacidad"])
    gi = pick_column(gcols, ["alumnos_inscritos", "inscritos", "matriculados"])
    gh = pick_column(gcols, ["horas_semana", "hreq", "horas", "hrs", "horas_totales"])
    gp = pick_column(gcols, ["periodo"])
    gs = pick_column(gcols, ["semestre", "sem", "grado", "nivel"])
    gt = pick_column(gcols, ["turno", "turno_grupo", "jornada"])

    g_exc_carr = pick_column(gcols, ["exclusivo_carrera"])
    g_exc_ret = pick_column(gcols, ["exclusivo_reticula"])
    g_carr = pick_column(gcols, ["carrera", "id_carrera", "clave_carrera"])
    g_ret = pick_column(gcols, ["reticula", "id_reticula", "plan"])

    c_carr = pick_column(ccols, ["carrera", "id_carrera", "clave_carrera"])
    c_ret = pick_column(ccols, ["reticula", "id_reticula", "plan"])
    c_level = pick_column(ccols, ["nivel_escolar", "nivel", "nivel_academico"])
    c_name = pick_column(ccols, ["nombre_carrera", "nombre", "desc_carrera", "descripcion", "nombre_largo"])

    if not (gm and gg and c_carr and c_ret and c_name):
        return False

    carr_expr = sql_coalesce_group_key_expr("g", g_exc_carr, g_carr)
    ret_expr = sql_coalesce_group_key_expr("g", g_exc_ret, g_ret)

    if not carr_expr or not ret_expr:
        return False

    where_parts = []
    params = []

    if c_level and MODEL_NIVEL:
        where_parts.append(f"UPPER(TRIM(c.`{c_level}`))=%s")
        params.append(MODEL_NIVEL)

    if MODEL_CARRERA_LIKE:
        where_parts.append(f"UPPER(TRIM(c.`{c_name}`)) LIKE %s")
        params.append(f"%{MODEL_CARRERA_LIKE}%")

    if EXCLUDE_CARRERA_LIKE:
        where_parts.append(f"UPPER(TRIM(c.`{c_name}`)) NOT LIKE %s")
        params.append(f"%{EXCLUDE_CARRERA_LIKE}%")

    if periodo and gp:
        where_parts.append(f"TRIM(CAST(g.`{gp}` AS CHAR))=%s")
        params.append(str(periodo))

    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    sql = f"""
        SELECT
            g.`{gm}`,
            g.`{gg}`,
            {('g.`' + gc + '`') if gc else 'NULL'},
            {('g.`' + gi + '`') if gi else 'NULL'},
            {('g.`' + gr + '`') if gr else 'NULL'},
            {carr_expr} AS carrera_eff,
            {ret_expr} AS reticula_eff,
            {('g.`' + gs + '`') if gs else 'NULL'},
            {('g.`' + gt + '`') if gt else 'NULL'},
            {('g.`' + gp + '`') if gp else 'NULL'},
            {('g.`' + gh + '`') if gh else 'NULL'}
        FROM `{SCHEMA}`.`grupos` g
        JOIN `{SCHEMA}`.`carreras` c
          ON TRIM(CAST(c.`{c_carr}` AS CHAR)) = {carr_expr}
         AND TRIM(CAST(c.`{c_ret}` AS CHAR))  = {ret_expr}
        WHERE {where_clause}
    """

    rows = sql_fetchall(sql, tuple(params))
    if not rows:
        return False

    for m, g, cap, ins, rfc, carr, ret, sem, tur, per, hrs in rows:
        add_row(m, g, cap, ins, rfc, carr, ret, sem, tur, per if per is not None else periodo, hrs)

    log.ok(f"Extracción principal grupos->carreras: {len(rows)} filas")
    return True


def route_groups_to_mc_to_carreras(periodo):
    if not (has_relation("grupos") and has_relation("materias_carreras") and has_relation("carreras")):
        return False

    gcols = table_columns("grupos")
    mccols = table_columns("materias_carreras")
    ccols = table_columns("carreras")

    gm = pick_column(gcols, ["materia", "id_materia", "clave_materia"])
    gg = pick_column(gcols, ["grupo"])
    gr = pick_column(gcols, ["rfc", "id_personal", "profesor", "nombre"])
    gc = pick_column(gcols, ["capacidad_grupo", "cupo", "capacidad"])
    gi = pick_column(gcols, ["alumnos_inscritos", "inscritos", "matriculados"])
    gh = pick_column(gcols, ["horas_semana", "hreq", "horas", "hrs", "horas_totales"])
    gp = pick_column(gcols, ["periodo"])
    gs = pick_column(gcols, ["semestre", "sem", "grado", "nivel"])
    gt = pick_column(gcols, ["turno", "turno_grupo", "jornada"])

    g_exc_carr = pick_column(gcols, ["exclusivo_carrera"])
    g_exc_ret = pick_column(gcols, ["exclusivo_reticula"])
    g_carr = pick_column(gcols, ["carrera", "id_carrera", "clave_carrera"])
    g_ret = pick_column(gcols, ["reticula", "id_reticula", "plan"])

    mc_mat = pick_column(mccols, ["materia", "id_materia", "clave_materia"])
    mc_carr = pick_column(mccols, ["carrera", "id_carrera", "clave_carrera"])
    mc_ret = pick_column(mccols, ["reticula", "id_reticula", "plan"])

    c_carr = pick_column(ccols, ["carrera", "id_carrera", "clave_carrera"])
    c_ret = pick_column(ccols, ["reticula", "id_reticula", "plan"])
    c_level = pick_column(ccols, ["nivel_escolar", "nivel", "nivel_academico"])
    c_name = pick_column(ccols, ["nombre_carrera", "nombre", "desc_carrera", "descripcion", "nombre_largo"])

    if not (gm and gg and mc_mat and mc_carr and mc_ret and c_carr and c_ret and c_name):
        return False

    carr_expr = sql_coalesce_group_key_expr("g", g_exc_carr, g_carr)
    ret_expr = sql_coalesce_group_key_expr("g", g_exc_ret, g_ret)

    carrera_eff_expr = carr_expr if carr_expr else f"TRIM(CAST(mc.`{mc_carr}` AS CHAR))"
    reticula_eff_expr = ret_expr if ret_expr else f"TRIM(CAST(mc.`{mc_ret}` AS CHAR))"

    where_parts = []
    params = []

    if c_level and MODEL_NIVEL:
        where_parts.append(f"UPPER(TRIM(c.`{c_level}`))=%s")
        params.append(MODEL_NIVEL)

    if MODEL_CARRERA_LIKE:
        where_parts.append(f"UPPER(TRIM(c.`{c_name}`)) LIKE %s")
        params.append(f"%{MODEL_CARRERA_LIKE}%")

    if EXCLUDE_CARRERA_LIKE:
        where_parts.append(f"UPPER(TRIM(c.`{c_name}`)) NOT LIKE %s")
        params.append(f"%{EXCLUDE_CARRERA_LIKE}%")

    if periodo and gp:
        where_parts.append(f"TRIM(CAST(g.`{gp}` AS CHAR))=%s")
        params.append(str(periodo))

    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    sql = f"""
        SELECT
            g.`{gm}`,
            g.`{gg}`,
            {('g.`' + gc + '`') if gc else 'NULL'},
            {('g.`' + gi + '`') if gi else 'NULL'},
            {('g.`' + gr + '`') if gr else 'NULL'},
            {carrera_eff_expr} AS carrera_eff,
            {reticula_eff_expr} AS reticula_eff,
            {('g.`' + gs + '`') if gs else 'NULL'},
            {('g.`' + gt + '`') if gt else 'NULL'},
            {('g.`' + gp + '`') if gp else 'NULL'},
            {('g.`' + gh + '`') if gh else 'NULL'}
        FROM `{SCHEMA}`.`grupos` g
        JOIN `{SCHEMA}`.`materias_carreras` mc
          ON TRIM(CAST(mc.`{mc_mat}` AS CHAR)) = TRIM(CAST(g.`{gm}` AS CHAR))
        JOIN `{SCHEMA}`.`carreras` c
          ON TRIM(CAST(c.`{c_carr}` AS CHAR)) = TRIM(CAST(mc.`{mc_carr}` AS CHAR))
         AND TRIM(CAST(c.`{c_ret}` AS CHAR))  = TRIM(CAST(mc.`{mc_ret}` AS CHAR))
        WHERE {where_clause}
    """

    rows = sql_fetchall(sql, tuple(params))
    if not rows:
        return False

    for m, g, cap, ins, rfc, carr, ret, sem, tur, per, hrs in rows:
        add_row(m, g, cap, ins, rfc, carr, ret, sem, tur, per if per is not None else periodo, hrs)

    log.ok(f"Extracción fallback grupos->materias_carreras->carreras: {len(rows)} filas")
    return True

def capacity_per_week():
    cap = len(D) * len(H)
    return cap if cap > 0 else 1


def demand_by_group():
    demand = {}
    mg_by_g = defaultdict(list)
    for materia, g_key in MG:
        hours = int(HREQ_MAP.get(f"{materia}|{g_key}", DEFAULT_HREQ))
        demand[g_key] = demand.get(g_key, 0) + hours
        mg_by_g[g_key].append((materia, g_key))
    return demand, mg_by_g


def print_overloaded_groups(demand, cap_week):
    rows = []
    for g_key, total in sorted(demand.items(), key=lambda x: -x[1]):
        if total > cap_week:
            rows.append([g_key, total, cap_week, total - cap_week])
    if rows:
        log.table(
            "Grupos con demanda superior a la capacidad semanal",
            ["Grupo", "Demanda", "Capacidad", "Exceso"],
            rows,
        )
    return rows


def auto_split_groups():
    if not AUTO_SPLIT_G:
        return 0, []

    cap_week = capacity_per_week()
    demand, mg_by_g = demand_by_group()
    overload = [(g_key, demand[g_key]) for g_key in demand if demand[g_key] > cap_week]
    if not overload:
        return 0, []

    global MG, G, SIZE_G, HREQ_MAP, MG_RFC, SEEN_MG, SEEN_G

    split_count = 0
    report = []

    def h_mg(materia, g_key):
        return int(HREQ_MAP.get(f"{materia}|{g_key}", DEFAULT_HREQ))

    for g_key, total in sorted(overload, key=lambda x: -x[1]):
        items = mg_by_g.get(g_key, [])
        if not items:
            continue

        parts = int(math.ceil(total / cap_week))
        buckets = {}

        for materia, g_old in items:
            mg_key = f"{materia}|{g_old}"
            bucket_key = MG_RFC.get(mg_key, "") if G_SPLIT_PREF == "RFC" else materia
            buckets.setdefault(bucket_key, []).append((materia, g_old))

        target_keys = [f"{g_key}~S{i + 1}" for i in range(parts)]
        load = [0] * parts
        assign = {k: [] for k in target_keys}

        grouped = []
        for bucket_key, bucket_items in buckets.items():
            bucket_load = sum(h_mg(m, g) for (m, g) in bucket_items)
            grouped.append((bucket_key, bucket_load, bucket_items))
        grouped.sort(key=lambda x: -x[1])

        for _, bucket_load, bucket_items in grouped:
            idx = min(range(parts), key=lambda j: load[j])
            assign[target_keys[idx]].extend(bucket_items)
            load[idx] += bucket_load

        old_items_set = set(items)
        old_size = SIZE_G.get(g_key, DEFAULT_GROUP_SIZE)

        MG = [pair for pair in MG if tuple(pair) not in old_items_set]

        if g_key in G:
            G.remove(g_key)
        SEEN_G.discard(g_key)
        SIZE_G.pop(g_key, None)

        old_hreq = {}
        old_rfc = {}
        for materia, g_old in items:
            old_hreq[materia] = HREQ_MAP.get(f"{materia}|{g_old}", DEFAULT_HREQ)
            old_rfc[materia] = MG_RFC.get(f"{materia}|{g_old}", "")
            HREQ_MAP.pop(f"{materia}|{g_old}", None)
            MG_RFC.pop(f"{materia}|{g_old}", None)
            SEEN_MG.discard((materia, g_old))

        for new_key in target_keys:
            if new_key not in G:
                G.append(new_key)
            SEEN_G.add(new_key)
            SIZE_G[new_key] = old_size

        for new_key in target_keys:
            for materia, _old in assign[new_key]:
                pair = (materia, new_key)
                if pair not in SEEN_MG:
                    MG.append([materia, new_key])
                    SEEN_MG.add(pair)
                HREQ_MAP[f"{materia}|{new_key}"] = old_hreq.get(materia, DEFAULT_HREQ)
                MG_RFC[f"{materia}|{new_key}"] = old_rfc.get(materia, "")

        split_count += 1
        report.append([g_key, parts, total, cap_week])

    return split_count, report


def compute_output_path(template, periodo):
    if "{periodo}" in template:
        return template.format(periodo=periodo or "ALL")
    root, ext = os.path.splitext(template)
    ext = ext if ext else ".json"
    return f"{root}_{periodo or 'ALL'}{ext}"



def build_room_whitelist_by_course():
    """
    Genera una whitelist por curso:
      - Laboratorios: se dejan completos (todos los AL válidos por capacidad).
      - Teoría: se diversifica FF + respaldo para que no todos los cursos vean
        exactamente el mismo subconjunto fuerte.
    """
    room_wl = {}
    theory_sizes = []
    lab_sizes = []
    examples = []

    at_all = sorted(AT_CONF)
    al_all = sorted(AL_CONF)
    preferred = set(PREFERRED_AT_LIST)
    backup = set(BACKUP_AT_LIST)

    for materia, g_key in MG:
        key = f"{materia}|{g_key}"
        group_size = SIZE_G.get(g_key, DEFAULT_GROUP_SIZE)

        if M_IS_LAB.get(materia, False):
            valid_labs = [a for a in al_all if CAP_A_DB.get(a, DEFAULT_AULA_CAP) >= group_size]
            room_wl[key] = valid_labs[:]
            lab_sizes.append(len(room_wl[key]))
            continue

        valid_at = [a for a in at_all if CAP_A_DB.get(a, DEFAULT_AULA_CAP) >= group_size]

        if not ENABLE_DIVERSIFIED_THEORY_WHITELIST:
            room_wl[key] = valid_at[:]
            theory_sizes.append(len(room_wl[key]))
            continue

        ff_rooms = [r for r in valid_at if r in preferred]
        backup_rooms = [r for r in valid_at if r in backup]
        other_rooms = [r for r in valid_at if r not in preferred and r not in backup]

        diversified = _build_diversified_theory_candidates(
            materia=materia,
            grupo=g_key,
            ff_rooms=ff_rooms,
            backup_rooms=backup_rooms + other_rooms,
            target_total=min(THEORY_WL_TOTAL_LIMIT, max(1, len(valid_at))),
            n_ff=THEORY_WL_FF_COUNT,
            n_backup=THEORY_WL_BACKUP_COUNT,
        )

        if not diversified:
            diversified = valid_at[:min(len(valid_at), THEORY_WL_TOTAL_LIMIT)]

        room_wl[key] = diversified
        theory_sizes.append(len(diversified))

        if len(examples) < 10:
            examples.append([materia, g_key, len(diversified), ", ".join(diversified)])

    log.section("Whitelist por curso (builder)")
    log.kv("Cursos con whitelist", len(room_wl))
    log.kv("Promedio teoría", f"{(sum(theory_sizes) / max(1, len(theory_sizes))):.2f}" if theory_sizes else "0.00")
    log.kv("Promedio laboratorio", f"{(sum(lab_sizes) / max(1, len(lab_sizes))):.2f}" if lab_sizes else "0.00")
    if examples:
        log.table("Ejemplos de whitelist teórica", ["Materia", "Grupo", "Tam", "Aulas"], examples, max_rows=10)

    return room_wl


def emit_json(out_path, periodo, at_list, al_list, aulas_finales, room_whitelist=None):
    if STRICT_ROOMSETS:
        aulas_finales = sorted(set(at_list) | set(al_list))
    else:
        aulas_finales = sorted(set(aulas_finales) | set(at_list) | set(al_list) | set(A_DB))

    cap_a = {a: CAP_A_DB.get(a, DEFAULT_AULA_CAP) for a in aulas_finales}
    a_tipo = build_a_tipo(aulas_finales)

    p_sorted = sorted(P_OBS)
    m_sorted = sorted(M_OBS)

    a_pm = {f"{p}|{m}": A_PM_HIGH for p in p_sorted for m in m_sorted}
    for p, m in PM_DIRECT:
        if p in P_OBS and m in M_OBS:
            a_pm[f"{p}|{m}"] = A_PM_DIRECT

    hreq = {}
    for m, g_key in MG:
        hreq[f"{m}|{g_key}"] = HREQ_MAP.get(f"{m}|{g_key}", DEFAULT_HREQ)

    minh = {p: DEFAULT_MINH for p in p_sorted}
    maxh = {p: DEFAULT_MAXH for p in p_sorted}
    u = {}

    room_whitelist = room_whitelist or {}

    payload = {
        "P": p_sorted,
        "A": aulas_finales,
        "D": D,
        "H": H,
        "M": m_sorted,
        "G": G,
        "MG": MG,
        "cap_A": cap_a,
        "size_G": SIZE_G,
        "a_pm": a_pm,
        "Hreq": hreq,
        "MinH": minh,
        "MaxH": maxh,
        "U": u,
        "AT": sorted(at_list),
        "AL": sorted(al_list),
        "AL_sem1_extras": SEM1_EXTRAS,
        "A_tipo": a_tipo,
        "PERIODO": periodo,
        "STRICT_ROOMSETS": STRICT_ROOMSETS,
        "M_is_lab": {m: bool(M_IS_LAB.get(m, False)) for m in m_sorted},
        "M_text": {m: M_TEXT.get(m, "") for m in m_sorted},
        "M_tipo": {m: M_TIPO.get(m, "") for m in m_sorted},
        "M_area": {m: M_AREA.get(m, "") for m in m_sorted},

        # compatibilidad para solver/dashboard
        "WhitelistRooms": room_whitelist,
        "ROOM_WHITELIST_BY_COURSE": room_whitelist,

        # trazabilidad
        "PREFERRED_AT_LIST": PREFERRED_AT_LIST,
        "BACKUP_AT_LIST": BACKUP_AT_LIST,
        "ENABLE_DIVERSIFIED_THEORY_WHITELIST": ENABLE_DIVERSIFIED_THEORY_WHITELIST,
        "THEORY_WL_FF_COUNT": THEORY_WL_FF_COUNT,
        "THEORY_WL_BACKUP_COUNT": THEORY_WL_BACKUP_COUNT,
        "THEORY_WL_TOTAL_LIMIT": THEORY_WL_TOTAL_LIMIT,
    }

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    WRITTEN.append(out_path)
    log.ok(f"JSON escrito: {out_path}")
    log.kv("|P|", len(p_sorted))
    log.kv("|M|", len(m_sorted))
    log.kv("|G|", len(G))
    log.kv("|MG|", len(MG))
    log.kv("|A|", len(aulas_finales))


def room_summary():
    log.section("Resumen de aulas configuradas")

    a_set = set(A_DB)
    in_at = sorted([a for a in AT_CONF if a in a_set])
    in_al = sorted([a for a in AL_CONF if a in a_set])

    log.kv("|A| catálogo BD", len(A_DB))
    log.kv("|AT| configuradas", len(AT_CONF))
    log.kv("|AL| configuradas", len(AL_CONF))
    log.kv("Whitelist teoría diversificada", "SI" if ENABLE_DIVERSIFIED_THEORY_WHITELIST else "NO")
    log.kv("THEORY_WL_FF_COUNT", THEORY_WL_FF_COUNT)
    log.kv("THEORY_WL_BACKUP_COUNT", THEORY_WL_BACKUP_COUNT)
    log.kv("THEORY_WL_TOTAL_LIMIT", THEORY_WL_TOTAL_LIMIT)
    log.kv("AT encontradas en BD", len(in_at))
    log.kv("AL encontradas en BD", len(in_al))

    at_missing = sorted([a for a in AT_CONF if a not in a_set])
    al_missing = sorted([a for a in AL_CONF if a not in a_set])

    if at_missing:
        log.warn(f"AT no encontradas en catálogo: {at_missing[:10]}")
    if al_missing:
        log.warn(f"AL no encontradas en catálogo: {al_missing[:10]}")

    at_wrong = sorted([a for a in in_at if TIPO_A_DB.get(a, "") and TIPO_A_DB.get(a, "") != "A"])
    al_wrong = sorted([a for a in in_al if TIPO_A_DB.get(a, "") and TIPO_A_DB.get(a, "") != "L"])

    log.kv("AT con tipo distinto de A", len(at_wrong))
    log.kv("AL con tipo distinto de L", len(al_wrong))

    if at_wrong:
        log.warn(f"AT con tipo_aula inconsistente: {at_wrong[:10]}")
    if al_wrong:
        log.warn(f"AL con tipo_aula inconsistente: {al_wrong[:10]}")

    if REAL_TO_ALIAS:
        rows = []
        for room in sorted(AL_CONF):
            rows.append([room, REAL_TO_ALIAS.get(room, "SIN_ALIAS")])
        log.table("Laboratorios y alias", ["Laboratorio real", "Alias"], rows, max_rows=50)


def course_summary():
    log.section("Resumen de materias en catálogo")
    total = len(COURSE_INFO)
    labs = sum(1 for _, info in COURSE_INFO.items() if info.get("is_lab", False))
    with_h = sum(1 for _, info in COURSE_INFO.items() if info.get("hreq") is not None)
    log.kv("Materias catalogadas", total)
    log.kv("Materias laboratorio", labs)
    log.kv("Materias con Hreq base", with_h)


def isc_course_diagnostic():
    log.section("Diagnóstico de materias ISC del periodo")
    rows = []
    for m in sorted(M_OBS):
        h_est, src = infer_hreq_for_materia(m)
        rows.append([
            m,
            "SI" if M_IS_LAB.get(m, False) else "NO",
            M_TIPO.get(m, ""),
            M_AREA.get(m, ""),
            M_BASE_HREQ.get(m, ""),
            h_est,
            src,
            M_TEXT.get(m, "")[:60],
        ])

    log.kv("Materias ISC distintas", len(rows))
    log.table(
        "Materias ISC y metadatos",
        ["Materia", "EsLab", "Tipo", "Area", "HreqBase", "HreqEst", "Fuente", "Texto"],
        rows,
        max_rows=200,
    )

    lab_rows = [r for r in rows if r[1] == "SI"]
    log.kv("Materias ISC marcadas como laboratorio", len(lab_rows))
    if lab_rows:
        log.table(
            "Materias ISC clasificadas como laboratorio",
            ["Materia", "EsLab", "Tipo", "Area", "HreqBase", "HreqEst", "Fuente", "Texto"],
            lab_rows,
            max_rows=200,
        )


def build_one_period(periodo):
    reset_accumulators()

    ok = (
        route_from_view(periodo)
        or route_groups_to_carreras(periodo)
        or route_groups_to_mc_to_carreras(periodo)
    )

    if not ok:
        log.warn(f"No fue posible extraer datos para el periodo {periodo}.")
        return False

    enrich_hreq_and_types()
    isc_course_diagnostic()

    cap_week = capacity_per_week()
    demand, _ = demand_by_group()
    print_overloaded_groups(demand, cap_week)

    g_counts = Counter([g for _, g in MG])
    if g_counts:
        avg_courses = sum(g_counts.values()) / max(1, len(g_counts))
        top = g_counts.most_common(5)
        log.kv("Promedio de cursos por grupo", f"{avg_courses:.2f}")
        log.info(f"Top 5 grupos por cursos: {top}")

    split_count, split_report = auto_split_groups()
    if split_count:
        log.table(
            "Grupos divididos automáticamente",
            ["Grupo base", "Partes", "Demanda", "Capacidad"],
            split_report,
            max_rows=50,
        )

    if STRICT_ROOMSETS:
        aulas_finales = sorted(set(AT_CONF) | set(AL_CONF))
    else:
        aulas_finales = sorted(set(A_DB) | set(AT_CONF) | set(AL_CONF))

    demand_t = 0
    demand_l = 0
    for materia, g_key in MG:
        hours = int(HREQ_MAP.get(f"{materia}|{g_key}", DEFAULT_HREQ))
        if M_IS_LAB.get(materia, False):
            demand_l += hours
        else:
            demand_t += hours

    cap_t = len(AT_CONF) * len(D) * len(H)
    cap_l = len(AL_CONF) * len(D) * len(H)

    log.section("Chequeo rápido de capacidad por tipo")
    log.kv("Demanda teórica", demand_t)
    log.kv("Capacidad teórica", cap_t)
    log.kv("Demanda laboratorio", demand_l)
    log.kv("Capacidad laboratorio", cap_l)

    if demand_t > cap_t:
        log.warn("La demanda teórica excede la capacidad teórica disponible.")
    if demand_l > cap_l:
        log.warn("La demanda de laboratorio excede la capacidad configurada.")

    room_whitelist = build_room_whitelist_by_course()
    out_path = compute_output_path(DATOS_JSON_TEMPLATE, periodo)
    emit_json(out_path, periodo, sorted(AT_CONF), sorted(AL_CONF), aulas_finales, room_whitelist)
    return True


def main():
    room_summary()
    course_summary()

    for i, periodo in enumerate(period_list, start=1):
        log.section(f"Construcción del periodo {periodo} ({i}/{len(period_list)})")
        build_one_period(periodo)

    try:
        cur.close()
        conn.close()
    except Exception:
        pass

    log.section("Resumen final")
    if WRITTEN:
        for path in WRITTEN:
            log.ok(f"Archivo generado: {path}")
    else:
        log.warn("No se generó ningún archivo JSON.")


if __name__ == "__main__":
    main()