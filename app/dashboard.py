"""
app.dashboard
=============

Inteligencia competitiva de la mesa sobre el warehouse de la Circular 1835.

Lee DuckDB en modo solo-lectura: el dashboard nunca escribe, asi que puede
correr mientras el loader recarga sin pelearse por el archivo.

    streamlit run app/dashboard.py
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: La base completa pesa mas de lo que admite un repositorio publico, asi que
#: el despliegue viaja con una muestra de los ultimos meses. Se prefiere la
#: base completa si esta; si no, la muestra. La variable de entorno gana sobre
#: las dos, para poder apuntar a un volumen montado sin tocar el codigo.
DB_COMPLETA = ROOT / "warehouse" / "data" / "cmf1835.duckdb"
DB_SAMPLE = ROOT / "warehouse" / "sample" / "cmf1835_sample.duckdb"


def _elegir_db() -> Path:
    import os
    if (env := os.environ.get("CMF1835_DB")):
        return Path(env)
    if DB_COMPLETA.exists():
        return DB_COMPLETA
    return DB_SAMPLE


DB = _elegir_db()

st.set_page_config(page_title="Mesa de Dinero -- Circular 1835",
                   page_icon="*", layout="wide",
                   initial_sidebar_state="expanded")

PRODUCTOS = ["FORWARD", "SWAP", "PACTO", "FUTURO", "OPCION"]
CATEGORIAS = ["ACTIVE", "NEW", "ROLL", "MATURITY", "UNWIND"]

#: Nuestro grupo. Todo el modulo de oportunidades se lee desde aqui: lo que el
#: cliente hace con otros y no con nosotros es el producto del dashboard.
BANCO_PROPIO = "BBVA"

#: Producto a nivel util para la mesa. `subtipo` ya separa IRS de CCS, que son
#: dos libros distintos aunque el anexo los meta en el mismo registro.
COL_PRODUCTO = "subtipo"


# ---------------------------------------------------------------------------
#  conexion y metadatos
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def conectar() -> duckdb.DuckDBPyConnection:
    if not DB.exists():
        st.error(f"No existe el warehouse en `{DB}`.\n\n"
                 f"Corre primero:\n\n"
                 f"```\npython -m warehouse.loader --data \"<carpeta de ZIP>\"\n"
                 f"python -m analytics.flows --todos\n```")
        st.stop()
    return duckdb.connect(str(DB), read_only=True)


@st.cache_data(show_spinner=False)
def columnas(tabla: str) -> set[str]:
    """Columnas realmente presentes.

    El warehouse se recarga y su esquema puede crecer; los filtros opcionales
    se guardan contra esto para que una columna que aun no existe no bote la
    aplicacion entera.
    """
    try:
        return {r[0] for r in conectar().execute(f"DESCRIBE {tabla}").fetchall()}
    except Exception:
        return set()


@st.cache_data(show_spinner=False)
def q(sql: str) -> pd.DataFrame:
    return conectar().execute(sql).fetch_df()


def existe(tabla: str) -> bool:
    return bool(columnas(tabla))


@st.cache_data(show_spinner=False)
def valores(tabla: str, col: str) -> list:
    if col not in columnas(tabla):
        return []
    df = q(f"SELECT DISTINCT {col} AS v FROM {tabla} "
           f"WHERE {col} IS NOT NULL ORDER BY 1")
    return df["v"].tolist()


@st.cache_data(show_spinner=False)
def rango(tabla: str, col: str) -> tuple[float, float] | None:
    if col not in columnas(tabla):
        return None
    r = q(f"SELECT MIN({col}) lo, MAX({col}) hi FROM {tabla} WHERE {col} IS NOT NULL")
    if r.empty or pd.isna(r.lo[0]) or pd.isna(r.hi[0]):
        return None
    return float(r.lo[0]), float(r.hi[0])


@st.cache_data(show_spinner=False)
def rango_fecha(tabla: str, col: str) -> tuple[_dt.date, _dt.date] | None:
    if col not in columnas(tabla):
        return None
    r = q(f"SELECT MIN({col}) lo, MAX({col}) hi FROM {tabla} WHERE {col} IS NOT NULL")
    if r.empty or pd.isna(r.lo[0]) or pd.isna(r.hi[0]):
        return None
    return pd.to_datetime(r.lo[0]).date(), pd.to_datetime(r.hi[0]).date()


# ---------------------------------------------------------------------------
#  construccion de predicados
# ---------------------------------------------------------------------------

def _lit(v) -> str:
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


# Todas las consultas del dashboard leen del hecho con alias `d` y le pegan
# dim_compania con alias `c`. Las dos tablas tienen rut_compania, asi que un
# predicado sin calificar es ambiguo y DuckDB rechaza la consulta entera. El
# prefijo va aqui, en un solo lugar, y no en cada llamada.
PFX = "d."


def cl_in(tabla: str, col: str, sel, universo, pfx: str = PFX) -> str:
    """IN (...) solo si la seleccion recorta algo. Sin seleccion no filtra."""
    if col not in columnas(tabla) or not sel or (universo and len(sel) == len(universo)):
        return ""
    return f"{pfx}{col} IN ({', '.join(_lit(v) for v in sel)})"


def cl_rango(tabla: str, col: str, sel, tope, pfx: str = PFX) -> str:
    """BETWEEN solo si el usuario movio alguna punta del slider."""
    if col not in columnas(tabla) or not sel or not tope:
        return ""
    lo, hi = sel
    if lo <= tope[0] and hi >= tope[1]:
        return ""
    return f"{pfx}{col} BETWEEN {lo} AND {hi}"


def cl_fecha(tabla: str, col: str, sel, tope, pfx: str = PFX) -> str:
    if col not in columnas(tabla) or not sel or not tope or len(sel) != 2:
        return ""
    lo, hi = sel
    if lo <= tope[0] and hi >= tope[1]:
        return ""
    return f"{pfx}{col} BETWEEN DATE '{lo}' AND DATE '{hi}'"


def where(partes: list[str]) -> str:
    vivos = [p for p in partes if p]
    return (" WHERE " + " AND ".join(vivos)) if vivos else ""


# ---------------------------------------------------------------------------
#  sidebar
# ---------------------------------------------------------------------------

def construir_sidebar() -> dict:
    """Filtros sobre todas las dimensiones y metricas del warehouse."""
    f: dict = {}
    sb = st.sidebar
    sb.title("Filtros")

    # --- periodo ------------------------------------------------------------
    periodos = [int(p) for p in valores("fact_derivado", "periodo_informacion")]
    if not periodos:
        st.error("El warehouse esta vacio."); st.stop()
    sb.caption(f"{len(periodos)} periodos: {periodos[0]} a {periodos[-1]}")
    f["periodos"] = sb.multiselect("Periodo (AAAAMM)", periodos, default=periodos,
                                   help="El universo de meses sobre el que se calcula todo.")
    f["periodo_foco"] = sb.selectbox(
        "Periodo foco", sorted(f["periodos"] or periodos, reverse=True),
        help="Mes para el Whitespace Map, el Roll-Off y la cascada de flujos.")
    f["_periodos_all"] = periodos

    # --- entidades ----------------------------------------------------------
    with sb.expander("Entidades", expanded=True):
        comp = q("SELECT rut_compania, COALESCE(nombre, CAST(rut_compania AS VARCHAR)) AS nombre "
                 "FROM dim_compania ORDER BY nombre") if existe("dim_compania") else pd.DataFrame()
        mapa_comp = dict(zip(comp["nombre"], comp["rut_compania"])) if not comp.empty else {}
        sel_comp = st.multiselect("Aseguradora", list(mapa_comp), default=list(mapa_comp))
        f["companias"] = [mapa_comp[n] for n in sel_comp]
        f["_companias_all"] = list(mapa_comp.values())
        f["mapa_compania"] = {v: k for k, v in mapa_comp.items()}

        grupos = valores("fact_derivado", "contraparte_grupo")
        f["grupos"] = st.multiselect("Grupo de contraparte", grupos, default=grupos)
        f["_grupos_all"] = grupos

        cps = valores("fact_derivado", "contraparte_key")
        f["contrapartes"] = st.multiselect("Contraparte (entidad)", cps, default=cps)
        f["_contrapartes_all"] = cps

        paises = valores("fact_derivado", "contraparte_pais")
        f["paises_cp"] = st.multiselect("Pais de contraparte", paises, default=paises)
        f["_paises_cp_all"] = paises

        tipos_cp = valores("fact_derivado", "contraparte_tipo")
        f["tipos_cp"] = st.multiselect("Tipo de contraparte", tipos_cp, default=tipos_cp)
        f["_tipos_cp_all"] = tipos_cp

        metodos = valores("fact_derivado", "resolucion_metodo")
        f["metodos"] = st.multiselect("Metodo de resolucion", metodos, default=metodos,
                                      help="Como se identifico la contraparte: RUT, alias, LEI o sin resolver.")
        f["_metodos_all"] = metodos
        f["confianza_min"] = st.slider("Confianza minima de resolucion", 0.0, 1.0, 0.0, 0.01)

    # --- contrato -----------------------------------------------------------
    with sb.expander("Contrato y producto", expanded=True):
        prods = valores("fact_derivado", "producto")
        f["productos"] = st.multiselect("Producto", prods, default=prods)
        f["_productos_all"] = prods
        for clave, col, etiqueta in [
            ("subtipos", "subtipo", "Subtipo (IRS / CCS / ...)"),
            ("subyacente", "subyacente_contrato", "Subyacente del contrato"),
            ("rol_fija", "rol_tasa_fija", "Rol en la tasa fija"),
            ("par_monedas", "par_monedas", "Par de monedas"),
            ("pata_larga", "pata_larga_tipo", "Indice pata larga"),
            ("pata_corta", "pata_corta_tipo", "Indice pata corta"),
        ]:
            vals = valores("fact_derivado", col)
            if vals:
                f[clave] = st.multiselect(etiqueta, vals, default=vals)
                f[f"_{clave}_all"] = vals

        for clave, col, etiqueta in [
            ("tipo_operacion", "tipo_operacion", "Tipo de operacion"),
            ("tipo_contrato", "tipo_contrato", "Tipo de contrato"),
            ("objetivo", "objetivo_contrato", "Objetivo del contrato"),
            ("tipo_contraparte", "tipo_contraparte", "Tipo de contraparte (contrato)"),
            ("relacionado", "relacionado", "Relacionado"),
            ("compensacion", "cm_compensacion_bilateral", "Compensacion bilateral"),
            ("documentacion", "tipo_documentacion", "Tipo de documentacion"),
            ("clasif_eeff", "clasif_valoriz_eeff", "Clasificacion EEFF"),
            ("nocional_origen", "nocional_origen", "Origen del nocional"),
        ]:
            vals = valores("fact_derivado", col)
            if vals:
                f[clave] = st.multiselect(etiqueta, vals, default=vals)
                f[f"_{clave}_all"] = vals

    # --- riesgo y moneda ----------------------------------------------------
    with sb.expander("Riesgo y moneda", expanded=True):
        cr = valores("fact_derivado", "clasificacion_riesgo")
        if cr:
            f["clasif_riesgo"] = st.multiselect("Clasificacion de riesgo (derivados)", cr, default=cr)
            f["_clasif_riesgo_all"] = cr
        crf = valores("fact_renta_fija", "clasificacion_riesgo")
        if crf:
            f["clasif_riesgo_rf"] = st.multiselect("Clasificacion de riesgo (renta fija)", crf, default=crf)
            f["_clasif_riesgo_rf_all"] = crf
        ci = valores("fact_renta_fija", "clasificacion_inversion")
        if ci:
            f["clasif_inversion"] = st.multiselect("Clasificacion de inversion", ci, default=ci)
            f["_clasif_inversion_all"] = ci
        mon = valores("fact_derivado", "moneda")
        f["monedas"] = st.multiselect("Moneda (derivados)", mon, default=mon)
        f["_monedas_all"] = mon
        um = valores("fact_renta_fija", "unidad_monetaria")
        f["unidades"] = st.multiselect("Unidad monetaria (renta fija)", um, default=um)
        f["_unidades_all"] = um
        ti = valores("fact_renta_fija", "tipo_instrumento")
        f["tipo_instrumento"] = st.multiselect("Tipo de instrumento", ti, default=ti)
        f["_tipo_instrumento_all"] = ti

    # --- fechas y plazos ----------------------------------------------------
    with sb.expander("Fechas y plazos", expanded=False):
        for clave, tabla, col, etiqueta in [
            ("f_operacion", "fact_derivado", "fecha_operacion", "Fecha de operacion"),
            ("f_vencimiento", "fact_derivado", "fecha_vencimiento", "Fecha de vencimiento (derivados)"),
            ("f_emision", "fact_renta_fija", "fecha_emision", "Fecha de emision (RF)"),
            ("f_compra", "fact_renta_fija", "fecha_compra", "Fecha de compra (RF)"),
            ("f_venc_rf", "fact_renta_fija", "fecha_vencimiento", "Fecha de vencimiento (RF)"),
        ]:
            tope = rango_fecha(tabla, col)
            if tope:
                f[f"_{clave}_tope"] = tope
                f[clave] = st.date_input(etiqueta, value=tope,
                                         min_value=tope[0], max_value=tope[1])
        f["plazo_dias"] = st.slider("Plazo al vencimiento (dias, derivados)",
                                    -3650, 18250, (-3650, 18250), 30,
                                    help="Dias entre el cierre del periodo y el vencimiento. "
                                         "Negativo = ya vencido.")
        tope = rango("fact_renta_fija", "plazo_meses")
        if tope:
            f["_plazo_rf_tope"] = tope
            f["plazo_rf"] = st.slider("Plazo al vencimiento (renta fija, MESES)",
                                      tope[0], tope[1], tope,
                                      help="El anexo informa este plazo en meses, no en dias.")
        tope = rango("fact_renta_fija", "duracion_modificada_aprox")
        if tope:
            f["_duracion_tope"] = tope
            f["duracion"] = st.slider("Duracion modificada aprox. (anios)", tope[0], tope[1], tope,
                                      help="Aproximacion bullet: el anexo no trae el calendario "
                                           "de cupones. Sirve para ordenar, no para un hedge.")

    # --- metricas -----------------------------------------------------------
    with sb.expander("Metricas", expanded=False):
        for clave, tabla, col, etiqueta in [
            ("nocional", "fact_derivado", "nocional_m", "Nocional (M$)"),
            ("mtm_a", "fact_derivado", "mtm_activo_m", "MTM activo (M$)"),
            ("mtm_p", "fact_derivado", "mtm_pasivo_m", "MTM pasivo (M$)"),
            ("margen", "fact_derivado", "margen_m", "Margen entregado (M$)"),
            ("tasa_c", "fact_derivado", "tasa_precio_contrato", "Tasa / precio pactado"),
            ("tasa_m", "fact_derivado", "tasa_precio_mercado", "Tasa / precio de mercado"),
            ("tir_c", "fact_renta_fija", "tir_compra", "TIR compra (RF)"),
            ("tir_mk", "fact_renta_fija", "tir_mercado", "TIR mercado (RF)"),
            ("tasa_em", "fact_renta_fija", "tasa_emision", "Tasa de emision (RF)"),
            ("vf_rf", "fact_renta_fija", "valor_final", "Valor final (RF, M$)"),
        ]:
            tope = rango(tabla, col)
            if tope and tope[0] < tope[1]:
                f[f"_{clave}_tope"] = tope
                f[clave] = st.slider(etiqueta, tope[0], tope[1], tope)

    # --- calidad y bitemporal ----------------------------------------------
    with sb.expander("Calidad del dato", expanded=False):
        ver = valores("fact_derivado", "veredicto")
        f["veredictos"] = st.multiselect("Veredicto de validacion", ver, default=ver)
        f["_veredictos_all"] = ver
        zips = valores("fact_derivado", "zip_origen")
        f["zips"] = st.multiselect("Publicacion de origen", zips, default=zips,
                                   help="La CMF republica: el mismo periodo puede venir "
                                        "dos veces con contenido distinto.")
        f["_zips_all"] = zips
        f["solo_resueltas"] = st.checkbox("Solo contrapartes resueltas", value=False)

    return f


# ---------------------------------------------------------------------------
#  predicados por tabla
# ---------------------------------------------------------------------------

def predicados_derivado(f: dict, *, periodos: list[int] | None = None) -> str:
    t = "fact_derivado"
    per = periodos if periodos is not None else f["periodos"]
    partes = [
        f"d.periodo_informacion IN ({', '.join(str(p) for p in per)})" if per else "1=0",
        cl_in(t, "rut_compania", f.get("companias"), f.get("_companias_all")),
        cl_in(t, "contraparte_grupo", f.get("grupos"), f.get("_grupos_all")),
        cl_in(t, "contraparte_key", f.get("contrapartes"), f.get("_contrapartes_all")),
        cl_in(t, "contraparte_pais", f.get("paises_cp"), f.get("_paises_cp_all")),
        cl_in(t, "contraparte_tipo", f.get("tipos_cp"), f.get("_tipos_cp_all")),
        cl_in(t, "resolucion_metodo", f.get("metodos"), f.get("_metodos_all")),
        cl_in(t, "producto", f.get("productos"), f.get("_productos_all")),
        cl_in(t, "tipo_operacion", f.get("tipo_operacion"), f.get("_tipo_operacion_all")),
        cl_in(t, "tipo_contrato", f.get("tipo_contrato"), f.get("_tipo_contrato_all")),
        cl_in(t, "objetivo_contrato", f.get("objetivo"), f.get("_objetivo_all")),
        cl_in(t, "tipo_contraparte", f.get("tipo_contraparte"), f.get("_tipo_contraparte_all")),
        cl_in(t, "relacionado", f.get("relacionado"), f.get("_relacionado_all")),
        cl_in(t, "cm_compensacion_bilateral", f.get("compensacion"), f.get("_compensacion_all")),
        cl_in(t, "tipo_documentacion", f.get("documentacion"), f.get("_documentacion_all")),
        cl_in(t, "clasif_valoriz_eeff", f.get("clasif_eeff"), f.get("_clasif_eeff_all")),
        cl_in(t, "nocional_origen", f.get("nocional_origen"), f.get("_nocional_origen_all")),
        cl_in(t, "subtipo", f.get("subtipos"), f.get("_subtipos_all")),
        cl_in(t, "subyacente_contrato", f.get("subyacente"), f.get("_subyacente_all")),
        cl_in(t, "rol_tasa_fija", f.get("rol_fija"), f.get("_rol_fija_all")),
        cl_in(t, "par_monedas", f.get("par_monedas"), f.get("_par_monedas_all")),
        cl_in(t, "pata_larga_tipo", f.get("pata_larga"), f.get("_pata_larga_all")),
        cl_in(t, "pata_corta_tipo", f.get("pata_corta"), f.get("_pata_corta_all")),
        cl_in(t, "clasificacion_riesgo", f.get("clasif_riesgo"), f.get("_clasif_riesgo_all")),
        cl_in(t, "moneda", f.get("monedas"), f.get("_monedas_all")),
        cl_in(t, "veredicto", f.get("veredictos"), f.get("_veredictos_all")),
        cl_in(t, "zip_origen", f.get("zips"), f.get("_zips_all")),
        cl_fecha(t, "fecha_operacion", f.get("f_operacion"), f.get("_f_operacion_tope")),
        cl_fecha(t, "fecha_vencimiento", f.get("f_vencimiento"), f.get("_f_vencimiento_tope")),
        cl_rango(t, "nocional_m", f.get("nocional"), f.get("_nocional_tope")),
        cl_rango(t, "mtm_activo_m", f.get("mtm_a"), f.get("_mtm_a_tope")),
        cl_rango(t, "mtm_pasivo_m", f.get("mtm_p"), f.get("_mtm_p_tope")),
        cl_rango(t, "margen_m", f.get("margen"), f.get("_margen_tope")),
        cl_rango(t, "tasa_precio_contrato", f.get("tasa_c"), f.get("_tasa_c_tope")),
        cl_rango(t, "tasa_precio_mercado", f.get("tasa_m"), f.get("_tasa_m_tope")),
    ]
    if f.get("confianza_min", 0) > 0:
        partes.append(f"d.resolucion_confianza >= {f['confianza_min']}")
    if f.get("solo_resueltas"):
        partes.append("d.contraparte_key NOT LIKE 'UNRESOLVED::%'")
    pl = f.get("plazo_dias")
    if pl and pl != (-3650, 18250):
        partes.append(
            "DATE_DIFF('day', LAST_DAY(STRPTIME(CAST(d.periodo_informacion AS VARCHAR)||'01','%Y%m%d')), "
            f"d.fecha_vencimiento) BETWEEN {pl[0]} AND {pl[1]}")
    return where(partes)


def predicados_rf(f: dict) -> str:
    t = "fact_renta_fija"
    partes = [
        f"d.periodo_informacion IN ({', '.join(str(p) for p in f['periodos'])})" if f["periodos"] else "1=0",
        cl_in(t, "rut_compania", f.get("companias"), f.get("_companias_all")),
        cl_in(t, "tipo_instrumento", f.get("tipo_instrumento"), f.get("_tipo_instrumento_all")),
        cl_in(t, "unidad_monetaria", f.get("unidades"), f.get("_unidades_all")),
        cl_in(t, "clasificacion_riesgo", f.get("clasif_riesgo_rf"), f.get("_clasif_riesgo_rf_all")),
        cl_in(t, "clasificacion_inversion", f.get("clasif_inversion"), f.get("_clasif_inversion_all")),
        cl_in(t, "veredicto", f.get("veredictos"), f.get("_veredictos_all")),
        cl_fecha(t, "fecha_emision", f.get("f_emision"), f.get("_f_emision_tope")),
        cl_fecha(t, "fecha_compra", f.get("f_compra"), f.get("_f_compra_tope")),
        cl_fecha(t, "fecha_vencimiento", f.get("f_venc_rf"), f.get("_f_venc_rf_tope")),
        cl_rango(t, "plazo_meses", f.get("plazo_rf"), f.get("_plazo_rf_tope")),
        cl_rango(t, "duracion_modificada_aprox", f.get("duracion"), f.get("_duracion_tope")),
        cl_rango(t, "tir_compra", f.get("tir_c"), f.get("_tir_c_tope")),
        cl_rango(t, "tir_mercado", f.get("tir_mk"), f.get("_tir_mk_tope")),
        cl_rango(t, "tasa_emision", f.get("tasa_em"), f.get("_tasa_em_tope")),
        cl_rango(t, "valor_final", f.get("vf_rf"), f.get("_vf_rf_tope")),
    ]
    return where(partes)


def predicados_garantia(f: dict) -> str:
    t = "fact_garantia"
    partes = [
        f"d.periodo_informacion IN ({', '.join(str(p) for p in f['periodos'])})" if f["periodos"] else "1=0",
        cl_in(t, "rut_compania", f.get("companias"), f.get("_companias_all")),
        cl_in(t, "contraparte_grupo", f.get("grupos"), f.get("_grupos_all")),
        cl_in(t, "contraparte_key", f.get("contrapartes"), f.get("_contrapartes_all")),
        cl_in(t, "moneda", f.get("monedas"), f.get("_monedas_all")),
    ]
    return where(partes)


def predicados_flujo(f: dict, periodo: int) -> str:
    t = "fact_flujo"
    partes = [
        f"d.periodo = {periodo}",
        cl_in(t, "rut_compania", f.get("companias"), f.get("_companias_all")),
        cl_in(t, "contraparte_grupo", f.get("grupos"), f.get("_grupos_all")),
        cl_in(t, "contraparte_key", f.get("contrapartes"), f.get("_contrapartes_all")),
        cl_in(t, "producto", f.get("productos"), f.get("_productos_all")),
        cl_in(t, "moneda", f.get("monedas"), f.get("_monedas_all")),
    ]
    return where(partes)


def nombre_compania_sql() -> str:
    return ("COALESCE(c.nombre, CAST(d.rut_compania AS VARCHAR))"
            if existe("dim_compania") else "CAST(d.rut_compania AS VARCHAR)")


JOIN_COMP = ("LEFT JOIN dim_compania c ON c.rut_compania = d.rut_compania"
             if existe("dim_compania") else "")


# ---------------------------------------------------------------------------
#  vistas
# ---------------------------------------------------------------------------

def vista_whitespace(f: dict) -> None:
    st.subheader("Whitespace Map")
    st.caption("Nocional activo por aseguradora y grupo bancario. "
               "Las celdas vacias son el producto: relacion que existe para otros y no para nosotros.")
    per = f["periodo_foco"]
    w = predicados_derivado(f, periodos=[per])
    df = q(f"""
        SELECT {nombre_compania_sql()} AS aseguradora,
               d.contraparte_grupo      AS grupo,
               SUM(COALESCE(d.nocional_m, 0)) AS nocional_m,
               COUNT(*) AS operaciones
        FROM fact_derivado d {JOIN_COMP}
        {w} {'AND' if w else 'WHERE'} d.contraparte_grupo IS NOT NULL
        GROUP BY 1, 2
    """)
    if df.empty:
        st.info("Sin datos para los filtros elegidos."); return

    c1, c2 = st.columns([1, 3])
    modo = c1.radio("Metrica", ["Nocional (M$)", "Operaciones", "% de la aseguradora"],
                    horizontal=False)
    val = {"Nocional (M$)": "nocional_m", "Operaciones": "operaciones",
           "% de la aseguradora": "nocional_m"}[modo]
    piv = df.pivot_table(index="aseguradora", columns="grupo", values=val,
                         aggfunc="sum", fill_value=0)
    if modo == "% de la aseguradora":
        tot = piv.sum(axis=1).replace(0, pd.NA)
        piv = (piv.div(tot, axis=0) * 100).fillna(0).round(1)
    piv = piv.loc[piv.sum(axis=1).sort_values(ascending=False).index]
    piv = piv[piv.sum(axis=0).sort_values(ascending=False).index]

    fig = px.imshow(piv, aspect="auto", color_continuous_scale="Blues",
                    labels=dict(x="Grupo contraparte", y="Aseguradora", color=modo),
                    text_auto=".0f" if modo != "Nocional (M$)" else False)
    fig.update_layout(height=max(380, 26 * len(piv) + 160), margin=dict(l=8, r=8, t=30, b=8))
    c2.plotly_chart(fig, width="stretch")

    huecos = int((piv == 0).sum().sum())
    st.metric("Celdas en blanco (sin relacion)", f"{huecos:,}",
              help="Pares aseguradora-banco sin una sola operacion en el periodo foco.")
    with st.expander("Detalle de los huecos mas grandes"):
        tot_aseg = df.groupby("aseguradora")["nocional_m"].sum().sort_values(ascending=False)
        faltantes = [
            {"aseguradora": a, "grupo_ausente": g, "nocional_total_aseguradora_m": tot_aseg.get(a, 0)}
            for a in piv.index for g in piv.columns if piv.loc[a, g] == 0
        ]
        if faltantes:
            st.dataframe(pd.DataFrame(faltantes)
                         .sort_values("nocional_total_aseguradora_m", ascending=False),
                         width="stretch", hide_index=True)
        else:
            # Con los filtros muy recortados puede no quedar ningun hueco; un
            # DataFrame vacio no tiene columnas que ordenar.
            st.success("Sin huecos: cada aseguradora del filtro opera con cada grupo del filtro.")


def vista_rolloff(f: dict) -> None:
    st.subheader("Roll-Off Calendar")
    st.caption("Perfil de vencimientos futuros desde el cierre del periodo foco, apilado por contraparte.")
    per = f["periodo_foco"]
    w = predicados_derivado(f, periodos=[per])
    gran = st.radio("Granularidad", ["Mensual", "Trimestral", "Anual"], horizontal=True)
    trunc = {"Mensual": "month", "Trimestral": "quarter", "Anual": "year"}[gran]
    horizonte = st.slider("Horizonte (meses)", 3, 120, 24, 3)

    df = q(f"""
        WITH base AS (
            SELECT d.*, LAST_DAY(STRPTIME(CAST(d.periodo_informacion AS VARCHAR)||'01','%Y%m%d')) AS cierre
            FROM fact_derivado d {JOIN_COMP}
            {w}
        )
        SELECT DATE_TRUNC('{trunc}', fecha_vencimiento) AS balde,
               COALESCE(contraparte_grupo, 'SIN RESOLVER') AS grupo,
               SUM(COALESCE(nocional_m, 0)) AS nocional_m,
               COUNT(*) AS operaciones
        FROM base
        WHERE fecha_vencimiento IS NOT NULL
          AND fecha_vencimiento >= cierre
          AND fecha_vencimiento < cierre + INTERVAL '{horizonte}' MONTH
        GROUP BY 1, 2 ORDER BY 1
    """)
    if df.empty:
        st.info("Sin vencimientos futuros para los filtros elegidos."); return

    fig = px.bar(df, x="balde", y="nocional_m", color="grupo",
                 labels={"balde": "Vencimiento", "nocional_m": "Nocional (M$)", "grupo": "Contraparte"},
                 hover_data=["operaciones"])
    fig.update_layout(barmode="stack", height=520, margin=dict(l=8, r=8, t=30, b=8),
                      legend=dict(orientation="h", y=-0.2))
    st.plotly_chart(fig, width="stretch")

    st.dataframe(
        df.pivot_table(index="balde", columns="grupo", values="nocional_m",
                       aggfunc="sum", fill_value=0).round(0),
        width="stretch")


def vista_price_discovery(f: dict) -> None:
    st.subheader("Price Discovery OTC")
    st.caption("Tasa o precio pactado contra plazo al vencimiento. Cada punto es una operacion.")
    prods = f.get("productos") or PRODUCTOS
    c1, c2, c3 = st.columns(3)
    # Mezclar productos en un mismo eje no dice nada: el precio de un forward de
    # dolar y la tasa de un swap no viven en la misma escala.
    prod = c1.selectbox("Producto", prods, index=0,
                        help="Se grafica un producto a la vez: sus precios no son comparables entre si.")
    eje = c2.selectbox("Eje Y", ["Tasa/precio pactado", "Spread contra mercado"])
    logx = c3.checkbox("Plazo en escala log", value=False)

    w = predicados_derivado(f)
    df = q(f"""
        SELECT d.periodo_informacion AS periodo,
               {nombre_compania_sql()} AS aseguradora,
               d.folio_operacion, d.item_operacion, d.producto, d.tipo_operacion,
               d.contraparte_grupo, d.contraparte_nombre, d.contraparte_key,
               d.moneda, d.clasificacion_riesgo, d.fecha_operacion, d.fecha_vencimiento,
               d.nocional_m, d.mtm_activo_m, d.mtm_pasivo_m,
               COALESCE(d.mtm_activo_m,0) - COALESCE(d.mtm_pasivo_m,0) AS mtm_neto_m,
               d.tasa_precio_contrato, d.tasa_precio_mercado, d.tasa_precio_origen,
               d.precio_spot, d.tasa_descuento, d.margen_m, d.veredicto,
               DATE_DIFF('day',
                   LAST_DAY(STRPTIME(CAST(d.periodo_informacion AS VARCHAR)||'01','%Y%m%d')),
                   d.fecha_vencimiento) AS plazo_dias
        FROM fact_derivado d {JOIN_COMP}
        {w}
        {'AND' if w else 'WHERE'} d.producto = {_lit(prod)}
          AND d.tasa_precio_contrato IS NOT NULL
          AND d.fecha_vencimiento IS NOT NULL
    """)
    if df.empty:
        st.info(f"Sin {prod} con tasa/precio informado para los filtros elegidos."); return

    df["spread"] = df["tasa_precio_contrato"] - df["tasa_precio_mercado"]
    ycol = "tasa_precio_contrato" if eje == "Tasa/precio pactado" else "spread"
    dfp = df[df[ycol].notna() & (df["plazo_dias"] > 0)] if logx else df[df[ycol].notna()]
    if dfp.empty:
        st.info("Nada que graficar con esa combinacion."); return

    fig = px.scatter(
        dfp, x="plazo_dias", y=ycol, color="contraparte_grupo",
        size=dfp["nocional_m"].abs().fillna(0) + 1, size_max=26, opacity=0.75,
        log_x=logx,
        labels={"plazo_dias": "Plazo al vencimiento (dias)", ycol: eje,
                "contraparte_grupo": "Contraparte"},
        hover_data={
            "folio_operacion": True, "item_operacion": True, "aseguradora": True,
            "contraparte_nombre": True, "periodo": True, "producto": True,
            "tipo_operacion": True, "moneda": True, "clasificacion_riesgo": True,
            "fecha_operacion": True, "fecha_vencimiento": True,
            "nocional_m": ":,.0f", "mtm_activo_m": ":,.0f", "mtm_pasivo_m": ":,.0f",
            "mtm_neto_m": ":,.0f", "margen_m": ":,.0f",
            "tasa_precio_contrato": ":,.4f", "tasa_precio_mercado": ":,.4f",
            "tasa_precio_origen": True, "precio_spot": ":,.4f",
            "tasa_descuento": ":,.4f", "veredicto": True, "plazo_dias": True,
        })
    fig.update_layout(height=600, margin=dict(l=8, r=8, t=30, b=8),
                      legend=dict(orientation="h", y=-0.18))
    st.plotly_chart(fig, width="stretch")

    st.caption(f"{len(dfp):,} operaciones graficadas. "
               f"Campo de origen del precio: {', '.join(sorted(dfp['tasa_precio_origen'].dropna().unique()))}")
    with st.expander("Dispersion por contraparte"):
        st.dataframe(
            dfp.groupby("contraparte_grupo")[ycol]
               .agg(n="count", mediana="median", p25=lambda s: s.quantile(.25),
                    p75=lambda s: s.quantile(.75), desv="std")
               .sort_values("n", ascending=False).round(4),
            width="stretch")


def vista_flujos(f: dict) -> None:
    st.subheader("Flujos mensuales")
    if not existe("fact_flujo"):
        st.warning("La tabla `fact_flujo` no existe todavia.\n\n"
                   "Corre:\n```\npython -m analytics.flows --todos\n```")
        return
    per = f["periodo_foco"]
    disponibles = [int(p) for p in valores("fact_flujo", "periodo")]
    if per not in disponibles:
        st.warning(f"El periodo {per} no esta clasificado. Hay: {disponibles}")
        return

    w = predicados_flujo(f, per)
    df = q(f"SELECT d.* FROM fact_flujo d {w}")
    if df.empty:
        st.info("Sin flujos para los filtros elegidos."); return

    def suma(cat: str, col: str) -> float:
        return float(df.loc[df.categoria == cat, col].fillna(0).sum())

    stock_prev = (suma("ACTIVE", "nocional_prev_m") + suma("MATURITY", "nocional_prev_m")
                  + suma("UNWIND", "nocional_prev_m") + suma("ROLL", "nocional_prev_m"))
    nuevos = suma("NEW", "nocional_m")
    roll_in = suma("ROLL", "nocional_m")
    roll_out = -suma("ROLL", "nocional_prev_m")
    vencidos = -suma("MATURITY", "nocional_prev_m")
    unwinds = -suma("UNWIND", "nocional_prev_m")
    deriva = suma("ACTIVE", "nocional_m") - suma("ACTIVE", "nocional_prev_m")
    stock_t = stock_prev + nuevos + roll_in + roll_out + vencidos + unwinds + deriva

    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=["absolute", "relative", "relative", "relative", "relative", "relative",
                 "relative", "total"],
        x=[f"Stock {anterior(per)}", "NEW", "ROLL entra", "ROLL sale", "MATURITY",
           "UNWIND", "Δ ACTIVE", f"Stock {per}"],
        y=[stock_prev, nuevos, roll_in, roll_out, vencidos, unwinds, deriva, stock_t],
        text=[f"{v:,.0f}" for v in
              [stock_prev, nuevos, roll_in, roll_out, vencidos, unwinds, deriva, stock_t]],
        connector={"line": {"color": "rgb(150,150,150)"}},
        increasing={"marker": {"color": "#2e7d32"}},
        decreasing={"marker": {"color": "#c62828"}},
        totals={"marker": {"color": "#1565c0"}},
    ))
    fig.update_layout(height=520, yaxis_title="Nocional (M$)",
                      margin=dict(l=8, r=8, t=30, b=8))
    st.plotly_chart(fig, width="stretch")
    st.caption("`Δ ACTIVE` es el movimiento de nocional de las posiciones que siguen vivas "
               "(amortizacion o revaluacion). Sin ese termino la cascada no cuadra.")

    c1, c2 = st.columns(2)
    res = (df.assign(nocional=lambda x: x.nocional_m.fillna(x.nocional_prev_m))
             .groupby("categoria")
             .agg(posiciones=("folio_operacion", "count"),
                  nocional_m=("nocional", "sum"),
                  contrapartes=("contraparte_grupo", "nunique"))
             .reindex(CATEGORIAS).fillna(0))
    res["% posiciones"] = (100 * res.posiciones / res.posiciones.sum()).round(2)
    c1.dataframe(res, width="stretch")
    c2.plotly_chart(
        px.bar(df.groupby(["producto", "categoria"]).size().reset_index(name="n"),
               x="producto", y="n", color="categoria", barmode="stack",
               category_orders={"categoria": CATEGORIAS},
               labels={"n": "Posiciones"}).update_layout(height=340, margin=dict(t=30)),
        width="stretch")

    # Una publicacion incompleta se lee como una fuga de clientes si nadie avisa.
    n_t = q(f"SELECT COUNT(DISTINCT rut_compania) n FROM fact_derivado "
            f"WHERE periodo_informacion = {per}").n[0]
    n_tm = q(f"SELECT COUNT(DISTINCT rut_compania) n FROM fact_derivado "
             f"WHERE periodo_informacion = {anterior(per)}").n[0]
    if n_t < n_tm:
        st.error(f"Solo {n_t} aseguradoras informaron {per}, contra {n_tm} en {anterior(per)}. "
                 f"Las posiciones de las que faltan aparecen como cerradas sin que nadie "
                 f"las haya cerrado: la lectura de UNWIND y MATURITY de este periodo "
                 f"no es utilizable hasta que la CMF complete la publicacion.")

    with st.expander("Rolls detectados (con su par y la diferencia de nocional)"):
        st.dataframe(df[df.categoria == "ROLL"]
                     .sort_values("nocional_m", ascending=False, na_position="last"),
                     width="stretch", hide_index=True)


def vista_garantias(f: dict) -> None:
    st.subheader("Mapa de garantias (B.14)")
    if not existe("fact_garantia"):
        st.warning("No hay `fact_garantia` en el warehouse."); return
    w = predicados_garantia(f)
    df = q(f"""
        SELECT d.periodo_informacion AS periodo,
               {nombre_compania_sql()} AS aseguradora,
               d.rut_compania,
               d.contraparte_grupo, d.contraparte_nombre, d.contraparte_key,
               d.posicion_compania, d.tipo_garantia, d.tipo_activo,
               d.identificador_garantia, d.codigo_instrumento, d.folio_instrumento,
               d.moneda, d.clasificacion_riesgo_pais, d.relacionado,
               d.valor_nominal_instrumento, d.valor_contable_m, d.monto_m,
               d.valor_contable_um, d.valor_razonable_um, d.cuenta_eeff, d.veredicto,
               d.source_file, d.line_no
        FROM fact_garantia d {JOIN_COMP}
        {w}
    """)
    if df.empty:
        st.info("Sin garantias para los filtros elegidos."); return

    # TIPO_ACTIVO_EN_GARANTIA distingue efectivo de instrumento; se agrupa en
    # dos baldes legibles sin perder el codigo original, que queda en la tabla.
    def clase(v: str) -> str:
        s = (v or "").upper()
        if any(k in s for k in ("EFEC", "CASH", "DEPO", "DINER")):
            return "Efectivo"
        return "Instrumento" if s.strip() else "Sin informar"
    df["clase_activo"] = df["tipo_activo"].map(clase)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Registros", f"{len(df):,}")
    c2.metric("Monto total (M$)", f"{df.monto_m.fillna(0).sum():,.0f}")
    c3.metric("Contrapartes", f"{df.contraparte_grupo.nunique():,}")
    c4.metric("Aseguradoras", f"{df.aseguradora.nunique():,}")

    st.markdown("**Quien postea a quien**")
    resumen = (df.groupby(["aseguradora", "contraparte_grupo", "posicion_compania",
                           "clase_activo", "moneda"], dropna=False)
                 .agg(registros=("monto_m", "size"),
                      monto_m=("monto_m", "sum"),
                      valor_contable_m=("valor_contable_m", "sum"))
                 .reset_index()
                 .sort_values("monto_m", ascending=False))
    st.dataframe(resumen, width="stretch", hide_index=True,
                 column_config={
                     "monto_m": st.column_config.NumberColumn("Monto (M$)", format="%.0f"),
                     "valor_contable_m": st.column_config.NumberColumn("Contable (M$)", format="%.0f"),
                     "posicion_compania": st.column_config.TextColumn("Posicion cia."),
                     "clase_activo": st.column_config.TextColumn("Efectivo / Instrumento"),
                 })

    c1, c2 = st.columns(2)
    c1.plotly_chart(
        px.bar(df.groupby(["contraparte_grupo", "clase_activo"], dropna=False)
                 .monto_m.sum().reset_index(),
               x="contraparte_grupo", y="monto_m", color="clase_activo", barmode="stack",
               labels={"monto_m": "Monto (M$)", "contraparte_grupo": "Contraparte"})
          .update_layout(height=380, margin=dict(t=30), legend=dict(orientation="h", y=-0.3)),
        width="stretch")
    c2.plotly_chart(
        px.pie(df.groupby("clase_activo").monto_m.sum().reset_index(),
               names="clase_activo", values="monto_m", hole=.45)
          .update_layout(height=380, margin=dict(t=30)),
        width="stretch")

    st.markdown("**Detalle**")
    st.dataframe(df, width="stretch", hide_index=True)
    st.download_button("Descargar garantias (CSV)",
                       df.to_csv(index=False).encode("utf-8"),
                       f"garantias_{'-'.join(str(p) for p in f['periodos'][:2])}.csv",
                       "text/csv")




# ---------------------------------------------------------------------------
#  modulo de oportunidades
# ---------------------------------------------------------------------------

def _col_producto() -> str:
    """`subtipo` si el warehouse ya lo trae; si no, `producto`."""
    return COL_PRODUCTO if COL_PRODUCTO in columnas("fact_derivado") else "producto"


@st.cache_data(show_spinner=False)
def matriz_competencia(periodos: tuple[int, ...], w_extra: str) -> pd.DataFrame:
    """Nocional por (aseguradora, producto, grupo bancario).

    Es la materia prima del modulo: con esto se responde que hace cada cliente,
    en que producto, y con quien.
    """
    cp = _col_producto()
    pers = ", ".join(str(x) for x in periodos) or "0"
    return q(f"""
        SELECT {nombre_compania_sql()}            AS aseguradora,
               d.rut_compania,
               d.{cp}                             AS producto,
               d.contraparte_grupo                AS banco,
               SUM(COALESCE(d.nocional_m, 0))     AS nocional_m,
               SUM(COALESCE(d.mtm_contrato_m, COALESCE(d.mtm_activo_m,0)-COALESCE(d.mtm_pasivo_m,0))) AS mtm_m,
               COUNT(*)                           AS operaciones,
               MIN(d.fecha_vencimiento)           AS primer_vencimiento,
               MAX(d.fecha_vencimiento)           AS ultimo_vencimiento
        FROM fact_derivado d {JOIN_COMP}
        WHERE d.periodo_informacion IN ({pers})
          AND d.contraparte_grupo IS NOT NULL
          AND d.{cp} IS NOT NULL
          {w_extra}
        GROUP BY 1, 2, 3, 4
    """)


def vista_whitespace2(f: dict) -> None:
    st.subheader("Whitespace 2.0 -- Competitor Intel")
    st.caption(f"Que hace cada cliente, en que producto, y con quien. "
               f"En rojo: lo que hace con la competencia y **no** con {BANCO_PROPIO}.")

    per = tuple(sorted(f["periodos"])[-1:]) if f.get("solo_ultimo") else tuple(f["periodos"])
    base = matriz_competencia(tuple(f["periodos"]), "")
    if base.empty:
        st.info("Sin datos."); return
    if f.get("companias") and len(f["companias"]) < len(f.get("_companias_all", [])):
        base = base[base.rut_compania.isin(f["companias"])]
    if f.get("productos"):
        base = base[base.producto.notna()]

    foco = f["periodo_foco"]
    st.caption(f"Periodos considerados: {min(f['periodos'])} a {max(f['periodos'])}.")

    # Estado de cada par (cliente, producto): con nosotros, solo competencia, o nadie.
    piv = base.pivot_table(index=["aseguradora", "producto"], columns="banco",
                           values="nocional_m", aggfunc="sum", fill_value=0)
    if BANCO_PROPIO not in piv.columns:
        piv[BANCO_PROPIO] = 0.0
    competencia = [c for c in piv.columns if c != BANCO_PROPIO]

    est = pd.DataFrame({
        "nocional_propio": piv[BANCO_PROPIO],
        "nocional_competencia": piv[competencia].sum(axis=1),
        "bancos_competidores": (piv[competencia] > 0).sum(axis=1),
    }).reset_index()
    est["principal_competidor"] = [
        (piv.loc[i, competencia].idxmax() if piv.loc[i, competencia].max() > 0 else None)
        for i in piv.index]
    est["estado"] = est.apply(
        lambda r: (f"Con {BANCO_PROPIO}" if r.nocional_propio > 0
                   else ("OPORTUNIDAD" if r.nocional_competencia > 0 else "Sin actividad")),
        axis=1)
    est["share_propio_%"] = (100 * est.nocional_propio /
                             (est.nocional_propio + est.nocional_competencia).replace(0, pd.NA)).round(1)

    c = st.columns(4)
    c[0].metric("Pares cliente-producto", f"{len(est):,}")
    c[1].metric(f"Con {BANCO_PROPIO}", f"{(est.estado == f'Con {BANCO_PROPIO}').sum():,}")
    c[2].metric("OPORTUNIDADES", f"{(est.estado == 'OPORTUNIDAD').sum():,}")
    c[3].metric("Nocional en manos de la competencia (M$)",
                f"{est.loc[est.estado == 'OPORTUNIDAD', 'nocional_competencia'].sum():,.0f}")

    # Mapa de calor: -1 oportunidad (rojo), 0 sin actividad, +1 con nosotros (verde).
    codigo = est.assign(v=est.estado.map({f"Con {BANCO_PROPIO}": 1, "OPORTUNIDAD": -1,
                                          "Sin actividad": 0}))
    m = codigo.pivot_table(index="aseguradora", columns="producto", values="v",
                           aggfunc="min", fill_value=0)
    fig = px.imshow(m, aspect="auto", color_continuous_scale=[
        (0.0, "#c62828"), (0.5, "#eceff1"), (1.0, "#2e7d32")],
        zmin=-1, zmax=1, labels=dict(x="Producto", y="Aseguradora", color="Estado"))
    fig.update_layout(height=max(360, 28 * len(m) + 160), margin=dict(l=8, r=8, t=30, b=8),
                      coloraxis_colorbar=dict(
                          tickvals=[-1, 0, 1],
                          ticktext=["Solo competencia", "Sin actividad", f"Con {BANCO_PROPIO}"]))
    st.plotly_chart(fig, width="stretch")

    st.markdown(f"**Oportunidades ordenadas por tamano** -- el cliente ya opera el producto, "
                f"pero no con {BANCO_PROPIO}")
    opo = (est[est.estado == "OPORTUNIDAD"]
           .sort_values("nocional_competencia", ascending=False)
           [["aseguradora", "producto", "nocional_competencia", "bancos_competidores",
             "principal_competidor"]])
    st.dataframe(opo, width="stretch", hide_index=True,
                 column_config={"nocional_competencia": st.column_config.NumberColumn(
                     "Nocional competencia (M$)", format="%.0f")})
    st.download_button("Descargar oportunidades (CSV)",
                       opo.to_csv(index=False).encode("utf-8"),
                       "whitespace_oportunidades.csv", "text/csv")

    with st.expander(f"Donde {BANCO_PROPIO} ya esta pero con share bajo"):
        bajo = (est[(est.estado == f"Con {BANCO_PROPIO}") & (est["share_propio_%"] < 50)]
                .sort_values("nocional_competencia", ascending=False))
        st.dataframe(bajo, width="stretch", hide_index=True)


def vista_oportunidades(f: dict) -> None:
    st.subheader("Opportunity Finder")
    st.caption("Alertas automaticas sobre la cartera. Cada una nombra al cliente, "
               "el producto, el banco incumbente y el monto en juego.")

    cp = _col_producto()
    foco = f["periodo_foco"]
    horizonte = st.slider("Horizonte de vencimientos (meses)", 1, 24, 3, 1)
    minimo = st.number_input("Monto minimo para levantar alerta (M$)",
                             0, 10_000_000_000, 1_000_000, 100_000)

    w = predicados_derivado(f, periodos=[foco])
    base = q(f"""
        SELECT {nombre_compania_sql()} AS aseguradora, d.rut_compania,
               d.{cp} AS producto, d.contraparte_grupo AS banco,
               d.folio_operacion, d.fecha_vencimiento, d.moneda,
               COALESCE(d.nocional_m,0) AS nocional_m,
               d.tasa_precio_contrato, d.tasa_precio_mercado,
               d.rol_tasa_fija, d.par_monedas, d.tasa_pacto,
               LAST_DAY(STRPTIME(CAST(d.periodo_informacion AS VARCHAR)||'01','%Y%m%d')) AS cierre
        FROM fact_derivado d {JOIN_COMP} {w}
    """)
    if base.empty:
        st.info("Sin datos para el periodo foco."); return

    alertas: list[dict] = []

    # --- 1. Muro de vencimientos con un competidor -> refinanciamiento -------
    venc = base[base.fecha_vencimiento.notna()].copy()
    venc["dias"] = (pd.to_datetime(venc.fecha_vencimiento) -
                    pd.to_datetime(venc.cierre)).dt.days
    prox = venc[(venc.dias >= 0) & (venc.dias <= horizonte * 31)]
    g = (prox.groupby(["aseguradora", "producto", "banco"])
              .agg(nocional=("nocional_m", "sum"), ops=("folio_operacion", "nunique"),
                   primero=("fecha_vencimiento", "min"), ultimo=("fecha_vencimiento", "max"))
              .reset_index())
    for r in g[(g.nocional >= minimo) & (g.banco != BANCO_PROPIO)].itertuples():
        alertas.append({
            "prioridad": r.nocional,
            "tipo": "Refinanciamiento",
            "cliente": r.aseguradora, "producto": r.producto, "banco_incumbente": r.banco,
            "monto_m": r.nocional, "operaciones": r.ops,
            "detalle": (f"{r.ops} operaciones de {r.producto} por {r.nocional:,.0f} M$ "
                        f"vencen con {r.banco} entre {r.primero} y {r.ultimo}. "
                        f"Ventana para ofrecer el refinanciamiento antes del roll."),
        })

    # --- 2. Whitespace de producto ------------------------------------------
    mat = matriz_competencia(tuple(f["periodos"]), "")
    if not mat.empty:
        piv = mat.pivot_table(index=["aseguradora", "producto"], columns="banco",
                              values="nocional_m", aggfunc="sum", fill_value=0)
        if BANCO_PROPIO not in piv.columns:
            piv[BANCO_PROPIO] = 0.0
        comp = [c for c in piv.columns if c != BANCO_PROPIO]
        for idx, fila in piv.iterrows():
            propio, otros = fila[BANCO_PROPIO], fila[comp].sum()
            if propio == 0 and otros >= minimo:
                lider = fila[comp].idxmax()
                alertas.append({
                    "prioridad": otros, "tipo": "Whitespace",
                    "cliente": idx[0], "producto": idx[1], "banco_incumbente": lider,
                    "monto_m": otros, "operaciones": None,
                    "detalle": (f"{idx[0]} opera {idx[1]} por {otros:,.0f} M$ con "
                                f"{int((fila[comp] > 0).sum())} banco(s), liderados por {lider}. "
                                f"{BANCO_PROPIO} no tiene una sola operacion de este producto "
                                f"con este cliente."),
                })

    # --- 3. Concentracion de contraparte -> pitch de diversificacion --------
    tot = base.groupby("aseguradora").nocional_m.sum()
    porbanco = base.groupby(["aseguradora", "banco"]).nocional_m.sum().reset_index()
    porbanco["share"] = porbanco.apply(
        lambda r: r.nocional_m / tot[r.aseguradora] if tot[r.aseguradora] else 0, axis=1)
    for r in porbanco[(porbanco.share >= 0.40) & (porbanco.banco != BANCO_PROPIO)
                      & (porbanco.nocional_m >= minimo)].itertuples():
        alertas.append({
            "prioridad": r.nocional_m * r.share, "tipo": "Concentracion",
            "cliente": r.aseguradora, "producto": "(todos)", "banco_incumbente": r.banco,
            "monto_m": r.nocional_m, "operaciones": None,
            "detalle": (f"{r.share:.0%} del libro de derivados de {r.aseguradora} esta "
                        f"con {r.banco} ({r.nocional_m:,.0f} M$). Argumento de "
                        f"diversificacion de riesgo de contraparte."),
        })

    # --- 4. Fondeo via pactos con la competencia ----------------------------
    pactos = base[(base.producto.astype(str).str.contains("PACTO", na=False))
                  & (base.banco != BANCO_PROPIO)]
    if not pactos.empty:
        gp = (pactos.groupby(["aseguradora", "banco"])
                    .agg(monto=("nocional_m", "sum"), tasa=("tasa_pacto", "median"),
                         ops=("folio_operacion", "nunique")).reset_index())
        for r in gp[gp.monto >= minimo].itertuples():
            tasa = f"{r.tasa:.2f}%" if pd.notna(r.tasa) else "sin tasa informada"
            alertas.append({
                "prioridad": r.monto, "tipo": "Fondeo",
                "cliente": r.aseguradora, "producto": "PACTO", "banco_incumbente": r.banco,
                "monto_m": r.monto, "operaciones": r.ops,
                "detalle": (f"{r.aseguradora} se fondea con {r.banco} via pactos por "
                            f"{r.monto:,.0f} M$ a una tasa mediana de {tasa}. "
                            f"Comparar contra nuestra curva de fondeo."),
            })

    # --- 5. Gap de precio contra la mediana del mercado ---------------------
    px_ = base[base.tasa_precio_contrato.notna()]
    if not px_.empty:
        med = px_.groupby("producto").tasa_precio_contrato.median()
        gp = (px_.groupby(["aseguradora", "producto", "banco"])
                 .agg(tasa=("tasa_precio_contrato", "median"),
                      monto=("nocional_m", "sum")).reset_index())
        gp["mediana_mercado"] = gp["producto"].map(med)
        gp["gap"] = gp.tasa - gp.mediana_mercado
        for r in gp[(gp.monto >= minimo) & (gp.banco != BANCO_PROPIO)
                    & (gp.gap.abs() > 0.25)].itertuples():
            alertas.append({
                "prioridad": r.monto * abs(r.gap), "tipo": "Precio",
                "cliente": r.aseguradora, "producto": r.producto,
                "banco_incumbente": r.banco, "monto_m": r.monto, "operaciones": None,
                "detalle": (f"{r.aseguradora} paga {r.tasa:.3f} en {r.producto} con "
                            f"{r.banco}, contra una mediana de mercado de "
                            f"{r.mediana_mercado:.3f} ({r.gap:+.3f}). "
                            f"Espacio para mejorar el precio."),
            })

    if not alertas:
        st.success("Sin alertas sobre el umbral elegido. Baja el monto minimo para ver mas.")
        return

    al = pd.DataFrame(alertas).sort_values("prioridad", ascending=False)
    tipos = st.multiselect("Tipo de alerta", sorted(al.tipo.unique()),
                           default=sorted(al.tipo.unique()))
    al = al[al.tipo.isin(tipos)]

    c = st.columns(len(tipos) + 1 if tipos else 1)
    c[0].metric("Alertas", f"{len(al):,}")
    for i, t in enumerate(tipos, start=1):
        if i < len(c):
            c[i].metric(t, f"{(al.tipo == t).sum():,}")

    st.markdown("**Top 15**")
    for r in al.head(15).itertuples():
        color = {"Refinanciamiento": "🔴", "Whitespace": "🟠", "Concentracion": "🟡",
                 "Fondeo": "🔵", "Precio": "🟣"}.get(r.tipo, "⚪")
        with st.container(border=True):
            st.markdown(f"{color} **{r.tipo} · {r.cliente} · {r.producto}** "
                        f"— incumbente **{r.banco_incumbente}** — **{r.monto_m:,.0f} M$**")
            st.caption(r.detalle)

    st.markdown("**Todas las alertas**")
    st.dataframe(al.drop(columns=["prioridad"]), width="stretch", hide_index=True,
                 column_config={"monto_m": st.column_config.NumberColumn(
                     "Monto (M$)", format="%.0f")})
    st.download_button("Descargar alertas (CSV)", al.to_csv(index=False).encode("utf-8"),
                       f"oportunidades_{foco}.csv", "text/csv")

    # --- segmentacion -------------------------------------------------------
    st.divider()
    st.markdown("**Segmentacion de clientes**")
    st.caption("Reglas explicitas sobre tres ejes, no un modelo opaco: en una mesa "
               "hay que poder defender por que un cliente cayo en un segmento.")
    seg = (base.groupby("aseguradora")
               .agg(nocional=("nocional_m", "sum"),
                    productos=("producto", "nunique"),
                    bancos=("banco", "nunique"),
                    operaciones=("folio_operacion", "nunique")).reset_index())
    if len(seg) >= 3:
        seg["tamano"] = pd.qcut(seg.nocional.rank(method="first"), 3,
                                labels=["Chico", "Mediano", "Grande"])
    else:
        seg["tamano"] = "Unico"
    seg["sofisticacion"] = seg.productos.map(lambda n: "Simple" if n <= 1 else
                                             ("Media" if n <= 3 else "Sofisticada"))
    seg["apertura"] = seg.bancos.map(lambda n: "Cautivo" if n <= 2 else
                                     ("Selectivo" if n <= 5 else "Multibanco"))
    propio = (base[base.banco == BANCO_PROPIO].groupby("aseguradora").nocional_m.sum())
    seg["share_propio_%"] = (100 * seg.aseguradora.map(propio).fillna(0)
                             / seg.nocional.replace(0, pd.NA)).round(1).fillna(0)
    seg["segmento"] = seg.tamano.astype(str) + " / " + seg.sofisticacion + " / " + seg.apertura
    st.dataframe(seg.sort_values("nocional", ascending=False), width="stretch",
                 hide_index=True,
                 column_config={"nocional": st.column_config.NumberColumn(
                     "Nocional (M$)", format="%.0f")})
    st.plotly_chart(
        px.scatter(seg, x="bancos", y="nocional", size="operaciones", color="sofisticacion",
                   hover_name="aseguradora", log_y=True,
                   hover_data=["productos", "share_propio_%", "segmento"],
                   labels={"bancos": "Bancos con los que opera",
                           "nocional": "Nocional total (M$, log)"})
          .update_layout(height=430, margin=dict(t=30)),
        width="stretch")


def vista_explorador(f: dict) -> None:
    st.subheader("Explorador libre")
    st.caption("La sabana completa con los filtros del sidebar aplicados, mas busqueda "
               "por texto y por anio. Todas las columnas del warehouse, crudas y calculadas.")

    TABLAS = {
        "Derivados": "fact_derivado",
        "Renta fija local (B.1)": "fact_renta_fija",
        "Renta fija EXTRANJERA (B.5)": "fact_extranjero_rf",
        "Renta variable extranjera (B.5)": "fact_extranjero_rv",
        "Equity y fondos de inversion (B.2)": "fact_equity",
        "Fondos mutuos (B.3)": "fact_fondo",
        "Otras inversiones (B.6)": "fact_otras_inv",
        "Control / totales (B.8)": "fact_control",
        "Garantias (B.14)": "fact_garantia",
        "Flujos": "fact_flujo",
        "Cuarentena": "fact_cuarentena",
    }
    c1, c2, c3 = st.columns([2, 2, 1])
    fuente = c1.selectbox("Tabla", [k for k, v in TABLAS.items() if existe(v)])
    tabla = TABLAS[fuente]
    cols_tabla = columnas(tabla)

    anios = sorted({int(str(p)[:4]) for p in f["periodos"]})
    sel_anios = c2.multiselect("Anio", anios, default=anios)
    limite = c3.number_input("Max filas", 1_000, 2_000_000, 50_000, 1_000)

    texto = st.text_input("Busqueda libre",
                          placeholder="nemotecnico, folio, contraparte, custodio, moneda...",
                          help="Busca el texto en todas las columnas de texto de la tabla, "
                               "sin distinguir mayusculas.")

    col_per = "periodo" if tabla == "fact_flujo" else "periodo_informacion"
    pers = [p for p in f["periodos"] if int(str(p)[:4]) in sel_anios] or f["periodos"]
    partes = [f"d.{col_per} IN ({', '.join(str(p) for p in pers)})" if pers else "1=0"]

    # Los filtros del sidebar que apliquen a esta tabla.
    if tabla == "fact_derivado":
        extra = predicados_derivado(f, periodos=pers)
    elif tabla == "fact_renta_fija":
        extra = predicados_rf(f)
    elif tabla == "fact_garantia":
        extra = predicados_garantia(f)
    else:
        extra = where(partes)
    if tabla in ("fact_equity", "fact_fondo", "fact_extranjero_rf",
                 "fact_extranjero_rv", "fact_otras_inv", "fact_control"):
        extra = where(partes + [cl_in(tabla, "rut_compania", f.get("companias"),
                                      f.get("_companias_all"))])

    if texto.strip():
        txt = texto.strip().replace("'", "''")
        # Se castea a VARCHAR y se compara en minusculas: asi la busqueda tambien
        # matchea un folio o un RUT, que estan guardados como numero.
        ors = " OR ".join(f"lower(CAST(d.{c} AS VARCHAR)) LIKE '%{txt.lower()}%'"
                          for c in sorted(cols_tabla))
        extra = (extra + (" AND " if extra else " WHERE ") + f"({ors})")

    calc = ""
    if tabla == "fact_derivado":
        calc = f""", {nombre_compania_sql()} AS aseguradora_nombre,
            COALESCE(d.mtm_contrato_m, COALESCE(d.mtm_activo_m,0)-COALESCE(d.mtm_pasivo_m,0)) AS mtm_neto_m,
            DATE_DIFF('day', LAST_DAY(STRPTIME(CAST(d.periodo_informacion AS VARCHAR)||'01','%Y%m%d')),
                      d.fecha_vencimiento) AS plazo_dias,
            d.tasa_precio_contrato - d.tasa_precio_mercado AS spread_vs_mercado"""
        join = JOIN_COMP
    elif tabla == "fact_renta_fija":
        calc = f""", {nombre_compania_sql()} AS aseguradora_nombre,
            DATE_DIFF('day', LAST_DAY(STRPTIME(CAST(d.periodo_informacion AS VARCHAR)||'01','%Y%m%d')),
                      d.fecha_vencimiento) AS plazo_dias,
            CASE WHEN d.valor_nominal > 0 THEN d.valor_nominal_vigente / d.valor_nominal END
                AS razon_vigente_nominal,
            d.tir_mercado - d.tir_compra AS delta_tir"""
        join = JOIN_COMP
    elif tabla in ("fact_equity", "fact_fondo", "fact_garantia", "fact_extranjero_rf",
                   "fact_extranjero_rv", "fact_otras_inv", "fact_control"):
        calc = f", {nombre_compania_sql()} AS aseguradora_nombre"
        join = JOIN_COMP
    else:
        join = ""

    sql = f"SELECT d.*{calc} FROM {tabla} d {join} {extra} LIMIT {limite}"
    df = q(sql)
    st.caption(f"{len(df):,} filas x {len(df.columns)} columnas"
               + (f"  ·  tope de {limite:,} alcanzado" if len(df) >= limite else ""))
    if df.empty:
        st.info("Sin filas para esos filtros."); return

    with st.expander("Elegir columnas", expanded=False):
        pre = st.radio("Preajuste", ["Todas", "Comerciales", "Numericas"], horizontal=True)
        if pre == "Comerciales":
            pref = [c for c in df.columns if any(k in c for k in (
                "aseguradora", "contraparte", "producto", "subtipo", "nemotecnico", "folio",
                "moneda", "nocional", "mtm", "tasa", "tir", "fecha", "plazo", "clasificacion",
                "valor_final", "duracion", "rol_tasa"))]
        elif pre == "Numericas":
            pref = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        else:
            pref = list(df.columns)
        cols = st.multiselect("Columnas", list(df.columns), default=pref or list(df.columns))

    vista = df[cols] if cols else df
    orden = st.selectbox("Ordenar por", ["(sin orden)"] + list(vista.columns))
    if orden != "(sin orden)":
        asc = st.checkbox("Ascendente", value=False)
        vista = vista.sort_values(orden, ascending=asc, na_position="last")

    st.dataframe(vista, width="stretch", hide_index=True, height=560)
    st.download_button("Descargar CSV", vista.to_csv(index=False).encode("utf-8"),
                       f"cmf1835_{tabla}.csv", "text/csv")
    with st.expander("SQL ejecutado"):
        st.code(sql, language="sql")


def anterior(periodo: int) -> int:
    a, m = divmod(int(periodo), 100)
    return (a - 1) * 100 + 12 if m == 1 else a * 100 + (m - 1)


# ---------------------------------------------------------------------------
#  main
# ---------------------------------------------------------------------------

def main() -> None:
    conectar()
    st.title("Mesa de Dinero -- Carteras de aseguradoras (Circular 1835 CMF)")
    if DB == DB_SAMPLE:
        st.info("Leyendo la **muestra** de despliegue (ultimos meses). "
                "Para la serie completa, genera el warehouse local con "
                "`python -m warehouse.loader --data <carpeta de ZIP>`.")

    f = construir_sidebar()
    if not f["periodos"]:
        st.warning("Elige al menos un periodo."); st.stop()

    w = predicados_derivado(f)
    kpi = q(f"""SELECT COUNT(*) ops, COUNT(DISTINCT folio_operacion) folios,
                       SUM(COALESCE(nocional_m,0)) nocional,
                       SUM(COALESCE(mtm_activo_m,0)-COALESCE(mtm_pasivo_m,0)) mtm,
                       COUNT(DISTINCT contraparte_grupo) grupos,
                       COUNT(DISTINCT rut_compania) cias
                FROM fact_derivado d {w}""")
    k = st.columns(6)
    k[0].metric("Operaciones", f"{int(kpi.ops[0]):,}")
    k[1].metric("Folios", f"{int(kpi.folios[0]):,}")
    k[2].metric("Nocional (M$)", f"{float(kpi.nocional[0] or 0):,.0f}")
    k[3].metric("MTM neto (M$)", f"{float(kpi.mtm[0] or 0):,.0f}")
    k[4].metric("Grupos contraparte", f"{int(kpi.grupos[0] or 0):,}")
    k[5].metric("Aseguradoras", f"{int(kpi.cias[0] or 0):,}")

    tabs = st.tabs([
        "Oportunidades", "Whitespace 2.0", "Whitespace Map", "Roll-Off Calendar",
        "Price Discovery", "Flujos mensuales", "Garantias", "Explorador libre"])
    with tabs[0]: vista_oportunidades(f)
    with tabs[1]: vista_whitespace2(f)
    with tabs[2]: vista_whitespace(f)
    with tabs[3]: vista_rolloff(f)
    with tabs[4]: vista_price_discovery(f)
    with tabs[5]: vista_flujos(f)
    with tabs[6]: vista_garantias(f)
    with tabs[7]: vista_explorador(f)


if __name__ == "__main__":
    main()
