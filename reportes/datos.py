"""
reportes.datos
==============

Capa de datos del reporte Excel: todo lo que se calcula, sin nada de formato.

Reglas que aplica y que el dashboard NO aplica
----------------------------------------------
1. Publicacion vigente en TODAS las tablas. En el warehouse solo
   fact_derivado y fact_renta_fija filtran la ultima publicacion de cada
   periodo; las demas vistas suman todas. En 202608 hay dos publicaciones y
   eso cuenta dos veces, por ejemplo, el B.8 (+41%). Aqui cada consulta se
   filtra con publicacion_vigente.recencia = 1.
2. Todo en MM USD, cada periodo a su propio dolar observado de cierre
   (utils.fetch_usd). Los montos fuente estan en M$, asi que
       MM USD = M$ / dolar / 1.000
3. El leasing inmobiliario (CLEAS) se cuenta UNA vez, en Real Estate y con la
   valorizacion del B.4. Aparece tambien en el B.1 como contrato; sumar ambos
   no cuadra contra el total que declara cada aseguradora (+6,25% agregado),
   contarlo una vez si (-1,15%).
4. Contrapartes por identificador legal (reportes.contrapartes), no por el
   grupo del catalogo.
5. Pactos fuera del total de derivados: son financiamiento, no derivados.

Nada aqui escribe en el warehouse: la conexion es de solo lectura.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from reportes.contrapartes import identificar, resumen_calidad
from utils.fetch_usd import leer as leer_usd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "warehouse" / "data" / "cmf1835.duckdb"

#: Cierres historicos contra los que se compara el periodo actual.
CIERRES = {"Dic-2023": 202312, "Dic-2024": 202412, "Dic-2025": 202512}

#: Orden en que se muestran las clases de activo.
CLASES = ["Renta Fija", "Equity", "ETF", "Fondos de Inversion", "Fondos Mutuos",
          "Real Estate", "Otros", "Derivados (valor razonable neto)", "Sin clasificar"]

#: Familias de derivado con hoja propia, en orden de presentacion.
FAMILIAS = ["CCS", "Swap Promesa", "Forward FX", "Forward UF", "IRS", "Opcion", "Futuro"]

_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def etiqueta_periodo(p: int) -> str:
    """202608 -> 'Ago-2026'."""
    return f"{_MESES[p % 100 - 1].capitalize()}-{p // 100}"


def nombre_aseguradora(nombre: str | None, rut: int) -> str:
    """Nombre publicado, limpio de artefactos de captura.

    Dos artefactos reales de la fuente: el prefijo numerico que antepone un
    informante ('033 METLIFE ...') y el '#' que otros escriben en vez de la
    enie ('COMPA#IA'). Se corrigen solo para mostrar; el RUT va al lado.
    """
    if not nombre:
        return str(rut)
    n = re.sub(r"^\d+\s+", "", str(nombre)).replace("#", "Ñ")
    return " ".join(n.split())


# ---------------------------------------------------------------------------
#  contexto
# ---------------------------------------------------------------------------

@dataclass
class Contexto:
    con: duckdb.DuckDBPyConnection
    periodo: int
    fecha_cierre: _dt.date
    zip_vigente: str
    fecha_publicacion: str
    fx: dict[int, tuple[float, str]]
    cierres: dict[str, int | None] = field(default_factory=dict)
    aseguradoras: pd.DataFrame = field(default_factory=pd.DataFrame)

    def a_mmusd(self, m_pesos, periodo: int | None = None):
        """M$ -> MM USD con el dolar de cierre del periodo."""
        return m_pesos / self.fx[periodo or self.periodo][0] / 1000.0


def contexto(periodo: int | None = None, db: Path = DB) -> Contexto:
    con = duckdb.connect(str(db), read_only=True)
    disponibles = {r[0] for r in con.execute("SELECT periodo FROM dim_periodo").fetchall()}
    if periodo is None:
        periodo = max(disponibles)
    if periodo not in disponibles:
        raise ValueError(f"El periodo {periodo} no esta en el warehouse ({min(disponibles)}..{max(disponibles)})")

    fecha_cierre = con.execute("SELECT fecha_cierre FROM dim_periodo WHERE periodo=?",
                               [periodo]).fetchone()[0]
    zip_v, fdesc = con.execute(
        "SELECT zip_origen, fecha_descarga FROM publicacion_vigente "
        "WHERE periodo_informacion=? AND recencia=1", [periodo]).fetchone()

    fx = leer_usd()
    cierres = {k: (p if p in disponibles else None) for k, p in CIERRES.items()}
    faltan = [p for p in [periodo, *[c for c in cierres.values() if c]] if p not in fx]
    if faltan:
        raise ValueError(f"Falta el dolar de cierre para {faltan}: correr python -m utils.fetch_usd")

    aseg = con.execute("SELECT rut_compania, nombre FROM dim_compania").fetch_df()
    aseg["aseguradora"] = [nombre_aseguradora(n, r) for n, r in zip(aseg.nombre, aseg.rut_compania)]
    return Contexto(con, periodo, fecha_cierre, zip_v, str(fdesc), fx, cierres,
                    aseg[["rut_compania", "aseguradora"]])


def _vigente(tabla: str, periodo: int) -> str:
    """FROM de una tabla cruda restringida a la publicacion vigente del periodo."""
    return (f"{tabla} t JOIN publicacion_vigente pv "
            f"ON pv.periodo_informacion = t.periodo_informacion "
            f"AND pv.zip_origen = t.zip_origen AND pv.recencia = 1 "
            f"WHERE t.periodo_informacion = {int(periodo)}")


# ---------------------------------------------------------------------------
#  stock por clase de activo
# ---------------------------------------------------------------------------

def _sql_stock(periodo: int) -> str:
    """Una fila por (aseguradora, clase, fuente) con el valor final en M$.

    El tipo de instrumento se clasifica por prefijo del codigo CMF, y lo que
    no calza con ningun prefijo cae en 'Sin clasificar' en vez de perderse.
    """
    V = lambda t: _vigente(t, periodo)  # noqa: E731
    clase_rv = """CASE
        WHEN t.tipo_instrumento LIKE 'AC%'  THEN 'Equity'
        WHEN t.tipo_instrumento LIKE 'ETF%' THEN 'ETF'
        WHEN t.tipo_instrumento LIKE 'CFM%' THEN 'Fondos Mutuos'
        WHEN t.tipo_instrumento LIKE 'CFI%' THEN 'Fondos de Inversion'
        ELSE 'Sin clasificar' END"""
    return f"""
    SELECT t.rut_compania, 'Renta Fija' clase, 'B.1 renta fija local (sin leasing)' fuente,
           sum(t.valor_final) m FROM {V('raw_renta_fija')}
           AND COALESCE(t.tipo_instrumento,'') <> 'CLEAS' GROUP BY 1,2,3
    UNION ALL
    SELECT t.rut_compania, 'Renta Fija', 'B.5 renta fija extranjera', sum(t.valor_final)
           FROM {V('raw_extranjero_rf')} GROUP BY 1,2,3
    UNION ALL
    SELECT t.rut_compania, {clase_rv}, 'B.2 acciones y cuotas FFII', sum(t.valor_final)
           FROM {V('raw_equity')} GROUP BY 1,2,3
    UNION ALL
    SELECT t.rut_compania, {clase_rv}, 'B.5 renta variable extranjera', sum(t.valor_final)
           FROM {V('raw_extranjero_rv')} GROUP BY 1,2,3
    UNION ALL
    SELECT t.rut_compania, 'Fondos Mutuos', 'B.3 cuotas de fondos mutuos', sum(t.valor_final)
           FROM {V('raw_fondo')} GROUP BY 1,2,3
    UNION ALL
    SELECT t.rut_compania, 'Real Estate',
           CASE WHEN t.tipo_instrumento = 'CLEAS' THEN 'B.4 leasing inmobiliario'
                ELSE 'B.4 bienes raices propios' END,
           sum(t.valor_final) FROM {V('raw_bienes_raices')} GROUP BY 1,2,3
    UNION ALL
    SELECT t.rut_compania, 'Otros', 'B.6 otras inversiones', sum(t.valor_final)
           FROM {V('raw_otras_inv')} GROUP BY 1,2,3
    UNION ALL
    SELECT t.rut_compania, 'Derivados (valor razonable neto)', 'B.7 derivados (activo - pasivo)',
           sum(COALESCE(t.mtm_activo_m,0)) - sum(COALESCE(t.mtm_pasivo_m,0))
           FROM {V('raw_derivado')} AND t.producto <> 'PACTO' GROUP BY 1,2,3
    """


def stock_detalle(ctx: Contexto, periodo: int) -> pd.DataFrame:
    """Long: rut, aseguradora, clase, fuente, MM USD."""
    df = ctx.con.execute(_sql_stock(periodo)).fetch_df()
    df["mm_usd"] = ctx.a_mmusd(df.pop("m").astype(float), periodo)
    df = df.merge(ctx.aseguradoras, on="rut_compania", how="left")
    return df[["rut_compania", "aseguradora", "clase", "fuente", "mm_usd"]]


def declarado_b8(ctx: Contexto, periodo: int) -> pd.Series:
    """Total que declara cada aseguradora en el B.8, en MM USD.

    VALOR_FINAL del B.8 viene nulo en el 64% de las filas: 30 aseguradoras
    escriben un signo '+' en un campo que el layout define sin signo. Se usa
    la identidad representativas + no representativas = valor final, que se
    cumple en 4.623 de 4.623 filas donde el valor final si se lee.
    """
    df = ctx.con.execute(f"""
        SELECT t.rut_compania, sum(COALESCE(t.repr_rt_pr,0) + COALESCE(t.no_repr_rt_pr,0)) m
        FROM {_vigente('raw_control', periodo)} GROUP BY 1""").fetch_df()
    return pd.Series(ctx.a_mmusd(df.m.astype(float).values, periodo), index=df.rut_compania)


def stock_por_clase(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    """Wide: una fila por aseguradora, una columna por clase, total y cuadratura."""
    periodo = periodo or ctx.periodo
    det = stock_detalle(ctx, periodo)
    wide = det.pivot_table(index=["rut_compania", "aseguradora"], columns="clase",
                           values="mm_usd", aggfunc="sum", fill_value=0.0)
    for c in CLASES:
        if c not in wide:
            wide[c] = 0.0
    wide = wide[CLASES]
    leasing = (det[det.fuente == "B.4 leasing inmobiliario"]
               .groupby(["rut_compania", "aseguradora"]).mm_usd.sum())
    wide.insert(wide.columns.get_loc("Real Estate") + 1, "de la cual leasing",
                leasing.reindex(wide.index).fillna(0.0))
    wide["Total"] = wide[CLASES].sum(axis=1)
    b8 = declarado_b8(ctx, periodo)
    wide["Total declarado B.8"] = [b8.get(r, np.nan) for r, _ in wide.index]
    wide["Diferencia vs B.8 (%)"] = np.where(
        wide["Total declarado B.8"].abs() > 0,
        (wide["Total"] / wide["Total declarado B.8"] - 1) * 100, np.nan)
    return wide.reset_index().sort_values("Total", ascending=False)


def comparativa(ctx: Contexto) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(por aseguradora, por aseguradora x clase), actual vs cierres de anio.

    Un cierre que no esta en el warehouse (Dic-2023) queda como columna vacia:
    la estructura no cambia cuando se cargue, y ningun numero se inventa.
    """
    actual = etiqueta_periodo(ctx.periodo)
    cols = [*ctx.cierres.keys(), actual]
    largos = []
    for etq, p in [*ctx.cierres.items(), (actual, ctx.periodo)]:
        if p is None:
            continue
        d = stock_detalle(ctx, p).groupby(["rut_compania", "aseguradora", "clase"],
                                          as_index=False).mm_usd.sum()
        d["columna"] = etq
        largos.append(d)
    largo = pd.concat(largos, ignore_index=True)
    # El nombre se toma de dim_compania, igual para todos los periodos.
    largo = largo.drop(columns="aseguradora").merge(ctx.aseguradoras, on="rut_compania", how="left")

    def armar(idx):
        w = largo.pivot_table(index=idx, columns="columna", values="mm_usd",
                              aggfunc="sum").reindex(columns=cols)
        base = ctx.cierres and list(ctx.cierres.keys())[-1]
        w[f"Var. {base} a {actual} (MM USD)"] = w[actual] - w[base]
        w[f"Var. {base} a {actual} (%)"] = np.where(
            w[base].abs() > 0, (w[actual] / w[base] - 1) * 100, np.nan)
        return w.reset_index()

    por_aseg = armar(["rut_compania", "aseguradora"]).sort_values(actual, ascending=False)
    por_clase = armar(["rut_compania", "aseguradora", "clase"])
    orden = {c: i for i, c in enumerate(CLASES)}
    por_clase["_o"] = por_clase["clase"].map(orden)
    por_clase = (por_clase.merge(por_aseg[["rut_compania", actual]].rename(columns={actual: "_t"}),
                                 on="rut_compania", how="left")
                 .sort_values(["_t", "rut_compania", "_o"], ascending=[False, True, True])
                 .drop(columns=["_o", "_t"]))
    return por_aseg, por_clase


