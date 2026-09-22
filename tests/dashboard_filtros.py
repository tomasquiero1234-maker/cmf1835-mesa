"""Regresion: los filtros del sidebar tienen que aplicarse a la tabla elegida.

El bug que motiva esta prueba: el Explorador Libre ramificaba solo por el nombre
del hecho crudo, asi que v_derivado_clasificado --la opcion por defecto-- caia al
`else` y recibia unicamente el filtro de periodo. El usuario seleccionaba
"grupo de contraparte = BBVA", los KPI de arriba respondian y la tabla de abajo
seguia mostrando 28.456 filas de todos los grupos.

Dos controles, ambos sobre el SQL que el explorador arma de verdad:

  1. Toda tabla del desplegable produce SQL valido (una vista que no expone la
     columna de un filtro no puede reventar la consulta).
  2. Un filtro restrictivo REALMENTE recorta: se compara contra la consulta sin
     filtro y se verifica que la columna filtrada quede con un solo valor.

Se corre contra la base que apunte CMF1835_DB, o la de produccion si no esta.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.dashboard as D  # noqa: E402

#: Mismo mapa que arma vista_explorador(). Si alla se agrega una tabla y aca no,
#: el control 1 deja de cubrirla; por eso se comparan los dos al final.
TABLAS = {
    "Derivados (clasificados)": "v_derivado_clasificado",
    "Renta fija (clasificada)": "v_renta_fija_clasificada",
    "Derivados (crudo)": "fact_derivado",
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


def _predicado(f, tabla, pers):
    """Replica exactamente la cadena de ramas de vista_explorador()."""
    col_per = "periodo" if tabla == "fact_flujo" else "periodo_informacion"
    partes = [f"d.{col_per} IN ({', '.join(str(p) for p in pers)})" if pers else "1=0"]
    D._OMITIDOS.clear()
    if tabla in ("fact_derivado", "v_derivado_clasificado"):
        extra = D.predicados_derivado(f, periodos=pers, tabla=tabla)
    elif tabla in ("fact_renta_fija", "v_renta_fija_clasificada"):
        extra = D.predicados_rf(f, tabla=tabla, periodos=pers)
    elif tabla == "fact_garantia":
        extra = D.predicados_garantia(f, periodos=pers)
    else:
        extra = D.where(partes)
    if tabla in ("fact_equity", "fact_fondo", "fact_extranjero_rf",
                 "fact_extranjero_rv", "fact_otras_inv", "fact_control"):
        extra = D.where(partes + [D.cl_in(tabla, "rut_compania", f.get("companias"),
                                          f.get("_companias_all"))])
    return extra, list(D._OMITIDOS)


def _base(periodos):
    """Filtros 'sin recortar': todo seleccionado, como arranca el sidebar."""
    return {"periodos": periodos}


def main() -> int:
    con = D.conectar()
    pers = [r[0] for r in con.execute(
        "SELECT DISTINCT periodo_informacion FROM fact_derivado "
        "ORDER BY 1 DESC LIMIT 3").fetchall()]
    print(f"base    : {os.environ.get('CMF1835_DB', '(produccion)')}")
    print(f"periodos: {pers}\n")

    fallas = []

    # -- Control 1: toda tabla del desplegable produce SQL valido --------------
    print("[1] SQL valido para cada tabla del desplegable")
    grupo = con.execute(
        "SELECT contraparte_grupo FROM fact_derivado "
        "WHERE contraparte_grupo IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC "
        "LIMIT 1").fetchone()[0]
    universo_g = [r[0] for r in con.execute(
        "SELECT DISTINCT contraparte_grupo FROM fact_derivado "
        "WHERE contraparte_grupo IS NOT NULL").fetchall()]
    f = _base(pers) | {"grupos": [grupo], "_grupos_all": universo_g}

    for etiqueta, tabla in TABLAS.items():
        if not D.existe(tabla):
            print(f"    -  {tabla:28} (ausente, se omite)")
            continue
        extra, _ = _predicado(f, tabla, pers)
        sql = f"SELECT count(*) FROM {tabla} d {extra}"
        try:
            n = con.execute(sql).fetchone()[0]
            print(f"    OK {tabla:28} {n:>10,} filas")
        except Exception as e:  # noqa: BLE001
            fallas.append(f"{tabla}: {type(e).__name__}: {e}")
            print(f"    XX {tabla:28} {type(e).__name__}: {e}")

    # -- Control 2: el filtro efectivamente recorta ----------------------------
    # Este es el control que el bug rompia: antes, 'con filtro' y 'sin filtro'
    # daban el MISMO numero en las vistas clasificadas.
    print("\n[2] un filtro restrictivo recorta de verdad")
    casos = [
        ("v_derivado_clasificado", "contraparte_grupo",
         {"grupos": [grupo], "_grupos_all": universo_g}),
        ("fact_derivado", "contraparte_grupo",
         {"grupos": [grupo], "_grupos_all": universo_g}),
    ]
    rut = con.execute(
        "SELECT rut_compania FROM fact_renta_fija GROUP BY 1 "
        "ORDER BY count(*) DESC LIMIT 1").fetchone()[0]
    ruts = [r[0] for r in con.execute(
        "SELECT DISTINCT rut_compania FROM fact_renta_fija").fetchall()]
    casos += [
        ("v_renta_fija_clasificada", "rut_compania",
         {"companias": [rut], "_companias_all": ruts}),
    ]
    # Moneda en la vista de RF prueba el alias unidad_monetaria -> moneda.
    mon = con.execute(
        "SELECT unidad_monetaria FROM fact_renta_fija WHERE unidad_monetaria "
        "IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 1").fetchone()[0]
    mons = [r[0] for r in con.execute(
        "SELECT DISTINCT unidad_monetaria FROM fact_renta_fija "
        "WHERE unidad_monetaria IS NOT NULL").fetchall()]
    casos += [
        ("v_renta_fija_clasificada", "moneda",
         {"unidades": [mon], "_unidades_all": mons}),
    ]

    for tabla, col, sel in casos:
        if not D.existe(tabla):
            continue
        sin, _ = _predicado(_base(pers), tabla, pers)
        con_f, omit = _predicado(_base(pers) | sel, tabla, pers)
        n0 = con.execute(f"SELECT count(*) FROM {tabla} d {sin}").fetchone()[0]
        n1 = con.execute(f"SELECT count(*) FROM {tabla} d {con_f}").fetchone()[0]
        # El control duro: ninguna fila devuelta puede estar fuera del filtro.
        fuera = con.execute(
            f"SELECT count(*) FROM {tabla} d {con_f} "
            f"{'AND' if con_f else 'WHERE'} d.{col} IS DISTINCT FROM "
            f"{D._lit(list(sel.values())[0][0])}").fetchone()[0]
        ok = n1 < n0 and fuera == 0 and not omit
        print(f"    {'OK' if ok else 'XX'} {tabla:26} {col:18} "
              f"{n0:>9,} -> {n1:>9,}  fuera={fuera}"
              + (f"  OMITIDOS={omit}" if omit else ""))
        if not ok:
            fallas.append(
                f"{tabla}.{col}: sin={n0} con={n1} fuera={fuera} omitidos={omit}")

    # -- Control 3: el mapa de tablas de la prueba sigue al del dashboard ------
    print("\n[3] el desplegable del dashboard no tiene tablas fuera de esta prueba")
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "dashboard.py")).read()
    bloque = src.split("TABLAS = {", 1)[1].split("}", 1)[0]
    vivas = set(re.findall(r'"([a-z_0-9]+)"\s*,?\s*$', bloque, re.M)) | \
        set(re.findall(r':\s*"([a-z_0-9]+)"', bloque))
    faltan = vivas - set(TABLAS.values())
    print(f"    {'OK' if not faltan else 'XX'} {len(vivas)} tablas en el dashboard"
          + (f"  SIN CUBRIR: {sorted(faltan)}" if faltan else ""))
    if faltan:
        fallas.append(f"tablas sin cubrir en la prueba: {sorted(faltan)}")

    print("\n" + ("TODO OK" if not fallas else f"{len(fallas)} FALLAS"))
    for x in fallas:
        print("   -", x)
    return 1 if fallas else 0


if __name__ == "__main__":
    raise SystemExit(main())
