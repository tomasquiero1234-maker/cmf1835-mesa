"""Pruebas del informe BI de derivados (reportes/informe.py).

Cada control verifica una afirmacion del informe contra el Excel, leido aca por
un camino INDEPENDIENTE (pandas por hoja, no el lector por nombre del informe):

  1. el informe lee SOLO el Excel: corre en un proceso donde importar duckdb,
     reportes.datos o reportes.exportar falla;
  2. salidas: HTML con sus 5 secciones y sus graficos, PDF A4 apaisado;
  3. stock al corte: detalle = Evol_Deriv_Grupo = informe, grupo por grupo;
  4. todo ranking es top 5 + Grupo BBVA con su puesto real, y suma el total;
     todas las aseguradoras y las subsidiarias suman el stock;
  5. tramos de plazo: cada operacion del mes cae en exactamente un tramo;
  6. camadas: originado = originacion mensual, vivo <= originado, nunca sube
     entre fotos, y lo vivo en la ultima foto = lo vivo hoy del resumen;
  7. supervivencia encadenada sobre un caso calculado a mano;
  8. el dashboard no se toco.

    python -m tests.informe                      # genera un Excel nuevo (~45 s)
    python -m tests.informe --excel libro.xlsx   # usa uno existente
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from reportes import informe as I  # noqa: E402

FALLAS: list[str] = []
HOJAS_DERIV = {"CCS": "CCS", "Swap UF/CLP": "Swap_UF_CLP", "Forward FX": "Forward_FX", "Forward UF": "Forward_UF",
               "IRS": "IRS", "Opcion": "Opciones", "Futuro": "Futuros"}


def check(ok: bool, texto: str) -> None:
    print(f"  {'OK' if ok else 'XX'} {texto}")
    if not ok:
        FALLAS.append(texto)


def hoja(xlsx: Path, nombre: str) -> pd.DataFrame:
    """Tabla de una hoja por el contrato del libro: encabezado en la fila 6 y
    datos hasta la primera fila vacia (los totales van despues, fuera)."""
    df = pd.read_excel(xlsx, sheet_name=nombre, header=5)
    vacia = df.isna().all(axis=1)
    return df.iloc[: int(vacia.values.argmax()) if vacia.any() else len(df)].reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", type=Path)
    a = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp())
    if a.excel:
        xlsx = a.excel
    else:
        from reportes.exportar import generar as generar_excel
        xlsx = generar_excel(salida=tmp / "libro.xlsx")

    print("\n[1] el informe lee solo el Excel")
    guardia = ("import sys; from pathlib import Path\n"
               "for m in ('duckdb', 'reportes.datos', 'reportes.exportar'): sys.modules[m] = None\n"
               f"sys.path.insert(0, {str(ROOT)!r})\n"
               "from reportes.informe import generar\n"
               f"r = generar(Path({str(xlsx)!r}), Path({str(tmp / 'salida')!r}))\n"
               "print('|'.join(f'{k}={v}' for k, v in r.items()))")
    r = subprocess.run([sys.executable, "-c", guardia], capture_output=True, text=True, cwd=ROOT, timeout=600)
    check(r.returncode == 0, "genera HTML y PDF con duckdb, reportes.datos y reportes.exportar bloqueados"
          + ("" if r.returncode == 0 else f": {r.stderr[-400:]}"))
    rutas = dict(x.split("=", 1) for x in r.stdout.strip().splitlines()[-1].split("|")) if r.returncode == 0 else {}

    print("\n[2] salidas")
    html = Path(rutas.get("html", "")).read_text(encoding="utf-8") if rutas.get("html") else ""
    secciones = all(f'id="{s}"' in html for s in ("resumen", "mercado", "plazos", "camadas", "metodo"))
    graficos = html.count("Plotly.newPlot(")
    check(secciones and graficos >= 10, f"HTML con las 5 secciones y {graficos} graficos")
    visible = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    check(not re.search(r">\s*(nan|NaN|None)\s*%?\s*<", visible), "ninguna celda visible dice nan o None")
    pdf = Path(rutas.get("pdf", "")).read_bytes() if rutas.get("pdf") else b""
    paginas = len(re.findall(rb"/Type\s*/Page[^s]", pdf))
    caja = set(re.findall(rb"/MediaBox\s*\[([^\]]+)\]", pdf))
    a4_apaisado = bool(caja) and all(abs(float(c.split()[2]) - 841.92) < 1 and abs(float(c.split()[3]) - 594.96) < 1
                                     for c in caja)
    check(paginas >= 8 and a4_apaisado, f"PDF de {paginas} paginas, A4 apaisado")

    # --- lectura independiente del Excel ------------------------------------------------------
    d = I.cargar(xlsx)
    p = d.periodo
    det = []
    for fam, nombre in HOJAS_DERIV.items():
        t = hoja(xlsx, nombre)
        det.append(pd.DataFrame({"familia": fam, "grupo": t["Grupo contraparte"], "aseg": t["Aseguradora"],
                                 "id": t["ID legal contraparte"],
                                 "noc": pd.to_numeric(t["Nocional (MM USD)"]),
                                 "fo": pd.to_datetime(t["Fecha operacion"]),
                                 "plazo": pd.to_numeric(t["Plazo original (dias)"])}))
    det = pd.concat(det, ignore_index=True)
    det["mes"] = det.fo.dt.year * 100 + det.fo.dt.month
    orig = hoja(xlsx, "Originacion")
    res = hoja(xlsx, "Camadas_Resumen")
    cam = hoja(xlsx, "Camadas")

    print("\n[3] stock al corte (lo principal)")
    evg = hoja(xlsx, "Evol_Deriv_Grupo")
    stock_det = det.groupby("grupo").noc.sum()
    stock_ev = evg[evg.Periodo == p].groupby("Grupo contraparte")["Nocional (MM USD)"].sum()
    r_res = I.seccion_resumen(d)
    check(abs(stock_det.sum() - stock_ev.sum()) < 1e-6 and abs(r_res["_stock"] - stock_det.sum()) < 1e-6
          and (stock_det - stock_ev.reindex(stock_det.index)).abs().max() < 1e-6,
          f"stock {stock_det.sum():,.4f} MM USD: hojas de detalle = Evol_Deriv_Grupo = informe, grupo por grupo")
    ev_ag = hoja(xlsx, "Evol_Deriv_Aseguradora")
    meses_ev = evg.groupby("Mes")["Nocional (MM USD)"].sum()
    check(all(abs(meses_ev[m] - pd.to_numeric(ev_ag[f"{m} (MM USD)"]).sum()) < 1e-6 for m in meses_ev.index),
          f"Evol_Deriv_Grupo = Evol_Deriv_Aseguradora en los {len(meses_ev)} cierres")

    print("\n[4] rankings: top 5 + Grupo BBVA con su puesto real")
    rk = r_res["_ranking"]
    orden = stock_det.sort_values(ascending=False)
    puesto = list(orden.index).index(I.GRUPO_BBVA) + 1
    check(list(rk.grupo[:5]) == list(orden.index[:5]), f"top 5 del stock: {', '.join(orden.index[:5])}")
    fila_b = rk[rk.grupo == I.GRUPO_BBVA]
    check(len(fila_b) == 1 and int(fila_b["rank"].iloc[0]) == puesto
          and abs(fila_b.share.iloc[0] - orden[I.GRUPO_BBVA] / orden.sum() * 100) < 1e-9,
          f"Grupo BBVA en el ranking del stock: puesto {puesto} de {len(orden)}, {orden[I.GRUPO_BBVA] / orden.sum() * 100:.2f}%")
    cuerpo = rk[rk._fila != "total"]
    check(abs(cuerpo.Total.sum() - stock_det.sum()) < 1e-6 and abs(cuerpo.share.sum() - 100) < 1e-9,
          "top 5 + BBVA + resto = total del stock y 100%")
    sh = I.seccion_mercado(d)["_share"]
    check(I.GRUPO_BBVA in set(sh.grupo) and list(sh.grupo[:5]) == list(orden.index[:5]),
          "grafico de participacion: top 5 + Grupo BBVA")
    fl = I.seccion_flujo(d)
    ind = det[det.mes == p].noc.sum()
    tb = pd.to_numeric(orig[orig["Mes origen"] == p]["Nocional originado (MM USD)"]).sum()
    check(all(I.GRUPO_BBVA in set(t_.grupo) for t_ in (fl["_r_mes"], fl["_r_12"]))
          and abs(fl["_tot"] - ind) < 1e-6 and abs(ind - tb) < 1e-6,
          f"flujo de {I.mes_corto(p)}: detalle {ind:,.4f} = tbl_originacion {tb:,.4f}; rankings del mes y 12M con BBVA")
    b12 = fl["_r_12"][fl["_r_12"].grupo == I.GRUPO_BBVA]
    o12 = orig[orig["Mes origen"].isin([I.mes_atras(p, k) for k in range(12)])].groupby("Grupo contraparte")[
        "Nocional originado (MM USD)"].sum().sort_values(ascending=False)
    check(int(b12["rank"].iloc[0]) == list(o12.index).index(I.GRUPO_BBVA) + 1,
          f"Grupo BBVA en la originacion de 12 meses: puesto {int(b12['rank'].iloc[0])} de {len(o12)}")

    print("\n[4b] todas las aseguradoras y stock por subsidiaria")
    mer = I.seccion_mercado(d)
    ag = mer["_aseg"]
    con_b = set(det[det.grupo == I.GRUPO_BBVA].aseg)
    check(len(ag) == det.aseg.nunique() and abs(ag.total.sum() - stock_det.sum()) < 1e-6
          and (ag.bbva + ag.otros_bancos - ag.total).abs().max() < 1e-9 and int((ag.bbva > 0).sum()) == len(con_b),
          f"{len(ag)} aseguradoras, suman el stock; con BBVA {len(con_b)}; con BBVA + con otros bancos = total")
    sin_b = det[~det.aseg.isin(con_b)].noc.sum()
    check(abs(ag[ag.bbva == 0].total.sum() - sin_b) < 1e-6,
          f"stock de las aseguradoras sin BBVA: {sin_b:,.1f} MM USD, recalculado desde el detalle")
    cp = I.seccion_contrapartes(d)["_tabla"]
    grupos = cp[cp._fila.isin(["grupo", "grupo-bbva"])]
    subs = cp[cp._fila == "sub"]
    check(abs(grupos.stock.sum() - stock_det.sum()) < 1e-6 and len(grupos) == det.grupo.nunique(),
          f"{len(grupos)} grupos: sus filas suman el stock")
    ok_sub = True
    for gi in grupos.index:
        siguientes = cp.loc[gi + 1:]
        fin = siguientes.index[siguientes._fila != "sub"]
        hijos = cp.loc[gi + 1: (fin[0] - 1) if len(fin) else cp.index[-1]]
        hijos = hijos[hijos._fila == "sub"]
        if len(hijos) and abs(hijos.stock.sum() - cp.loc[gi, "stock"]) > 1e-6:
            ok_sub = False
    n_ent = len(subs) + int((~grupos.entidad.str.endswith("entidades legales")).sum())
    check(ok_sub and n_ent == det.id.nunique(),
          f"las subsidiarias suman su grupo y son {n_ent} entidades legales, una por RUT o LEI")

    print("\n[5] tramos de plazo")
    tr = I.seccion_plazos(d)["_tramos"]
    check(abs(tr.noc.sum() - ind) < 1e-6 and int(tr.ops.sum()) == int((det.mes == p).sum()),
          f"{int(tr.ops.sum())} operaciones del mes, cada una en un tramo; suman {tr.noc.sum():,.1f} MM USD")
    check(det[det.mes == p].plazo.notna().all() and (det[det.mes == p].plazo > 0).all(),
          "toda operacion del mes tiene plazo original positivo")

    print("\n[6] camadas")
    ro = res.groupby("Mes origen")["Nocional originado (MM USD)"].sum()
    oo = orig.groupby("Mes origen")["Nocional originado (MM USD)"].sum()
    check((ro - oo.reindex(ro.index)).abs().max() < 1e-6, "originado por camada = originacion mensual, mes a mes")
    check((cam["Nocional vivo (MM USD)"] <= cam["Nocional originado (MM USD)"] + 1e-9).all(), "vivo <= originado")
    k = ["Instrumento", "Subyacente", "Mes origen"]
    sube = cam.sort_values(k + ["Foto"]).groupby(k)["Nocional vivo (MM USD)"].diff().max()
    check(pd.isna(sube) or sube <= 1e-9, "lo vivo de una camada nunca sube entre fotos")
    ult = cam[cam.Foto == p].set_index(k)["Nocional vivo (MM USD)"]
    hoy = res.set_index(k)["Nocional vivo hoy (MM USD)"]
    check((ult - hoy.reindex(ult.index)).abs().max() < 1e-6 and len(ult) == len(hoy),
          "lo vivo en la ultima foto = 'Nocional vivo hoy' del resumen, camada por camada")
    desde = int(res["Mes origen"].min())
    vivas_det = det[det.mes >= desde].groupby("familia").size()
    vivas_res = res.groupby("Instrumento")["Operaciones vivas hoy"].sum()
    check((vivas_det - vivas_res.reindex(vivas_det.index).fillna(0)).abs().max() == 0,
          f"operaciones vivas hoy = operaciones vigentes con fecha desde {I.mes_corto(desde)} ({int(vivas_det.sum()):,})")

    print("\n[7] supervivencia encadenada")
    ej = pd.DataFrame({"mes_origen": [1, 1, 1, 2, 2], "meses": [0, 1, 2, 0, 1],
                       "vivo": [100.0, 50.0, 25.0, 100.0, 100.0], "originado": [100.0] * 5})
    s = I.supervivencia(ej)
    check(np.allclose(s.values, [100.0, 75.0, 37.5]), f"caso a mano: 100, 75, 37,5 -> {[round(float(v), 4) for v in s.values]}")

    print("\n[8] el dashboard no se toco")
    diff = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "app/dashboard.py"], cwd=ROOT)
    check(diff.returncode == 0, "app/dashboard.py sin cambios respecto del ultimo commit")

    print("\n" + ("TODO OK" if not FALLAS else f"{len(FALLAS)} FALLAS"))
    for f in FALLAS:
        print("   -", f)
    return 1 if FALLAS else 0


if __name__ == "__main__":
    raise SystemExit(main())