# ---------------------------------------------------------------------------
#  derivados
# ---------------------------------------------------------------------------

def _familia(instrumento: str | None) -> str:
    i = str(instrumento or "")
    if "swap promesa" in i:
        return "Swap Promesa"
    if i.startswith("CCS"):
        return "CCS"
    if i.startswith("Forward FX"):
        return "Forward FX"
    if i == "Forward UF":
        return "Forward UF"
    if i.startswith("IRS"):
        return "IRS"
    if i == "Opcion":
        return "Opcion"
    if i == "Futuro":
        return "Futuro"
    if i == "Pacto":
        return "Pacto"
    return "Otro derivado"


_MON = {"PROM": "USD", "$$": "CLP"}


def _mon(c) -> str:
    c = str(c or "").strip().upper()
    return _MON.get(c, c)


def _par(a, b) -> str:
    """Par de monedas en orden canonico: XXX/CLP, y UF delante de otra divisa.

    Sin esto el mismo subyacente aparece dos veces (USD/CLP y CLP/USD, UF/USD
    y USD/UF) segun cual pata informo cada aseguradora como larga.
    """
    a, b = _mon(a), _mon(b)
    if a == "CLP" and b != "CLP":
        a, b = b, a
    elif b == "UF" and a not in ("UF", "CLP"):
        a, b = b, a
    return f"{a}/{b}"


