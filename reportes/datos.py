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


def _clasificar(df: pd.DataFrame) -> pd.DataFrame:
    """Familia y subyacente de cada operacion. Unica fuente de la regla, para
    que la foto del periodo y la historia de camadas clasifiquen igual."""
    df["familia"] = df["instrumento"].map(_familia)
    df["subyacente"] = df.apply(_subyacente, axis=1)
    # Un forward UF contra pesos es un forward de inflacion aunque la
    # aseguradora lo informe con subyacente "moneda extranjera": va con los
    # Forward UF, donde aplica la inflacion implicita. La etiqueta original
    # queda en `instrumento`.
    df.loc[(df.familia == "Forward FX") & (df.subyacente == "UF/CLP"), "familia"] = "Forward UF"
    return df


def _nombres_grupo() -> dict[str, str]:
    """Clave de grupo del catalogo -> nombre para mostrar (config/entities.yaml)."""
    import yaml
    ents = yaml.safe_load(open(ROOT / "config" / "entities.yaml", encoding="utf-8"))
    return {e["key"]: e["name"] for e in (ents.get("entities") or ents)}


def grupo_legible(claves: pd.Series) -> pd.Series:
    """Grupo economico del catalogo, con su nombre. Es la unica fuente de
    consolidacion: la identidad legal (RUT o LEI) sigue en su propia columna."""
    nombres = _nombres_grupo()
    return claves.map(lambda k: "Sin grupo (no resuelta)"
                      if k is None or (isinstance(k, float) and np.isnan(k)) or str(k).startswith("UNRESOLVED")
                      else nombres.get(k, k))


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
    df = _clasificar(df)
    df["grupo_contraparte"] = grupo_legible(df["contraparte_grupo"])
    for src, dst in (("nocional_m", "nocional_mmusd"), ("mtm_neto_m", "mtm_mmusd"),
                     ("valor_presente_largo_m", "vp_largo_mmusd"),
                     ("valor_presente_corto_m", "vp_corto_mmusd"),
                     ("monto_subyacente_m", "monto_subyacente_mmusd")):
        df[dst] = ctx.a_mmusd(pd.to_numeric(df[src], errors="coerce"), periodo)
    df["plazo_residual_dias"] = (pd.to_datetime(df["fecha_vencimiento"])
                                 - pd.Timestamp(ctx.fecha_cierre)).dt.days
    df["plazo_original_dias"] = (pd.to_datetime(df["fecha_vencimiento"])
                                 - pd.to_datetime(df["fecha_operacion"])).dt.days
    df["tasa_metrica"] = metrica_tasa(df)
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
                 grupo_contraparte=("grupo_contraparte", "first"),
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


# ---------------------------------------------------------------------------
#  evolucion mensual
# ---------------------------------------------------------------------------

def periodos_disponibles(ctx: Contexto) -> list[int]:
    """Todos los periodos del warehouse hasta el actual, en orden."""
    return [r[0] for r in ctx.con.execute(
        f"SELECT periodo FROM dim_periodo WHERE periodo <= {ctx.periodo} ORDER BY 1").fetchall()]


def evolucion_stock(ctx: Contexto) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(por aseguradora, por aseguradora x clase): una columna por mes.

    Cada mes se convierte con el dolar de SU cierre, igual que la comparativa.
    """
    pers = periodos_disponibles(ctx)
    faltan = [p for p in pers if p not in ctx.fx]
    if faltan:
        raise ValueError(f"Falta el dolar de cierre para {faltan}")
    largo = pd.concat([stock_detalle(ctx, p).assign(columna=etiqueta_periodo(p)) for p in pers],
                      ignore_index=True)
    largo = largo.drop(columns="aseguradora").merge(ctx.aseguradoras, on="rut_compania", how="left")
    cols = [etiqueta_periodo(p) for p in pers]
    actual = cols[-1]

    def armar(idx):
        return (largo.pivot_table(index=idx, columns="columna", values="mm_usd", aggfunc="sum")
                .reindex(columns=cols).reset_index())

    por_aseg = armar(["rut_compania", "aseguradora"]).sort_values(actual, ascending=False)
    por_clase = armar(["rut_compania", "aseguradora", "clase"])
    orden = {c: i for i, c in enumerate(CLASES)}
    por_clase = (por_clase.assign(_o=por_clase.clase.map(orden))
                 .merge(por_aseg[["rut_compania", actual]].rename(columns={actual: "_t"}),
                        on="rut_compania", how="left")
                 .sort_values(["_t", "rut_compania", "_o"], ascending=[False, True, True])
                 .drop(columns=["_o", "_t"]))
    return por_aseg, por_clase


def evolucion_derivados(ctx: Contexto) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(por aseguradora, por contraparte legal): nocional mensual sin pactos.

    La identidad legal se calcula sobre TODOS los meses a la vez, para que una
    misma entidad tenga el mismo nombre en toda la serie.
    """
    pers = periodos_disponibles(ctx)
    df = ctx.con.execute(
        f"SELECT * FROM v_derivado_clasificado WHERE periodo_informacion <= {ctx.periodo}").fetch_df()
    df = identificar(df)
    df["familia"] = df["instrumento"].map(_familia)
    df = df[df.familia != "Pacto"].merge(ctx.aseguradoras, on="rut_compania", how="left")
    df["grupo_contraparte"] = grupo_legible(df["contraparte_grupo"])
    tc = df.periodo_informacion.map(lambda p: ctx.fx[p][0])
    df["nocional_mmusd"] = pd.to_numeric(df.nocional_m, errors="coerce") / tc / 1000.0
    df["columna"] = df.periodo_informacion.map(etiqueta_periodo)
    cols = [etiqueta_periodo(p) for p in pers]
    actual = cols[-1]

    aseg = (df.pivot_table(index=["rut_compania", "aseguradora"], columns="columna",
                           values="nocional_mmusd", aggfunc="sum")
            .reindex(columns=cols).reset_index().sort_values(actual, ascending=False))
    cp = (df.pivot_table(index="entidad_id", columns="columna", values="nocional_mmusd", aggfunc="sum")
          .reindex(columns=cols))
    # Nombre, tipo y pais del mes mas reciente en que aparece la entidad.
    meta = (df.sort_values("periodo_informacion")
            .groupby("entidad_id")[["entidad_tipo_id", "entidad_nombre", "entidad_pais", "grupo_contraparte"]].last())
    cp = meta.join(cp).reset_index().sort_values(actual, ascending=False, na_position="last")
    return aseg, cp


