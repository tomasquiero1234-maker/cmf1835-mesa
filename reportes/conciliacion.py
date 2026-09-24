"""
reportes.conciliacion
=====================

Conciliacion del libro de BBVA con Confuturo contra lo que Confuturo informa a
la CMF. Codifica la conciliacion que se hizo y valido a mano sobre 202607; el
test tests/reporte_excel.py exige que la reproduzca exacta.

Criterios (los validados, no reinventados)
------------------------------------------
Swaps (GROUP = CS)
    Candidatos: swaps de Confuturo con contrapartes del grupo BBVA cuya fecha
    de operacion este a +-5 dias de TRN.DATE (el script original usaba esa
    ventana: el swap BBVA del 09-ago-2019 es el folio 7215 del 12-ago-2019).
    Se exige ademas el mismo vencimiento y que RATE sea la tasa de una de las
    dos patas (tolerancia 0,006: el libro redondea a dos decimales, 4,63
    contra 4,625). El emparejamiento es uno a uno: asi se separan las dos
    operaciones del 26-nov-2024 (4,16 -> 10666 y 4,60 -> 10665), que el
    script original habia juntado en el mismo folio.

Repos (GROUP = REPO): dos paquetes de intercambio de colateral
    Cada paquete (mismo START) entrega un Treasury y recibe bonos
    corporativos. Confuturo los informa partidos en folios de pactos: uno con
    el Treasury (ISIN US91282...) y otro con los corporativos, todos con el
    vencimiento del paquete. Se exige que el numero de ISIN distintos del
    lado CMF sea igual al numero de bonos corporativos del paquete. Los
    montos se comparan al tipo de cambio que la propia Confuturo informa en el
    periodo (mediana de tipo_cambio_mercado); la diferencia esperada es el
    interes devengado, porque el libro trae precio limpio.

Forwards (GROUP = FXD) y bonos (GROUP = BOND)
    Se buscan en lo que Confuturo informa. Si no aparecen, quedan "sin
    reflejo en la CMF".

Correcciones de datos del libro, verificadas contra la CMF
    Vencimientos anteriores a la fecha de operacion (1933 -> 2033, 1937 ->
    2037, 1956 -> 2056): el sistema de origen guarda el ano con dos digitos.
    Cuando hay folio CMF, este confirma el ano; los de 1956 no tienen
    contraparte y quedan marcados como no confirmados.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

LIBRO_BBVA = Path("/Users/tomasquiero/Desktop/Posiciones  Confuturo.xlsx")
RUT_CONFUTURO = 96571890

#: Claves del catalogo que corresponden a entidades BBVA en la base.
CLAVES_BBVA = ("BBVA_ES", "BBVA_CL_LEGACY")

VENTANA_DIAS = 5
TOL_TASA = 0.006
PREFIJO_TREASURY = "US91282"


def leer_libro(path: Path = LIBRO_BBVA) -> pd.DataFrame:
    """Libro de BBVA tal cual, mas el vencimiento con el siglo corregido."""
    df = pd.read_excel(path)
    df = df[df.notna().any(axis=1)].reset_index(drop=True)
    df.insert(0, "fila_libro", df.index + 2)          # fila de Excel (1 = encabezado)
    for c in ("TRN.DATE", "START", "EXPIRY"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    # Un vencimiento no puede ser anterior a la operacion: el sistema de origen
    # guarda el ano con dos digitos y 2033 sale como 1933. La regla es objetiva
    # (fecha imposible), no un umbral elegido a mano.
    exp = df["EXPIRY"]
    malo = exp < df["TRN.DATE"]
    df["EXPIRY_CORREGIDA"] = exp.where(~malo, exp + pd.DateOffset(years=100))
    df["siglo_corregido"] = malo
    return df


def _cmf(con, periodo: int) -> pd.DataFrame:
    return con.execute(f"""
        SELECT folio_operacion, item_operacion, producto, subtipo, instrumento,
               fecha_operacion, fecha_vencimiento, nocional_m, pata_larga_tasa, pata_corta_tasa,
               activo_subyacente, tasa_pacto, tipo_cambio_mercado, contraparte_key,
               contraparte_nombre, moneda
        FROM v_derivado_clasificado
        WHERE rut_compania = {RUT_CONFUTURO} AND periodo_informacion = {int(periodo)}
    """).fetch_df()


def conciliar(con, periodo: int, libro: Path = LIBRO_BBVA) -> dict[str, pd.DataFrame]:
    """Devuelve {'libro': una fila por fila del libro, 'cmf_sin_libro': ...,
    'resumen': conteos}."""
    bk = leer_libro(libro)
    cmf = _cmf(con, periodo)
    cmf["fecha_operacion"] = pd.to_datetime(cmf["fecha_operacion"])
    cmf["fecha_vencimiento"] = pd.to_datetime(cmf["fecha_vencimiento"])
    bbva = cmf[cmf.contraparte_key.isin(CLAVES_BBVA)]

    tc = (cmf.tipo_cambio_mercado[(cmf.tipo_cambio_mercado > 500) & (cmf.tipo_cambio_mercado < 1500)]
          .median())

    res = {i: {"resultado": "", "contrapartida_cmf": "", "detalle": ""} for i in bk.index}
    usados: set = set()

    # --- swaps ----------------------------------------------------------------
    sw = bbva[bbva.producto == "SWAP"]
    for i, r in bk[bk.GROUP == "CS"].iterrows():
        cand = sw[((sw.fecha_operacion - r["TRN.DATE"]).abs() <= pd.Timedelta(days=VENTANA_DIAS))
                  & (sw.fecha_vencimiento == r.EXPIRY_CORREGIDA)
                  & (((sw.pata_larga_tasa - r.RATE).abs() <= TOL_TASA)
                     | ((sw.pata_corta_tasa - r.RATE).abs() <= TOL_TASA))
                  & ~sw.folio_operacion.isin(usados)]
        if len(cand):
            c = cand.iloc[0]
            usados.add(c.folio_operacion)
            res[i] = {"resultado": "Cuadra 1 a 1",
                      "contrapartida_cmf": f"folio {c.folio_operacion}",
                      "detalle": (f"operacion {c.fecha_operacion.date()}, vence {c.fecha_vencimiento.date()}, "
                                  f"tasas {c.pata_larga_tasa}/{c.pata_corta_tasa}, "
                                  f"nocional CMF {c.nocional_m / 1e3:,.1f} MM$, {c.contraparte_nombre}")}
        else:
            res[i]["resultado"] = "Sin reflejo en la CMF"

    # --- repos: paquetes de intercambio de colateral ----------------------------
    pac = bbva[bbva.producto == "PACTO"]
    for start, g in bk[bk.GROUP == "REPO"].groupby("START"):
        exp = g.EXPIRY_CORREGIDA.iloc[0]
        tes = g[g["PL INSTRUMENT"].astype(str).str.startswith("T ")]
        cor = g[~g.index.isin(tes.index)]
        del_paquete = pac[pac.fecha_vencimiento == exp]
        es_tes = del_paquete.activo_subyacente.astype(str).str.startswith(PREFIJO_TREASURY)
        f_tes = sorted(del_paquete[es_tes].folio_operacion.unique())
        f_cor = sorted(del_paquete[~es_tes].folio_operacion.unique())
        isin_cor = del_paquete[~es_tes].activo_subyacente.nunique()
        cmf_tes = del_paquete[es_tes].nocional_m.sum() / 1e3
        cmf_cor = del_paquete[~es_tes].nocional_m.sum() / 1e3
        mv_tes = (tes["NOMINAL 0"] * tes.RATE / 100).sum()
        mv_cor = (cor["NOMINAL 0"] * cor.RATE / 100).sum()
        ok = bool(f_tes) and bool(f_cor) and isin_cor == len(cor)
        paquete = f"Paquete de colateral {pd.Timestamp(start).date()}"
        for i in tes.index:
            res[i] = {"resultado": "Cuadra como paquete" if ok else "Sin reflejo en la CMF",
                      "contrapartida_cmf": ", ".join(f"folio {f}" for f in f_tes),
                      "detalle": (f"{paquete}: Treasury; CMF {cmf_tes:,.1f} MM$ = {cmf_tes / tc:,.2f} MM USD "
                                  f"al TC {tc:,.2f} vs libro {mv_tes / 1e6:,.2f} MM USD "
                                  f"({(cmf_tes / tc) / (mv_tes / 1e6) * 100 - 100:+.1f}%, interes devengado)")}
        for i in cor.index:
            res[i] = {"resultado": "Cuadra como paquete" if ok else "Sin reflejo en la CMF",
                      "contrapartida_cmf": ", ".join(f"folio {f}" for f in f_cor),
                      "detalle": (f"{paquete}: {len(cor)} bonos en el libro, {isin_cor} ISIN distintos en la CMF; "
                                  f"CMF {cmf_cor:,.1f} MM$ = {cmf_cor / tc:,.2f} MM USD vs libro "
                                  f"{mv_cor / 1e6:,.2f} MM USD ({(cmf_cor / tc) / (mv_cor / 1e6) * 100 - 100:+.1f}%)")}
        usados.update(f_tes + f_cor)

    # --- forwards y bonos --------------------------------------------------------
    fwd_conf = cmf[cmf.subtipo == "FORWARD"]
    for i, r in bk[bk.GROUP == "FXD"].iterrows():
        if fwd_conf.empty:
            res[i] = {"resultado": "Sin reflejo en la CMF", "contrapartida_cmf": "",
                      "detalle": f"Confuturo no informa ningun forward en {periodo}, con ninguna contraparte"}
        else:
            c = fwd_conf[fwd_conf.fecha_vencimiento == r.EXPIRY_CORREGIDA]
            res[i] = ({"resultado": "Cuadra 1 a 1", "contrapartida_cmf": f"folio {c.folio_operacion.iloc[0]}",
                       "detalle": ""} if len(c) else
                      {"resultado": "Sin reflejo en la CMF", "contrapartida_cmf": "",
                       "detalle": "Sin forward de Confuturo con ese vencimiento"})
    rf = con.execute(f"""SELECT count(*) FROM fact_renta_fija WHERE rut_compania = {RUT_CONFUTURO}
        AND periodo_informacion = {int(periodo)}
        AND (upper(nemotecnico) LIKE '%BBVASM%' OR emisor_rut IN (97032000))""").fetchone()[0]
    for i, r in bk[bk.GROUP == "BOND"].iterrows():
        res[i] = {"resultado": "Sin reflejo en la CMF" if rf == 0 else "Revisar",
                  "contrapartida_cmf": "",
                  "detalle": ("Confuturo no informa renta fija emitida por BBVA en el periodo"
                              if rf == 0 else f"{rf} registros candidatos en el B.1")}

    # --- notas de datos del libro ------------------------------------------------
    clave = ["GROUP", "B/S", "NOMINAL 0", "NOMINAL 1", "TRN.DATE", "START", "PL INSTRUMENT"]
    dup = bk.duplicated(subset=clave, keep=False)
    notas = []
    for i, r in bk.iterrows():
        n = []
        if r.siglo_corregido:
            confirmado = (r.GROUP == "CS" and res[i]["resultado"] == "Cuadra 1 a 1")
            n.append(f"Vencimiento {r.EXPIRY.date()} corregido a {r.EXPIRY_CORREGIDA.date()}"
                     + (" (confirmado por la CMF)" if confirmado else " (sin contraparte CMF que lo confirme)"))
        if dup[i]:
            n.append("Fila identica a otra del libro: posible doble registro")
        n0, n1 = abs(r["NOMINAL 0"] or 0), abs(r["NOMINAL 1"] or 0)
        if r.GROUP == "REPO" and n0 and n1 / n0 > 100:
            n.append(f"NOMINAL 0 = {n0:,.0f} contra NOMINAL 1 = {n1:,.0f}: parece faltar un factor 1.000")
        notas.append("; ".join(n))

    libro_out = bk.copy()
    libro_out["resultado"] = [res[i]["resultado"] for i in bk.index]
    libro_out["contrapartida_cmf"] = [res[i]["contrapartida_cmf"] for i in bk.index]
    libro_out["detalle"] = [res[i]["detalle"] for i in bk.index]
    libro_out["nota_datos_libro"] = notas

    # --- lo que la CMF atribuye a BBVA y no esta en el libro ---------------------
    resto = bbva[~bbva.folio_operacion.isin(usados)]
    sin_libro = (resto.groupby(["folio_operacion", "producto", "instrumento", "contraparte_key",
                                "contraparte_nombre", "fecha_operacion", "fecha_vencimiento"],
                               dropna=False)
                 .agg(items=("item_operacion", "count"), nocional_mm_pesos=("nocional_m", "sum"))
                 .reset_index())
    sin_libro["nocional_mm_pesos"] = sin_libro.nocional_mm_pesos / 1e3
    sin_libro["nota"] = np.where(sin_libro.contraparte_key == "BBVA_CL_LEGACY",
                                 "Banco BBVA Chile: vendido a Scotiabank en 2018, fuera del libro de BBVA",
                                 "Informado por Confuturo contra BBVA, sin fila equivalente en el libro")

    resumen = (libro_out.groupby(["GROUP", "resultado"]).size().rename("filas").reset_index())
    return {"libro": libro_out, "cmf_sin_libro": sin_libro, "resumen": resumen,
            "tipo_cambio": pd.DataFrame({"tipo_cambio_confuturo": [tc]})}
