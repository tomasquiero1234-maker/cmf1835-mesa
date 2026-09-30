"""
reportes.informe
================

Informe BI de derivados de aseguradoras para la mesa de dinero de BBVA NY:
HTML interactivo (Plotly) y PDF (Chrome sin interfaz, sobre el mismo HTML).

Lee SOLO las tablas con nombre (tbl_*) del libro Excel que arma
``python -m reportes``: no abre el warehouse ni importa reportes.datos. Con el
Excel entregado, cualquiera reproduce el informe.

    python -m reportes.informe
    python -m reportes.informe --excel reportes/salida/stock_aseguradoras_202608.xlsx --salida reportes/salida
    python -m reportes.informe --sin-pdf

Reglas del pedido
-----------------
- Volumen, participacion y rankings: NOCIONAL en USD. El MtM no se usa.
- Solo operaciones vigentes: cada numero sale de una foto mensual del B.7, que
  lista lo vigente al cierre; ademas se descarta lo que venza antes del corte.
- Originacion y camadas: por fecha de operacion.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import math
import os
import posixpath
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries
from plotly.colors import sample_colorscale
from plotly.offline import get_plotlyjs

ROOT = Path(__file__).resolve().parents[1]
SALIDA = ROOT / "reportes" / "salida"
PLANTILLAS = Path(__file__).resolve().parent / "plantillas"

GRUPO_BBVA = "Grupo BBVA"

# ---------------------------------------------------------------------------
#  estilo
# ---------------------------------------------------------------------------

AZUL_OSCURO = "#072146"
AZUL = "#004481"
AZUL_MEDIO = "#1973B8"
AZUL_CLARO = "#5BBEFF"
NARANJA = "#F7893B"
GRIS = "#BFC5CC"
GRIS_OSCURO = "#6B7785"
TEXTO = "#1F2D3D"
FUENTE = "Helvetica Neue, Helvetica, Arial, sans-serif"
ANCHO = 1000                       # px: cabe en A4 apaisado con margenes de 10 mm

FAMILIAS = ["CCS", "Forward FX", "Forward UF", "Swap Promesa", "IRS", "Opcion", "Futuro"]
NOMBRE_FAM = {"CCS": "CCS", "Forward FX": "Forward FX", "Forward UF": "Forward UF",
              "Swap Promesa": "Swap promesa", "IRS": "IRS", "Opcion": "Opciones", "Futuro": "Futuros"}
COLOR_FAM = {"CCS": AZUL, "Forward FX": AZUL_MEDIO, "Forward UF": AZUL_CLARO, "Swap Promesa": "#2DCCCD",
             "IRS": NARANJA, "Opcion": "#D8BE75", "Futuro": "#8F7AE5"}
#: Competidores en las lineas de participacion: grises y azul-grises, BBVA en azul.
COLOR_COMP = ["#3E4A57", "#A07F4F", "#3A9A9A", "#8C7AB8", "#8A96A3"]

#: Hoja tailor-made de cada instrumento: tabla, columna del subyacente y
#: columna de la tasa al pactar (None si el instrumento no tiene una comparable).
TABLAS_DERIV = {
    "CCS": ("tbl_ccs", "Cruce", "Diferencial compuesto moneda 1 vs 2 (pb)"),
    "Swap Promesa": ("tbl_swap_promesa", "Cruce", "Inflacion breakeven (%)"),
    "Forward FX": ("tbl_forward_fx", "Par", "Precio pactado"),
    "Forward UF": ("tbl_forward_uf", "Par", "Inflacion implicita al pactar (% anual)"),
    "IRS": ("tbl_irs", "Subyacente", "Tasa fija (%)"),
    "Opcion": ("tbl_opciones", "Subyacente", None),
    "Futuro": ("tbl_futuros", "Subyacente", None),
}

#: Tramos de plazo ORIGINAL (fecha de operacion a vencimiento), en dias.
TRAMOS_DIAS = [-np.inf, 31, 92, 183, 366, 731, 1827, 3653, 7305, np.inf]
TRAMOS = ["Hasta 1M", "1-3M", "3-6M", "6-12M", "1-2A", "2-5A", "5-10A", "10-20A", "Más de 20A"]

_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def mes_corto(p: int) -> str:
    """202608 -> 'ago-26'."""
    p = int(p)
    return f"{_MESES[p % 100 - 1]}-{str(p // 100)[2:]}"


def mes_atras(p: int, k: int) -> int:
    """El mes AAAAMM que esta k meses antes de p."""
    t = (int(p) // 100) * 12 + int(p) % 100 - 1 - k
    return (t // 12) * 100 + t % 12 + 1


def num(x, dec: int = 1, signo: bool = False) -> str:
    """Numero con separadores en castellano: 1.234,5. Vacio como raya."""
    if x is None or x is pd.NA or (isinstance(x, (float, np.floating)) and not math.isfinite(x)):
        return "–"
    s = f"{x:+,.{dec}f}" if signo else f"{x:,.{dec}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def pct(x, dec: int = 1, signo: bool = False) -> str:
    s = num(x, dec, signo)
    return s if s == "–" else s + "%"


# ---------------------------------------------------------------------------
#  lectura del Excel: SOLO tablas con nombre
# ---------------------------------------------------------------------------

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _destino(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target))


def mapa_tablas(xlsx: Path) -> dict[str, tuple[str, str]]:
    """{nombre de tabla: (hoja, rango)}, leido del XML del libro."""
    mapa: dict[str, tuple[str, str]] = {}
    with zipfile.ZipFile(xlsx) as z:
        nombres = set(z.namelist())
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid = {r.get("Id"): r.get("Target") for r in rels.findall("pr:Relationship", _NS)}
        for hoja in wb.find("m:sheets", _NS):
            ruta = _destino("xl/workbook.xml", rid[hoja.get(f"{{{_NS['r']}}}id")])
            rr = posixpath.join(posixpath.dirname(ruta), "_rels", posixpath.basename(ruta) + ".rels")
            if rr not in nombres:
                continue
            for r in ET.fromstring(z.read(rr)).findall("pr:Relationship", _NS):
                if r.get("Type", "").endswith("/table"):
                    t = ET.fromstring(z.read(_destino(ruta, r.get("Target"))))
                    mapa[t.get("displayName")] = (hoja.get("name"), t.get("ref"))
    return mapa


def leer_tablas(xlsx: Path, nombres: list[str]) -> dict[str, pd.DataFrame]:
    """Lee tablas de Excel POR NOMBRE. Falla si el libro no las trae."""
    mapa = mapa_tablas(xlsx)
    faltan = [n for n in nombres if n not in mapa]
    if faltan:
        raise KeyError(f"El libro {xlsx.name} no trae {faltan}: regenerarlo con 'python -m reportes'")
    wb = load_workbook(xlsx, read_only=True, data_only=True)
    try:
        out = {}
        for n in nombres:
            hoja, ref = mapa[n]
            c1, r1, c2, r2 = range_boundaries(ref)
            filas = list(wb[hoja].iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2, values_only=True))
            # Una tabla sin datos trae una fila en blanco (Excel exige al menos una).
            out[n] = pd.DataFrame(filas[1:], columns=filas[0]).dropna(how="all").reset_index(drop=True)
    finally:
        wb.close()
    return out


@dataclass
class Datos:
    excel: Path
    corte: _dt.date
    periodo: int
    dolar: float
    dolar_fecha: str
    publicacion: str
    vig: pd.DataFrame          # operaciones vigentes al corte, todas las hojas de derivados
    orig: pd.DataFrame         # tbl_originacion
    res: pd.DataFrame          # tbl_camadas_resumen
    cam: pd.DataFrame          # tbl_camadas
    evol: pd.DataFrame         # tbl_evol_deriv_grupo: stock mensual por instrumento y entidad legal
    vencidas_excluidas: int    # filas de detalle con vencimiento anterior al corte


def cargar(xlsx: Path) -> Datos:
    tablas = ["tbl_portada", "tbl_originacion", "tbl_camadas_resumen", "tbl_camadas", "tbl_evol_deriv_grupo"]
    t = leer_tablas(xlsx, tablas + [v[0] for v in TABLAS_DERIV.values()])
    port = dict(zip(t["tbl_portada"]["Campo"], t["tbl_portada"]["Valor"]))
    corte = _dt.datetime.strptime(str(port["Datos al"]), "%d-%m-%Y").date()
    periodo = int(str(port["Periodo"])[:6])
    # "923.45 CLP por USD, observado del 2026-08-31 (...)"
    dol = str(port["Dolar del periodo"])
    dolar = float(dol.split()[0].replace(",", ""))
    dolar_fecha = dol.split("observado del ")[1].split()[0] if "observado del " in dol else ""

    partes = []
    for fam, (tabla, col_sub, col_tasa) in TABLAS_DERIV.items():
        d = t[tabla]
        if d.empty:
            continue
        partes.append(pd.DataFrame({
            "instrumento": fam,
            "subyacente": d[col_sub].astype(str),
            "grupo": d["Grupo contraparte"].astype(str),
            "entidad": d["Contraparte (nombre legal)"].astype(str),
            "entidad_id": d["ID legal contraparte"].astype(str),
            "pais": d["Pais contraparte"],
            "aseguradora": d["Aseguradora"].astype(str),
            "nocional": pd.to_numeric(d["Nocional (MM USD)"], errors="coerce"),
            "fecha_operacion": pd.to_datetime(d["Fecha operacion"]),
            "fecha_vencimiento": pd.to_datetime(d["Fecha vencimiento"]),
            "plazo_dias": pd.to_numeric(d["Plazo original (dias)"], errors="coerce"),
            "tasa": pd.to_numeric(d[col_tasa], errors="coerce") if col_tasa else np.nan,
        }))
    vig = pd.concat(partes, ignore_index=True)
    vig["mes_origen"] = vig.fecha_operacion.dt.year * 100 + vig.fecha_operacion.dt.month
    vencidas = vig.fecha_vencimiento < pd.Timestamp(corte)
    vig = vig[~vencidas].reset_index(drop=True)

    orig = t["tbl_originacion"].rename(columns={
        "Mes origen": "mes_origen", "Instrumento": "instrumento", "Subyacente": "subyacente",
        "Grupo contraparte": "grupo", "Contraparte (nombre legal)": "entidad", "ID legal contraparte": "entidad_id",
        "Operaciones": "operaciones", "Nocional originado (MM USD)": "nocional"})
    res = t["tbl_camadas_resumen"].rename(columns={
        "Instrumento": "instrumento", "Subyacente": "subyacente", "Mes origen": "mes_origen",
        "Operaciones originadas": "operaciones", "Nocional originado (MM USD)": "originado",
        "Metrica de tasa": "metrica", "Operaciones con tasa": "ops_tasa", "Tasa ponderada al pactar": "tasa",
        "Operaciones vivas hoy": "ops_vivas", "Nocional vivo hoy (MM USD)": "vivo", "Vivo hoy (%)": "pct_vivo",
        "Tasa ponderada de lo vivo": "tasa_viva"})
    cam = t["tbl_camadas"].rename(columns={
        "Instrumento": "instrumento", "Subyacente": "subyacente", "Mes origen": "mes_origen", "Foto": "foto",
        "Meses desde origen": "meses", "Operaciones vivas": "ops_vivas", "Nocional vivo (MM USD)": "vivo",
        "Nocional originado (MM USD)": "originado", "Vivo (%)": "pct_vivo"})
    evol = t["tbl_evol_deriv_grupo"].rename(columns={
        "Periodo": "periodo", "Instrumento": "instrumento", "Grupo contraparte": "grupo",
        "Contraparte (nombre legal)": "entidad", "ID legal contraparte": "entidad_id", "Operaciones": "operaciones",
        "Nocional (MM USD)": "nocional"})
    for df, cols in ((orig, ["mes_origen", "operaciones"]), (res, ["mes_origen", "operaciones", "ops_vivas", "ops_tasa"]),
                     (cam, ["mes_origen", "foto", "meses", "ops_vivas"]), (evol, ["periodo", "operaciones"])):
        for c in cols:
            df[c] = pd.to_numeric(df[c]).astype(int)
    for df, cols in ((orig, ["nocional"]), (res, ["originado", "vivo", "pct_vivo", "tasa", "tasa_viva"]),
                     (cam, ["vivo", "originado", "pct_vivo"]), (evol, ["nocional"])):
        for c in cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return Datos(xlsx, corte, periodo, dolar, dolar_fecha, str(port["Publicacion CMF usada"]),
                 vig, orig, res, cam, evol, int(vencidas.sum()))


# ---------------------------------------------------------------------------
#  piezas de presentacion
# ---------------------------------------------------------------------------

def _fig(alto: int = 400, ancho: int = ANCHO) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        width=ancho, height=alto, separators=",.", paper_bgcolor="white", plot_bgcolor="white",
        font=dict(family=FUENTE, size=12, color=TEXTO), margin=dict(l=70, r=30, t=20, b=60),
        legend=dict(orientation="h", yanchor="top", y=-0.14, x=0, font=dict(size=11)),
        hoverlabel=dict(font_family=FUENTE), bargap=0.25)
    fig.update_xaxes(showgrid=False, linecolor="#9AA5B1", ticks="outside", tickcolor="#9AA5B1")
    fig.update_yaxes(gridcolor="#E6EAEE", zeroline=True, zerolinecolor="#9AA5B1", linecolor="#9AA5B1")
    return fig


_CONFIG = {"displaylogo": False, "responsive": False,
           "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
           "toImageButtonOptions": {"format": "png", "scale": 2}}


def html_fig(fig: go.Figure, id_: str) -> Markup:
    return Markup(fig.to_html(full_html=False, include_plotlyjs=False, div_id=id_, config=_CONFIG))


_FMT = {
    "txt": lambda v: html.escape(str(v)) if v is not None and not (isinstance(v, float) and math.isnan(v)) else "–",
    "mm": lambda v: num(v, 1),
    "mm0": lambda v: num(v, 0),
    "int": lambda v: num(v, 0),
    "pct": lambda v: pct(v, 1),
    "pct2": lambda v: pct(v, 2),
    "var": lambda v: pct(v, 1, signo=True),
    "pp": lambda v: num(v, 1, signo=True),
    "pb": lambda v: num(v, 0),
    "n2": lambda v: num(v, 2),
    "n1": lambda v: num(v, 1),
    "fecha": lambda v: v.strftime("%d-%m-%Y") if isinstance(v, (_dt.date, pd.Timestamp)) and not pd.isna(v) else "–",
}


def tabla(df: pd.DataFrame, cols: list[tuple[str, str, str]], clase: str = "", nota: str | None = None) -> Markup:
    """Tabla HTML. cols = [(encabezado, columna, formato)]. Una columna '_fila'
    opcional da la clase CSS de cada fila (bbva, total, resto, ref)."""
    num_cols = {c for _, c, f in cols if f != "txt"}
    cab = "".join(f'<th class="{"n" if c in num_cols else ""}">{html.escape(h)}</th>' for h, c, f in cols)
    filas = []
    for _, r in df.iterrows():
        cls = r.get("_fila", "") if "_fila" in df.columns else ""
        celdas = "".join(f'<td class="{"n" if c in num_cols else ""}">{_FMT[f](r[c])}</td>' for _, c, f in cols)
        filas.append(f'<tr class="{cls}">{celdas}</tr>')
    pie = f'<p class="nota-tabla">{html.escape(nota)}</p>' if nota else ""
    return Markup(f'<table class="tabla {clase}"><thead><tr>{cab}</tr></thead>'
                  f'<tbody>{"".join(filas)}</tbody></table>{pie}')


def metrica(fam: str, sub: str) -> tuple[str, str, str, str] | None:
    """(nombre de la tasa, formato de eje, sufijo, titulo corto de eje) de un
    instrumento y subyacente. None si no hay una tasa comparable."""
    if fam == "CCS" and "/" in sub:
        a, b = sub.split("/")[:2]
        return f"Diferencial {a} vs {b} al pactar (pb)", ",.0f", " pb", f"Diferencial {a} vs {b} (pb)"
    if fam == "Forward FX" and "/" in sub:
        div = sub.split("/")[0]
        return f"Tipo de cambio forward pactado (CLP por {div})", ",.1f", "", f"CLP por {div} pactado"
    if fam == "Forward UF":
        return "Inflación implícita al pactar (% anual)", ".2f", "%", "Inflación implícita (%)"
    if fam == "Swap Promesa":
        return "Inflación breakeven al pactar (%)", ".2f", "%", "Breakeven (%)"
    if fam == "IRS":
        return "Tasa fija pactada (%)", ".2f", "%", "Tasa fija (%)"
    return None


def rangos_doble_eje(barras_max: float, linea: pd.Series) -> tuple[list[float], list[float]]:
    """Rangos de los dos ejes para que las barras ocupen la parte baja del
    grafico y la linea la alta, sin cruzarse las etiquetas."""
    v = pd.Series(linea).dropna()
    lo, hi = (float(v.min()), float(v.max())) if len(v) else (0.0, 1.0)
    rango = max(hi - lo, abs(hi) * 0.01, 1e-6)
    return [0, barras_max * 1.75], [lo - rango * 1.6, hi + rango * 0.35]


NARANJA_TEXTO = "#C8581A"


def _ponderada(v: pd.Series, w: pd.Series) -> float:
    ok = v.notna() & w.notna()
    return float(np.average(v[ok], weights=w[ok])) if ok.any() and w[ok].sum() > 0 else np.nan


# ---------------------------------------------------------------------------
#  rankings: siempre top N + Grupo BBVA con su puesto real
# ---------------------------------------------------------------------------

def top_mas_bbva(t: pd.DataFrame, col: str, n: int = 5, resto: bool = True,
                 no_sumar: tuple[str, ...] = (), totales: dict | None = None) -> pd.DataFrame:
    """Top n grupos por `col`, MAS el Grupo BBVA con su puesto real aunque este
    fuera del top, mas 'Resto' y 'Total'.

    t: una fila por grupo (indice = grupo) con columnas numericas. Las columnas
    de `no_sumar` (conteos de valores distintos, como aseguradoras) quedan
    vacias en Resto y toman el valor de `totales` en la fila Total.
    """
    t = t.copy().sort_values(col, ascending=False)
    pos = t[t[col] > 0]
    t["rank"] = pd.Series(np.arange(1, len(pos) + 1), index=pos.index).reindex(t.index)
    tot = t[col].sum()
    t["share"] = t[col] / tot * 100 if tot else np.nan
    top = t[t["rank"] <= n]
    partes = [top]
    if GRUPO_BBVA not in top.index:
        partes.append(t.loc[[GRUPO_BBVA]] if GRUPO_BBVA in t.index else
                      pd.DataFrame({c: [0.0] for c in t.columns if c != "rank"} | {"rank": [np.nan]},
                                   index=[GRUPO_BBVA]))
    mostrados = set(pd.concat(partes).index)
    numericas = [c for c in t.columns if c != "rank" and pd.api.types.is_numeric_dtype(t[c])]
    sumables = [c for c in numericas if c not in no_sumar]
    r = t[~t.index.isin(mostrados)]
    if resto and len(r):
        fila = r[sumables].sum().to_dict() | {c: np.nan for c in no_sumar} | {"rank": np.nan}
        partes.append(pd.DataFrame([fila], index=[f"Resto ({len(r)} grupos)"]))
    fila = t[sumables].sum().to_dict() | dict(totales or {}) | {"rank": np.nan, "share": 100.0}
    partes.append(pd.DataFrame([fila], index=["Total"]))
    out = pd.concat(partes)
    out.index.name = "grupo"
    out = out.reset_index()
    out["_fila"] = np.select([out.grupo == GRUPO_BBVA, out.grupo.str.startswith("Resto ("), out.grupo == "Total"],
                             ["bbva", "resto", "total"], "")
    return out


def puesto_bbva(serie: pd.Series) -> tuple[int | None, int]:
    """(puesto del Grupo BBVA, cantidad de grupos con posicion) en una serie grupo -> valor."""
    s = serie[serie > 0].sort_values(ascending=False)
    return (list(s.index).index(GRUPO_BBVA) + 1 if GRUPO_BBVA in s.index else None), len(s)


def _fila_bbva_txt(share: float, rk: int | None, n: int) -> str:
    return f"{pct(share)} · puesto {rk} de {n}" if rk else "sin posición"


# ---------------------------------------------------------------------------
#  1. resumen ejecutivo: el STOCK al corte
# ---------------------------------------------------------------------------

def seccion_resumen(d: Datos) -> dict:
    p, v, ev = d.periodo, d.vig, d.evol
    stock = v.nocional.sum()
    tot_m = ev.groupby("periodo").nocional.sum()
    p_prev, p_dic = mes_atras(p, 1), (p // 100 - 1) * 100 + 12
    s_prev, s_dic = tot_m.get(p_prev, np.nan), tot_m.get(p_dic, np.nan)

    # ranking del stock por grupo, con apertura por instrumento y participacion a diciembre
    pv = v.pivot_table(index="grupo", columns="instrumento", values="nocional", aggfunc="sum", fill_value=0.0)
    fams = [f for f in FAMILIAS if f in pv.columns]
    pv = pv[fams]
    pv.insert(0, "Total", pv.sum(axis=1))
    g_dic = ev[ev.periodo == p_dic].groupby("grupo").nocional.sum()
    pv = pv.reindex(pv.index.union(g_dic.index), fill_value=0.0)
    pv["ops"] = v.groupby("grupo").size().reindex(pv.index).fillna(0)
    pv["clientes"] = v.groupby("grupo").aseguradora.nunique().reindex(pv.index).fillna(0)
    pv["s_dic"] = (g_dic / g_dic.sum() * 100).reindex(pv.index).fillna(0.0)
    rk = top_mas_bbva(pv, "Total", 5, no_sumar=("clientes",), totales={"clientes": v.aseguradora.nunique(), "s_dic": 100.0})
    rk["d_pp"] = rk.share - rk.s_dic
    ranking = tabla(rk, [("#", "rank", "int"), ("Grupo contraparte", "grupo", "txt"), ("Stock vigente", "Total", "mm"),
                         ("Part. %", "share", "pct"), (f"Part. {mes_corto(p_dic)}", "s_dic", "pct"),
                         ("Var. pp", "d_pp", "pp")]
                    + [(NOMBRE_FAM[f], f, "mm") for f in fams]
                    + [("Oper.", "ops", "int"), ("Aseg.", "clientes", "int")], "league",
                    f"MM USD de nocional vigente al {d.corte:%d-%m-%Y}. Top 5 grupos y el Grupo BBVA con su puesto "
                    f"real. Part. {mes_corto(p_dic)}: participación en el stock de ese cierre; Var. pp: cambio en "
                    "puntos porcentuales. Aseg.: aseguradoras con posición vigente con el grupo.")

    # top 5 + BBVA por instrumento, en stock
    por_inst = []
    for f in fams:
        vf = v[v.instrumento == f]
        g = vf.groupby("grupo").agg(noc=("nocional", "sum"))
        t = top_mas_bbva(g, "noc", 5, resto=False)
        t = t[t._fila != "total"]
        por_inst.append({"titulo": NOMBRE_FAM[f], "total": num(vf.nocional.sum(), 1), "ops": len(vf),
                         "tabla": tabla(t, [("#", "rank", "int"), ("Grupo", "grupo", "txt"),
                                            ("MM USD", "noc", "mm"), ("Part. %", "share", "pct")], "mini")})

    # composicion del stock por instrumento, mes a mes
    pers = sorted(ev.periodo.unique())
    comp = (ev.pivot_table(index="periodo", columns="instrumento", values="nocional", aggfunc="sum", fill_value=0.0)
            .reindex(index=pers, fill_value=0.0))
    fams_c = [f for f in FAMILIAS if f in comp.columns]
    x = [mes_corto(m) for m in pers]
    fig = _fig(390)
    for f in fams_c:
        fig.add_bar(x=x, y=comp[f], name=NOMBRE_FAM[f], marker_color=COLOR_FAM[f],
                    customdata=np.stack([comp[f] / comp[fams_c].sum(axis=1) * 100], axis=-1),
                    hovertemplate=f"<b>{NOMBRE_FAM[f]}</b> %{{x}}<br>%{{y:,.1f}} MM USD (%{{customdata[0]:.1f}}% del stock)"
                                  "<extra></extra>")
    totales = comp[fams_c].sum(axis=1)
    fig.add_scatter(x=x, y=totales, mode="text", text=[num(t / 1000, 1) + " mil" for t in totales],
                    textposition="top center", textfont=dict(size=10, color=TEXTO), showlegend=False, hoverinfo="skip")
    fig.update_layout(barmode="stack", legend=dict(traceorder="normal"),
                      yaxis=dict(title="MM USD de nocional vigente", tickformat=",.0f", range=[0, totales.max() * 1.12]))
    fig.add_vrect(x0=len(x) - 1.5, x1=len(x) - 0.5, fillcolor=AZUL_CLARO, opacity=0.10, line_width=0)
    comp_t = pd.DataFrame({"Instrumento": [NOMBRE_FAM[f] for f in fams_c],
                           "actual": [comp.loc[p, f] for f in fams_c],
                           "prev": [comp.loc[p_prev, f] if p_prev in comp.index else np.nan for f in fams_c],
                           "dic": [comp.loc[p_dic, f] if p_dic in comp.index else np.nan for f in fams_c]})
    comp_t = pd.concat([comp_t, pd.DataFrame([{"Instrumento": "Total", "actual": comp_t.actual.sum(),
                                               "prev": comp_t.prev.sum(), "dic": comp_t.dic.sum(), "_fila": "total"}])],
                       ignore_index=True)
    comp_t["_fila"] = comp_t.get("_fila", pd.Series(dtype=str)).fillna("")
    comp_t["part"] = comp_t.actual / comp_t.actual.iloc[-1] * 100
    comp_t["v_prev"] = (comp_t.actual / comp_t.prev - 1) * 100
    comp_t["v_dic"] = (comp_t.actual / comp_t.dic - 1) * 100
    tabla_comp = tabla(comp_t, [("Instrumento", "Instrumento", "txt"), (f"Stock {mes_corto(p)}", "actual", "mm"),
                                ("% del stock", "part", "pct"), (f"Stock {mes_corto(p_prev)}", "prev", "mm"),
                                (f"Var. vs {mes_corto(p_prev)}", "v_prev", "var"),
                                (f"Stock {mes_corto(p_dic)}", "dic", "mm"), (f"Var. vs {mes_corto(p_dic)}", "v_dic", "var")],
                       "compacta", "MM USD de nocional vigente; cada cierre a su dólar, así que la variación incluye "
                                   "el efecto cambiario.")

    # KPIs y lectura, todo en stock
    g_st = v.groupby("grupo").nocional.sum().sort_values(ascending=False)
    rk_b, n_g = puesto_bbva(g_st)
    rk_dic, n_dic = puesto_bbva(g_dic)
    b_st = g_st.get(GRUPO_BBVA, 0.0)
    top5 = g_st.head(5)
    b_inst = []
    for f in fams:
        gf = v[v.instrumento == f].groupby("grupo").nocional.sum()
        r_, n_ = puesto_bbva(gf)
        if r_:
            b_inst.append((f, gf[GRUPO_BBVA] / gf.sum() * 100, r_, n_))
    fam_st = v.groupby("instrumento").nocional.sum().sort_values(ascending=False)
    mes = v[v.mes_origen == p]
    ult_bbva = v[v.grupo == GRUPO_BBVA].fecha_operacion.max()
    kpis = [
        {"titulo": f"Stock vigente al {d.corte:%d-%m-%Y}", "valor": num(stock, 1), "unidad": "MM USD de nocional",
         "nota": f"{num(len(v), 0)} oper. · {pct((stock / s_prev - 1) * 100, 1, True)} vs {mes_corto(p_prev)}"},
        {"titulo": f"Variación desde {mes_corto(p_dic)}", "valor": pct((stock / s_dic - 1) * 100, 1, True),
         "unidad": "del stock (incluye efecto cambiario)", "nota": f"{mes_corto(p_dic)}: {num(s_dic, 1)} MM USD"},
        {"titulo": "Concentración top 5 grupos", "valor": pct(top5.sum() / stock * 100), "unidad": "del stock",
         "nota": f"de {n_g} grupos con posición vigente"},
        {"titulo": "Grupo BBVA en el stock", "valor": pct(b_st / stock * 100), "unidad": f"{num(b_st, 1)} MM USD",
         "clase": "bbva", "nota": (f"puesto {rk_b} de {n_g}" if rk_b else "sin posición")
                                  + (f" · {mes_corto(p_dic)}: puesto {rk_dic}" if rk_dic else "")},
    ]
    lectura = [
        f"Stock vigente de derivados al {d.corte:%d-%m-%Y}: {num(stock, 1)} MM USD de nocional en {num(len(v), 0)} "
        f"operaciones con {n_g} grupos; {pct((stock / s_prev - 1) * 100, 1, True)} contra {mes_corto(p_prev)} y "
        f"{pct((stock / s_dic - 1) * 100, 1, True)} contra {mes_corto(p_dic)} (cada cierre a su dólar).",
        "Top 5 del stock: " + ", ".join(f"{g} {pct(x / stock * 100)}" for g, x in top5.items())
        + f"; concentran {pct(top5.sum() / stock * 100)}.",
        f"Grupo BBVA: {num(b_st, 1)} MM USD, {pct(b_st / stock * 100)} del stock, puesto {rk_b} de {n_g}"
        + (f" ({mes_corto(p_dic)}: {pct(g_dic.get(GRUPO_BBVA, 0) / g_dic.sum() * 100)}, puesto {rk_dic})" if rk_dic else "")
        + ". Por instrumento: " + "; ".join(f"{NOMBRE_FAM[f]} {pct(sh)}, puesto {r_} de {n_}" for f, sh, r_, n_ in b_inst)
        + ".",
        "Mezcla del stock: " + ", ".join(f"{NOMBRE_FAM[f]} {pct(x / stock * 100)}" for f, x in fam_st.items()) + ".",
        f"Flujo de {mes_corto(p)} (sección 3): {num(mes.nocional.sum(), 1)} MM USD originados; Grupo BBVA "
        f"{num(mes[mes.grupo == GRUPO_BBVA].nocional.sum(), 1)}"
        + (f" (su operación vigente más reciente es del {ult_bbva:%d-%m-%Y})." if not pd.isna(ult_bbva) else "."),
    ]
    return {"kpis": kpis, "lectura": lectura, "ranking": ranking, "por_instrumento": por_inst,
            "graf_composicion": html_fig(fig, "g_composicion"), "tabla_composicion": tabla_comp,
            "_stock": stock, "_ranking": rk}


# ---------------------------------------------------------------------------
#  2. participacion en el stock: Grupo BBVA contra el top 5
# ---------------------------------------------------------------------------

def seccion_mercado(d: Datos) -> dict:
    p, v, ev = d.periodo, d.vig, d.evol
    stock = v.nocional.sum()
    g_st = v.groupby("grupo").nocional.sum()
    t = top_mas_bbva(g_st.to_frame("stock"), "stock", 5)
    grupos = t[~t._fila.isin(["resto", "total"])]

    # barras: participacion en el stock
    b = grupos.iloc[::-1]
    etiquetas = [f"{g}  (#{int(r)})" if g == GRUPO_BBVA and not pd.isna(r) else g for g, r in zip(b.grupo, b["rank"])]
    fig = _fig(300)
    fig.add_bar(y=etiquetas, x=b.share, orientation="h",
                marker_color=[AZUL_MEDIO if g == GRUPO_BBVA else GRIS for g in b.grupo],
                text=[f"{pct(s_)} · {num(m, 0)} MM USD" for s_, m in zip(b.share, b.stock)], textposition="outside",
                cliponaxis=False, hovertemplate="<b>%{y}</b><br>%{x:.1f}% del stock<extra></extra>", showlegend=False)
    fig.update_layout(xaxis=dict(title=f"Participación en el stock vigente al {d.corte:%d-%m-%Y} (%)", ticksuffix="%",
                                 range=[0, b.share.max() * 1.35]), margin=dict(l=230, r=40, t=10, b=50))
    resto = t[t._fila == "resto"]

    # lineas: participacion mensual en el stock, top 5 actual + BBVA
    pers = sorted(ev.periodo.unique())
    tot_m = ev.groupby("periodo").nocional.sum()
    fig2 = _fig(360)
    x = [mes_corto(m) for m in pers]
    rayas = ["solid", "dash", "dot", "dashdot", "longdash"]
    for i, g in enumerate([g for g in grupos.grupo if g != GRUPO_BBVA]):
        sh = ev[ev.grupo == g].groupby("periodo").nocional.sum().reindex(pers, fill_value=0.0) / tot_m * 100
        fig2.add_scatter(x=x, y=sh, name=g, mode="lines", line=dict(color=COLOR_COMP[i], width=1.8, dash=rayas[i]),
                         hovertemplate=f"<b>{g}</b> %{{x}}: %{{y:.1f}}% del stock<extra></extra>")
    sb = ev[ev.grupo == GRUPO_BBVA].groupby("periodo").nocional.sum().reindex(pers, fill_value=0.0) / tot_m * 100
    fig2.add_scatter(x=x, y=sb, name=GRUPO_BBVA, mode="lines+markers", line=dict(color=AZUL_MEDIO, width=3.5),
                     marker=dict(size=6), hovertemplate="<b>Grupo BBVA</b> %{x}: %{y:.1f}% del stock<extra></extra>")
    fig2.update_layout(yaxis=dict(title="Participación en el stock (%)", ticksuffix="%", rangemode="tozero"))
    rk_mes = {m: puesto_bbva(ev[ev.periodo == m].groupby("grupo").nocional.sum()) for m in pers}
    evol_bbva = pd.DataFrame({"mes": x, "stock": [ev[(ev.periodo == m) & (ev.grupo == GRUPO_BBVA)].nocional.sum()
                                                  for m in pers],
                              "share": sb.values, "rank": [rk_mes[m][0] for m in pers],
                              "n": [rk_mes[m][1] for m in pers]})

    # participacion por instrumento: top 5 + BBVA en filas, instrumentos en columnas
    fams = [f for f in FAMILIAS if f in set(v.instrumento)]
    mat = pd.DataFrame({"grupo": grupos.grupo.tolist()})
    for f in fams:
        gf = v[v.instrumento == f].groupby("grupo").nocional.sum()
        mat[f] = mat.grupo.map(gf / gf.sum() * 100).fillna(0.0)
    mat["Total"] = mat.grupo.map(g_st / stock * 100).fillna(0.0)
    fila_rk = {"grupo": "Puesto del Grupo BBVA"}
    for f in fams + ["Total"]:
        r_, n_ = puesto_bbva(v[v.instrumento == f].groupby("grupo").nocional.sum() if f != "Total" else g_st)
        fila_rk[f] = f"{r_} de {n_}" if r_ else "sin posición"
    mat["_fila"] = np.where(mat.grupo == GRUPO_BBVA, "bbva", "")
    mat_txt = mat.copy()
    for c in fams + ["Total"]:
        mat_txt[c] = mat[c].map(lambda z: pct(z))
    mat_txt = pd.concat([mat_txt, pd.DataFrame([fila_rk | {"_fila": "total"}])], ignore_index=True)
    tabla_mat = tabla(mat_txt, [("Grupo contraparte", "grupo", "txt")]
                      + [(NOMBRE_FAM[f], f, "txt") for f in fams] + [("Total derivados", "Total", "txt")],
                      "share num-txt", "Participación de cada grupo en el stock vigente de cada instrumento. Filas: top 5 "
                                       "del stock total y el Grupo BBVA.")

    # perfil de vencimientos: stock por plazo residual y participacion de BBVA en cada tramo
    vr = v.assign(resid=(v.fecha_vencimiento - pd.Timestamp(d.corte)).dt.days)
    vr["tramo"] = pd.cut(vr.resid, TRAMOS_DIAS, labels=TRAMOS, right=True)
    tr = vr.pivot_table(index="tramo", columns="instrumento", values="nocional", aggfunc="sum", fill_value=0.0,
                        observed=False).reindex(TRAMOS, fill_value=0.0)
    tr = tr.loc[tr.sum(axis=1) > 0]
    tb = vr[vr.grupo == GRUPO_BBVA].groupby("tramo", observed=False).nocional.sum().reindex(tr.index, fill_value=0.0)
    sh_b = tb / tr.sum(axis=1) * 100
    fig3 = _fig(340)
    for f in [f for f in FAMILIAS if f in tr.columns]:
        fig3.add_bar(x=list(tr.index), y=tr[f], name=NOMBRE_FAM[f], marker_color=COLOR_FAM[f],
                     hovertemplate=f"<b>{NOMBRE_FAM[f]}</b> %{{x}}: %{{y:,.1f}} MM USD<extra></extra>")
    fig3.add_scatter(x=list(tr.index), y=sh_b, name="Participación Grupo BBVA en el tramo (%)", yaxis="y2",
                     mode="lines+markers+text", text=[pct(z) for z in sh_b], textposition="top center",
                     textfont=dict(color=AZUL_MEDIO, size=10), line=dict(color=AZUL_MEDIO, width=3), marker=dict(size=7),
                     hovertemplate="%{x}: Grupo BBVA %{y:.1f}% del tramo<extra></extra>")
    r_barras, r_linea = rangos_doble_eje(tr.sum(axis=1).max(), sh_b)
    fig3.update_layout(barmode="stack", margin=dict(l=70, r=80, t=40, b=50),
                       legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, traceorder="normal"),
                       yaxis=dict(title="MM USD de nocional vigente", tickformat=",.0f", range=r_barras),
                       yaxis2=dict(title="Grupo BBVA en el tramo", overlaying="y", side="right", showgrid=False,
                                   showticklabels=False, zeroline=False, range=r_linea,
                                   title_font=dict(color=AZUL_MEDIO)),
                       xaxis=dict(title="Plazo residual: del corte al vencimiento"))

    # flujo del mes, solo para el drill-down (columna de originacion)
    mes = v[v.mes_origen == p]
    m12 = [mes_atras(p, k) for k in range(12)]
    return {"graf_share_stock": html_fig(fig, "g_share_stock"), "graf_share_evol": html_fig(fig2, "g_share_evol"),
            "resto_txt": (f"{resto.grupo.iloc[0]}: {pct(resto.share.iloc[0])} del stock." if len(resto) else ""),
            "tabla_evol_bbva": _tabla_evol_bbva(evol_bbva.tail(12)),
            "tabla_mat": tabla_mat, "graf_vencimientos": html_fig(fig3, "g_vencimientos"),
            "_share": t, "_mat": mat, "_evol_bbva": evol_bbva, **_bbva(d, mes.groupby("grupo").nocional.sum(), m12)}


def _tabla_evol_bbva(e: pd.DataFrame) -> Markup:
    """Grupo BBVA mes a mes en el stock: meses en columnas."""
    filas = [{"concepto": "Stock Grupo BBVA (MM USD)", **{m: num(x, 1) for m, x in zip(e.mes, e.stock)}},
             {"concepto": "Participación en el stock", **{m: pct(x) for m, x in zip(e.mes, e.share)}},
             {"concepto": "Puesto", **{m: (f"{int(r)} de {n}" if not pd.isna(r) else "\u2013")
                                      for m, r, n in zip(e.mes, e["rank"], e.n)}}]
    df = pd.DataFrame(filas)
    df["_fila"] = ["", "bbva", ""]
    return tabla(df, [("", "concepto", "txt")] + [(m, m, "txt") for m in e.mes], "compacta num-txt",
                 "Cada cierre a su dólar. Puesto entre los grupos con posición vigente ese mes.")


# ---------------------------------------------------------------------------
#  3. flujo: originacion del mes y de 12 meses (complemento del stock)
# ---------------------------------------------------------------------------

def seccion_flujo(d: Datos) -> dict:
    p = d.periodo
    mes = d.vig[d.vig.mes_origen == p]
    tot = mes.nocional.sum()
    m12 = [mes_atras(p, k) for k in range(11, -1, -1)]
    o12 = d.orig[d.orig.mes_origen.isin(m12)]
    serie = d.orig.groupby("mes_origen").nocional.sum()
    prev = serie.get(mes_atras(p, 1), np.nan)

    def rk(df: pd.DataFrame, col_noc: str) -> pd.DataFrame:
        g = df.groupby("grupo").agg(noc=(col_noc, "sum"))
        return top_mas_bbva(g, "noc", 5)

    cols = [("#", "rank", "int"), ("Grupo contraparte", "grupo", "txt"), ("MM USD", "noc", "mm"), ("Part. %", "share", "pct")]
    r_mes, r_12 = rk(mes, "nocional"), rk(o12, "nocional")
    tabla_mes = tabla(r_mes, cols, "compacta", f"Operaciones vigentes con fecha de operación en {mes_corto(p)}.")
    tabla_12 = tabla(r_12, cols, "compacta", f"Originado de {mes_corto(m12[0])} a {mes_corto(p)}, al dólar del cierre "
                                             "en que se informó; incluye lo que ya venció.")

    comp = (o12.pivot_table(index="mes_origen", columns="instrumento", values="nocional", aggfunc="sum", fill_value=0.0)
            .reindex(index=m12, fill_value=0.0))
    fams_c = [f for f in FAMILIAS if f in comp.columns]
    fig = _fig(320)
    for f in fams_c:
        fig.add_bar(x=[mes_corto(m) for m in m12], y=comp[f], name=NOMBRE_FAM[f], marker_color=COLOR_FAM[f],
                    hovertemplate=f"<b>{NOMBRE_FAM[f]}</b> %{{x}}: %{{y:,.1f}} MM USD<extra></extra>")
    totales = comp[fams_c].sum(axis=1)
    fig.add_scatter(x=[mes_corto(m) for m in m12], y=totales, mode="text", text=[num(z, 0) for z in totales],
                    textposition="top center", textfont=dict(size=11, color=TEXTO), showlegend=False, hoverinfo="skip")
    fig.update_layout(barmode="stack", legend=dict(traceorder="normal"),
                      yaxis=dict(title="MM USD de nocional originado", tickformat=",.0f", range=[0, totales.max() * 1.12]))
    return {"tabla_flujo_mes": tabla_mes, "tabla_flujo_12m": tabla_12, "graf_flujo": html_fig(fig, "g_flujo"),
            "tot": num(tot, 1), "ops": num(len(mes), 0), "var_prev": pct((tot / prev - 1) * 100, 1, True),
            "mes_prev": mes_corto(mes_atras(p, 1)), "desde12": mes_corto(m12[0]),
            "_tot": tot, "_r_mes": r_mes, "_r_12": r_12}


def _bbva(d: Datos, g_mes: pd.Series, m12: list[int]) -> dict:
    """Drill-down por entidad legal del Grupo BBVA, con las entidades de nombre
    BBVA que no consolidan como referencia."""
    p = d.periodo
    todas = pd.concat([d.vig[["grupo", "entidad", "entidad_id", "pais"]],
                       d.orig[["grupo", "entidad", "entidad_id"]].assign(pais=None)])
    es_grupo = todas.grupo == GRUPO_BBVA
    ref = ~es_grupo & todas.entidad.str.upper().str.contains("BBVA|BILBAO", regex=True)
    ents = (todas[es_grupo | ref].sort_values("pais", na_position="last")
            .drop_duplicates("entidad_id").reset_index(drop=True))
    filas = []
    for e in ents.itertuples():
        v = d.vig[d.vig.entidad_id == e.entidad_id]
        o = d.orig[d.orig.entidad_id == e.entidad_id]
        filas.append({
            "entidad": e.entidad, "entidad_id": e.entidad_id, "pais": e.pais or "\u2013", "grupo": e.grupo,
            "consolida": "Sí" if e.grupo == GRUPO_BBVA else "No",
            "mes": o[o.mes_origen == p].nocional.sum(), "m12": o[o.mes_origen.isin(m12)].nocional.sum(),
            "ventana": o.nocional.sum(), "stock": v.nocional.sum(), "ops": len(v),
            "clientes": v.aseguradora.nunique(), "ultima": v.fecha_operacion.max(),
            "_fila": "bbva" if e.grupo == GRUPO_BBVA else "ref"})
    ent = (pd.DataFrame(filas).assign(_o=lambda x: x._fila != "bbva")
           .sort_values(["_o", "stock"], ascending=[True, False]).drop(columns="_o"))
    desde = mes_corto(d.orig.mes_origen.min())
    tabla_ent = tabla(ent, [("Entidad legal", "entidad", "txt"), ("ID legal", "entidad_id", "txt"),
                            ("País", "pais", "txt"), ("Consolida", "consolida", "txt"),
                            (f"Orig. {mes_corto(p)}", "mes", "mm"), ("Orig. 12M", "m12", "mm"),
                            (f"Orig. desde {desde}", "ventana", "mm"), ("Stock vigente", "stock", "mm"),
                            ("Oper. vivas", "ops", "int"), ("Aseg.", "clientes", "int"),
                            ("Última oper.", "ultima", "fecha")],
                      "bbva-ent", "MM USD de nocional. Última oper.: fecha de operación más reciente entre las "
                                  "operaciones vigentes.")

    b = d.vig[d.vig.grupo == GRUPO_BBVA]
    ob = d.orig[(d.orig.grupo == GRUPO_BBVA) & d.orig.mes_origen.isin(m12)]
    inst = (b.groupby(["instrumento", "subyacente"]).agg(stock=("nocional", "sum"), ops=("nocional", "size"),
                                                         ultima=("fecha_operacion", "max"))
            .join(ob.groupby(["instrumento", "subyacente"]).nocional.sum().rename("m12"), how="outer")
            .reset_index())
    inst[["stock", "m12"]] = inst[["stock", "m12"]].fillna(0.0)
    inst["ops"] = inst.ops.fillna(0)
    inst["s_stock"] = [st / d.vig[(d.vig.instrumento == i) & (d.vig.subyacente == s)].nocional.sum() * 100
                       for i, s, st in zip(inst.instrumento, inst.subyacente, inst.stock)]
    inst["instrumento"] = inst.instrumento.map(lambda f: NOMBRE_FAM.get(f, f))
    inst = inst.sort_values(["stock", "m12"], ascending=False)
    tabla_inst = tabla(inst, [("Instrumento", "instrumento", "txt"), ("Subyacente", "subyacente", "txt"),
                              ("Stock vigente", "stock", "mm"), ("Oper. vivas", "ops", "int"),
                              ("Part. BBVA en el stock del subyacente", "s_stock", "pct"),
                              ("Originado 12M", "m12", "mm"), ("Última oper. vigente", "ultima", "fecha")],
                       "compacta", "MM USD de nocional. Originado 12M incluye operaciones que ya vencieron.")
    cli = (b.groupby("aseguradora").agg(stock=("nocional", "sum"), ops=("nocional", "size"),
                                        inst=("instrumento", lambda x: ", ".join(NOMBRE_FAM[f] for f in FAMILIAS
                                                                                if f in set(x))),
                                        ultima=("fecha_operacion", "max"))
           .sort_values("stock", ascending=False).reset_index())
    cli["share"] = cli.stock / b.nocional.sum() * 100
    tabla_cli = tabla(cli, [("Aseguradora", "aseguradora", "txt"), ("Stock vigente con BBVA", "stock", "mm"),
                            ("% del stock BBVA", "share", "pct"), ("Oper. vivas", "ops", "int"),
                            ("Instrumentos", "inst", "txt"), ("Última oper.", "ultima", "fecha")],
                      "compacta", "MM USD de nocional.")

    en_grupo, refs = ent[ent._fila == "bbva"], ent[ent._fila == "ref"]
    hechos = []
    if len(en_grupo) == 1:
        e = en_grupo.iloc[0]
        hechos.append(f"La única entidad legal del Grupo BBVA que aparece como contraparte en el B.7 es {e.entidad} "
                      f"({e.entidad_id}). Una sucursal no es una persona jurídica distinta: una operación con BBVA "
                      "Nueva York o con Madrid se informa con ese mismo identificador, así que los datos CMF no "
                      "permiten separarlas. No aparece ninguna filial del grupo con RUT o LEI propio.")
    elif len(en_grupo) > 1:
        hechos.append(f"El Grupo BBVA aparece con {len(en_grupo)} entidades legales: "
                      + "; ".join(f"{e.entidad} ({e.entidad_id})" for e in en_grupo.itertuples()) + ".")
    for e in refs.itertuples():
        hechos.append(f"{e.entidad_id} figura en el catálogo como «{e.grupo}» y no consolida en el Grupo BBVA. "
                      + (f"Stock vigente {num(e.stock, 1)} MM USD en {e.ops} operaciones de {e.clientes} "
                         f"aseguradoras; la más reciente es del {e.ultima:%d-%m-%Y}." if e.ops else
                         "Sin operaciones vigentes."))
    ult = b.fecha_operacion.max()
    if g_mes.get(GRUPO_BBVA, 0.0) == 0 and not pd.isna(ult):
        meses_sin = (p // 100 * 12 + p % 100) - (ult.year * 12 + ult.month)
        hechos.append(f"Sin originación en {mes_corto(p)}: la operación vigente más reciente del grupo es del "
                      f"{ult:%d-%m-%Y}, {meses_sin} meses antes del corte.")
    return {"tabla_bbva_ent": tabla_ent, "tabla_bbva_inst": tabla_inst, "tabla_bbva_cli": tabla_cli,
            "hechos_bbva": hechos}


# ---------------------------------------------------------------------------
#  3. plazos de la originacion del mes
# ---------------------------------------------------------------------------

def seccion_plazos(d: Datos, min_part: float = 5.0, min_ops: int = 10) -> dict:
    p = d.periodo
    mes = d.vig[d.vig.mes_origen == p].copy()
    tot = mes.nocional.sum()
    mes["tramo"] = pd.cut(mes.plazo_dias, TRAMOS_DIAS, labels=TRAMOS, right=True)
    tramos = (mes.groupby(["instrumento", "subyacente", "tramo"], observed=True)
              .agg(noc=("nocional", "sum"), ops=("nocional", "size")).reset_index())
    resumen, graficos = [], []
    claves = (mes.groupby(["instrumento", "subyacente"]).nocional.sum().sort_values(ascending=False))
    for (fam, sub), noc in claves.items():
        m = mes[(mes.instrumento == fam) & (mes.subyacente == sub)]
        met = metrica(fam, sub)
        tasa = _ponderada(m.tasa, m.nocional) if met else np.nan
        resumen.append({"instrumento": NOMBRE_FAM[fam], "subyacente": sub, "ops": len(m), "noc": noc,
                        "share": noc / tot * 100, "plazo": _ponderada(m.plazo_dias / 365.25, m.nocional),
                        "tasa": num(tasa, 0 if fam == "CCS" else 2) if met else "–",
                        "metrica": met[0] if met else "Sin tasa comparable",
                        "con_tasa": int(m.tasa.notna().sum()) if met else 0})
        if noc / tot * 100 < min_part or len(m) < min_ops:
            continue
        g = (m.groupby("tramo", observed=True)
             .apply(lambda x: pd.Series({"noc": x.nocional.sum(), "ops": len(x),
                                         "tasa": _ponderada(x.tasa, x.nocional) if met else np.nan,
                                         "con_tasa": int(x.tasa.notna().sum())}), include_groups=False)
             .reset_index())
        fig = _fig(300)
        fig.add_bar(x=g.tramo.astype(str), y=g.noc, name="Nocional originado (MM USD)", marker_color=AZUL_MEDIO,
                    text=[num(v, 1) for v in g.noc], textposition="inside", insidetextanchor="start",
                    textfont=dict(color="white", size=11), cliponaxis=False,
                    customdata=np.stack([g.ops], axis=-1),
                    hovertemplate="%{x}: %{y:,.1f} MM USD en %{customdata[0]} operaciones<extra></extra>")
        if met:
            fig.add_scatter(x=g.tramo.astype(str), y=g.tasa, name=met[0], yaxis="y2", mode="lines+markers+text",
                            line=dict(color=NARANJA, width=2.5), marker=dict(size=8),
                            text=[num(v, 0 if fam == "CCS" else (1 if fam == "Forward FX" else 2)) for v in g.tasa],
                            textposition="top center", textfont=dict(color=NARANJA_TEXTO, size=11),
                            customdata=np.stack([g.con_tasa], axis=-1),
                            hovertemplate=f"%{{x}}: %{{y:{met[1]}}}{met[2]} (%{{customdata[0]}} oper. con tasa)"
                                          "<extra></extra>")
        r_barras, r_linea = rangos_doble_eje(g.noc.max(), g.tasa if met else pd.Series(dtype=float))
        if met:
            fig.update_layout(yaxis2=dict(title=met[3], overlaying="y", side="right", showgrid=False,
                                          tickformat=met[1], ticksuffix=met[2], range=r_linea,
                                          title_font=dict(color=NARANJA_TEXTO), tickfont=dict(color=NARANJA_TEXTO)))
        else:
            r_barras = [0, g.noc.max() * 1.15]
        fig.update_layout(yaxis=dict(title="MM USD", tickformat=",.0f", range=r_barras),
                          xaxis=dict(title="Plazo original: de la fecha de operación al vencimiento"),
                          legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                          margin=dict(l=70, r=90, t=40, b=50))
        graficos.append({"titulo": f"{NOMBRE_FAM[fam]} {sub}",
                         "sub": f"{num(noc, 1)} MM USD en {len(m)} operaciones ({pct(noc / tot * 100)} del mes)"
                                + (f" · {met[0]} ponderada: {num(tasa, 0 if fam == 'CCS' else 2)}" if met else ""),
                         "grafico": html_fig(fig, f"g_plazo_{len(graficos)}")})
    res = pd.DataFrame(resumen)
    tabla_res = tabla(res, [("Instrumento", "instrumento", "txt"), ("Subyacente", "subyacente", "txt"),
                            ("Oper.", "ops", "int"), ("Nocional MM USD", "noc", "mm"), ("% del mes", "share", "pct"),
                            ("Plazo prom. pond. (años)", "plazo", "n1"), ("Tasa pond. al pactar", "tasa", "txt"),
                            ("Métrica de la tasa", "metrica", "txt"), ("Oper. con tasa", "con_tasa", "int")], "compacta",
                      "Ponderado por nocional. La tasa solo se promedia dentro de un mismo instrumento y subyacente.")
    return {"graficos_plazo": graficos, "tabla_plazos": tabla_res, "min_part": num(min_part, 0), "min_ops": min_ops,
            "_tramos": tramos,
            "ops_mes": len(mes), "noc_mes": num(tot, 1)}


# ---------------------------------------------------------------------------
#  4. camadas
# ---------------------------------------------------------------------------

def supervivencia(c: pd.DataFrame) -> pd.Series:
    """Curva de supervivencia agregada, encadenada mes a mes (tipo Kaplan-Meier).

    c: una fila por camada y meses desde el origen, con vivo y originado. En
    cada mes k el factor es vivo(k) / vivo(k-1) sumando SOLO las camadas que
    ya tienen el mes k observado; la curva es el producto de los factores. Asi
    los meses largos, que solo ven las camadas mas antiguas, no saltan por
    composicion. Se corta donde no queda nada vivo que seguir.
    """
    piv = c.pivot_table(index="mes_origen", columns="meses", values="vivo", aggfunc="sum")
    orig = c.groupby("mes_origen").originado.first()
    curva = {0: piv[0].sum() / orig.reindex(piv.index[piv[0].notna()]).sum() * 100}
    for k in range(1, int(piv.columns.max()) + 1):
        ok = piv[k].notna() & piv[k - 1].notna()
        den = piv.loc[ok, k - 1].sum()
        if den <= 0:
            break
        curva[k] = curva[k - 1] * piv.loc[ok, k].sum() / den
    return pd.Series(curva)


def seccion_camadas(d: Datos, min_ops: int = 30) -> dict:
    p = d.periodo
    res, cam = d.res, d.cam
    desde = mes_corto(res.mes_origen.min())

    def por_camada(f: str) -> pd.DataFrame:
        return (cam[cam.instrumento == f].groupby(["mes_origen", "meses"])
                .agg(vivo=("vivo", "sum"), originado=("originado", "sum")).reset_index())

    # resumen por instrumento (todas las camadas y subyacentes)
    fam = []
    for f in [x for x in FAMILIAS if x in set(res.instrumento)]:
        r = res[res.instrumento == f]
        curva = supervivencia(por_camada(f))
        bajo = curva[curva < 50]
        if r.operaciones.sum() < min_ops:
            vida = f"n/d (menos de {min_ops} oper.)"
        elif len(bajo):
            k = int(bajo.index[0])
            vida = f"{k} mes" if k == 1 else f"{k} meses"
        else:
            vida = f"más de {int(curva.index.max())} meses"
        fam.append({"instrumento": NOMBRE_FAM[f], "ops": r.operaciones.sum(), "orig": r.originado.sum(),
                    "vivas": r.ops_vivas.sum(), "vivo": r.vivo.sum(), "pct": r.vivo.sum() / r.originado.sum() * 100,
                    "vida": vida})
    fam = pd.DataFrame(fam)
    tot = {"instrumento": "Total", "ops": fam.ops.sum(), "orig": fam.orig.sum(), "vivas": fam.vivas.sum(),
           "vivo": fam.vivo.sum(), "pct": fam.vivo.sum() / fam.orig.sum() * 100, "vida": "", "_fila": "total"}
    fam = pd.concat([fam, pd.DataFrame([tot])], ignore_index=True)
    tabla_fam = tabla(fam, [("Instrumento", "instrumento", "txt"), ("Oper. originadas", "ops", "int"),
                            ("Nocional originado", "orig", "mm"), ("Oper. vivas hoy", "vivas", "int"),
                            ("Nocional vivo hoy", "vivo", "mm"), ("% vivo", "pct", "pct"),
                            ("Vida mediana observada", "vida", "txt")], "compacta",
                      f"MM USD, camadas {desde} a {mes_corto(p)}. Vida mediana: primer mes desde el origen en que la "
                      "curva de supervivencia encadenada baja de 50%.")

    # un bloque por instrumento principal: su subyacente de mayor volumen
    claves = []
    for f in ("CCS", "Forward FX"):
        r = res[res.instrumento == f]
        if len(r):
            claves.append((f, r.groupby("subyacente").originado.sum().idxmax()))
    bloques = []
    for f, s in claves:
        r = res[(res.instrumento == f) & (res.subyacente == s)].sort_values("mes_origen")
        met = metrica(f, s)
        x = [mes_corto(m) for m in r.mes_origen]
        fig = _fig(360)
        fig.add_bar(x=x, y=r.vivo, name="Vivo hoy", marker_color=AZUL,
                    customdata=np.stack([r.ops_vivas, r.pct_vivo], axis=-1),
                    hovertemplate="%{x}: %{y:,.1f} MM USD vivos (%{customdata[1]:.1f}%, %{customdata[0]} oper.)"
                                  "<extra></extra>")
        fig.add_bar(x=x, y=r.originado - r.vivo, name="Vencido o deshecho", marker_color="#B5C9E3",
                    hovertemplate="%{x}: %{y:,.1f} MM USD ya no vigentes<extra></extra>")
        if met:
            fig.add_scatter(x=x, y=r.tasa, name="Tasa al pactar: camada completa", yaxis="y2", mode="lines+markers",
                            line=dict(color=NARANJA, width=2.5), marker=dict(size=7),
                            hovertemplate=f"%{{x}}: %{{y:{met[1]}}}{met[2]}<extra>camada completa</extra>")
            fig.add_scatter(x=x, y=r.tasa_viva, name="Tasa al pactar: solo lo vivo hoy", yaxis="y2", mode="lines",
                            line=dict(color=NARANJA, width=1.5, dash="dot"),
                            hovertemplate=f"%{{x}}: %{{y:{met[1]}}}{met[2]}<extra>lo vivo hoy</extra>")
        r_barras, r_linea = rangos_doble_eje(r.originado.max(), pd.concat([r.tasa, r.tasa_viva]))
        if met:
            fig.update_layout(yaxis2=dict(title=met[3], overlaying="y", side="right", showgrid=False,
                                          tickformat=met[1], ticksuffix=met[2], range=r_linea,
                                          title_font=dict(color=NARANJA_TEXTO), tickfont=dict(color=NARANJA_TEXTO)))
        else:
            r_barras = [0, r.originado.max() * 1.1]
        fig.update_layout(barmode="stack", margin=dict(l=70, r=90, t=40, b=55),
                          legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, traceorder="normal"),
                          yaxis=dict(title="MM USD de nocional originado", tickformat=",.0f", range=r_barras))
        rt = r.copy()
        rt["mes"] = [mes_corto(m) for m in rt.mes_origen]
        rt["_fila"] = ""
        tr = {"mes": "Total", "operaciones": rt.operaciones.sum(), "originado": rt.originado.sum(),
              "tasa": _ponderada(rt.tasa, rt.originado), "ops_vivas": rt.ops_vivas.sum(), "vivo": rt.vivo.sum(),
              "pct_vivo": rt.vivo.sum() / rt.originado.sum() * 100, "tasa_viva": _ponderada(rt.tasa_viva, rt.vivo),
              "ops_tasa": rt.ops_tasa.sum(), "_fila": "total"}
        rt = pd.concat([rt, pd.DataFrame([tr])], ignore_index=True)
        ft = "pb" if f == "CCS" else ("n1" if f == "Forward FX" else "n2")
        tab = tabla(rt, [("Mes de origen", "mes", "txt"), ("Oper.", "operaciones", "int"),
                         ("Originado MM USD", "originado", "mm"), ("Tasa al pactar", "tasa", ft),
                         ("Oper. con tasa", "ops_tasa", "int"), ("Oper. vivas", "ops_vivas", "int"),
                         ("Vivo hoy MM USD", "vivo", "mm"), ("% vivo", "pct_vivo", "pct"),
                         ("Tasa de lo vivo", "tasa_viva", ft)], "compacta camada",
                    (f"Tasa: {met[0]}, ponderada por nocional originado; la de lo vivo, por nocional vivo."
                     if met else None))
        bloques.append({"titulo": f"{NOMBRE_FAM[f]} {s}", "grafico": html_fig(fig, f"g_camada_{len(bloques)}"),
                        "tabla": tab, "metrica": met[0] if met else "",
                        "sub": f"{num(r.originado.sum(), 1)} MM USD originados desde {desde}; vivos hoy "
                               f"{num(r.vivo.sum(), 1)} ({pct(r.vivo.sum() / r.originado.sum() * 100)})"})

    # curvas de decaimiento por instrumento (todas las camadas y subyacentes)
    decaimiento = []
    for f in [k[0] for k in claves]:
        c = por_camada(f)
        c["pct"] = c.vivo / c.originado * 100
        camadas = sorted(c.mes_origen.unique())
        colores = sample_colorscale("Blues", list(np.linspace(0.3, 1.0, len(camadas))))
        fig = _fig(340, 490)
        for m, col in zip(camadas, colores):
            cm = c[c.mes_origen == m]
            fig.add_scatter(x=cm.meses, y=cm.pct, mode="lines", line=dict(color=col, width=1.3), name=mes_corto(m),
                            showlegend=False, hovertemplate=f"<b>{mes_corto(m)}</b> mes %{{x}}: %{{y:.1f}}% vivo"
                                                            "<extra></extra>")
        curva = supervivencia(c)
        fig.add_scatter(x=curva.index, y=curva.values, mode="lines", name="Supervivencia encadenada",
                        line=dict(color=NARANJA, width=3, dash="dash"),
                        hovertemplate="Encadenada, mes %{x}: %{y:.1f}% vivo<extra></extra>")
        fig.update_layout(xaxis=dict(title="Meses desde el origen", dtick=3),
                          yaxis=dict(title="% del nocional originado que sigue vivo", ticksuffix="%",
                                     range=[0, 105]),
                          legend=dict(y=-0.2), margin=dict(l=65, r=15, t=20, b=70))
        decaimiento.append({"titulo": NOMBRE_FAM[f], "grafico": html_fig(fig, f"g_decae_{len(decaimiento)}")})

    # libro vigente de CCS por anio de origen: incluye lo pactado antes de la
    # ventana de camadas, que alli no se puede seguir
    v = d.vig[d.vig.instrumento == "CCS"].copy()
    corte_anio = 2016
    v["anio"] = np.where(v.fecha_operacion.dt.year <= corte_anio, f"{corte_anio} o antes",
                         v.fecha_operacion.dt.year.astype(str))
    anios = sorted(v.anio.unique(), key=lambda a: (not a.endswith("antes"), a))
    subs = v.groupby("subyacente").nocional.sum().sort_values(ascending=False).index.tolist()
    col_sub = [AZUL, AZUL_MEDIO, AZUL_CLARO, "#2DCCCD", "#9FB3C8", "#D8BE75", "#8F7AE5"]
    fig = _fig(360)
    for i, sb in enumerate(subs):
        serie = v[v.subyacente == sb].groupby("anio").nocional.sum().reindex(anios, fill_value=0.0)
        fig.add_bar(x=anios, y=serie, name=sb, marker_color=col_sub[i % len(col_sub)],
                    hovertemplate=f"<b>{sb}</b> %{{x}}: %{{y:,.1f}} MM USD<extra></extra>")
    principal = subs[0]
    met = metrica("CCS", principal)
    vp = v[v.subyacente == principal]
    linea = vp.groupby("anio").apply(lambda z: _ponderada(z.tasa, z.nocional), include_groups=False).reindex(anios)
    fig.add_scatter(x=anios, y=linea, name=f"{met[0]} ({principal}, ponderado)", yaxis="y2",
                    mode="lines+markers+text", text=[num(t, 0) for t in linea], textposition="top center",
                    textfont=dict(color=NARANJA_TEXTO, size=10), line=dict(color=NARANJA, width=2.5), marker=dict(size=7),
                    hovertemplate=f"%{{x}}: %{{y:{met[1]}}}{met[2]}<extra></extra>")
    tot_v = v.groupby("anio").nocional.sum().reindex(anios)
    r_barras, r_linea = rangos_doble_eje(tot_v.max(), linea)
    fig.update_layout(barmode="stack", margin=dict(l=70, r=90, t=40, b=50),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, traceorder="normal"),
                      yaxis=dict(title="MM USD vigentes (dólar del corte)", tickformat=",.0f", range=r_barras),
                      yaxis2=dict(title=met[3], overlaying="y", side="right", showgrid=False, tickformat=met[1],
                                  ticksuffix=met[2], range=r_linea,
                                  title_font=dict(color=NARANJA_TEXTO), tickfont=dict(color=NARANJA_TEXTO)),
                      xaxis=dict(title="Año de la fecha de operación"))
    libro = {"titulo": "CCS", "grafico": html_fig(fig, "g_libro_ccs"), "total": num(v.nocional.sum(), 1),
             "ops": num(len(v), 0), "principal": principal,
             "previo": num(v[v.mes_origen < res.mes_origen.min()].nocional.sum() / v.nocional.sum() * 100, 1)}

    o_tot, v_tot = res.originado.sum(), res.vivo.sum()
    return {"tabla_camadas_fam": tabla_fam, "bloques": bloques, "decaimiento": decaimiento, "libro": libro,
            "desde": desde, "orig_tot": num(o_tot, 1), "vivo_tot": num(v_tot, 1), "pct_tot": pct(v_tot / o_tot * 100),
            "n_camadas": res.mes_origen.nunique(), "min_ops": min_ops}


# ---------------------------------------------------------------------------
#  armado
# ---------------------------------------------------------------------------

def controles(d: Datos, resumen: dict) -> list[str]:
    """Cuadraturas internas que el informe muestra en su nota metodologica."""
    p = d.periodo
    a = d.vig[d.vig.mes_origen == p].nocional.sum()
    b = d.orig[d.orig.mes_origen == p].nocional.sum()
    c_res = d.res.groupby("mes_origen").originado.sum()
    c_org = d.orig.groupby("mes_origen").nocional.sum()
    dif_cam = float((c_res - c_org.reindex(c_res.index)).abs().max())
    vivas_res = d.res.ops_vivas.sum()
    vivas_vig = int((d.vig.mes_origen >= d.res.mes_origen.min()).sum())
    g_det = d.vig.groupby("grupo").nocional.sum()
    g_ev = d.evol[d.evol.periodo == p].groupby("grupo").nocional.sum()
    dif_st = float((g_det - g_ev.reindex(g_det.index).fillna(0.0)).abs().max())
    return [
        f"Stock vigente al {d.corte:%d-%m-%Y}: hojas de detalle {num(g_det.sum(), 2)} MM USD = tbl_evol_deriv_grupo "
        f"{num(g_ev.sum(), 2)} MM USD; diferencia máxima por grupo {num(dif_st, 4)}.",
        f"Originación de {mes_corto(p)}: hojas de detalle (operaciones vigentes) {num(a, 2)} MM USD = "
        f"tbl_originacion {num(b, 2)} MM USD (diferencia {num(abs(a - b), 4)}).",
        f"Originado por camada (tbl_camadas_resumen) = originación mensual (tbl_originacion): diferencia máxima "
        f"{num(dif_cam, 4)} MM USD.",
        f"Operaciones vivas hoy de las camadas {mes_corto(d.res.mes_origen.min())} en adelante: {num(vivas_res, 0)} "
        f"en tbl_camadas_resumen y {num(vivas_vig, 0)} en las hojas de detalle.",
        f"Filas de detalle con vencimiento anterior al corte, excluidas: {d.vencidas_excluidas}.",
    ]


def construir(xlsx: Path) -> tuple[str, Datos, dict]:
    d = cargar(xlsx)
    r = seccion_resumen(d)
    ctx = {
        "titulo": "Derivados de aseguradoras: stock, participación de mercado y camadas",
        "corte": f"{d.corte:%d-%m-%Y}", "mes": mes_corto(d.periodo), "periodo": d.periodo,
        "dolar": num(d.dolar, 2), "dolar_fecha": d.dolar_fecha, "publicacion": d.publicacion,
        "excel": d.excel.name, "generado": f"{_dt.datetime.now():%d-%m-%Y %H:%M}",
        "resumen": r, "mercado": seccion_mercado(d), "flujo": seccion_flujo(d), "plazos": seccion_plazos(d),
        "camadas": seccion_camadas(d),
        "controles": controles(d, r), "plotlyjs": Markup(get_plotlyjs()),
    }
    env = Environment(loader=FileSystemLoader(PLANTILLAS), autoescape=select_autoescape(["html"]))
    return env.get_template("informe.html").render(**ctx), d, ctx


def buscar_chrome() -> str | None:
    candidatos = [os.environ.get("CHROME"),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
                  shutil.which("msedge")]
    return next((c for c in candidatos if c and Path(c).exists()), None)


def a_pdf(html_path: Path, pdf_path: Path, chrome: str | None = None, espera: float = 180.0) -> Path:
    """Imprime el HTML a PDF con Chrome sin interfaz (A4 apaisado, lo fija el CSS).

    Chrome sin interfaz en macOS escribe el PDF en segundos pero a veces no
    termina el proceso: se espera a que el archivo exista y deje de crecer, y
    despues se cierra Chrome.
    """
    exe = chrome or buscar_chrome()
    if not exe:
        raise FileNotFoundError("No encontre Chrome/Chromium/Edge para el PDF: indicar la ruta con --chrome o "
                                "la variable CHROME, o usar --sin-pdf")
    pdf_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as perfil:
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               f"--user-data-dir={perfil}", "--use-mock-keychain", "--password-store=basic", "--disable-extensions",
               "--disable-background-networking", "--disable-component-update", "--disable-sync",
               "--no-pdf-header-footer", "--hide-scrollbars", "--run-all-compositor-stages-before-draw",
               "--virtual-time-budget=15000", f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        limite, tam = time.time() + espera, -1
        while time.time() < limite and proc.poll() is None:
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                if pdf_path.stat().st_size == tam:
                    break
                tam = pdf_path.stat().st_size
            time.sleep(1.0)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM) if hasattr(os, "killpg") else proc.terminate()
                proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
        err = proc.stderr.read()[-800:] if proc.stderr else ""
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RuntimeError(f"Chrome no genero el PDF: {err}")
    return pdf_path


def generar(xlsx: Path | None = None, salida: Path | None = None, pdf: bool = True,
            chrome: str | None = None) -> dict[str, Path]:
    if xlsx is None:
        libros = sorted(SALIDA.glob("stock_aseguradoras_*.xlsx"))
        if not libros:
            raise FileNotFoundError("No hay Excel en reportes/salida: correr antes 'python -m reportes'")
        xlsx = libros[-1]
    salida = salida or SALIDA
    salida.mkdir(parents=True, exist_ok=True)
    texto, d, _ = construir(Path(xlsx))
    base = salida / f"informe_derivados_{d.periodo}"
    rutas = {"html": base.with_suffix(".html")}
    rutas["html"].write_text(texto, encoding="utf-8")
    if pdf:
        rutas["pdf"] = a_pdf(rutas["html"], base.with_suffix(".pdf"), chrome)
    return rutas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Informe BI de derivados (HTML y PDF) desde el Excel del reporte.")
    ap.add_argument("--excel", type=Path, help="libro de 'python -m reportes'; por defecto el mas reciente")
    ap.add_argument("--salida", type=Path, help="carpeta de salida; por defecto reportes/salida")
    ap.add_argument("--sin-pdf", action="store_true", help="solo HTML")
    ap.add_argument("--chrome", help="ruta de Chrome/Chromium/Edge para el PDF")
    a = ap.parse_args(argv)
    t = time.time()
    rutas = generar(a.excel, a.salida, not a.sin_pdf, a.chrome)
    for k, v in rutas.items():
        print(f"{k.upper()}: {v}")
    print(f"Informe generado en {time.time() - t:.1f}s")
    return 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
