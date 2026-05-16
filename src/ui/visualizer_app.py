# ======================================================================================
# Dashboard ISC mejorado v4
# - Muestra TODAS las aulas del catálogo (aunque tengan 0 uso)
# - Conserva comparación entre soluciones
# - Soporta alias visuales de laboratorios A..I
# - Diagnóstico específico de aula fija por curso
# - Agrega vistas de profesores y materias
#   * Profesores -> materias, grupos, bloques y aulas
#   * Materias -> profesores, grupos, bloques y aulas
#   * Horario detallado por profesor
# - Corrige merge con catalogo de profesores y evita "nan"
# ======================================================================================

import os
import io
import re
import glob
import json
import unicodedata
from pathlib import Path
from typing import Dict, Any, List

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

st.set_page_config(page_title="Dashboard de Horarios", layout="wide")
load_dotenv()

# ------------------------------------------------------------------------------
# Configuración general del dashboard y valores de respaldo
# ------------------------------------------------------------------------------
DEFAULT_PREFIX_TPL = "salidas/isc_{periodo}"
DEFAULT_JSON_TPL = "salidas/datos_modelo_{periodo}.json"

# Instancias conocidas. Ajusta estos prefijos si tus archivos cambian de nombre.
INSTANCIAS = {
    "ISC": {
        "prefix": "salidas/isc_20251_cobertura_p2_20251",
        "json": "salidas/datos_modelo_20251.json",
    },
    "Industrial": {
        "prefix": "salidas/industrial_20251_20251",
        "json": "salidas/datos_modelo_industrial_20251_20251.json",
    },
}

ENV_PREFIX = os.getenv("EXPORT_PREFIX", "")
ENV_JSON = os.getenv("DATOS_JSON", "")
ENV_SINGLE_ROOM = os.getenv("SINGLE_ROOM_PER_COURSE", "0").lower() in ("1", "true", "yes")

AT_HARD = [s.strip().upper() for s in (os.getenv("AT_HARD", "")).replace(";", ",").split(",") if s.strip()]
AL_HARD = [s.strip().upper() for s in (os.getenv("AL_HARD", "")).replace(";", ",").split(",") if s.strip()]
LAB_ALIAS_MAP_ENV = os.getenv("LAB_ALIAS_MAP_JSON", "{}")
PROF_CATALOG_CSV = os.getenv("PROFESORES_CSV", "").strip()

try:
    LAB_ALIAS_MAP_ENV = json.loads(LAB_ALIAS_MAP_ENV) if LAB_ALIAS_MAP_ENV else {}
except Exception:
    LAB_ALIAS_MAP_ENV = {}

DAY_ORDER = {"L": 1, "M": 2, "X": 3, "J": 4, "V": 5, "S": 6, "D": 7}

# ------------------------------------------------------------------------------
# Utilidades auxiliares
# ------------------------------------------------------------------------------
def has_template(s: str) -> bool:
    return "{periodo}" in (s or "")


def coerce_template(user_value: str, env_value: str, fallback_tpl: str) -> str:
    if has_template(user_value):
        return user_value
    if has_template(env_value):
        return env_value
    if user_value and user_value.strip():
        return user_value
    if env_value and env_value.strip():
        return env_value
    return fallback_tpl


def df_to_bytes(df: pd.DataFrame, kind: str = "csv"):
    if df is None:
        return None
    if kind == "csv":
        return df.to_csv(index=False).encode("utf-8")
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="reporte")
    return bio.getvalue()


def strip_accents_lower(txt: str) -> str:
    txt = str(txt or "")
    t = unicodedata.normalize("NFKD", txt)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.lower()


def _clean_aula_series(s: pd.Series) -> pd.Series:
    if s.empty:
        return s
    empty = {"", "SIN_AULA", "SIN AULA", "NA", "N/A", "NULL", "NONE"}
    return s.fillna("").astype(str).str.strip().str.upper().replace({k: "" for k in empty})


def _norm_prof_key(v: Any) -> str:
    s = str(v or "").strip().upper()
    s = re.sub(r"\s+", "", s)
    return s


def normalize_day(v: Any) -> str:
    s = str(v or "").strip().upper()
    sa = strip_accents_lower(s).upper()
    mapping = {
        "LUNES": "L", "L": "L",
        "MARTES": "M", "M": "M",
        "MIERCOLES": "X", "MIÉRCOLES": "X", "X": "X",
        "JUEVES": "J", "J": "J",
        "VIERNES": "V", "V": "V",
    }
    return mapping.get(s, mapping.get(sa, s[:1]))


def safe_slug(s: str) -> str:
    s = str(s or "").strip()
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", s)
    return s.strip("_") or "archivo"


def _pick_col(df: pd.DataFrame, candidates: List[str]) -> str:
    cols = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in cols:
            return cols[cand.lower()]
    return ""


def _join_unique(values) -> str:
    vals = []
    for v in values:
        if pd.isna(v):
            continue
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "null"}:
            continue
        vals.append(s)
    return ", ".join(sorted(set(vals)))


def discover_periods(template: str, suffix: str = "") -> List[str]:
    if not has_template(template):
        return []
    p0, p1 = template.split("{periodo}", 1)
    files = glob.glob(f"{p0}*{p1}{suffix}")
    periods = set()
    rp0 = re.escape(p0.replace("\\", "/"))
    rp1 = re.escape(p1.replace("\\", "/"))
    for f in files:
        f2 = f.replace("\\", "/")
        m = re.match(rf"^{rp0}(.+){rp1}{re.escape(suffix)}$", f2)
        if m:
            periods.add(m.group(1))
    return sorted(periods)


