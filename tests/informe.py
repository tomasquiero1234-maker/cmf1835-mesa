"""Pruebas del informe BI de derivados (reportes/informe.py).

Cada control verifica una afirmacion del informe contra el Excel, leido aca por
un camino INDEPENDIENTE (pandas por hoja, no el lector por nombre del informe):

  1. el informe lee SOLO el Excel: corre en un proceso donde importar duckdb,
     reportes.datos o reportes.exportar falla;
  2. salidas: HTML con sus 5 secciones y sus graficos, PDF A4 apaisado;
  3. originacion del mes: detalle vigente = tbl_originacion = lo que muestra;
  4. league table y participaciones: suman el total y el 100%;
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
HOJAS_DERIV = {"CCS": "CCS", "Swap Promesa": "Swap_Promesa", "Forward FX": "Forward_FX", "Forward UF": "Forward_UF",
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
        det.append(pd.DataFrame({"familia": fam, "grupo": t["Grupo contraparte"],
                                 "noc": pd.to_numeric(t["Nocional (MM USD)"]),
                                 "fo": pd.to_datetime(t["Fecha operacion"]),
                                 "plazo": pd.to_numeric(t["Plazo original (dias)"])}))
    det = pd.concat(det, ignore_index=True)
    det["mes"] = det.fo.dt.year * 100 + det.fo.dt.month
    orig = hoja(xlsx, "Originacion")
    res = hoja(xlsx, "Camadas_Resumen")
    cam = hoja(xlsx, "Camadas")

    print("\n[3] originacion del mes")
    ind = det[det.mes == p].noc.sum()
    tb = pd.to_numeric(orig[orig["Mes origen"] == p]["Nocional originado (MM USD)"]).sum()
    r_res = I.seccion_resumen(d)
    check(abs(ind - tb) < 1e-6 and abs(r_res["_tot"] - ind) < 1e-6,
          f"{I.mes_corto(p)}: detalle {ind:,.4f} = tbl_originacion {tb:,.4f} = informe {r_res['_tot']:,.4f} MM USD")
    por_fam = det[det.mes == p].groupby("familia").noc.sum()
    por_fam_o = orig[orig["Mes origen"] == p].groupby("Instrumento")["Nocional originado (MM USD)"].sum()
    check((por_fam - por_fam_o.reindex(por_fam.index)).abs().max() < 1e-6, "y lo mismo instrumento por instrumento")

    print("\n[4] league table y participaciones")
    mes = d.vig[d.vig.mes_origen == p]
    lt, fams = I._league(mes)
    filas = lt[lt._fila != "total"]
    tot_row = lt[lt._fila == "total"].iloc[0]
    check(abs(filas.Total.sum() - ind) < 1e-6 and abs(tot_row.Total - ind) < 1e-6 and abs(filas.share.sum() - 100) < 1e-9,
          f"top + BBVA + resto = total industria ({ind:,.1f}) y 100% de participacion")
    check(abs(tot_row[fams].sum() - ind) < 1e-6, "la apertura por instrumento suma el total")
    sh = I.seccion_mercado(d)["_share"]
    g_ind = det[det.mes == p].groupby("grupo").noc.sum() / ind * 100
    check((sh.set_index("grupo").s_mes - g_ind.reindex(sh.grupo).fillna(0.0).values).abs().max() < 1e-9,
          "participacion del mes de cada grupo = recalculada desde las hojas de detalle")
    bbva = det[(det.mes == p) & (det.grupo == I.GRUPO_BBVA)].noc.sum() / ind * 100
    check(abs(sh.set_index("grupo").s_mes[I.GRUPO_BBVA] - bbva) < 1e-9, f"Grupo BBVA en el mes: {bbva:.2f}%")

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