def evolucion_deriv_grupo(ctx: Contexto) -> pd.DataFrame:
    """Nocional vigente de derivados (sin pactos) por mes, instrumento y
    entidad legal con su grupo, en formato largo. Cada mes al dolar de SU
    cierre, como Evol_Deriv_Aseguradora. La clasificacion es la misma de las
    hojas de derivados (un forward UF informado como moneda es Forward UF)."""
    df = ctx.con.execute(
        f"SELECT * FROM v_derivado_clasificado WHERE periodo_informacion <= {ctx.periodo}").fetch_df()
    df = identificar(df)
    df = _clasificar(df)
    df = df[df.familia != "Pacto"].copy()
    df["grupo_contraparte"] = grupo_legible(df["contraparte_grupo"])
    tc = df.periodo_informacion.map(lambda p: ctx.fx[p][0])
    df["nocional_mmusd"] = pd.to_numeric(df.nocional_m, errors="coerce") / tc / 1000.0
    # Nombre y grupo del mes mas reciente de cada entidad: la misma entidad no
    # se parte en dos filas si un mes se informo con otro nombre.
    meta = (df.sort_values("periodo_informacion")
            .groupby("entidad_id")[["entidad_nombre", "grupo_contraparte"]].last())
    t = (df.groupby(["periodo_informacion", "familia", "entidad_id"])
           .agg(operaciones=("folio_operacion", "size"), nocional_mmusd=("nocional_mmusd", "sum"))
           .reset_index().join(meta, on="entidad_id"))
    t["mes_etiqueta"] = t.periodo_informacion.map(etiqueta_periodo)
    orden = {f: i for i, f in enumerate(FAMILIAS)}
    return (t.assign(_o=t.familia.map(orden))
             .sort_values(["periodo_informacion", "_o", "nocional_mmusd"], ascending=[True, True, False])
             .drop(columns="_o"))


# ---------------------------------------------------------------------------
#  nocional de derivados: informativo, NUNCA suma al stock
# ---------------------------------------------------------------------------

def nocional_derivados(ctx: Contexto, periodos: dict[str, int | None]) -> pd.DataFrame:
    """Nocional de derivados (sin pactos) por aseguradora, una columna por periodo.

    El nocional no es stock: es el tamano de referencia del contrato, no lo que
    vale. Sumarlo a la cartera seria sumar peras con manzanas; va en columnas
    aparte. Lo que si suma al stock es el valor razonable neto del derivado,
    que es lo que la aseguradora registra en su balance y declara en el B.8.
    """
    out = None
    for etq, p in periodos.items():
        if p is None:
            s = pd.DataFrame({"rut_compania": pd.Series(dtype="int64"), etq: pd.Series(dtype="float64")})
        else:
            s = ctx.con.execute(f"""SELECT rut_compania, sum(nocional_m) m FROM v_derivado_clasificado
                WHERE periodo_informacion = {int(p)} AND instrumento <> 'Pacto' GROUP BY 1""").fetch_df()
            s[etq] = ctx.a_mmusd(s.pop("m").astype(float), p)
        out = s if out is None else out.merge(s, on="rut_compania", how="outer")
    return out