def discover_prefixes(prefix_template: str) -> List[str]:
    if has_template(prefix_template):
        p0, p1 = prefix_template.split("{periodo}", 1)
        pattern = f"{p0}*{p1}_calendario.csv"
        out = []
        for f in glob.glob(pattern):
            if f.endswith("_calendario.csv"):
                out.append(f[:-len("_calendario.csv")])
        return sorted(set(out))
    base = prefix_template.strip()
    if not base:
        return []
    out = []
    for f in glob.glob(base + "*_calendario.csv"):
        out.append(f[:-len("_calendario.csv")])
    if os.path.isfile(base + "_calendario.csv"):
        out.append(base)
    return sorted(set(out))

# ------------------------------------------------------------------------------
# Carga de archivos
# ------------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_json(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(show_spinner=False)
def load_csv(path: str) -> pd.DataFrame:
    if not path or not os.path.isfile(path):
        return pd.DataFrame()
    df = pd.read_csv(path, dtype=str).fillna("")
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


@st.cache_data(show_spinner=False)
def load_bundle(prefix: str) -> Dict[str, pd.DataFrame]:
    prof = load_csv(prefix + "_profesores.csv")
    aulas = load_csv(prefix + "_aulas.csv")
    cal = load_csv(prefix + "_calendario.csv")

    if not cal.empty:
        if "aula" in cal.columns:
            cal["aula"] = _clean_aula_series(cal["aula"])
        if "dia" in cal.columns:
            cal["dia"] = cal["dia"].apply(normalize_day)
        if "hora" in cal.columns:
            cal["hora"] = cal["hora"].astype(str).str.strip()

    if not aulas.empty and "aula" in aulas.columns:
        aulas["aula"] = _clean_aula_series(aulas["aula"])

    return {"prof": prof, "aulas": aulas, "cal": cal}


@st.cache_data(show_spinner=False)
def load_prof_catalog() -> pd.DataFrame:
    if not PROF_CATALOG_CSV or not os.path.isfile(PROF_CATALOG_CSV):
        return pd.DataFrame(columns=["profesor", "profesor_nombre"])

    df = pd.read_csv(PROF_CATALOG_CSV, dtype=str).fillna("")
    df.columns = [str(c).strip().lower() for c in df.columns]

    rfc_col = _pick_col(df, ["rfc", "profesor", "docente", "clave"])
    name_col = _pick_col(df, ["nombre_completo", "nombre", "docente_nombre", "profesor_nombre"])

    if not rfc_col or not name_col:
        return pd.DataFrame(columns=["profesor", "profesor_nombre"])

    out = df[[rfc_col, name_col]].copy()
    out.columns = ["profesor", "profesor_nombre"]
    out["profesor"] = out["profesor"].apply(_norm_prof_key)
    out["profesor_nombre"] = out["profesor_nombre"].astype(str).str.strip()

    out = out[(out["profesor"] != "") & (out["profesor_nombre"] != "")]
    out = out.drop_duplicates("profesor")
    return out

# ------------------------------------------------------------------------------
# Transformaciones analíticas
# ------------------------------------------------------------------------------
def get_alias_map(meta: dict) -> Dict[str, str]:
    amap = {}
    if meta and meta.get("LAB_ALIAS_MAP"):
        amap.update({str(k).upper(): str(v) for k, v in meta.get("LAB_ALIAS_MAP", {}).items()})
    for alias, real in LAB_ALIAS_MAP_ENV.items():
        if real:
            amap[str(real).upper()] = str(alias)
    return amap