def _subyacente(r) -> str:
    f = r["familia"]
    if f in ("CCS", "Swap Promesa"):
        par = str(r["instrumento"]).replace("CCS ", "").replace(" (swap promesa)", "")
        return _par(*par.split("/")) if "/" in par else par
    if f in ("Forward FX", "Forward UF"):
        return _par(r["activo_objeto_largo"], r["activo_objeto_corto"])
    if f == "IRS":
        return f"Tasa {_mon(r['moneda'])} ({r['indice_flotante']})"
    if f == "Opcion":
        return "Equity"
    if f == "Futuro":
        return "Futuro de tasa" if r["subyacente_contrato"] == "TASA_O_INFLACION" else "Futuro"
    return "Otro"


def derivados(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    """Operaciones del B.7 del periodo, con familia, subyacente, entidad legal
    y montos en MM USD. Incluye pactos (familia 'Pacto') para su hoja propia."""
    periodo = periodo or ctx.periodo
    # fact_derivado ya filtra la publicacion vigente; la vista clasificada se
    # construye sobre el, asi que hereda el filtro.
    df = ctx.con.execute(
        f"SELECT * FROM v_derivado_clasificado WHERE periodo_informacion = {int(periodo)}"
    ).fetch_df()
    df = identificar(df)
    df = df.merge(ctx.aseguradoras, on="rut_compania", how="left")
    df["familia"] = df["instrumento"].map(_familia)
    df["subyacente"] = df.apply(_subyacente, axis=1)
    # Un forward UF contra pesos es un forward de inflacion aunque la
    # aseguradora lo informe con subyacente "moneda extranjera": va a la hoja
    # de Forward UF, donde aplica la inflacion implicita. La etiqueta original
    # queda en `instrumento`.
    df.loc[(df.familia == "Forward FX") & (df.subyacente == "UF/CLP"), "familia"] = "Forward UF"
    for src, dst in (("nocional_m", "nocional_mmusd"), ("mtm_neto_m", "mtm_mmusd"),
                     ("valor_presente_largo_m", "vp_largo_mmusd"),
                     ("valor_presente_corto_m", "vp_corto_mmusd"),
                     ("monto_subyacente_m", "monto_subyacente_mmusd")):
        df[dst] = ctx.a_mmusd(pd.to_numeric(df[src], errors="coerce"), periodo)
    df["plazo_residual_dias"] = (pd.to_datetime(df["fecha_vencimiento"])
                                 - pd.Timestamp(ctx.fecha_cierre)).dt.days
    return df


def _tabla_nocional(df: pd.DataFrame, por: list[str]) -> pd.DataFrame:
    """Nocional por familia (wide) + total, operaciones y MTM neto."""
    d = df[df.familia != "Pacto"]
    w = d.pivot_table(index=por, columns="familia", values="nocional_mmusd",
                      aggfunc="sum", fill_value=0.0)
    fams = [f for f in FAMILIAS if f in w.columns] + [c for c in w.columns if c not in FAMILIAS]
    w = w.reindex(columns=fams, fill_value=0.0)
    w.insert(0, "Nocional total derivados (MM USD)", w[fams].sum(axis=1))
    g = d.groupby(por)
    w.insert(1, "Operaciones", g.size())
    w["MTM neto (MM USD)"] = g.mtm_mmusd.sum()
    pactos = df[df.familia == "Pacto"].groupby(por).nocional_mmusd.sum()
    w["Pactos, fuera del total (MM USD)"] = pactos.reindex(w.index).fillna(0.0)
    return w.rename(columns={f: f"{f} (MM USD)" for f in fams}).reset_index()


def deriv_por_aseguradora(df: pd.DataFrame) -> pd.DataFrame:
    t = _tabla_nocional(df, ["rut_compania", "aseguradora"])
    return t.sort_values("Nocional total derivados (MM USD)", ascending=False)


def deriv_por_contraparte(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df.familia != "Pacto"]
    t = _tabla_nocional(d, ["entidad_id"])
    meta = (d.groupby("entidad_id")
            .agg(entidad_tipo_id=("entidad_tipo_id", "first"), entidad_nombre=("entidad_nombre", "first"),
                 entidad_pais=("entidad_pais", "first"), entidad_fuente_nombre=("entidad_fuente_nombre", "first"),
                 entidad_alerta=("entidad_alerta", "first"), aseguradoras=("rut_compania", "nunique"))
            .reset_index())
    t = meta.merge(t, on="entidad_id")
    return t.sort_values("Nocional total derivados (MM USD)", ascending=False)


def deriv_aseg_x_contraparte(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df.familia != "Pacto"]
    t = _tabla_nocional(d, ["rut_compania", "aseguradora", "entidad_id"])
    nom = d.groupby("entidad_id").entidad_nombre.first()
    t.insert(3, "entidad_nombre", t.entidad_id.map(nom))
    return t.sort_values(["aseguradora", "Nocional total derivados (MM USD)"], ascending=[True, False])


def deriv_por_subyacente(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df.familia != "Pacto"]
    g = d.groupby(["familia", "subyacente"])
    t = pd.DataFrame({
        "Nocional (MM USD)": g.nocional_mmusd.sum(),
        "Operaciones": g.size(),
        "Aseguradoras": g.rut_compania.nunique(),
        "Contrapartes": g.entidad_id.nunique(),
        "MTM neto (MM USD)": g.mtm_mmusd.sum(),
    }).reset_index()
    orden = {f: i for i, f in enumerate(FAMILIAS)}
    return (t.assign(_o=t.familia.map(orden))
             .sort_values(["_o", "Nocional (MM USD)"], ascending=[True, False]).drop(columns="_o"))


def calidad_contrapartes(df: pd.DataFrame) -> pd.DataFrame:
    return resumen_calidad(df[df.familia != "Pacto"])


# ---------------------------------------------------------------------------
#  metricas propias de cada instrumento
# ---------------------------------------------------------------------------

def breakeven_promesa(r) -> float:
    """Inflacion implicita de un swap promesa fijo-fijo UF contra CLP.

    (1 + tasa CLP) / (1 + tasa UF) - 1, con la tasa de cada pata identificada
    por su moneda. Solo tiene sentido si ambas patas son fijas y son UF y CLP.
    """
    if r.get("rol_tasa_fija") != "FIJA_CONTRA_FIJA":
        return np.nan
    patas = {_mon(r.get("m_larga")): r.get("pata_larga_tasa"), _mon(r.get("m_corta")): r.get("pata_corta_tasa")}
    if set(patas) != {"UF", "CLP"} or any(pd.isna(v) for v in patas.values()):
        return np.nan
    # Una pata en cero es una pata PLANA, estructura habitual del swap promesa:
    # "pesos 0% contra UF -2,8%" da un breakeven de 2,88%, y "UF 0% contra pesos
    # 2,99%" uno de 2,99%. Solo AMBAS en cero es un contrato no informado (hay
    # uno asi): ahi el breakeven daria 0% exacto, un numero falso con cara de dato.
    if all(v == 0 for v in patas.values()):
        return np.nan
    return ((1 + patas["CLP"] / 100) / (1 + patas["UF"] / 100) - 1) * 100


def inflacion_implicita_fwd_uf(r) -> float:
    """Inflacion anual implicita en el precio de mercado de un forward UF.

    (precio forward de mercado / UF de cierre) ^ (365 / dias al vencimiento) - 1.
    Usa solo lo que informa la propia aseguradora y la UF oficial del cierre.
    """
    f, s, d = r.get("tasa_precio_mercado"), r.get("precio_spot"), r.get("plazo_residual_dias")
    if not f or not s or pd.isna(d) or d <= 0:
        return np.nan
    return ((f / s) ** (365.0 / d) - 1) * 100


def direccion_fwd(r) -> str:
    """Compra o venta de la divisa, leida de los activos objeto informados."""
    a, b = _mon(r.get("activo_objeto_largo")), _mon(r.get("activo_objeto_corto"))
    if b == "CLP" and a != "CLP":
        return f"Compra {a}"
    if a == "CLP" and b != "CLP":
        return f"Venta {b}"
    return f"{a} contra {b}"