# ---------------------------------------------------------------------------
#  detalle tailor-made de las inversiones (no derivados)
# ---------------------------------------------------------------------------

def _con_aseg(ctx: Contexto, df: pd.DataFrame, cols_mm: list[str], periodo: int) -> pd.DataFrame:
    for c in cols_mm:
        df[c] = ctx.a_mmusd(pd.to_numeric(df[c], errors="coerce").astype(float), periodo)
    return df.merge(ctx.aseguradoras, on="rut_compania", how="left")


def _mon_sql(col: str) -> str:
    return f"CASE upper(trim({col})) WHEN 'PROM' THEN 'USD' WHEN '$$' THEN 'CLP' ELSE upper(trim({col})) END"


def detalle_renta_fija(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    """Un papel por fila, nacional (B.1) e internacional (B.5), sin leasing.

    El leasing (CLEAS) va en Real Estate, igual que en el stock: aqui se
    excluye para que el total de la hoja cuadre con la columna Renta Fija.
    Se parte de v_renta_fija_clasificada (segmento y nombre del emisor ya
    resueltos) filtrada a la publicacion vigente, y se completan los campos
    que la vista no trae desde el anexo de origen, por (zip, archivo, linea).
    """
    p = periodo or ctx.periodo
    # Dos ramas con join de IGUALDAD sobre datos ya filtrados al periodo. Un
    # LEFT JOIN condicional (ambito = 'LOCAL' AND ...) impedia el hash join y
    # obligaba a un loop anidado contra 2,2 millones de filas: 280 segundos.
    comunes = """v.rut_compania, v.tipo_instrumento, v.segmento_emisor, v.instrumento_id,
                 v.emisor_nombre, v.emisor_grupo, v.emisor_rut, v.moneda, v.valor_final,
                 v.tasa_emision, v.tir_compra, v.tir_mercado, v.duracion, v.duracion_origen,
                 v.clasificacion_riesgo, v.fecha_vencimiento,
                 o.pais, o.valor_nominal, o.fecha_emision, o.fecha_compra"""
    df = ctx.con.execute(f"""
        WITH v AS (
            SELECT v.* FROM v_renta_fija_clasificada v
            JOIN publicacion_vigente pv ON pv.periodo_informacion = v.periodo_informacion
             AND pv.zip_origen = v.zip_origen AND pv.recencia = 1
            WHERE v.periodo_informacion = {int(p)} AND COALESCE(v.tipo_instrumento, '') <> 'CLEAS'),
        rl AS (SELECT zip_origen, source_file, line_no, pais, valor_nominal, fecha_emision, fecha_compra
               FROM raw_renta_fija WHERE periodo_informacion = {int(p)}),
        rx AS (SELECT zip_origen, source_file, line_no, pais, valor_nominal, fecha_emision, fecha_compra
               FROM raw_extranjero_rf WHERE periodo_informacion = {int(p)})
        SELECT 'Nacional' AS ambito, {comunes} FROM v JOIN rl o
          ON o.zip_origen = v.zip_origen AND o.source_file = v.source_file AND o.line_no = v.line_no
          WHERE v.ambito = 'LOCAL'
        UNION ALL
        SELECT 'Internacional' AS ambito, {comunes} FROM v JOIN rx o
          ON o.zip_origen = v.zip_origen AND o.source_file = v.source_file AND o.line_no = v.line_no
          WHERE v.ambito = 'EXTRANJERO'
    """).fetch_df()
    # PDBC (pagare descontable) y BCU (bono en UF) son instrumentos del Banco
    # Central por su propio codigo CMF, y los informan con el RUT del Banco
    # Central (97029000). La vista del warehouse los rotula 'Bancario' y 'Otros'
    # porque supone otro RUT para el Banco Central; se corrige aqui, sin tocar
    # la vista que usa el dashboard.
    bc = df.tipo_instrumento.isin(["PDBC", "BCU"]) & (pd.to_numeric(df.emisor_rut, errors="coerce") == 97029000)
    df.loc[bc, "segmento_emisor"] = "Soberano"
    df["plazo_residual_dias"] = (pd.to_datetime(df.fecha_vencimiento) - pd.Timestamp(ctx.fecha_cierre)).dt.days
    df = _con_aseg(ctx, df, ["valor_final"], p)
    return df.sort_values(["aseguradora", "valor_final"], ascending=[True, False])


def _renta_variable(ctx: Contexto, p: int, filtro: str) -> pd.DataFrame:
    """B.2 (nacional) y B.5 renta variable (internacional) con un filtro de
    tipo de instrumento, en columnas comunes."""
    V = lambda t: _vigente(t, p)  # noqa: E731
    return ctx.con.execute(f"""
        SELECT t.rut_compania, 'Nacional' AS ambito, t.tipo_instrumento,
               t.nemotecnico AS instrumento, CAST(t.emisor_rut AS BIGINT) AS emisor_rut,
               -- NOMBRE_DEL_FONDO no es el nombre del instrumento: la CMF lo define
               -- como el fondo CUI de la aseguradora que el papel respalda ('NO
               -- APLICA' si ninguno). El B.2 no trae el nombre del fondo invertido.
               NULL AS nombre, t.nombre_fondo AS campo_fondo_cui,
               CAST(t.rut_fondo AS BIGINT) AS rut_fondo,
               'CL' AS pais, NULL AS bolsa, {_mon_sql('t.unidad_monetaria')} AS moneda,
               t.tipo_fondo, t.segmento_fondo, t.subyacente, t.serie,
               t.unidades, NULL AS valor_unitario, NULL AS valor_cuota,
               t.valor_final, t.presencia_bursatil, t.participacion_pct,
               t.filial_coligada, t.relacionado, t.clasificacion_riesgo, t.custodio
        FROM {V('raw_equity')} AND ({filtro})
        UNION ALL BY NAME
        SELECT t.rut_compania, 'Internacional' AS ambito, t.tipo_instrumento,
               t.isin AS instrumento, NULL AS emisor_rut, t.emisor AS nombre,
               NULL AS campo_fondo_cui, NULL AS rut_fondo,
               t.pais, t.bolsa, {_mon_sql('t.moneda')} AS moneda,
               t.tipo_fondo, t.segmento_fondo, t.subyacente, t.serie,
               t.unidades, t.valor_bursatil_unitario AS valor_unitario, t.valor_cuota,
               t.valor_final, NULL AS presencia_bursatil, t.participacion_pct,
               NULL AS filial_coligada, t.relacionado, t.clasificacion_riesgo, t.custodio
        FROM {V('raw_extranjero_rv')} AND ({filtro})
    """).fetch_df()


def detalle_acciones(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    """Acciones (codigos AC*). El nombre del emisor nacional sale de la nomina
    de emisores de la CMF; el internacional lo informa la aseguradora."""
    from utils.fetch_emisores import leer as leer_emisores
    p = periodo or ctx.periodo
    df = _renta_variable(ctx, p, "t.tipo_instrumento LIKE 'AC%'")
    nomina = leer_emisores()
    # Nacional: nombre desde el RUT en la nomina de emisores de la CMF, o vacio.
    # Internacional: el nombre que informa la aseguradora.
    df["emisor"] = [nomina.get(int(r)) if pd.notna(r) else n
                    for r, n in zip(df.emisor_rut, df.nombre)]
    df = _con_aseg(ctx, df, ["valor_final"], p)
    return df.sort_values(["aseguradora", "valor_final"], ascending=[True, False])


def detalle_etf(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    p = periodo or ctx.periodo
    df = _con_aseg(ctx, _renta_variable(ctx, p, "t.tipo_instrumento LIKE 'ETF%'"), ["valor_final"], p)
    return df.sort_values(["aseguradora", "valor_final"], ascending=[True, False])


def detalle_fondos_inversion(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    p = periodo or ctx.periodo
    df = _con_aseg(ctx, _renta_variable(ctx, p, "t.tipo_instrumento LIKE 'CFI%'"), ["valor_final"], p)
    return df.sort_values(["aseguradora", "valor_final"], ascending=[True, False])


def detalle_fondos_mutuos(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    """B.3 completo (nacional) + codigos CFM* del B.5 (internacional). La
    administradora nacional se nombra con la nomina de emisores de la CMF."""
    from utils.fetch_emisores import leer as leer_emisores
    p = periodo or ctx.periodo
    df = ctx.con.execute(f"""
        SELECT t.rut_compania, 'Nacional' AS ambito, t.tipo_instrumento, t.nemotecnico AS instrumento,
               NULL AS nombre, t.nombre_fondo AS campo_fondo_cui,
               CAST(t.rut_administradora AS BIGINT) AS rut_administradora,
               'CL' AS pais, t.tipo_fondo, t.serie, {_mon_sql('t.unidad_monetaria')} AS moneda,
               t.unidades, t.valor_cuota, t.valor_final, t.relacionado, t.clasificacion_riesgo
        FROM {_vigente('raw_fondo', p)}
        UNION ALL BY NAME
        SELECT t.rut_compania, 'Internacional' AS ambito, t.tipo_instrumento, t.isin AS instrumento,
               t.emisor AS nombre, NULL AS campo_fondo_cui, NULL AS rut_administradora, t.pais,
               t.tipo_fondo, t.serie, {_mon_sql('t.moneda')} AS moneda, t.unidades, t.valor_cuota,
               t.valor_final, t.relacionado, t.clasificacion_riesgo
        FROM {_vigente('raw_extranjero_rv', p)} AND t.tipo_instrumento LIKE 'CFM%'
    """).fetch_df()
    nomina = leer_emisores()
    df["administradora"] = [nomina.get(int(r)) if pd.notna(r) else None for r in df.rut_administradora]
    df = _con_aseg(ctx, df, ["valor_final"], p)
    return df.sort_values(["aseguradora", "valor_final"], ascending=[True, False])


def detalle_real_estate(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    """B.4 completo: bienes raices propios (BZ) y en leasing (CLEAS).

    No se exporta el arrendatario: en el leasing habitacional es una persona
    natural, y el analisis de cartera no lo necesita (ademas viene vacio).
    """
    p = periodo or ctx.periodo
    df = ctx.con.execute(f"""
        SELECT t.rut_compania,
               -- El B.4 no trae pais, pero el 100% de los registros tiene comuna
               -- con codigo SEIL de Chile (1 a 347) y ciudad chilena.
               'Nacional' AS ambito,
               CASE t.tipo_instrumento WHEN 'CLEAS' THEN 'Leasing' WHEN 'BZ' THEN 'Propio'
                    ELSE t.tipo_instrumento END AS tenencia,
               t.tipo_instrumento, t.rol, t.nemotecnico, t.tipo_inmueble,
               CASE t.urbano WHEN 'UR' THEN 'Urbano' WHEN 'NU' THEN 'No urbano' ELSE t.urbano END AS urbano,
               CASE t.destino WHEN 'HA' THEN 'Habitacional' WHEN 'NH' THEN 'No habitacional'
                    ELSE t.destino END AS destino,
               t.uso, t.comuna, t.ciudad, t.fecha_compra, t.m2_terreno, t.m2_construccion,
               t.costo_actualizado, t.depreciacion_acumulada, t.costo_corregido,
               t.tasacion_1, t.tasacion_2,
               CASE WHEN COALESCE(t.tasacion_1,0) > 0 AND COALESCE(t.tasacion_2,0) > 0
                         THEN least(t.tasacion_1, t.tasacion_2)
                    WHEN COALESCE(t.tasacion_1,0) > 0 THEN t.tasacion_1
                    WHEN COALESCE(t.tasacion_2,0) > 0 THEN t.tasacion_2 END AS menor_tasacion,
               t.fecha_tasacion_1, t.fecha_tasacion_2, t.deterioro, t.valor_final,
               t.monto_arriendo_uf, t.saldo_plazo_arriendo_meses, t.vida_util_restante_meses,
               t.copropiedad_pct, t.prohibicion_o_gravamen
        FROM {_vigente('raw_bienes_raices', p)}
    """).fetch_df()
    df = _con_aseg(ctx, df, ["costo_actualizado", "depreciacion_acumulada", "costo_corregido",
                             "tasacion_1", "tasacion_2", "menor_tasacion", "deterioro", "valor_final"], p)
    return df.sort_values(["aseguradora", "valor_final"], ascending=[True, False])


def detalle_otras(ctx: Contexto, periodo: int | None = None) -> pd.DataFrame:
    """B.6. Ambito: OIED es 'otra inversion extranjera' por definicion del
    anexo; para el resto se usa el pais informado."""
    p = periodo or ctx.periodo
    df = ctx.con.execute(f"""
        SELECT t.rut_compania,
               CASE WHEN t.tipo_instrumento LIKE '%OIED' OR COALESCE(upper(t.pais), 'CL') <> 'CL'
                    THEN 'Internacional' ELSE 'Nacional' END AS ambito,
               t.tipo_instrumento, t.codigo_inversion, t.nemotecnico, t.pais,
               {_mon_sql('t.moneda')} AS moneda, t.valor_costo, t.depreciacion, t.valor_razonable,
               t.deterioro, t.valor_final, t.clasificacion_riesgo, t.custodio
        FROM {_vigente('raw_otras_inv', p)}
    """).fetch_df()
    df = _con_aseg(ctx, df, ["valor_costo", "depreciacion", "valor_razonable", "deterioro", "valor_final"], p)
    return df.sort_values(["aseguradora", "valor_final"], ascending=[True, False])


# ---------------------------------------------------------------------------
#  historia de operaciones: originacion mensual y camadas
# ---------------------------------------------------------------------------
#
# La foto de un mes muestra lo que SIGUE VIVO, no lo que se origino: una
# operacion que vencio antes del cierre ya no esta. Para saber cuanto se
# origino y cuanto sobrevive hay que seguir cada operacion a traves de las
# fotos mensuales del warehouse. La clave (aseguradora, folio, item) no se
# repite dentro de una foto, el 97,9% de las operaciones aparece por primera
# vez en su propio mes de origen y solo 2 de 9.563 tienen huecos.
#
# Reglas:
#   - una operacion esta VIVA desde su mes de origen hasta la ultima foto en
#     que aparece (si se informo con atraso, igual existia en el intertanto);
#   - su nocional INICIAL es el de la primera foto, al dolar de ese cierre, y
#     la supervivencia se mide sobre ese valor fijo: la curva refleja
#     vencimientos y deshaces, no movimientos del tipo de cambio;
#   - solo camadas desde la primera foto del warehouse (dic-2024): para las
#     anteriores no se observa el volumen inicial;
#   - lo que se origina y vence dentro del mismo mes no es observable con
#     fotos de cierre de mes.

#: Metrica de tasa de cada familia: solo donde hay una tasa bien definida y
#: COMPARABLE entre operaciones del mismo subyacente.
#:
#:   CCS          Diferencial entre patas fijas, moneda 1 contra moneda 2 del
#:                cruce canonico (UF contra USD en un UF/USD), en forma
#:                compuesta: (1 + tasa 1) / (1 + tasa 2) - 1. En los CCS UF/USD
#:                conviven dos convenciones de reporte: tasa por pata (UF 3,49%
#:                contra USD 6,14%) y pata USD plana (USD 0%, UF -2,19%). La
#:                tasa de la pata UF sola mezcla ambas y cae cuando cambia la
#:                mezcla sin que el mercado se mueva. El diferencial compuesto
#:                es exactamente la tasa UF cuando la pata USD es plana, y deja
#:                ~10 pb entre convenciones a igual plazo (la resta simple
#:                dejaba ~25).
#:   Swap Promesa Inflacion breakeven: (1 + tasa pesos) / (1 + tasa UF) - 1,
#:                solo fija contra fija.
#:   IRS          Tasa fija.
#:   Forward FX   Tipo de cambio forward pactado (CLP por unidad de la divisa),
#:                exacto. El diferencial implicito anualizado necesita el spot
#:                de la ejecucion; con el dolar observado del dia su ruido
#:                (p10-p90 de +-2 a +-5% anual bajo 3 meses) es mayor que la
#:                senal, asi que no se usa.
#:   Forward UF   Inflacion implicita al pactar: (precio pactado / UF del dia)
#:                ^ (365 / dias) - 1. Exacta: la UF de cada dia es oficial.
#:                Bajo 3 meses la domina el IPC ya conocido del mes (abr-2026:
#:                ~18% anualizado a 40 dias, con la UF efectivamente subiendo
#:                1,6% en ese plazo); menos de DIAS_MIN_FWD dias queda fuera.
METRICA_TASA = {"CCS": "Diferencial de tasas moneda 1 vs moneda 2 del cruce (pb)",
                "Swap Promesa": "Inflacion breakeven (%)",
                "IRS": "Tasa fija (%)",
                "Forward FX": "Tipo de cambio forward pactado (CLP por unidad de divisa)",
                "Forward UF": "Inflacion implicita al pactar (% anual)"}

DIAS_MIN_FWD = 14


def _serie_diaria(nombre: str) -> pd.Series:
    """Serie diaria de config/series (uf o usd), indexada por fecha y ordenada."""
    import json
    doc = json.load(open(ROOT / "config" / "series" / f"{nombre}.json", encoding="utf-8"))
    d = doc["diaria"] if isinstance(doc.get("diaria"), dict) else doc["dias"]
    return pd.Series({pd.Timestamp(k): float(v) for k, v in d.items()}).sort_index()


def _valor_serie(serie: pd.Series, fechas: pd.Series) -> pd.Series:
    """Valor de la serie en cada fecha: el ultimo publicado con fecha <= fecha."""
    f = pd.to_datetime(fechas)
    pos = serie.index.searchsorted(f.fillna(pd.Timestamp("1900-01-01")).values, side="right") - 1
    vals = serie.values[np.clip(pos, 0, len(serie) - 1)]
    return pd.Series(np.where((pos >= 0) & f.notna().values, vals, np.nan), index=fechas.index)


def metrica_tasa(ops: pd.DataFrame) -> pd.Series:
    """Metrica de tasa de cada operacion segun su familia (ver METRICA_TASA).

    Necesita familia, subyacente, monedas y tasas de las patas, rol_tasa_fija,
    tasa_fija, tasa_precio_contrato y las fechas de operacion y vencimiento.
    """
    m = pd.Series(np.nan, index=ops.index, dtype="float64")
    ml, mc = ops.m_larga.map(_mon), ops.m_corta.map(_mon)
    tl = pd.to_numeric(ops.pata_larga_tasa, errors="coerce")
    tco = pd.to_numeric(ops.pata_corta_tasa, errors="coerce")

    # CCS: moneda 1 del cruce canonico contra moneda 2, compuesto. Ambas patas
    # en 0% es un contrato no informado, no un diferencial de 0.
    primera = pd.Series([_par(a, b).split("/")[0] for a, b in zip(ml, mc)], index=ops.index)
    ccs = ((ops.familia == "CCS") & (ops.rol_tasa_fija == "FIJA_CONTRA_FIJA") & (ml != mc)
           & tl.notna() & tco.notna() & ~((tl == 0) & (tco == 0)))
    t1, t2 = np.where(ml == primera, tl, tco), np.where(ml == primera, tco, tl)
    m[ccs] = (((1 + t1 / 100) / (1 + t2 / 100) - 1) * 1e4)[ccs.values]

    pr = ops.familia == "Swap Promesa"
    if pr.any():
        m[pr] = ops[pr].apply(breakeven_promesa, axis=1)

    irs = ops.familia == "IRS"
    m[irs] = pd.to_numeric(ops.tasa_fija, errors="coerce")[irs]

    precio = pd.to_numeric(ops.tasa_precio_contrato, errors="coerce")
    fx = (ops.familia == "Forward FX") & (precio > 0)
    m[fx] = precio[fx]

    dias = (pd.to_datetime(ops.fecha_vencimiento) - pd.to_datetime(ops.fecha_operacion)).dt.days
    uf = (ops.familia == "Forward UF") & (ops.subyacente == "UF/CLP") & (dias >= DIAS_MIN_FWD) & (precio > 0)
    if uf.any():
        spot = _valor_serie(_serie_diaria("uf"), ops.fecha_operacion[uf])
        m[uf] = ((precio[uf] / spot) ** (365.0 / dias[uf]) - 1) * 100
    return m


def historia_operaciones(ctx: Contexto) -> tuple[pd.DataFrame, list[int]]:
    """Una fila por OPERACION (sin pactos), con su primera y ultima foto.

    Lo pactado (nocional, monedas y tasas de las patas, precio) sale de la
    PRIMERA foto en que aparece la operacion. La clasificacion, la contraparte
    y las fechas salen de la ULTIMA, igual que en el stock del periodo: si la
    aseguradora corrigio lo que informa, vale lo corregido (18 operaciones
    pasaron de CCS a swap promesa entre fotos).
    """
    pers = periodos_disponibles(ctx)
    h = ctx.con.execute(f"""
        SELECT periodo_informacion, rut_compania, folio_operacion, item_operacion, instrumento,
               activo_objeto_largo, activo_objeto_corto, moneda, indice_flotante, subyacente_contrato,
               fecha_operacion, fecha_vencimiento, nocional_m, m_larga, m_corta, pata_larga_tasa,
               pata_corta_tasa, rol_tasa_fija, tasa_fija, tasa_precio_contrato,
               contraparte_rut, contraparte_dv, contraparte_lei, contraparte_nombre_informado,
               contraparte_key, contraparte_nombre, contraparte_grupo, resolucion_metodo
        FROM v_derivado_clasificado
        WHERE periodo_informacion <= {ctx.periodo} AND instrumento <> 'Pacto'
    """).fetch_df()
    clave = ["rut_compania", "folio_operacion", "item_operacion"]
    pactado = ["nocional_m", "m_larga", "m_corta", "rol_tasa_fija", "pata_larga_tasa",
               "pata_corta_tasa", "tasa_fija", "tasa_precio_contrato"]
    # drop_duplicates sobre filas ordenadas toma filas COMPLETAS; groupby().first()
    # mezclaria filas al saltarse nulos.
    h = h.sort_values("periodo_informacion", kind="stable")
    primera = (h.drop_duplicates(clave, keep="first")[clave + ["periodo_informacion"] + pactado]
               .rename(columns={"periodo_informacion": "primera_foto"}))
    ultima = (h.drop_duplicates(clave, keep="last").drop(columns=pactado)
              .rename(columns={"periodo_informacion": "ultima_foto"}))
    ops = ultima.merge(primera, on=clave, validate="one_to_one")
    ops = identificar(ops)
    ops = _clasificar(ops)
    ops["grupo_contraparte"] = grupo_legible(ops["contraparte_grupo"])
    tc = ops.primera_foto.map(lambda p: ctx.fx[p][0])
    ops["nocional_inicial_mmusd"] = pd.to_numeric(ops.nocional_m, errors="coerce") / tc / 1000.0
    fo = pd.to_datetime(ops.fecha_operacion)
    ops["mes_origen"] = (fo.dt.year * 100 + fo.dt.month).astype("Int64")
    ops["tasa_metrica"] = metrica_tasa(ops)
    return ops, pers


def _en_ventana(ops: pd.DataFrame, pers: list[int]) -> pd.DataFrame:
    return ops[ops.mes_origen.notna() & (ops.mes_origen >= pers[0]) & (ops.mes_origen <= pers[-1])]


def originacion_mensual(ops: pd.DataFrame, pers: list[int]) -> pd.DataFrame:
    """Nocional ORIGINADO por mes, instrumento y contraparte legal (con su grupo)."""
    o = _en_ventana(ops, pers)
    t = (o.groupby(["mes_origen", "familia", "subyacente", "grupo_contraparte", "entidad_id", "entidad_nombre"],
                   dropna=False)
          .agg(operaciones=("folio_operacion", "size"), nocional_inicial_mmusd=("nocional_inicial_mmusd", "sum"))
          .reset_index())
    t["mes_etiqueta"] = t.mes_origen.map(lambda p: etiqueta_periodo(int(p)))
    return t.sort_values(["mes_origen", "familia", "nocional_inicial_mmusd"], ascending=[True, True, False])


def camadas(ops: pd.DataFrame, pers: list[int]) -> pd.DataFrame:
    """Supervivencia de cada camada (instrumento, subyacente y mes de origen)
    en cada foto mensual desde su origen, medida sobre el nocional inicial.
    Una camada ya extinguida aparece con 0, no desaparece."""
    o = _en_ventana(ops, pers)
    k = ["familia", "subyacente", "mes_origen"]
    inicial = o.groupby(k).nocional_inicial_mmusd.sum()
    filas = []
    for foto in pers:
        base = inicial[inicial.index.get_level_values("mes_origen") <= foto]
        viva = o[(o.mes_origen <= foto) & (o.ultima_foto >= foto)]
        g = (viva.groupby(k).agg(operaciones_vivas=("folio_operacion", "size"),
                                 nocional_vivo_mmusd=("nocional_inicial_mmusd", "sum"))
             .reindex(base.index, fill_value=0))
        g["nocional_inicial_camada_mmusd"] = base
        g["foto"] = foto
        filas.append(g.reset_index())
    t = pd.concat(filas, ignore_index=True)
    t["operaciones_vivas"] = t.operaciones_vivas.astype(int)
    t["nocional_vivo_mmusd"] = t.nocional_vivo_mmusd.astype(float)
    t["pct_vivo"] = np.where(t.nocional_inicial_camada_mmusd > 0,
                             t.nocional_vivo_mmusd / t.nocional_inicial_camada_mmusd * 100, np.nan)
    t["meses_desde_origen"] = ((t.foto // 100 - t.mes_origen // 100) * 12 + (t.foto % 100 - t.mes_origen % 100)).astype(int)
    t["mes_etiqueta"] = t.mes_origen.map(lambda p: etiqueta_periodo(int(p)))
    t["foto_etiqueta"] = t.foto.map(etiqueta_periodo)
    orden = {f: i for i, f in enumerate(FAMILIAS)}
    return (t.assign(_o=t.familia.map(orden)).sort_values(["_o", "subyacente", "mes_origen", "foto"])
             .drop(columns="_o"))


def camadas_resumen(ops: pd.DataFrame, pers: list[int]) -> pd.DataFrame:
    """Una fila por camada: cuanto se origino, a que tasa y cuanto sigue vivo hoy."""
    o = _en_ventana(ops, pers).copy()
    o["viva_hoy"] = o.ultima_foto == pers[-1]
    o["_w"] = np.where(o.tasa_metrica.notna(), o.nocional_inicial_mmusd, 0.0)
    o["_tw"] = o.tasa_metrica.fillna(0.0) * o._w
    o["_wv"] = np.where(o.viva_hoy, o._w, 0.0)
    o["_twv"] = np.where(o.viva_hoy, o._tw, 0.0)
    k = ["familia", "subyacente", "mes_origen"]
    g = o.groupby(k)
    t = pd.DataFrame({
        "operaciones_iniciales": g.size(),
        "nocional_inicial_mmusd": g.nocional_inicial_mmusd.sum(),
        "operaciones_vivas_hoy": g.viva_hoy.sum(),
        "nocional_vivo_hoy_mmusd": o[o.viva_hoy].groupby(k).nocional_inicial_mmusd.sum(),
        "operaciones_con_tasa": g.tasa_metrica.count(),
        "_tw": g._tw.sum(), "_w": g._w.sum(), "_twv": g._twv.sum(), "_wv": g._wv.sum(),
    }).reset_index()
    t["nocional_vivo_hoy_mmusd"] = t.nocional_vivo_hoy_mmusd.fillna(0.0)
    t["pct_vivo_hoy"] = np.where(t.nocional_inicial_mmusd > 0,
                                 t.nocional_vivo_hoy_mmusd / t.nocional_inicial_mmusd * 100, np.nan)
    t["metrica_tasa"] = np.where(t._w > 0, t.familia.map(METRICA_TASA), "Sin metrica comparable")
    t["tasa_inicial_ponderada"] = np.where(t._w > 0, t._tw / t._w, np.nan)
    t["tasa_viva_hoy_ponderada"] = np.where(t._wv > 0, t._twv / t._wv, np.nan)
    t["mes_etiqueta"] = t.mes_origen.map(lambda p: etiqueta_periodo(int(p)))
    orden = {f: i for i, f in enumerate(FAMILIAS)}
    return (t.drop(columns=["_tw", "_w", "_twv", "_wv"]).assign(_o=t.familia.map(orden))
             .sort_values(["_o", "subyacente", "mes_origen"]).drop(columns="_o"))