def get_materia_name_map(meta: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    raw = meta.get("M_text", {}) or meta.get("m_text", {}) or {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            txt = str(v or "").strip()
            if txt:
                txt = txt.split("|")[0].strip()
            out[str(k).upper()] = txt
    return out


def add_alias_column(df: pd.DataFrame, alias_map: Dict[str, str], room_col: str = "aula") -> pd.DataFrame:
    if df is None or df.empty or room_col not in df.columns:
        return df
    out = df.copy()
    out["aula_alias"] = out[room_col].astype(str).str.upper().map(alias_map).fillna("")
    return out


def enrich_calendar_labels(df_cal: pd.DataFrame, meta: dict, prof_catalog: pd.DataFrame) -> pd.DataFrame:
    if df_cal.empty:
        return df_cal

    out = df_cal.copy()
    materia_map = get_materia_name_map(meta)

    if "materia" in out.columns:
        out["materia_nombre"] = out["materia"].astype(str).str.upper().map(materia_map).fillna("")

    if "profesor" in out.columns:
        out["profesor"] = out["profesor"].apply(_norm_prof_key)
        if not prof_catalog.empty:
            out = out.merge(prof_catalog, on="profesor", how="left")
            out["profesor_nombre"] = out["profesor_nombre"].fillna("").astype(str).str.strip()
        else:
            out["profesor_nombre"] = ""

    return out


def derive_course_room_from_calendar(df_cal: pd.DataFrame) -> pd.DataFrame:
    if df_cal.empty or not {"materia", "grupo", "aula"}.issubset(df_cal.columns):
        return pd.DataFrame(columns=["materia", "grupo", "aula", "aula_modo"])

    cal = df_cal.copy()
    cal["aula"] = _clean_aula_series(cal["aula"])
    cal = cal[cal["aula"] != ""]
    if cal.empty:
        return pd.DataFrame(columns=["materia", "grupo", "aula", "aula_modo"])

    cnt = cal.groupby(["materia", "grupo", "aula"]).size().reset_index(name="sesiones")
    top = cnt.sort_values(["materia", "grupo", "sesiones"], ascending=[True, True, False]).drop_duplicates(["materia", "grupo"])
    distinct = cnt.groupby(["materia", "grupo"])["aula"].nunique().reset_index(name="n_aulas")
    out = top.merge(distinct, on=["materia", "grupo"], how="left")
    out["aula_modo"] = out["n_aulas"].apply(lambda x: "UNICA" if int(x) == 1 else "MULTI")
    return out[["materia", "grupo", "aula", "aula_modo"]]


def build_course_assignments(df_prof: pd.DataFrame, df_aulas: pd.DataFrame, df_cal: pd.DataFrame, prefer_calendar=True) -> pd.DataFrame:
    if prefer_calendar and (df_aulas.empty or "aula" not in df_aulas.columns):
        df_aulas = derive_course_room_from_calendar(df_cal)
    elif prefer_calendar and not df_cal.empty:
        der = derive_course_room_from_calendar(df_cal)
        if not der.empty:
            df_aulas = df_aulas.merge(der[["materia", "grupo", "aula_modo"]], on=["materia", "grupo"], how="left") if not df_aulas.empty else der

    if df_prof.empty and df_aulas.empty:
        return pd.DataFrame()

    keys = ["materia", "grupo"]
    if "periodo" in df_prof.columns and "periodo" in df_aulas.columns:
        keys.append("periodo")
    elif "periodo" in df_prof.columns:
        df_aulas = df_aulas.copy()
        df_aulas["periodo"] = ""
    elif "periodo" in df_aulas.columns:
        df_prof = df_prof.copy()
        df_prof["periodo"] = ""

    if df_prof.empty:
        base = df_aulas.copy()
    elif df_aulas.empty:
        base = df_prof.copy()
    else:
        base = pd.merge(df_prof, df_aulas, on=keys, how="outer")

    return base.fillna("")


def derive_DH(meta: dict, df_cal: pd.DataFrame):
    D = [str(x) for x in meta.get("D", [])] if meta else []
    H = [str(x) for x in meta.get("H", [])] if meta else []

    if D and H:
        return D, H

    if not df_cal.empty:
        D = sorted(df_cal["dia"].astype(str).unique().tolist(), key=lambda x: DAY_ORDER.get(str(x), 99)) if "dia" in df_cal.columns else ["L", "M", "X", "J", "V"]
        H = sorted(df_cal["hora"].astype(str).unique().tolist(), key=lambda x: (len(x), x)) if "hora" in df_cal.columns else []
        return D, H

    return ["L", "M", "X", "J", "V"], [f"{h:02d}" for h in range(7, 22)]


def build_room_catalog(meta: dict, alias_map: Dict[str, str]) -> pd.DataFrame:
    A = [str(x).upper() for x in (meta.get("A", []) or [])]
    AT = set(str(x).upper() for x in (meta.get("AT", []) or []))
    AL = set(str(x).upper() for x in (meta.get("AL", []) or []))
    cap_A = {str(k).upper(): v for k, v in (meta.get("cap_A", {}) or {}).items()}

    if not A:
        A = sorted((set(AT_HARD) | set(AL_HARD)))
        AT = set(AT_HARD)
        AL = set(AL_HARD)

    rows = []
    for a in A:
        if a in AL:
            tipo = "LAB"
        elif a in AT:
            tipo = "TEORIA"
        else:
            tipo = "OTRA"
        rows.append({
            "aula": a,
            "tipo": tipo,
            "capacidad": cap_A.get(a, ""),
            "aula_alias": alias_map.get(a, ""),
        })

    return pd.DataFrame(rows).sort_values(["tipo", "aula"]).reset_index(drop=True)


def build_utilization(df_cal: pd.DataFrame, meta: dict, alias_map: Dict[str, str]) -> pd.DataFrame:
    D, H = derive_DH(meta, df_cal)
    cap = len(D) * len(H)

    catalog = build_room_catalog(meta, alias_map)
    if catalog.empty:
        return pd.DataFrame(columns=["aula", "tipo", "aula_alias", "capacidad", "sesiones", "cap_bloques", "utilizacion_%", "huecos_libres", "estado_uso"])

    if df_cal.empty or "aula" not in df_cal.columns:
        agg = pd.DataFrame(columns=["aula", "sesiones"])
    else:
        v = df_cal.copy()
        v["aula"] = _clean_aula_series(v["aula"])
        v = v[v["aula"] != ""]
        agg = v.groupby("aula").size().reset_index(name="sesiones") if not v.empty else pd.DataFrame(columns=["aula", "sesiones"])

    out = catalog.merge(agg, on="aula", how="left")
    out["sesiones"] = out["sesiones"].fillna(0).astype(int)
    out["cap_bloques"] = cap
    out["utilizacion_%"] = (out["sesiones"] / cap * 100).round(1)
    out["huecos_libres"] = cap - out["sesiones"]
    out["estado_uso"] = out["sesiones"].apply(lambda x: "USADA" if int(x) > 0 else "SIN_USO")

    return out.sort_values(["sesiones", "tipo", "aula"], ascending=[False, True, True]).reset_index(drop=True)


def detect_room_overlaps(df_cal: pd.DataFrame) -> pd.DataFrame:
    if df_cal.empty or not {"aula", "dia", "hora"}.issubset(df_cal.columns):
        return pd.DataFrame(columns=["aula", "dia", "hora", "count"])
    cols = ["aula", "dia", "hora"]
    if "periodo" in df_cal.columns:
        cols = ["periodo"] + cols
    dup = df_cal.groupby(cols).size().reset_index(name="count")
    return dup[dup["count"] > 1].sort_values(cols)


def detect_prof_overlaps(df_cal: pd.DataFrame) -> pd.DataFrame:
    if df_cal.empty or not {"profesor", "dia", "hora"}.issubset(df_cal.columns):
        return pd.DataFrame(columns=["profesor", "dia", "hora", "count"])
    cols = ["profesor", "dia", "hora"]
    if "periodo" in df_cal.columns:
        cols = ["periodo"] + cols
    v = df_cal[df_cal["profesor"] != ""]
    dup = v.groupby(cols).size().reset_index(name="count")
    return dup[dup["count"] > 1].sort_values(cols)


def detect_group_overlaps(df_cal: pd.DataFrame) -> pd.DataFrame:
    if df_cal.empty or not {"grupo", "dia", "hora"}.issubset(df_cal.columns):
        return pd.DataFrame(columns=["grupo", "dia", "hora", "count"])
    cols = ["grupo", "dia", "hora"]
    if "periodo" in df_cal.columns:
        cols = ["periodo"] + cols
    dup = df_cal.groupby(cols).size().reset_index(name="count")
    return dup[dup["count"] > 1].sort_values(cols)


def build_fixed_room_consistency(df_cal: pd.DataFrame, alias_map: Dict[str, str]) -> pd.DataFrame:
    if df_cal.empty or not {"materia", "grupo", "aula"}.issubset(df_cal.columns):
        return pd.DataFrame(columns=["materia", "grupo", "n_aulas", "aula_principal", "sesiones_principal", "cumple_aula_fija"])

    v = df_cal.copy()
    v["aula"] = _clean_aula_series(v["aula"])
    v = v[v["aula"] != ""]
    cnt = v.groupby(["materia", "grupo", "aula"]).size().reset_index(name="sesiones")
    top = cnt.sort_values(["materia", "grupo", "sesiones"], ascending=[True, True, False]).drop_duplicates(["materia", "grupo"])
    n = cnt.groupby(["materia", "grupo"])["aula"].nunique().reset_index(name="n_aulas")
    out = top.merge(n, on=["materia", "grupo"], how="left")
    out["cumple_aula_fija"] = out["n_aulas"].eq(1)
    out["aula_alias"] = out["aula"].astype(str).str.upper().map(alias_map).fillna("")
    out = out.rename(columns={"aula": "aula_principal", "sesiones": "sesiones_principal"})
    return out.sort_values(["cumple_aula_fija", "n_aulas", "materia", "grupo"], ascending=[True, False, True, True])


def build_phase2_coverage(meta: dict) -> pd.DataFrame:
    room_wl = meta.get("ROOM_WHITELIST_BY_COURSE", {}) or meta.get("room_whitelist_by_course", {}) or {}
    rows = []
    for k, rooms in room_wl.items():
        if "|" in str(k):
            parts = str(k).split("|", 1)
            materia, grupo = parts[0], parts[1]
        else:
            materia, grupo = str(k), ""
        rooms = list(rooms or [])
        rows.append({
            "materia": materia,
            "grupo": grupo,
            "n_aulas_whitelist": len(rooms),
            "aulas_whitelist": ", ".join(rooms)
        })
    return pd.DataFrame(rows)


def compare_two_calendars(df_a: pd.DataFrame, df_b: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    out = {}
    if df_a.empty or df_b.empty:
        return out

    keys = ["materia", "grupo", "dia", "hora", "profesor", "aula"]
    if not set(keys).issubset(df_a.columns) or not set(keys).issubset(df_b.columns):
        return out

    a = df_a[keys].drop_duplicates().copy()
    b = df_b[keys].drop_duplicates().copy()

    out["solo_actual"] = a.merge(b, on=keys, how="left", indicator=True)
    out["solo_actual"] = out["solo_actual"][out["solo_actual"]["_merge"] == "left_only"].drop(columns=["_merge"])

    out["solo_comp"] = b.merge(a, on=keys, how="left", indicator=True)
    out["solo_comp"] = out["solo_comp"][out["solo_comp"]["_merge"] == "left_only"].drop(columns=["_merge"])

    return out


def build_teacher_subjects(df_cal: pd.DataFrame) -> pd.DataFrame:
    if df_cal.empty or "profesor" not in df_cal.columns:
        return pd.DataFrame(columns=["profesor", "profesor_nombre", "n_materias", "materias", "materias_nombre", "n_grupos", "grupos", "bloques", "aulas"])

    v = df_cal.copy()
    v = v[v["profesor"].astype(str).str.strip() != ""]
    if v.empty:
        return pd.DataFrame(columns=["profesor", "profesor_nombre", "n_materias", "materias", "materias_nombre", "n_grupos", "grupos", "bloques", "aulas"])

    rows = []
    for prof, g in v.groupby("profesor"):
        rows.append({
            "profesor": prof,
            "profesor_nombre": _join_unique(g["profesor_nombre"]) if "profesor_nombre" in g.columns else "",
            "n_materias": g["materia"].nunique() if "materia" in g.columns else 0,
            "materias": _join_unique(g["materia"]) if "materia" in g.columns else "",
            "materias_nombre": _join_unique(g["materia_nombre"]) if "materia_nombre" in g.columns else "",
            "n_grupos": g["grupo"].nunique() if "grupo" in g.columns else 0,
            "grupos": _join_unique(g["grupo"]) if "grupo" in g.columns else "",
            "bloques": len(g),
            "aulas": _join_unique(g["aula"]) if "aula" in g.columns else "",
        })

    out = pd.DataFrame(rows)
    return out.sort_values(["bloques", "n_materias", "profesor_nombre", "profesor"], ascending=[False, False, True, True]).reset_index(drop=True)


def build_subject_teachers(df_cal: pd.DataFrame) -> pd.DataFrame:
    if df_cal.empty or "materia" not in df_cal.columns:
        return pd.DataFrame(columns=["materia", "materia_nombre", "n_profesores", "profesores", "profesores_nombre", "n_grupos", "grupos", "bloques", "aulas"])

    v = df_cal.copy()
    rows = []
    for mat, g in v.groupby("materia"):
        rows.append({
            "materia": mat,
            "materia_nombre": _join_unique(g["materia_nombre"]) if "materia_nombre" in g.columns else "",
            "n_profesores": g["profesor"].replace("", pd.NA).dropna().nunique() if "profesor" in g.columns else 0,
            "profesores": _join_unique(g["profesor"]) if "profesor" in g.columns else "",
            "profesores_nombre": _join_unique(g["profesor_nombre"]) if "profesor_nombre" in g.columns else "",
            "n_grupos": g["grupo"].nunique() if "grupo" in g.columns else 0,
            "grupos": _join_unique(g["grupo"]) if "grupo" in g.columns else "",
            "bloques": len(g),
            "aulas": _join_unique(g["aula"]) if "aula" in g.columns else "",
        })

    out = pd.DataFrame(rows)
    return out.sort_values(["bloques", "materia_nombre", "materia"], ascending=[False, True, True]).reset_index(drop=True)


def build_teacher_schedule(df_cal: pd.DataFrame) -> pd.DataFrame:
    if df_cal.empty or "profesor" not in df_cal.columns:
        return pd.DataFrame(columns=["profesor", "profesor_nombre", "dia", "hora", "materia", "materia_nombre", "grupo", "aula", "aula_alias"])

    cols = [c for c in ["profesor", "profesor_nombre", "dia", "hora", "materia", "materia_nombre", "grupo", "aula", "aula_alias"] if c in df_cal.columns]
    out = df_cal[cols].copy()
    out = out[out["profesor"].astype(str).str.strip() != ""]

    return out.sort_values(
        by=[c for c in ["profesor_nombre", "profesor", "dia", "hora", "materia_nombre", "materia", "grupo"] if c in out.columns],
        key=lambda s: s.map(DAY_ORDER) if s.name == "dia" else s
    ).reset_index(drop=True)

# ------------------------------------------------------------------------------
# Construcción principal de la interfaz
# ------------------------------------------------------------------------------
st.title("Dashboard de Horarios Académicos")
st.caption("Visualización y validación de soluciones para ISC, Industrial u otras instancias del modelo.")

st.sidebar.header("Configuración")

instancia = st.sidebar.selectbox(
    "Instancia",
    ["ISC", "Industrial", "Personalizada"],
    index=0,
    key="cfg_instancia",
)

if instancia in INSTANCIAS:
    default_prefix = INSTANCIAS[instancia]["prefix"]
    default_json = INSTANCIAS[instancia]["json"]
else:
    default_prefix = ENV_PREFIX or DEFAULT_PREFIX_TPL
    default_json = ENV_JSON or DEFAULT_JSON_TPL

# ------------------------------------------------------------------------------
# Corrección importante:
# Streamlit conserva el valor de los text_input por su key. Por eso, si primero
# cargas ISC y luego cambias a Industrial, los campos pueden quedarse con el
# prefijo/JSON anterior. Este bloque fuerza la actualización cuando cambia la
# instancia seleccionada.
# ------------------------------------------------------------------------------
if "last_instancia" not in st.session_state:
    st.session_state.last_instancia = instancia

if "cfg_prefix" not in st.session_state:
    st.session_state.cfg_prefix = default_prefix

if "cfg_json" not in st.session_state:
    st.session_state.cfg_json = default_json

if st.session_state.last_instancia != instancia:
    st.session_state.cfg_prefix = default_prefix
    st.session_state.cfg_json = default_json
    st.session_state.last_instancia = instancia
    st.cache_data.clear()

prefix_input = st.sidebar.text_input(
    "Prefijo exportado",
    key="cfg_prefix"
)

json_input = st.sidebar.text_input(
    "JSON del modelo",
    key="cfg_json"
)

prefix_template = coerce_template(prefix_input, "", DEFAULT_PREFIX_TPL)
json_template = coerce_template(json_input, "", DEFAULT_JSON_TPL)

periods = sorted(set(discover_periods(prefix_template, "_calendario.csv")) | set(discover_periods(json_template)))
if periods:
    periodo = st.sidebar.selectbox("Periodo", periods, index=max(0, len(periods) - 1), key="cfg_periodo")
else:
    periodo = st.sidebar.text_input("Periodo manual", value=os.getenv("TARGET_PERIOD", "20251"), key="cfg_periodo_manual")

candidate_prefixes = [p for p in discover_prefixes(prefix_template) if periodo in Path(p).name]
expected_default = prefix_template.format(periodo=periodo) if has_template(prefix_template) else prefix_template
if expected_default not in candidate_prefixes and os.path.isfile(expected_default + "_calendario.csv"):
    candidate_prefixes.insert(0, expected_default)
if not candidate_prefixes:
    candidate_prefixes = [expected_default]

main_prefix = st.sidebar.selectbox("Solución principal", candidate_prefixes, index=0, key="cfg_sol_principal")
compare_enabled = st.sidebar.checkbox("Comparar contra otra solución", value=(len(candidate_prefixes) > 1), key="cfg_compare")

other_prefix = None
if compare_enabled:
    other_opts = [p for p in candidate_prefixes if p != main_prefix]
    if other_opts:
        other_prefix = st.sidebar.selectbox("Solución de comparación", other_opts, index=0, key="cfg_sol_compare")

prefer_calendar_room = st.sidebar.checkbox("Derivar aula por curso desde calendario cuando ayude", value=True, key="cfg_prefer_calendar_room")
show_only_fixed_violations = st.sidebar.checkbox("Mostrar solo cursos que violan aula fija", value=False, key="cfg_show_only_fixed_violations")
show_only_unused_rooms = st.sidebar.checkbox("Mostrar solo aulas sin uso", value=False, key="cfg_show_only_unused")

if st.sidebar.button("Recargar / limpiar caché", key="cfg_reload"):
    st.cache_data.clear()
    st.rerun()

json_path = json_template.format(periodo=periodo) if has_template(json_template) else json_template

# Validación visual de archivos cargados
st.sidebar.markdown("---")
st.sidebar.caption("Archivos cargados")
st.sidebar.write(f"Instancia: `{instancia}`")
st.sidebar.write(f"Prefix: `{main_prefix}`")
st.sidebar.write(f"JSON: `{json_path}`")

if not os.path.isfile(main_prefix + "_calendario.csv"):
    st.sidebar.error("No existe el calendario para este prefijo.")
if not os.path.isfile(json_path):
    st.sidebar.error("No existe el JSON seleccionado.")

meta = load_json(json_path)
bundle = load_bundle(main_prefix)
compare_bundle = load_bundle(other_prefix) if other_prefix else {"prof": pd.DataFrame(), "aulas": pd.DataFrame(), "cal": pd.DataFrame()}

alias_map = get_alias_map(meta)
prof_catalog = load_prof_catalog()

for key in ["aulas", "cal"]:
    if not bundle[key].empty and "aula" in bundle[key].columns:
        bundle[key] = add_alias_column(bundle[key], alias_map, "aula")
    if compare_enabled and not compare_bundle[key].empty and "aula" in compare_bundle[key].columns:
        compare_bundle[key] = add_alias_column(compare_bundle[key], alias_map, "aula")

bundle["cal"] = enrich_calendar_labels(bundle["cal"], meta, prof_catalog)
if compare_enabled:
    compare_bundle["cal"] = enrich_calendar_labels(compare_bundle["cal"], meta, prof_catalog)

course_tbl = build_course_assignments(bundle["prof"], bundle["aulas"], bundle["cal"], prefer_calendar_room)
if not course_tbl.empty and "aula" in course_tbl.columns:
    course_tbl = add_alias_column(course_tbl, alias_map, "aula")

if not course_tbl.empty:
    materia_map = get_materia_name_map(meta)
    if "materia" in course_tbl.columns:
        course_tbl["materia_nombre"] = course_tbl["materia"].astype(str).str.upper().map(materia_map).fillna("")
    if "profesor" in course_tbl.columns:
        course_tbl["profesor"] = course_tbl["profesor"].apply(_norm_prof_key)
        if not prof_catalog.empty:
            course_tbl = course_tbl.merge(prof_catalog, on="profesor", how="left")
            course_tbl["profesor_nombre"] = course_tbl["profesor_nombre"].fillna("").astype(str).str.strip()
        else:
            course_tbl["profesor_nombre"] = ""

D, H = derive_DH(meta, bundle["cal"])
cap_bloques = len(D) * len(H)
util_tbl = build_utilization(bundle["cal"], meta, alias_map)
fixed_tbl = build_fixed_room_consistency(bundle["cal"], alias_map)
phase2_tbl = build_phase2_coverage(meta)
room_overlaps = detect_room_overlaps(bundle["cal"])
prof_overlaps = detect_prof_overlaps(bundle["cal"])
group_overlaps = detect_group_overlaps(bundle["cal"])
compare_diff = compare_two_calendars(bundle["cal"], compare_bundle["cal"]) if other_prefix else {}
room_catalog = build_room_catalog(meta, alias_map)

teacher_subjects = build_teacher_subjects(bundle["cal"])
subject_teachers = build_subject_teachers(bundle["cal"])
teacher_schedule = build_teacher_schedule(bundle["cal"])

# ------------------------------------------------------------------------------
# Métricas ejecutivas
# ------------------------------------------------------------------------------
col1, col2, col3, col4, col5, col6 = st.columns(6)
col1.metric(f"Cursos (MG) — {instancia}", len(meta.get("MG", []) or []))
col2.metric("Profesores", len(meta.get("P", []) or []))
col3.metric("Aulas permitidas", len(meta.get("A", []) or room_catalog["aula"].unique().tolist()))
col4.metric("Eventos", len(bundle["cal"]))
col5.metric("Bloques/semana", cap_bloques)
if fixed_tbl.empty:
    col6.metric("Cursos con aula fija", "N/D")
else:
    ok_fixed = int(fixed_tbl["cumple_aula_fija"].sum())
    total_fixed = len(fixed_tbl)
    col6.metric("Cursos con aula fija", f"{ok_fixed}/{total_fixed}")

st.markdown("---")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Resumen ejecutivo",
    "Asignaciones y calendario",
    "Aulas y utilización",
    "Profesores y materias",
    "Aula fija / Fase 2",
    "Diagnóstico y comparación",
])

# ------------------------------------------------------------------------------
# TAB 1: Resumen ejecutivo
# ------------------------------------------------------------------------------
with tab1:
    st.subheader("Resumen ejecutivo")
    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown(f"**Solución principal — {instancia}**")
        st.code(main_prefix, language="text")
        st.write(f"JSON: `{json_path}`")

    with c2:
        st.markdown("**Configuración detectada**")
        st.write(f"AT: {len(meta.get('AT', []) or [])}")
        st.write(f"AL: {len(meta.get('AL', []) or [])}")
        st.write(f"Aula fija en .env: {'Sí' if ENV_SINGLE_ROOM else 'No'}")
        st.write(f"Catálogo profesores cargado: {len(prof_catalog)}")

    with c3:
        st.markdown("**Validación rápida**")
        st.write(f"Solapes aula: {len(room_overlaps)}")
        st.write(f"Solapes profesor: {len(prof_overlaps)}")
        st.write(f"Solapes grupo: {len(group_overlaps)}")

    if not util_tbl.empty:
        usadas = int((util_tbl["sesiones"] > 0).sum())
        sin_uso = int((util_tbl["sesiones"] == 0).sum())
        st.markdown("**Cobertura del catálogo de aulas**")
        st.write(f"Aulas usadas: {usadas}")
        st.write(f"Aulas sin uso: {sin_uso}")

    if not fixed_tbl.empty:
        total = len(fixed_tbl)
        ok = int(fixed_tbl["cumple_aula_fija"].sum())
        st.markdown("**Consistencia de aula fija**")
        st.progress(ok / total if total else 0.0, text=f"{ok} de {total} cursos usan una sola aula en el calendario")

    if not bundle["cal"].empty and "profesor_nombre" in bundle["cal"].columns:
        con_nombre = int((bundle["cal"]["profesor_nombre"].astype(str).str.strip() != "").sum())
        total_eventos_prof = int((bundle["cal"]["profesor"].astype(str).str.strip() != "").sum()) if "profesor" in bundle["cal"].columns else 0
        st.markdown("**Cobertura de nombres de profesor en calendario**")
        st.write(f"Eventos con profesor_nombre: {con_nombre}")
        st.write(f"Eventos con profesor (RFC): {total_eventos_prof}")

    st.markdown("**Alias de laboratorios**")
    if alias_map:
        alias_df = pd.DataFrame(sorted(alias_map.items()), columns=["aula_real", "alias_visual"])
        st.dataframe(alias_df, use_container_width=True, height=220)
    else:
        st.info("No se detectó mapa de alias.")

# ------------------------------------------------------------------------------
# TAB 2: Asignaciones y calendario
# ------------------------------------------------------------------------------
with tab2:
    st.subheader("Asignaciones por curso")
    if course_tbl.empty:
        st.warning("No se encontraron asignaciones por curso.")
    else:
        filt_cols = st.columns(5)
        fm = filt_cols[0].selectbox("Materia", [""] + sorted(course_tbl["materia"].unique().tolist()), key="tab2_materia")
        fg = filt_cols[1].selectbox("Grupo", [""] + sorted(course_tbl["grupo"].unique().tolist()), key="tab2_grupo")
        fp = filt_cols[2].selectbox("Profesor", [""] + sorted(course_tbl.get("profesor", pd.Series([], dtype=str)).unique().tolist()), key="tab2_profesor")
        fa = filt_cols[3].selectbox("Aula", [""] + sorted(course_tbl.get("aula", pd.Series([], dtype=str)).unique().tolist()), key="tab2_aula") if "aula" in course_tbl.columns else ""
        q = filt_cols[4].text_input("Búsqueda libre", "", key="tab2_busqueda").strip().lower()

        df = course_tbl.copy()
        if fm:
            df = df[df["materia"] == fm]
        if fg:
            df = df[df["grupo"] == fg]
        if fp and "profesor" in df.columns:
            df = df[df["profesor"] == fp]
        if fa and "aula" in df.columns:
            df = df[df["aula"] == fa]
        if q:
            df = df[df.apply(lambda r: q in " ".join(map(str, r.values)).lower(), axis=1)]

        st.caption(f"Filas: {len(df)}")
        st.dataframe(
            df.sort_values([c for c in ["periodo", "materia_nombre", "materia", "grupo"] if c in df.columns]),
            use_container_width=True,
            height=420
        )
        st.download_button(
            "Descargar asignaciones CSV",
            df_to_bytes(df, "csv"),
            file_name=f"asignaciones_{safe_slug(periodo)}.csv",
            mime="text/csv"
        )

    st.markdown("---")
    st.subheader("Calendario de sesiones")
    if bundle["cal"].empty:
        st.warning("No se encontró calendario.")
    else:
        fc = bundle["cal"].copy()
        c1, c2, c3, c4 = st.columns(4)
        sel_dia = c1.selectbox("Día", [""] + list(D), key="tab2_cal_dia")
        sel_hora = c2.selectbox("Hora", [""] + list(H), key="tab2_cal_hora")
        sel_aula = c3.selectbox("Aula", [""] + sorted(fc["aula"].unique().tolist()), key="tab2_cal_aula")
        sel_prof = c4.text_input("Profesor contiene", "", key="tab2_cal_profesor").strip().lower()

        if sel_dia:
            fc = fc[fc["dia"] == sel_dia]
        if sel_hora:
            fc = fc[fc["hora"] == sel_hora]
        if sel_aula:
            fc = fc[fc["aula"] == sel_aula]
        if sel_prof:
            mask_rfc = fc["profesor"].astype(str).str.lower().str.contains(sel_prof, na=False) if "profesor" in fc.columns else False
            mask_nom = fc["profesor_nombre"].astype(str).str.lower().str.contains(sel_prof, na=False) if "profesor_nombre" in fc.columns else False
            fc = fc[mask_rfc | mask_nom]

        order = [c for c in ["materia", "materia_nombre", "grupo", "dia", "hora", "aula", "aula_alias", "profesor", "profesor_nombre"] if c in fc.columns]
        st.dataframe(
            fc[order].sort_values(
                by=[c for c in ["dia", "hora", "aula", "materia", "grupo"] if c in fc.columns],
                key=lambda s: s.map(DAY_ORDER) if s.name == "dia" else s
            ),
            use_container_width=True,
            height=420
        )
        st.download_button(
            "Descargar calendario CSV",
            df_to_bytes(fc, "csv"),
            file_name=f"calendario_{safe_slug(periodo)}.csv",
            mime="text/csv"
        )

# ------------------------------------------------------------------------------
# TAB 3: Utilización de aulas
# ------------------------------------------------------------------------------
with tab3:
    st.subheader("Utilización de aulas")
    if util_tbl.empty:
        st.warning("No hay datos de utilización.")
    else:
        util_show = util_tbl.copy()
        if show_only_unused_rooms:
            util_show = util_show[util_show["sesiones"] == 0]

        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Aulas del catálogo", len(util_tbl))
        with c2:
            st.metric("Aulas usadas", int((util_tbl["sesiones"] > 0).sum()))
        with c3:
            st.metric("Aulas sin uso", int((util_tbl["sesiones"] == 0).sum()))

        st.dataframe(util_show, use_container_width=True, height=420)
        st.download_button(
            "Descargar utilización CSV",
            df_to_bytes(util_show, "csv"),
            file_name=f"utilizacion_aulas_{safe_slug(periodo)}.csv",
            mime="text/csv"
        )

        st.markdown("**Top aulas usadas**")
        top_used = util_tbl[util_tbl["sesiones"] > 0].sort_values("sesiones", ascending=False).head(15)
        if not top_used.empty:
            st.bar_chart(top_used.set_index("aula")["sesiones"])

        st.markdown("**Aulas sin uso**")
        unused = util_tbl[util_tbl["sesiones"] == 0].sort_values(["tipo", "aula"])
        if not unused.empty:
            st.dataframe(unused, use_container_width=True, height=260)

# ------------------------------------------------------------------------------
# TAB 4: Profesores y materias
# ------------------------------------------------------------------------------
with tab4:
    st.subheader("Profesores y materias")
    sub1, sub2, sub3 = st.tabs(["Profesores → materias", "Materias → profesores", "Horario por profesor"])

    with sub1:
        if teacher_subjects.empty:
            st.info("No hay información de profesores en el calendario.")
        else:
            qprof = st.text_input("Filtrar profesor", "", key="tab4_prof_filter").strip().lower()
            tdf = teacher_subjects.copy()
            if qprof:
                tdf = tdf[
                    tdf["profesor"].astype(str).str.lower().str.contains(qprof, na=False)
                    | tdf["profesor_nombre"].astype(str).str.lower().str.contains(qprof, na=False)
                ]
            st.dataframe(tdf, use_container_width=True, height=430)
            st.download_button(
                "Descargar profesores_materias CSV",
                df_to_bytes(tdf, "csv"),
                file_name=f"profesores_materias_{safe_slug(periodo)}.csv",
                mime="text/csv"
            )

    with sub2:
        if subject_teachers.empty:
            st.info("No hay información de materias en el calendario.")
        else:
            qmat = st.text_input("Filtrar materia", "", key="tab4_mat_filter").strip().lower()
            sdf = subject_teachers.copy()
            if qmat:
                sdf = sdf[
                    sdf["materia"].astype(str).str.lower().str.contains(qmat, na=False)
                    | sdf["materia_nombre"].astype(str).str.lower().str.contains(qmat, na=False)
                ]
            st.dataframe(sdf, use_container_width=True, height=430)
            st.download_button(
                "Descargar materias_profesores CSV",
                df_to_bytes(sdf, "csv"),
                file_name=f"materias_profesores_{safe_slug(periodo)}.csv",
                mime="text/csv"
            )

    with sub3:
        if teacher_schedule.empty:
            st.info("No hay horario de profesores disponible.")
        else:
            prof_opts = [""] + sorted(teacher_schedule["profesor"].unique().tolist())
            prof_sel = st.selectbox("Profesor", prof_opts, key="tab4_sched_prof")
            hs = teacher_schedule.copy()
            if prof_sel:
                hs = hs[hs["profesor"] == prof_sel]
            st.dataframe(hs, use_container_width=True, height=430)
            st.download_button(
                "Descargar horario_profesor CSV",
                df_to_bytes(hs, "csv"),
                file_name=f"horario_profesor_{safe_slug(periodo)}.csv",
                mime="text/csv"
            )

# ------------------------------------------------------------------------------
# TAB 5: Aula fija / Fase 2
# ------------------------------------------------------------------------------
with tab5:
    st.subheader("Diagnóstico de aula fija por curso")
    if fixed_tbl.empty:
        st.warning("No se pudo derivar consistencia de aula fija desde el calendario.")
    else:
        view = fixed_tbl.copy()
        if show_only_fixed_violations:
            view = view[~view["cumple_aula_fija"]]
        st.caption(f"Cursos evaluados: {len(fixed_tbl)}")
        st.dataframe(view, use_container_width=True, height=420)
        st.download_button(
            "Descargar diagnóstico aula fija CSV",
            df_to_bytes(view, "csv"),
            file_name=f"aula_fija_{safe_slug(periodo)}.csv",
            mime="text/csv"
        )

    st.markdown("---")
    st.subheader("Cobertura de Fase 2")
    if phase2_tbl.empty:
        st.info("No se detectó whitelist por curso en el JSON temporal / meta cargada.")
    else:
        st.dataframe(phase2_tbl.sort_values(["n_aulas_whitelist", "materia", "grupo"]), use_container_width=True, height=420)
        st.download_button(
            "Descargar whitelist fase 2 CSV",
            df_to_bytes(phase2_tbl, "csv"),
            file_name=f"fase2_whitelist_{safe_slug(periodo)}.csv",
            mime="text/csv"
        )

# ------------------------------------------------------------------------------
# TAB 6: Diagnóstico y comparación
# ------------------------------------------------------------------------------
with tab6:
    st.subheader("Diagnóstico de solapes")
    c1, c2, c3 = st.columns(3)

    with c1:
        if room_overlaps.empty:
            st.success("Sin solapes de aula")
        else:
            st.error(f"Solapes de aula: {len(room_overlaps)}")
            st.dataframe(room_overlaps, use_container_width=True, height=260)

    with c2:
        if prof_overlaps.empty:
            st.success("Sin solapes de profesor")
        else:
            st.error(f"Solapes de profesor: {len(prof_overlaps)}")
            st.dataframe(prof_overlaps, use_container_width=True, height=260)

    with c3:
        if group_overlaps.empty:
            st.success("Sin solapes de grupo")
        else:
            st.error(f"Solapes de grupo: {len(group_overlaps)}")
            st.dataframe(group_overlaps, use_container_width=True, height=260)

    st.markdown("---")
    st.subheader("Comparación entre dos soluciones")
    if not other_prefix:
        st.info("Activa la comparación en la barra lateral para contrastar soluciones.")
    elif compare_bundle["cal"].empty or bundle["cal"].empty:
        st.warning("Falta calendario en alguna de las dos soluciones.")
    else:
        st.write(f"Actual: `{main_prefix}`")
        st.write(f"Comparación: `{other_prefix}`")
        diffs = compare_diff
        c1, c2 = st.columns(2)

        with c1:
            st.metric("Sesiones solo en actual", len(diffs.get("solo_actual", pd.DataFrame())))
            if "solo_actual" in diffs and not diffs["solo_actual"].empty:
                st.dataframe(diffs["solo_actual"], use_container_width=True, height=300)

        with c2:
            st.metric("Sesiones solo en comparación", len(diffs.get("solo_comp", pd.DataFrame())))
            if "solo_comp" in diffs and not diffs["solo_comp"].empty:
                st.dataframe(diffs["solo_comp"], use_container_width=True, height=300)

st.caption("Consejo: ahora la tabla de utilización incluye todas las aulas permitidas, aunque tengan 0 uso, y las vistas de profesores/materias pueden mostrar nombre completo si defines PROFESORES_CSV en el .env.")
