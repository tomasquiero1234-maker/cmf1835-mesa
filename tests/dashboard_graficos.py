"""Regresion: el panel de graficos del explorador, instrumento por instrumento.

Cada grafico declara a que instrumentos aplica y que columnas necesita
pobladas. Esta prueba recorre TODA la matriz --tabla x instrumento x grafico--
y exige tres cosas:

  1. Ningun grafico ofrecido revienta al dibujarse.
  2. Ningun grafico se ofrece sobre un instrumento al que no le corresponde.
     Es el control que importa: la columna tasa_precio_contrato guarda un tipo
     de cambio en un forward y una tasa en un IRS, asi que ofrecer el grafico
     equivocado produce un grafico que miente, no un error.
  3. Un grafico que no se ofrece deja dicho por que.

    python3 -m tests.dashboard_graficos
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.dashboard as D  # noqa: E402

#: Graficos que NO pueden aparecer sobre cada instrumento, por unidades.
#: Un forward no tiene tasa fija ni cruce de monedas; un pacto tampoco.
PROHIBIDOS = {
    "FORWARD": {"Curva: tasa fija contra tenor", "Paga fija contra recibe fija",
                "Nocional por cruce de monedas", "Tasa contra plazo"},
    "PACTO": {"Curva: tasa fija contra tenor", "Paga fija contra recibe fija",
              "Nocional por cruce de monedas", "Precio pactado contra mercado"},
    "OPCION": {"Curva: tasa fija contra tenor", "Nocional por cruce de monedas",
               "Precio pactado contra mercado", "Tasa contra plazo"},
    "FUTURO": {"Curva: tasa fija contra tenor", "Nocional por cruce de monedas",
               "Precio pactado contra mercado", "Tasa contra plazo"},
    "CCS": {"Precio pactado contra mercado", "Tasa contra plazo"},
    "IRS": {"Precio pactado contra mercado", "Tasa contra plazo"},
}

TABLAS = ["v_derivado_clasificado", "fact_derivado",
          "v_renta_fija_clasificada", "fact_renta_fija"]


def main() -> int:
    con = D.conectar()
    pers = [r[0] for r in con.execute(
        "SELECT DISTINCT periodo_informacion FROM fact_derivado "
        "ORDER BY 1 DESC LIMIT 2").fetchall()]
    where = f" WHERE d.periodo_informacion IN ({', '.join(map(str, pers))})"
    print(f"base    : {os.environ.get('CMF1835_DB', '(produccion)')}")
    print(f"periodos: {pers}\n")

    fallas, dibujados = [], 0
    for tabla in TABLAS:
        if not D.existe(tabla):
            print(f"-- {tabla} (ausente, se omite)"); continue
        print(f"== {tabla}")

        subs = [None]
        if "subtipo" in D.columnas(tabla):
            subs += [r[0] for r in con.execute(
                f"SELECT DISTINCT subtipo FROM {tabla} d {where} "
                f"AND subtipo IS NOT NULL").fetchall()]

        catalogo = (D._CAT_DERIV if tabla in ("fact_derivado", "v_derivado_clasificado")
                    else D._CAT_RF)
        for sub in subs:
            wg = D._y(where, f"d.subtipo = {D._lit(sub)}") if sub else where
            aplica = [e for e in catalogo if e[2] is None or (sub and sub in e[2])]
            cob = D._cobertura(tabla, wg, {c for e in aplica for c in e[3]})

            ok_titulos, caidos = [], []
            for clave, titulo, _s, req, fn in aplica:
                faltan = [c for c in req if cob.get(c, 0) == 0]
                (ok_titulos if not faltan else caidos).append((titulo, clave, fn, faltan))

            # Control 2: nada prohibido puede estar entre los ofrecidos.
            malos = {t for t, _c, _f, _x in ok_titulos} & PROHIBIDOS.get(sub or "", set())
            if malos:
                fallas.append(f"{tabla}/{sub}: se ofrece {sorted(malos)}")

            # Control 1: cada grafico ofrecido se dibuja sin reventar. Se
            # ejecuta el SQL del grafico, que es donde puede romperse.
            rotos = []
            for titulo, clave, fn, _x in ok_titulos:
                try:
                    _ejecutar_sql_de(fn, tabla, wg, clave, con)
                    dibujados += 1
                except Exception as e:  # noqa: BLE001
                    rotos.append(f"{titulo}: {type(e).__name__}: {e}")
                    fallas.append(f"{tabla}/{sub}/{titulo}: {type(e).__name__}: {e}")

            etiqueta = sub or "(ejes comunes)"
            print(f"   {'OK' if not (malos or rotos) else 'XX'} {etiqueta:12} "
                  f"ofrecidos={len(ok_titulos)}  omitidos={len(caidos)}"
                  + (f"  ROTOS={rotos}" if rotos else "")
                  + (f"  INDEBIDOS={sorted(malos)}" if malos else ""))
            for t, _c, _f, fl in caidos:
                print(f"        - {t}  (sin {', '.join(fl)})")

    print(f"\ngraficos ejecutados: {dibujados}")
    print("TODO OK" if not fallas else f"{len(fallas)} FALLAS")
    for x in fallas:
        print("   -", x)
    return 1 if fallas else 0


def _ejecutar_sql_de(fn, tabla, where, clave, con):
    """Corre el grafico con st y px neutralizados: solo interesa que el SQL
    y el armado del DataFrame no revienten."""
    import types
    import pandas as pd

    capt = {}

    def q_real(sql):
        capt["sql"] = sql
        return con.execute(sql).fetch_df()

    falso_st = types.SimpleNamespace(
        info=lambda *a, **k: None, caption=lambda *a, **k: None,
        plotly_chart=lambda *a, **k: None)

    class FalsoPX:
        def __getattr__(self, _n):
            def f(df, *a, **k):
                return types.SimpleNamespace(
                    update_layout=lambda *a, **k: types.SimpleNamespace(
                        update_yaxes=lambda *a, **k: None,
                        update_xaxes=lambda *a, **k: None),
                    update_yaxes=lambda *a, **k: None,
                    update_xaxes=lambda *a, **k: None,
                    add_hline=lambda *a, **k: None)
            return f

    viejo = (D.q, D.st, D.px, D.eje_mm)
    D.q, D.st, D.px = q_real, falso_st, FalsoPX()
    D.eje_mm = lambda fig, *a, **k: fig
    try:
        fn(tabla, where, clave)
    finally:
        D.q, D.st, D.px, D.eje_mm = viejo


if __name__ == "__main__":
    raise SystemExit(main())
