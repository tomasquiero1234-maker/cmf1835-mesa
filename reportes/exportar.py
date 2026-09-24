"""
reportes.exportar
=================

Arma el libro completo: datos (reportes.datos) y formato (reportes.excel).

    python -m reportes                      # periodo mas reciente
    python -m reportes --periodo 202608 --salida reportes/salida/stock.xlsx
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import numpy as np
import pandas as pd

from reportes import datos as D
from reportes.excel import FILA_HEADER, Col, Hoja, Libro

ROOT = Path(__file__).resolve().parents[1]
SALIDA = ROOT / "reportes" / "salida"


def _mon(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip().str.upper().replace({"PROM": "USD", "$$": "CLP"})


# ---------------------------------------------------------------------------
#  columnas comunes
# ---------------------------------------------------------------------------

C_ASEG = [Col("Aseguradora", "aseguradora", "texto", "Compania informante, nombre publicado en la CMF"),
          Col("RUT aseguradora", "rut_compania", "id", "RUT de la compania informante, sin digito verificador")]

C_CP = [Col("Contraparte (nombre legal)", "entidad_nombre", "texto",
            "Nombre legal de la contraparte: GLEIF si se informo LEI, catalogo si se resolvio por RUT, "
            "nombre informado si no hay identificador"),
        Col("ID legal contraparte", "entidad_id", "texto",
            "RUT o LEI tal como lo informo la aseguradora. Cada ID es una persona juridica distinta: "
            "no se agrupan filiales bajo su matriz"),
        Col("Pais contraparte", "entidad_pais", "texto", "Pais del domicilio legal (GLEIF) o CL si es RUT"),
        Col("Alerta contraparte", "entidad_alerta", "texto_largo",
            "Hechos verificables sobre el identificador: LEI invalido o no vigente, LEI de otra entidad, "
            "sin identificador, agrupacion del catalogo del dashboard")]

#: Alerta de una linea, al final de las hojas de operaciones. Sin ajuste de
#: texto: con miles de filas, una alerta que ocupa cinco lineas vuelve la hoja
#: ilegible. El texto completo esta en Calidad_Contrapartes.
ALERTA_OP = Col("Alerta contraparte", "entidad_alerta_op", "texto",
                "Hechos sobre el identificador que afectan a la operacion: LEI invalido, no vigente, de otra "
                "entidad o de un fondo, o sin identificador. Detalle completo en Calidad_Contrapartes")
C_CP_OP = C_CP[:3]

C_OP = [Col("Folio", "folio_operacion", "texto", "Folio de la operacion en el anexo B.7"),
        Col("Item", "item_operacion", "texto", "Item dentro del folio"),
        Col("Fecha operacion", "fecha_operacion", "fecha", "Fecha en que se pacto la operacion"),
        Col("Fecha vencimiento", "fecha_vencimiento", "fecha", "Fecha de vencimiento del contrato"),
        Col("Plazo residual (dias)", "plazo_residual_dias", "entero",
            "Dias desde la fecha de corte hasta el vencimiento")]

NOC = Col("Nocional (MM USD)", "nocional_mmusd", "mm",
          "Nocional informado en M$, convertido al dolar observado del cierre", total=True)
MTM = Col("MTM neto (MM USD)", "mtm_mmusd", "mm",
          "Valor razonable neto del contrato (activo menos pasivo), en MM USD", total=True)
TENOR = Col("Tenor residual (anios)", "tenor_anios", "anios", "Anios desde la fecha de corte al vencimiento")


def _pct_contra_mercado(df):
    c, m = pd.to_numeric(df.tasa_precio_contrato, errors="coerce"), pd.to_numeric(df.tasa_precio_mercado, errors="coerce")
    return ((c / m - 1) * 100).where(m > 0)


# ---------------------------------------------------------------------------
#  hojas tailor-made por instrumento
# ---------------------------------------------------------------------------

def hojas_instrumentos(ops: pd.DataFrame) -> list[Hoja]:
    f = lambda fam: ops[ops.familia == fam].sort_values(["aseguradora", "nocional_mmusd"],  # noqa: E731
                                                        ascending=[True, False])
    out: list[Hoja] = []

    out.append(Hoja(
        "CCS", "tbl_ccs", "Cross Currency Swaps",
        "Swaps de monedas. Cada pata con su moneda y su tasa; el cruce en orden canonico (UF/USD, USD/CLP).",
        C_ASEG + C_CP_OP + [
            Col("Cruce", "subyacente", "texto", "Par de monedas del swap, en orden canonico"),
            Col("Instrumento informado", "instrumento", "texto", "Clasificacion del warehouse a partir del anexo"),
            Col("Moneda recibe", "moneda_recibe", "texto", "Moneda de la pata que recibe la aseguradora"),
            Col("Moneda entrega", "moneda_entrega", "texto", "Moneda de la pata que entrega la aseguradora"),
            Col("Tasa pata larga (%)", "pata_larga_tasa", "tasa", "Tasa de la pata activa, en % anual segun contrato"),
            Col("Tasa pata corta (%)", "pata_corta_tasa", "tasa", "Tasa de la pata pasiva, en % anual segun contrato"),
            Col("Spread entre patas (pb)", "spread_patas_pb", "pb", "Diferencia entre las tasas de ambas patas"),
            Col("Estructura", "direccion", "texto", "Fija contra fija, paga o recibe fija, flotante"),
            Col("Indice flotante", "indice_flotante", "texto", "Indice de la pata flotante, si la hay"),
            Col("TC contrato", "tipo_cambio_contrato", "num2", "Tipo de cambio pactado"),
            Col("TC mercado", "tipo_cambio_mercado", "num2", "Tipo de cambio de mercado al cierre, informado"),
            NOC,
            Col("VP pata larga (MM USD)", "vp_largo_mmusd", "mm", "Valor presente de la pata activa", total=True),
            Col("VP pata corta (MM USD)", "vp_corto_mmusd", "mm", "Valor presente de la pata pasiva", total=True),
            MTM, TENOR] + C_OP + [ALERTA_OP],
        f("CCS"), total_etiqueta="Total CCS (fuera de la tabla)"))

    promesa = f("Swap Promesa").copy()
    promesa["breakeven"] = promesa.apply(D.breakeven_promesa, axis=1)
    promesa["m_larga_n"], promesa["m_corta_n"] = _mon(promesa.m_larga), _mon(promesa.m_corta)
    out.append(Hoja(
        "Swap_Promesa", "tbl_swap_promesa", "Swaps promesa (UF contra pesos)",
        "Swaps entre UF y pesos, para calzar inflacion y duracion. La inflacion breakeven solo aplica si ambas patas son fijas.",
        C_ASEG + C_CP_OP + [
            Col("Cruce", "subyacente", "texto", "Par de monedas, en orden canonico"),
            Col("Instrumento informado", "instrumento", "texto", "Clasificacion del warehouse"),
            Col("Direccion", "direccion", "texto", "Paga fija, recibe fija o fija contra fija"),
            Col("Moneda pata larga", "m_larga_n", "texto", "Moneda de la pata activa"),
            Col("Tasa pata larga (%)", "pata_larga_tasa", "tasa", "Tasa de la pata activa, % anual"),
            Col("Moneda pata corta", "m_corta_n", "texto", "Moneda de la pata pasiva"),
            Col("Tasa pata corta (%)", "pata_corta_tasa", "tasa", "Tasa de la pata pasiva, % anual"),
            Col("Indice flotante", "indice_flotante", "texto", "Indice de la pata flotante, si la hay"),
            Col("Inflacion breakeven (%)", "breakeven", "pct",
                "(1 + tasa pesos) / (1 + tasa UF) - 1, solo si ambas patas son fijas y son UF y pesos. Una pata "
                "en 0% es plana y se calcula; ambas en 0% es un contrato no informado y queda vacio"),
            NOC, MTM, TENOR] + C_OP + [ALERTA_OP],
        promesa, total_etiqueta="Total swaps promesa (fuera de la tabla)"))

    fx = f("Forward FX").copy()
    fx["operacion"] = fx.apply(D.direccion_fwd, axis=1)
    fx["vs_mercado"] = _pct_contra_mercado(fx)
    out.append(Hoja(
        "Forward_FX", "tbl_forward_fx", "Forwards de moneda",
        "Forwards de divisa contra pesos. La direccion sale de los activos objeto informados, no del codigo.",
        C_ASEG + C_CP_OP + [
            Col("Par", "subyacente", "texto", "Divisa contra pesos, p.ej. USD/CLP"),
            Col("Operacion", "operacion", "texto", "Compra o venta de la divisa, leida de los activos objeto"),
            Col("Codigo CMF", "tipo_operacion", "texto", "Codigo de operacion tal como se informa (FWC, FWV)"),
            Col("Precio pactado", "tasa_precio_contrato", "num2", "Tipo de cambio forward pactado"),
            Col("Precio spot", "precio_spot", "num2", "Tipo de cambio spot al cierre, informado"),
            Col("Precio forward de mercado", "tasa_precio_mercado", "num2", "Tipo de cambio forward de mercado al cierre"),
            Col("Pactado vs mercado (%)", "vs_mercado", "pct",
                "Precio pactado / precio de mercado - 1. Positivo: se pacto sobre el mercado"),
            NOC, MTM] + C_OP + [ALERTA_OP],
        fx, total_etiqueta="Total forwards de moneda (fuera de la tabla)"))

    uf = f("Forward UF").copy()
    uf["operacion"] = uf.apply(D.direccion_fwd, axis=1)
    uf["infl_impl"] = uf.apply(D.inflacion_implicita_fwd_uf, axis=1)
    uf["vs_mercado"] = _pct_contra_mercado(uf)
    out.append(Hoja(
        "Forward_UF", "tbl_forward_uf", "Forwards de UF (inflacion)",
        "Forwards de UF contra pesos: cobertura de inflacion. Incluye los informados como moneda extranjera con subyacente UF.",
        C_ASEG + C_CP_OP + [
            Col("Instrumento informado", "instrumento", "texto",
                "Como lo clasifico el warehouse: 'Forward UF' o 'Forward FX UF' (misma economia)"),
            Col("Operacion", "operacion", "texto", "Compra o venta de UF, leida de los activos objeto"),
            Col("Codigo CMF", "tipo_operacion", "texto", "Codigo de operacion tal como se informa"),
            Col("Unidades UF", "unidades_uf", "num0", "Cantidad de UF del contrato"),
            Col("Precio pactado (CLP por UF)", "tasa_precio_contrato", "num2", "Valor de la UF pactado"),
            Col("UF de cierre", "precio_spot", "num2", "UF a la fecha de corte, informada"),
            Col("Precio forward de mercado (CLP por UF)", "tasa_precio_mercado", "num2", "UF forward de mercado al cierre"),
            Col("Inflacion implicita de mercado (% anual)", "infl_impl", "pct",
                "(UF forward de mercado / UF de cierre) ^ (365 / dias al vencimiento) - 1"),
            Col("Pactado vs mercado (%)", "vs_mercado", "pct", "Precio pactado / precio de mercado - 1"),
            NOC, MTM] + C_OP + [ALERTA_OP],
        uf, total_etiqueta="Total forwards UF (fuera de la tabla)"))

    irs = f("IRS").copy()
    irs["moneda_n"] = _mon(irs.moneda)
    out.append(Hoja(
        "IRS", "tbl_irs", "Interest Rate Swaps",
        "Swaps de tasa: una pata fija y una flotante en la misma moneda (Camara, SOFR).",
        C_ASEG + C_CP_OP + [
            Col("Indice flotante", "indice_flotante", "texto", "Indice de la pata flotante"),
            Col("Direccion", "direccion", "texto", "Paga fija o recibe fija, desde la aseguradora"),
            Col("Moneda", "moneda_n", "texto", "Moneda del swap"),
            Col("Tasa fija (%)", "tasa_fija", "tasa", "Tasa de la pata fija, % anual"),
            Col("Tasa pata larga (%)", "pata_larga_tasa", "tasa", "Tasa informada de la pata activa"),
            Col("Tasa pata corta (%)", "pata_corta_tasa", "tasa", "Tasa informada de la pata pasiva"),
            Col("Spread vs mercado (pb)", "spread_vs_mercado_pb", "pb", "Tasa pactada menos tasa de mercado"),
            NOC, MTM, TENOR] + C_OP + [ALERTA_OP],
        irs, total_etiqueta="Total IRS (fuera de la tabla)"))

    op = f("Opcion").copy()
    s, k = pd.to_numeric(op.precio_spot, errors="coerce"), pd.to_numeric(op.tasa_precio_contrato, errors="coerce")
    op["moneyness"] = ((s / k - 1) * 100).where(k > 0)
    out.append(Hoja(
        "Opciones", "tbl_opciones", "Opciones",
        "Opciones sobre acciones o indices. El tipo se muestra con el codigo CMF tal como se informa.",
        C_ASEG + C_CP_OP + [
            Col("Codigo CMF", "tipo_operacion", "texto", "Codigo de operacion tal como se informa"),
            Col("Activo objeto largo", "activo_objeto_largo", "texto", "Activo de la posicion larga"),
            Col("Activo objeto corto", "activo_objeto_corto", "texto", "Activo de la posicion corta"),
            Col("Strike", "tasa_precio_contrato", "num2", "Precio de ejercicio"),
            Col("Spot", "precio_spot", "num2", "Precio del subyacente al cierre, informado"),
            Col("Spot vs strike (%)", "moneyness", "pct", "Spot / strike - 1"),
            NOC, MTM] + C_OP + [ALERTA_OP],
        op, total_etiqueta="Total opciones (fuera de la tabla)"))

    fu = f("Futuro")
    out.append(Hoja(
        "Futuros", "tbl_futuros", "Futuros",
        "Futuros. El subyacente es el codigo que informa la aseguradora.",
        C_ASEG + C_CP_OP + [
            Col("Codigo CMF", "tipo_operacion", "texto", "Codigo de operacion tal como se informa"),
            Col("Subyacente (codigo)", "activo_objeto_largo", "texto", "Codigo del subyacente informado"),
            Col("Precio pactado", "tasa_precio_contrato", "num2", "Precio del contrato"),
            Col("Precio de mercado", "tasa_precio_mercado", "num2", "Precio de mercado al cierre"),
            NOC, MTM] + C_OP + [ALERTA_OP],
        fu, total_etiqueta="Total futuros (fuera de la tabla)"))

    pa = f("Pacto").copy()
    tipo = {"PVCC": "Venta con compromiso de compra (repo)", "PCCV": "Compra con compromiso de venta (reverse repo)"}
    pa["tipo_pacto"] = pa.tipo_operacion.map(tipo).fillna(pa.tipo_operacion)
    pa["moneda_n"] = _mon(pa.moneda)
    out.append(Hoja(
        "Pactos", "tbl_pactos", "Pactos (repos y reverse repos)",
        "Financiamiento, no derivados: fuera del total de derivados. El B.7 no pide RUT ni LEI de la contraparte de un pacto.",
        C_ASEG + [
            Col("Contraparte (nombre informado)", "contraparte_nombre_informado", "texto",
                "Nombre de la contraparte tal como lo escribio la aseguradora: el unico identificador de un pacto"),
            Col("Contraparte (catalogo dashboard)", "contraparte_nombre", "texto", "Como la resuelve el catalogo, de referencia"),
            Col("Tipo de pacto", "tipo_pacto", "texto", "Repo (PVCC) o reverse repo (PCCV)"),
            Col("Codigo CMF", "tipo_operacion", "texto", "Codigo de operacion tal como se informa"),
            Col("Moneda", "moneda_n", "texto", "Moneda del pacto"),
            Col("Tasa del pacto (segun contrato)", "tasa_pacto", "tasa",
                "La CMF pide la tasa 'indicada en el contrato' sin fijar base, y en los datos conviven dos "
                "ordenes de magnitud: ~0,4 en pesos y UF, y en USD tanto ~0,6-0,7 como ~4,9. No se convierte: "
                "comparar tasas solo dentro de un mismo orden de magnitud"),
            Col("TIR compra (%)", "tir_pacto_compra", "tasa", "TIR de compra informada"),
            Col("Activo subyacente", "activo_subyacente", "texto", "Instrumento entregado o recibido (ISIN o nemotecnico)"),
            Col("Monto subyacente (MM USD)", "monto_subyacente_mmusd", "mm", "Valor del subyacente informado", total=True),
            NOC] + C_OP,
        pa, total_etiqueta="Total pactos (fuera de la tabla)"))
    return out


# ---------------------------------------------------------------------------
#  hojas tailor-made de las inversiones (no derivados)
# ---------------------------------------------------------------------------

AMBITO = Col("Ambito", "ambito", "texto",
             "Nacional o Internacional. Nacional: anexos locales (B.1 a B.4; B.6 con pais CL). "
             "Internacional: B.5 (inversiones en el extranjero) y B.6 con otro pais o codigo OIED")
TIPO = Col("Tipo instrumento", "tipo_instrumento", "texto", "Codigo CMF del instrumento, tal como se informa")
VF = Col("Valor final (MM USD)", "valor_final", "mm",
         "Valor final informado en M$, convertido al dolar observado del cierre", total=True)
MONEDA = Col("Moneda", "moneda", "texto", "Unidad o moneda del instrumento (UF, CLP, USD, EUR...)")
PAIS = Col("Pais", "pais", "texto", "Pais informado")
REL = Col("Relacionado", "relacionado", "texto", "Si el emisor esta relacionado con la aseguradora, segun se informa")


def hojas_inversiones(ctx) -> list[Hoja]:
    """Una hoja por clase de activo, con las columnas propias de cada una. La
    suma del valor final de cada hoja cuadra con su columna en Stock_Clase."""
    out: list[Hoja] = []

    out.append(Hoja(
        "Renta_Fija", "tbl_renta_fija", "Renta fija nacional e internacional",
        "Un papel por fila. Nacional = B.1; internacional = B.5. Sin leasing: va en Real_Estate, como en el stock.",
        C_ASEG + [AMBITO, TIPO,
            Col("Segmento emisor", "segmento_emisor", "texto",
                "Soberano, Bancario, Corporativo, Hipotecario/Leasing u Otros. PDBC y BCU (Banco Central) van como "
                "Soberano; el dashboard todavia los rotula distinto"),
            Col("Instrumento", "instrumento_id", "texto", "Nemotecnico (nacional) o ISIN (internacional)"),
            Col("Emisor", "emisor_nombre", "texto",
                "Nacional: nombre resuelto desde el RUT (nomina CMF, catalogo, aseguradoras), vacio si no se pudo; "
                "internacional: el que informa la aseguradora"),
            Col("Grupo emisor", "emisor_grupo", "texto", "Grupo economico del emisor, si esta en el catalogo"),
            Col("RUT emisor", "emisor_rut", "id", "RUT del emisor (solo nacional)"),
            PAIS, MONEDA,
            Col("Valor nominal (unidad del instrumento)", "valor_nominal", "num2", "Nominal en la unidad del instrumento"),
            VF,
            Col("Tasa de emision (%)", "tasa_emision", "tasa", "Tasa de emision del papel"),
            Col("TIR de compra (%)", "tir_compra", "tasa", "TIR a la que se compro"),
            Col("TIR de mercado (%)", "tir_mercado", "tasa", "TIR de mercado al cierre"),
            Col("Duracion (anios)", "duracion", "anios",
                "Nacional: aproximada con el plazo y la TIR (el B.1 no la pide); internacional: informada"),
            Col("Origen de la duracion", "duracion_origen", "texto", "aproximada o informada"),
            Col("Clasificacion de riesgo", "clasificacion_riesgo", "texto", "Clasificacion de riesgo informada"),
            Col("Fecha emision", "fecha_emision", "fecha", "Fecha de emision"),
            Col("Fecha compra", "fecha_compra", "fecha", "Fecha de compra"),
            Col("Fecha vencimiento", "fecha_vencimiento", "fecha", "Fecha de vencimiento"),
            Col("Plazo residual (dias)", "plazo_residual_dias", "entero", "Dias desde la fecha de corte al vencimiento")],
        D.detalle_renta_fija(ctx), total_etiqueta="Total renta fija (fuera de la tabla)"))

    out.append(Hoja(
        "Acciones", "tbl_acciones", "Acciones (equity)",
        "Acciones nacionales (B.2) e internacionales (B.5): codigos CMF AC*.",
        C_ASEG + [AMBITO, TIPO,
            Col("Instrumento", "instrumento", "texto", "Nemotecnico (nacional) o ISIN (internacional)"),
            Col("Emisor", "emisor", "texto",
                "Nacional: nombre segun la nomina de emisores de la CMF, desde el RUT; internacional: informado"),
            Col("RUT emisor", "emisor_rut", "id", "RUT del emisor (solo nacional)"),
            PAIS, Col("Bolsa", "bolsa", "texto", "Bolsa donde transa (solo internacional)"), MONEDA,
            Col("Unidades (acciones)", "unidades", "num0", "Numero de acciones"),
            VF,
            Col("Presencia bursatil (%)", "presencia_bursatil", "pct", "Presencia bursatil de la accion (solo nacional)"),
            Col("Participacion en la sociedad (%)", "participacion_pct", "pct", "Porcentaje de la sociedad que posee"),
            Col("Filial o coligada", "filial_coligada", "texto", "Si la sociedad es filial o coligada (solo nacional)"),
            REL, Col("Clasificacion de riesgo", "clasificacion_riesgo", "texto", "Clasificacion informada"),
            Col("Campo fondo CUI (tal como se informa)", "campo_fondo_cui", "texto",
                "Campo NOMBRE_DEL_FONDO del anexo. La CMF lo define como el fondo del seguro con cuenta unica "
                "de inversion (CUI) que respalda el papel, o 'NO APLICA'; algunas aseguradoras escriben ahi otra "
                "descripcion. No es el nombre del instrumento. Solo nacional")],
        D.detalle_acciones(ctx), total_etiqueta="Total acciones (fuera de la tabla)"))

    out.append(Hoja(
        "ETF", "tbl_etf", "ETF",
        "Fondos transados en bolsa (B.5): codigos CMF ETF*.",
        C_ASEG + [AMBITO, TIPO,
            Col("Instrumento (ISIN)", "instrumento", "texto", "ISIN del ETF"),
            Col("Nombre", "nombre", "texto", "Nombre del ETF, informado"),
            PAIS, Col("Bolsa", "bolsa", "texto", "Bolsa donde transa"), MONEDA,
            Col("Subyacente", "subyacente", "texto", "Subyacente informado"),
            Col("Unidades", "unidades", "num2", "Cuotas del ETF"),
            Col("Precio unitario (moneda del ETF)", "valor_unitario", "num2", "Valor bursatil unitario informado"),
            VF, Col("Custodio", "custodio", "texto", "Custodio informado")],
        D.detalle_etf(ctx), total_etiqueta="Total ETF (fuera de la tabla)"))

    out.append(Hoja(
        "Fondos_Inversion", "tbl_fondos_inversion", "Fondos de inversion",
        "Cuotas de fondos de inversion nacionales (B.2) e internacionales (B.5): codigos CMF CFI*.",
        C_ASEG + [AMBITO, TIPO,
            Col("Instrumento", "instrumento", "texto", "Nemotecnico (nacional) o ISIN (internacional)"),
            Col("Fondo (nombre)", "nombre", "texto",
                "Solo internacional (B.5). El B.2 no informa el nombre del fondo: identificarlo por el nemotecnico"),
            Col("RUT emisor informado", "emisor_rut", "id",
                "Campo RUT del emisor del B.2 (en cuotas de fondos suele ser el de la administradora)"),
            PAIS, MONEDA,
            Col("Tipo de fondo", "tipo_fondo", "texto", "Tipo de fondo informado"),
            Col("Segmento", "segmento_fondo", "texto", "Segmento del fondo informado"),
            Col("Subyacente", "subyacente", "texto", "Subyacente informado"),
            Col("Cuotas", "unidades", "num2", "Cuotas que posee la aseguradora"),
            Col("Valor cuota (moneda del fondo)", "valor_cuota", "num2", "Solo internacional: el B.2 no lo informa"),
            VF,
            Col("Participacion en el fondo (%)", "participacion_pct", "pct", "Porcentaje del fondo que posee"),
            REL, Col("Campo fondo CUI (tal como se informa)", "campo_fondo_cui", "texto",
                "Campo NOMBRE_DEL_FONDO del anexo. La CMF lo define como el fondo del seguro con cuenta unica "
                "de inversion (CUI) que respalda el papel, o 'NO APLICA'; algunas aseguradoras escriben ahi otra "
                "descripcion. No es el nombre del instrumento. Solo nacional")],
        D.detalle_fondos_inversion(ctx), total_etiqueta="Total fondos de inversion (fuera de la tabla)"))

    out.append(Hoja(
        "Fondos_Mutuos", "tbl_fondos_mutuos", "Fondos mutuos",
        "Cuotas de fondos mutuos nacionales (B.3) e internacionales (B.5): codigos CMF CFM*.",
        C_ASEG + [AMBITO, TIPO,
            Col("Instrumento", "instrumento", "texto", "Nemotecnico (nacional) o ISIN (internacional)"),
            Col("Fondo (nombre)", "nombre", "texto",
                "Solo internacional (B.5). El B.3 no informa el nombre del fondo: identificarlo por el nemotecnico"),
            Col("Administradora", "administradora", "texto",
                "Nacional: nombre segun la nomina de emisores de la CMF; vacio si no esta (las administradoras "
                "generales de fondos estan en otro registro)"),
            Col("RUT administradora", "rut_administradora", "id", "RUT de la administradora (solo nacional)"),
            PAIS, MONEDA,
            Col("Tipo de fondo", "tipo_fondo", "texto", "Tipo de fondo informado"),
            Col("Serie", "serie", "texto", "Serie de la cuota"),
            Col("Cuotas", "unidades", "num2", "Cuotas que posee la aseguradora"),
            Col("Valor cuota (moneda del fondo)", "valor_cuota", "num2", "Valor cuota informado"),
            VF, REL, Col("Campo fondo CUI (tal como se informa)", "campo_fondo_cui", "texto",
                "Campo NOMBRE_DEL_FONDO del anexo. La CMF lo define como el fondo del seguro con cuenta unica "
                "de inversion (CUI) que respalda el papel, o 'NO APLICA'; algunas aseguradoras escriben ahi otra "
                "descripcion. No es el nombre del instrumento. Solo nacional")],
        D.detalle_fondos_mutuos(ctx), total_etiqueta="Total fondos mutuos (fuera de la tabla)"))

    out.append(Hoja(
        "Real_Estate", "tbl_real_estate", "Real estate: bienes raices propios y en leasing",
        "B.4 completo. No se incluye el nombre del arrendatario (persona natural en el leasing habitacional).",
        C_ASEG + [AMBITO,
            Col("Tenencia", "tenencia", "texto", "Propio (BZ) o dado en leasing (CLEAS)"),
            Col("Rol SII", "rol", "texto", "Rol del bien raiz en el Servicio de Impuestos Internos"),
            Col("Nemotecnico (leasing)", "nemotecnico", "texto", "Codigo del contrato de leasing, si lo es"),
            Col("Tipo de inmueble", "tipo_inmueble", "texto",
                "Marcas S del anexo: local, oficina, bodega, estacionamiento, terreno, casa, edificio..."),
            Col("Urbano", "urbano", "texto", "Urbano o no urbano"),
            Col("Destino", "destino", "texto", "Habitacional o no habitacional"),
            Col("Uso (codigo SEIL)", "uso", "texto", "Codigo de uso segun la codificacion CMF; sin traducir"),
            Col("Comuna (codigo SEIL)", "comuna", "texto", "Codigo de comuna segun la codificacion CMF"),
            Col("Ciudad", "ciudad", "texto", "Ciudad informada"),
            Col("Fecha compra", "fecha_compra", "fecha", "Fecha de adquisicion"),
            Col("m2 terreno", "m2_terreno", "num0", "Superficie del terreno"),
            Col("m2 construccion", "m2_construccion", "num0", "Superficie construida"),
            Col("Costo actualizado (MM USD)", "costo_actualizado", "mm", "Costo de adquisicion revalorizado", total=True),
            Col("Depreciacion acumulada (MM USD)", "depreciacion_acumulada", "mm", "Depreciacion acumulada", total=True),
            Col("Costo corregido (MM USD)", "costo_corregido", "mm", "Costo actualizado menos depreciacion", total=True),
            Col("Tasacion 1 (MM USD)", "tasacion_1", "mm", "Primera tasacion (NCG 42)"),
            Col("Tasacion 2 (MM USD)", "tasacion_2", "mm", "Segunda tasacion (NCG 42)"),
            Col("Menor tasacion (MM USD)", "menor_tasacion", "mm", "La menor de las tasaciones informadas"),
            Col("Fecha tasacion 1", "fecha_tasacion_1", "fecha", "Fecha de la primera tasacion"),
            VF,
            Col("Arriendo mensual (UF)", "monto_arriendo_uf", "num2", "Arriendo mensual equivalente, en UF"),
            Col("Saldo plazo arriendo (meses)", "saldo_plazo_arriendo_meses", "entero", "Meses restantes del leasing"),
            Col("Vida util restante (meses)", "vida_util_restante_meses", "entero", "Meses de vida util restante"),
            Col("Copropiedad (%)", "copropiedad_pct", "pct", "Porcentaje de propiedad si es copropietaria"),
            Col("Prohibicion o gravamen", "prohibicion_o_gravamen", "texto", "S si tiene prohibicion o gravamen")],
        D.detalle_real_estate(ctx), total_etiqueta="Total real estate (fuera de la tabla)"))

    out.append(Hoja(
        "Otras_Inversiones", "tbl_otras_inversiones", "Otras inversiones",
        "B.6: caja, prestamos, avances a tenedores, mobiliario y otras inversiones (NCG 152).",
        C_ASEG + [AMBITO, TIPO,
            Col("Codigo o nombre de la inversion", "codigo_inversion", "texto", "Tal como se informa"),
            Col("Nemotecnico", "nemotecnico", "texto", "Nemotecnico, si lo tiene"),
            PAIS, MONEDA,
            Col("Valor costo (MM USD)", "valor_costo", "mm", "Valor de costo actualizado", total=True),
            Col("Depreciacion (MM USD)", "depreciacion", "mm", "Depreciacion acumulada", total=True),
            Col("Valor razonable (MM USD)", "valor_razonable", "mm", "Valor razonable informado", total=True),
            Col("Deterioro (MM USD)", "deterioro", "mm", "Deterioro informado", total=True),
            VF,
            Col("Clasificacion de riesgo", "clasificacion_riesgo", "texto", "Clasificacion informada"),
            Col("Custodio", "custodio", "texto", "Custodio informado")],
        D.detalle_otras(ctx), total_etiqueta="Total otras inversiones (fuera de la tabla)"))
    return out


# ---------------------------------------------------------------------------
#  libro completo
# ---------------------------------------------------------------------------

def generar(periodo: int | None = None, salida: Path | None = None) -> Path:
    ctx = D.contexto(periodo)
    actual = D.etiqueta_periodo(ctx.periodo)
    tc, tc_fecha = ctx.fx[ctx.periodo]
    unidades = (f"Montos en millones de USD (MM USD), al dolar observado de cierre: "
                f"{tc:,.2f} CLP por USD ({tc_fecha}). Fuente: CMF, Circular 1835, "
                f"publicacion {ctx.zip_vigente}.")
    salida = salida or SALIDA / f"stock_aseguradoras_{ctx.periodo}.xlsx"
    L = Libro(ctx.fecha_cierre, unidades)

    # --- datos ---------------------------------------------------------------
    stock = D.stock_por_clase(ctx)
    comp_aseg, comp_clase = D.comparativa(ctx)
    ops = D.derivados(ctx)
    periodos_noc = {**ctx.cierres, actual: ctx.periodo}
    nocs = D.nocional_derivados(ctx, periodos_noc)
    for k, p in periodos_noc.items():
        if p is not None:          # sin derivados ese mes = 0; mes no disponible queda vacio
            nocs[k] = nocs[k].fillna(0.0)
    nocs = nocs.rename(columns={k: f"noc_{k}" for k in periodos_noc})
    stock = stock.merge(nocs[["rut_compania", f"noc_{actual}"]], on="rut_compania", how="left")
    stock[f"noc_{actual}"] = stock[f"noc_{actual}"].fillna(0.0)
    comp_aseg = comp_aseg.merge(nocs, on="rut_compania", how="left")
    for k, p in periodos_noc.items():
        if p is not None:
            comp_aseg[f"noc_{k}"] = comp_aseg[f"noc_{k}"].fillna(0.0)

    # --- portada -----------------------------------------------------------------
    ws = L.wb.create_sheet("Portada")
    L._encabezado(ws, "Stock de inversiones de las aseguradoras - Circular 1835 CMF",
                  "Foto del balance al cierre del periodo, en USD. Sin flujos.", unidades)
    cierres_txt = "; ".join(f"{k}: {ctx.fx[p][0]:,.2f} ({ctx.fx[p][1]})" if p else f"{k}: no disponible"
                            for k, p in ctx.cierres.items())
    meta = pd.DataFrame([
        ("Datos al", f"{ctx.fecha_cierre:%d-%m-%Y}"),
        ("Periodo", f"{ctx.periodo} ({actual})"),
        ("Fecha de corte: definicion", "Ultimo dia del mes: la CMF define VALOR_FINAL 'a la fecha de cierre del mes' "
                                       "y la cabecera de los archivos solo informa AAAAMM"),
        ("Publicacion CMF usada", f"{ctx.zip_vigente}, descargada el {ctx.fecha_publicacion}"),
        ("Moneda", "USD (millones)"),
        ("Dolar del periodo", f"{tc:,.2f} CLP por USD, observado del {tc_fecha} (mindicador.cl, contrastado con api.gael.cloud)"),
        ("Dolar de los cierres comparados", cierres_txt),
        ("Aseguradoras con datos", f"{len(stock)}"),
        ("Operaciones de derivados", f"{int((ops.familia != 'Pacto').sum()):,} (mas {int((ops.familia == 'Pacto').sum()):,} pactos)"),
        ("Generado", f"{_dt.datetime.now():%d-%m-%Y %H:%M}"),
    ], columns=["Campo", "Valor"])
    fila = L.tabla(ws, meta, [Col("Campo", "Campo", "texto", "Atributo del reporte"),
                              Col("Valor", "Valor", "texto_largo", "Valor del atributo")],
                   "tbl_portada", hoja="Portada")

    ind = stock[D.CLASES + ["Total", "Total declarado B.8"]].sum()
    conceptos = [f"{c} ({actual})" for c in ind.index] + [f"Derivados: nocional, NO suma al total ({actual})"]
    valores = list(ind.values.astype(float)) + [float(stock[f"noc_{actual}"].sum())]
    kpi = pd.DataFrame({"Concepto": conceptos, "Valor (MM USD)": valores})
    fila = L.tabla(ws, kpi, [Col("Concepto", "Concepto", "texto", "Total de la industria por clase de activo"),
                             Col("Valor (MM USD)", "Valor (MM USD)", "mm", "Suma de todas las aseguradoras")],
                   "tbl_industria", fila=fila + 3, hoja="Portada")

    idx = [("Stock_Clase", "tbl_stock_clase", f"Stock por aseguradora y clase de activo, {actual}, y nocional de derivados aparte"),
           ("Comparativa", "tbl_comparativa", "Stock total por aseguradora: Dic-2023, Dic-2024, Dic-2025 y periodo actual, "
                                              "y nocional de derivados aparte"),
           ("Comparativa_Clase", "tbl_comparativa_clase", "Lo mismo, abierto por clase de activo"),
           ("Evol_Stock", "tbl_evol_stock", "Stock total por aseguradora, mes a mes desde Dic-2024"),
           ("Evol_Stock_Clase", "tbl_evol_stock_clase", "Stock mensual por aseguradora y clase de activo"),
           ("Renta_Fija", "tbl_renta_fija", "Detalle tailor-made: renta fija, nacional e internacional"),
           ("Acciones", "tbl_acciones", "Detalle tailor-made: acciones"),
           ("ETF", "tbl_etf", "Detalle tailor-made: ETF"),
           ("Fondos_Inversion", "tbl_fondos_inversion", "Detalle tailor-made: fondos de inversion"),
           ("Fondos_Mutuos", "tbl_fondos_mutuos", "Detalle tailor-made: fondos mutuos"),
           ("Real_Estate", "tbl_real_estate", "Detalle tailor-made: bienes raices propios y en leasing"),
           ("Otras_Inversiones", "tbl_otras_inversiones", "Detalle tailor-made: otras inversiones"),
           ("Deriv_Aseguradora", "tbl_deriv_aseguradora", "Nocional de derivados por aseguradora y por instrumento"),
           ("Deriv_Contraparte", "tbl_deriv_contraparte", "Nocional por contraparte, entidad legal por entidad legal"),
           ("Deriv_Aseg_x_Contraparte", "tbl_deriv_aseg_contraparte", "Cruce aseguradora por contraparte"),
           ("Deriv_Subyacente", "tbl_deriv_subyacente", "Nocional por activo subyacente"),
           ("Evol_Deriv_Aseguradora", "tbl_evol_deriv_aseguradora", "Nocional de derivados mensual por aseguradora"),
           ("Evol_Deriv_Contraparte", "tbl_evol_deriv_contraparte", "Nocional de derivados mensual por contraparte legal"),
           ("CCS", "tbl_ccs", "Detalle tailor-made: cross currency swaps"),
           ("Swap_Promesa", "tbl_swap_promesa", "Detalle tailor-made: swaps UF contra pesos"),
           ("Forward_FX", "tbl_forward_fx", "Detalle tailor-made: forwards de moneda"),
           ("Forward_UF", "tbl_forward_uf", "Detalle tailor-made: forwards de inflacion"),
           ("IRS", "tbl_irs", "Detalle tailor-made: swaps de tasa"),
           ("Opciones", "tbl_opciones", "Detalle tailor-made: opciones"),
           ("Futuros", "tbl_futuros", "Detalle tailor-made: futuros"),
           ("Pactos", "tbl_pactos", "Pactos, fuera del total de derivados"),
           ("Calidad_Contrapartes", "tbl_calidad_contrapartes", "Identificadores de contraparte con alertas"),
           ("Diccionario", "tbl_diccionario", "Unidad y definicion de cada columna")]
    fila = L.tabla(ws, pd.DataFrame(idx, columns=["Hoja", "Tabla", "Contenido"]),
                   [Col("Hoja", "Hoja", "texto", "Pestana"), Col("Tabla", "Tabla", "texto", "Tabla de Excel"),
                    Col("Contenido", "Contenido", "texto_largo", "Que contiene")],
                   "tbl_indice", fila=fila + 3, hoja="Portada")

    se_va = [a for a in ctx.aseguradoras.itertuples()
             if a.rut_compania in set(comp_aseg.rut_compania)
             and pd.isna(comp_aseg.set_index("rut_compania").get(actual, pd.Series()).get(a.rut_compania))]
    notas = [
        "Stock: valor final informado en cada anexo de detalle (B.1 a B.7), sin flujos.",
        "Derivados en el stock: se suma su valor razonable neto, que es lo que la aseguradora registra en su "
        "balance y declara en el B.8. El nocional va en columnas aparte y NO suma al total: es el tamano de "
        "referencia del contrato, no lo que vale. Sumarlo seria mezclar peras con manzanas.",
        "Real Estate = bienes raices del B.4, propios y en leasing. El leasing se cuenta una vez: se excluye del "
        "B.1, donde tambien figura como contrato. Contarlo dos veces no cuadra con el total declarado.",
        "Hojas de detalle por clase (Renta_Fija a Otras_Inversiones): la suma del valor final de cada hoja es "
        "igual a su columna en Stock_Clase.",
        "Cuadratura: 'Total declarado B.8' es lo que cada aseguradora declara como total de inversiones "
        "(representativas + no representativas). En la industria el detalle queda "
        f"{(stock.Total.sum() / stock['Total declarado B.8'].sum() - 1) * 100:+.2f}% del declarado.",
        "Cada periodo se convierte a USD con el dolar de SU cierre: la variacion entre cierres incluye el "
        "efecto cambiario.",
        "Dic-2023 no esta disponible: su ZIP usa la generacion anterior del layout (B.1 a B.6 cambiaron de "
        "formato). La columna existe vacia y se llenara al transcribir esa generacion.",
        "Pactos (repos) fuera del total de derivados: son financiamiento. Tienen hoja propia.",
        "Contrapartes por identificador legal (RUT o LEI): no se agrupan filiales bajo su matriz. Los nombres de "
        "LEI vienen de GLEIF. Ver Calidad_Contrapartes para LEI invalidos o de otra entidad.",
        "Publicacion vigente: si un periodo tiene dos publicaciones se usa la mas reciente en todas las tablas.",
        "En ene-2026 Seguros Vida Security Prevision deja de informar y BICE Vida sube en un monto "
        "equivalente (dic-2025: BICE 8.106 + Vida Security 4.344 = 12.450 MM USD; ene-2026: BICE 13.213, "
        "+6,1%, igual a la apreciacion del peso ese mes). La variacion de BICE contra Dic-2025 refleja la "
        "fusion, no crecimiento organico.",
    ]
    if se_va:
        notas.append("Aseguradoras con datos en algun cierre pero no en el periodo actual: "
                     + "; ".join(a.aseguradora for a in se_va) + ".")
    L.tabla(ws, pd.DataFrame({"N": range(1, len(notas) + 1), "Nota": notas}),
            [Col("N", "N", "entero", "Numero de nota"), Col("Nota", "Nota", "texto_largo", "Nota metodologica")],
            "tbl_notas", fila=fila + 3, hoja="Portada")
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 95

    RUT_A = [Col("RUT aseguradora", "rut_compania", "id", "RUT de la compania"),
             Col("Aseguradora", "aseguradora", "texto", "Nombre publicado en la CMF")]

    # --- stock por clase -------------------------------------------------------------
    cl = [Col(c, c, "mm", d, total=True) for c, d in [
        ("Renta Fija", "B.1 local (sin leasing) + B.5 renta fija extranjera"),
        ("Equity", "Acciones: codigos AC* del B.2 y del B.5"),
        ("ETF", "ETF: codigos ETF* del B.5"),
        ("Fondos de Inversion", "Cuotas de fondos de inversion: codigos CFI* del B.2 y del B.5"),
        ("Fondos Mutuos", "B.3 completo + codigos CFM* del B.5"),
        ("Real Estate", "B.4: bienes raices propios y en leasing"),
        ("de la cual leasing", "Parte de Real Estate que son bienes raices dados en leasing (CLEAS)"),
        ("Otros", "B.6: otras inversiones (caja, prestamos, avances, mobiliario)"),
        ("Derivados (valor razonable neto)", "B.7 sin pactos: valor razonable activo menos pasivo. Es lo que suma al "
                                             "stock; el nocional va aparte"),
        ("Sin clasificar", "Codigos de instrumento que no calzan con ninguna clase; deberia ser 0"),
        ("Total", "Suma de las clases (sin contar 'de la cual leasing' dos veces). No incluye el nocional")]]
    noc_actual = Col(f"Derivados: nocional, fuera del total (MM USD)", f"noc_{actual}", "mm",
                     "Nocional de derivados, sin pactos. Informativo: NO suma al Total, porque el nocional es el "
                     "tamano de referencia del contrato y no lo que vale", total=True)
    L.hoja(Hoja("Stock_Clase", "tbl_stock_clase", f"Stock por clase de activo - {actual}",
                "Cuanto tiene invertido cada aseguradora en cada clase de activo. El nocional de derivados va al "
                "final, aparte, y no suma al total.",
                RUT_A + cl + [
                    Col("Total declarado B.8", "Total declarado B.8", "mm",
                        "Total que declara la aseguradora en el B.8 (representativas + no representativas)", total=True),
                    Col("Diferencia vs B.8 (%)", "Diferencia vs B.8 (%)", "pct", "Total / total declarado - 1"),
                    noc_actual],
                stock, total_etiqueta="Total industria (fuera de la tabla)"))

    # --- comparativas --------------------------------------------------------------------
    per_cols = [Col(f"{k} (MM USD)", k, "mm",
                    f"Stock total al cierre de {k}" + (" (no disponible: generacion anterior del layout)" if p is None else ""),
                    total=True) for k, p in ctx.cierres.items()]
    per_cols.append(Col(f"{actual} (MM USD)", actual, "mm", "Stock total al cierre del periodo actual", total=True))
    base = list(ctx.cierres)[-1]
    var_cols = [Col(f"Var. {base} a {actual} (MM USD)", f"Var. {base} a {actual} (MM USD)", "mm",
                    "Diferencia entre el periodo actual y el ultimo cierre de anio", total=True),
                Col(f"Var. {base} a {actual} (%)", f"Var. {base} a {actual} (%)", "pct",
                    "Variacion porcentual, incluye efecto cambiario")]
    noc_cols = [Col(f"Nocional derivados {k} (MM USD)", f"noc_{k}", "mm",
                    f"Nocional de derivados (sin pactos) al cierre de {k}. Informativo: no suma al stock"
                    + (" (no disponible)" if p is None else ""), total=True)
                for k, p in periodos_noc.items()]
    L.hoja(Hoja("Comparativa", "tbl_comparativa", "Comparativa historica por aseguradora",
                "Stock total del periodo actual contra los cierres de anio, cada uno a su dolar de cierre. "
                "Al final, el nocional de derivados de cada cierre, aparte.",
                RUT_A + per_cols + var_cols + noc_cols,
                comp_aseg, total_etiqueta="Total industria (fuera de la tabla)"))
    L.hoja(Hoja("Comparativa_Clase", "tbl_comparativa_clase", "Comparativa historica por clase de activo",
                "Mismo cruce, abierto por clase de activo.",
                RUT_A + [Col("Clase de activo", "clase", "texto", "Clase de activo")]
                + [Col(c.header, c.fuente, c.fmt, c.descripcion) for c in per_cols + var_cols],
                comp_clase, congelar_cols=3))

    # --- evolucion mensual del stock --------------------------------------------------------------
    ev_a, ev_c = D.evolucion_stock(ctx)
    meses = [D.etiqueta_periodo(p) for p in D.periodos_disponibles(ctx)]
    nota_tc = "Cada mes al dolar observado de su propio cierre: la variacion incluye efecto cambiario."

    def cols_meses(que: str):
        return [Col(f"{m} (MM USD)", m, "mm", f"{que} al cierre de {m}", total=True) for m in meses]

    L.hoja(Hoja("Evol_Stock", "tbl_evol_stock", "Evolucion mensual del stock por aseguradora",
                f"Stock total mes a mes, {meses[0]} a {meses[-1]}. {nota_tc}",
                RUT_A + cols_meses("Stock total"), ev_a,
                total_etiqueta="Total industria (fuera de la tabla)"))
    L.hoja(Hoja("Evol_Stock_Clase", "tbl_evol_stock_clase", "Evolucion mensual del stock por clase de activo",
                f"Mismo cruce, abierto por clase de activo. {nota_tc}",
                RUT_A + [Col("Clase de activo", "clase", "texto", "Clase de activo")]
                + [Col(c.header, c.fuente, c.fmt, c.descripcion) for c in cols_meses("Stock de la clase")],
                ev_c, congelar_cols=3))

    # --- detalle tailor-made de las inversiones ----------------------------------------------------
    for h in hojas_inversiones(ctx):
        L.hoja(h)

    # --- derivados: resumenes ------------------------------------------------------------------
    fams = [f for f in D.FAMILIAS]

    def cols_noc(df):
        out = [Col("Nocional total derivados (MM USD)", "Nocional total derivados (MM USD)", "mm",
                   "Suma lineal de nocionales, sin pactos", total=True),
               Col("Operaciones", "Operaciones", "entero", "Cantidad de operaciones (sin pactos)", total=True)]
        out += [Col(f"{f} (MM USD)", f"{f} (MM USD)", "mm", f"Nocional en {f}", total=True)
                for f in fams if f"{f} (MM USD)" in df.columns]
        out += [Col("MTM neto (MM USD)", "MTM neto (MM USD)", "mm", "Valor razonable neto", total=True),
                Col("Pactos, fuera del total (MM USD)", "Pactos, fuera del total (MM USD)", "mm",
                    "Nocional de pactos, de referencia", total=True)]
        return out

    da = D.deriv_por_aseguradora(ops)
    L.hoja(Hoja("Deriv_Aseguradora", "tbl_deriv_aseguradora", "Derivados por aseguradora",
                "Suma lineal de nocionales por aseguradora y por tipo de instrumento.",
                RUT_A + cols_noc(da), da, total_etiqueta="Total industria (fuera de la tabla)"))
    dc = D.deriv_por_contraparte(ops)
    L.hoja(Hoja("Deriv_Contraparte", "tbl_deriv_contraparte", "Derivados por contraparte (entidad legal)",
                "Cada RUT o LEI es una entidad: filiales y matrices por separado.",
                [C_CP[1], Col("Tipo ID", "entidad_tipo_id", "texto", "RUT, LEI, invalido o sin identificador"),
                 C_CP[0], C_CP[2],
                 Col("Aseguradoras", "aseguradoras", "entero", "Aseguradoras con operaciones vigentes con la entidad")]
                + cols_noc(dc) + [C_CP[3]],
                dc, total_etiqueta="Total (fuera de la tabla)"))
    dx = D.deriv_aseg_x_contraparte(ops)
    L.hoja(Hoja("Deriv_Aseg_x_Contraparte", "tbl_deriv_aseg_contraparte", "Derivados: aseguradora por contraparte",
                "Una fila por par aseguradora - entidad legal.",
                RUT_A + [C_CP[1], C_CP[0]] + cols_noc(dx), dx, congelar_cols=2))
    ds = D.deriv_por_subyacente(ops)
    L.hoja(Hoja("Deriv_Subyacente", "tbl_deriv_subyacente", "Derivados por activo subyacente",
                "Nocional por subyacente: pares de monedas, tasa por indice, equity.",
                [Col("Instrumento", "familia", "texto", "Tipo de derivado"),
                 Col("Subyacente", "subyacente", "texto", "Par de monedas (orden canonico), tasa e indice, o equity"),
                 Col("Nocional (MM USD)", "Nocional (MM USD)", "mm", "Suma lineal de nocionales", total=True),
                 Col("Operaciones", "Operaciones", "entero", "Cantidad de operaciones", total=True),
                 Col("Aseguradoras", "Aseguradoras", "entero", "Aseguradoras con posicion"),
                 Col("Contrapartes", "Contrapartes", "entero", "Entidades legales distintas"),
                 Col("MTM neto (MM USD)", "MTM neto (MM USD)", "mm", "Valor razonable neto", total=True)],
                ds, total_etiqueta="Total (fuera de la tabla)"))

    # --- evolucion mensual de derivados -----------------------------------------------------------
    ev_da, ev_dc = D.evolucion_derivados(ctx)
    L.hoja(Hoja("Evol_Deriv_Aseguradora", "tbl_evol_deriv_aseguradora",
                "Evolucion mensual del nocional de derivados por aseguradora",
                f"Suma lineal de nocionales, sin pactos. {nota_tc}",
                RUT_A + cols_meses("Nocional de derivados (sin pactos)"), ev_da,
                total_etiqueta="Total industria (fuera de la tabla)"))
    L.hoja(Hoja("Evol_Deriv_Contraparte", "tbl_evol_deriv_contraparte",
                "Evolucion mensual del nocional de derivados por contraparte legal",
                f"Cada RUT o LEI es una entidad: filiales y matrices por separado. Sin pactos. {nota_tc}",
                [C_CP[1], Col("Tipo ID", "entidad_tipo_id", "texto", "RUT, LEI, invalido o sin identificador"),
                 C_CP[0], C_CP[2]] + cols_meses("Nocional de derivados con la entidad (sin pactos)"),
                ev_dc, total_etiqueta="Total (fuera de la tabla)"))

    # --- hojas tailor-made de derivados --------------------------------------------------------------
    for h in hojas_instrumentos(ops):
        L.hoja(h)

    # --- calidad de contrapartes ---------------------------------------------------------------------
    cq = D.calidad_contrapartes(ops)
    L.hoja(Hoja("Calidad_Contrapartes", "tbl_calidad_contrapartes", "Calidad de identificacion de contrapartes",
                "Identificadores de contraparte (derivados, sin pactos) con alertas verificables.",
                [Col("ID legal contraparte", "entidad_id", "texto", "RUT o LEI informado"),
                 Col("Tipo ID", "entidad_tipo_id", "texto", "RUT, LEI, invalido o sin identificador"),
                 Col("Nombre legal", "entidad_nombre", "texto", "GLEIF, catalogo o nombre informado"),
                 Col("Nombres informados", "nombres_informados", "texto_largo", "Como lo escribieron las aseguradoras"),
                 Col("Catalogo dashboard", "catalogo_dashboard", "texto", "Entidad a la que lo asigna el dashboard"),
                 Col("Operaciones", "operaciones", "entero", "Operaciones vigentes en el periodo"),
                 Col("Aseguradoras", "aseguradoras", "entero", "Aseguradoras que lo informan"),
                 Col("Alerta", "entidad_alerta", "texto_largo", "Hechos verificables sobre el identificador")],
                cq, congelar_cols=1),
           unidades="Conteos de operaciones del periodo actual.")

    L.diccionario()
    L.guardar(salida)
    return salida
