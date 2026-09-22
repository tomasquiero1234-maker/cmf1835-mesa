"""Regresion: ninguna vista puede traerse el warehouse entero a pandas.

Streamlit Community Cloud mata el contenedor alrededor de 1 GB, y un proceso
muerto por OOM no deja traceback: la app solo muestra "Error running app" y el
log corta despues de "Uvicorn server started". Es la falla mas cara de
diagnosticar del proyecto, porque no se parece a un error.

El caso que motiva esta prueba: vista_renta_fija() traia las 643.433 filas de
v_renta_fija_clasificada --186 MB en un solo DataFrame-- para calcular cuatro
promedios y un groupby, y ademas graficaba cada fila como un punto. La seccion
por defecto tocaba 1453 MB de pico y la app no levantaba.

Se mide sobre el tamano de los DataFrames, no sobre el RSS del proceso: el RSS
depende de la maquina y del asignador, el tamano de un DataFrame no. Se
instrumenta DuckDB directamente para ver toda consulta, venga de donde venga.

    python3 -m tests.memoria
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb  # noqa: E402

#: Ninguna consulta suelta deberia superar esto. Las mas gordas legitimas son
#: los derivados clasificados de un periodo (~32 MB): el margen es amplio a
#: proposito, la prueba busca el orden de magnitud equivocado, no el ruido.
TOPE_CONSULTA_MB = 60.0

#: Suma de todo lo materializado al renderizar una seccion.
TOPE_SECCION_MB = 150.0

SECCIONES = ["Mercado y precios", "Oportunidades", "Flujos y garantias",
             "Explorador y copiloto"]

REG: list[tuple[float, int, int, str]] = []
_ULT = {"sql": ""}
_exec = duckdb.DuckDBPyConnection.execute
_fdf = duckdb.DuckDBPyConnection.fetch_df


def _execute(self, sql, *a, **k):
    _ULT["sql"] = sql if isinstance(sql, str) else str(sql)
    return _exec(self, sql, *a, **k)


def _fetch_df(self, *a, **k):
    df = _fdf(self, *a, **k)
    try:
        REG.append((df.memory_usage(deep=True).sum() / 1048576,
                    len(df), len(df.columns), " ".join(_ULT["sql"].split())[:200]))
    except Exception:  # noqa: BLE001
        pass
    return df


def main() -> int:
    duckdb.DuckDBPyConnection.execute = _execute
    duckdb.DuckDBPyConnection.fetch_df = _fetch_df
    from streamlit.testing.v1 import AppTest

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(raiz)
    print(f"base: {os.environ.get('CMF1835_DB', '(por defecto)')}")
    print(f"topes: {TOPE_CONSULTA_MB:.0f} MB por consulta, "
          f"{TOPE_SECCION_MB:.0f} MB por seccion\n")

    fallas = []
    for s in SECCIONES:
        REG.clear()
        at = AppTest.from_file(os.path.join(raiz, "app/dashboard.py"),
                               default_timeout=600)
        at.run()
        for rad in at.radio:
            if s in (rad.options or []):
                rad.set_value(s)
                break
        at.run()
        errs = [e.value for e in at.exception]
        total = sum(r[0] for r in REG)
        peor = max(REG, default=(0, 0, 0, ""))
        ok = total <= TOPE_SECCION_MB and peor[0] <= TOPE_CONSULTA_MB and not errs
        print(f"{'OK' if ok else 'XX'} {s:24} {len(REG):>3} consultas  "
              f"total={total:7.1f} MB  peor={peor[0]:6.1f} MB "
              f"({peor[1]:,} filas x {peor[2]} col)")
        if peor[0] > TOPE_CONSULTA_MB:
            print(f"     consulta: {peor[3][:150]}")
            fallas.append(f"{s}: una consulta materializa {peor[0]:.0f} MB "
                          f"({peor[1]:,} filas)")
        if total > TOPE_SECCION_MB:
            fallas.append(f"{s}: la seccion materializa {total:.0f} MB en total")
        if errs:
            fallas.append(f"{s}: excepcion {errs[0][:120]}")

    print("\n" + ("TODO OK" if not fallas else f"{len(fallas)} FALLAS"))
    for x in fallas:
        print("   -", x)
    return 1 if fallas else 0


if __name__ == "__main__":
    raise SystemExit(main())
