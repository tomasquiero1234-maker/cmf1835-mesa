"""
warehouse.loader
================

Materializa la cartera en Parquet particionado por periodo y monta un
esquema estrella encima con DuckDB.

Modelo
------
Dimensiones conformadas (compartidas por todos los hechos):

    dim_periodo       AAAAMM, fecha de cierre y UF de ese cierre
    dim_compania      la aseguradora que informa
    dim_contraparte   contraparte resuelta, con su grupo economico
    dim_instrumento   el papel (nemotecnico, tipo, moneda, emisor)

Hechos, uno por grano:

    fact_derivado     un registro de B.7 (opciones, forwards, futuros,
                      swaps y pactos: los cinco tipos, no solo forwards)
    fact_renta_fija   un registro de detalle de B.1
    fact_garantia     un registro de detalle de B.14
    fact_cuarentena   lo que no cerro, con su linea cruda

Bitemporalidad
--------------
La CMF republica: el mismo periodo puede venir dos veces con contenido
distinto -- 202608 existe en tres versiones, de 96, 209 y 240 archivos. Por
eso cada fila lleva DOS tiempos:

    periodo_informacion   el mes al que se refiere el dato
    fecha_descarga        cuando obtuvimos ESA publicacion

y ademas ``zip_origen``, que identifica la publicacion concreta. Con eso se
puede preguntar "que sabiamos de junio el 15 de julio" sin que una
republicacion posterior reescriba la historia. Las vistas ``v_*`` se quedan
con la ultima publicacion de cada periodo, que es lo que uno quiere el 99%
del tiempo; los hechos crudos conservan todo.

Uso
---
    python -m warehouse.loader --data "/ruta/a/los/ZIP"
    python -m warehouse.loader --data ./zips --periodo 202608
    python -m warehouse.loader --data ./zips --out ./warehouse/data
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import re
import sys
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parse.engine import FixedWidthEngine, ParsedRecord, UnknownFileTypeError
from validate.arithmetic import ArithmeticValidator, Verdict
from normalize.entities import EntityResolver

_LOG = logging.getLogger("cmf1835.warehouse")

LAYOUTS = ROOT / "config" / "layouts" / "cmf_v2024.yaml"
ENTITIES = ROOT / "config" / "entities.yaml"
UF_JSON = ROOT / "config" / "series" / "uf.json"
DEFAULT_OUT = ROOT / "warehouse" / "data"

#: Nombre legible de cada tipo de registro de B.7. El grano de fact_derivado
#: es un registro de CUALQUIERA de estos cinco: la mesa opera todos.
PRODUCTO = {
    "2": "OPCION",
    "3": "FORWARD",
    "4": "FUTURO",
    "5": "SWAP",
    "6": "PACTO",
}

#: De donde sale el nocional en cada producto. La CMF no usa un campo unico,
#: asi que se declara la fuente por tipo y se guarda en `nocional_origen`:
#: un nocional sin procedencia es un numero que nadie puede auditar.
_NOCIONAL: dict[str, tuple[str, ...]] = {
    "2": ("MONTO(activo)", "MONTO(pasivo)"),
    "3": ("NOCIONAL_POSICION_LARGA(monto)", "NOCIONAL_POSICION_CORTA(monto)"),
    "4": ("NOCIONAL_POSICION_LARGA(monto)", "NOCIONAL_POSICION_CORTA(monto)"),
    "5": ("NOCIONAL_POSICION_LARGA(monto)", "NOCIONAL_POSICION_CORTA(monto)"),
    "6": ("ACTIVO_OBJETO(monto)(M$)",),
}

#: Tasa o precio PACTADO en el contrato, por producto. Cada derivado expresa
#: su precio en un campo distinto; la vista de price discovery necesita una
#: sola columna comparable, con su procedencia al lado.
_TASA_CONTRATO: dict[str, tuple[str, ...]] = {
    "2": ("PRECIO_DE_EJERCICIO",),
    "3": ("PRECIO_FORWARD_CONTRATO",),
    "4": ("PRECIO_FUTURO_DE_MERCADO_AL_INICIO_DE_LA_OPERACION",),
    "5": ("TASA_A_FUTURO_CONTRATO_POSICION_LARGA", "TASA_A_FUTURO_CONTRATO_POSICION_CORTA"),
    "6": ("TASA_PACTO", "TIR_COMPRA"),
}

#: El mismo precio, pero de mercado a la fecha de informacion. La diferencia
#: contra el pactado es justamente lo que la mesa quiere ver.
_TASA_MERCADO: dict[str, tuple[str, ...]] = {
    "2": ("PRECIO_SPOT_DEL_ACTIVO_SUBYACENTE",),
    "3": ("PRECIO_FORWARD_MERCADO",),
    "4": ("PRECIO_FUTURO_DE_MERCADO_A_LA_FECHA_DE_INFORMACION",),
    "5": ("TASA_A_FUTURO_MERCADOPOSICION_LARGA", "TASA_A_FUTURO_MERCADO_POSICION_CORTA"),
    "6": ("TASA_O_PRECIO_DE_MERCADO",),
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _num(v: Any) -> float | None:
    """Pasa a float para Parquet. Decimal es exacto pero no es un tipo columnar."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(str(v).strip().replace(",", ""))
    except (ValueError, InvalidOperation):
        return None


def _txt(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


_RE_TASA = re.compile(r"(-?\d+(?:[.,]\d+)?)")


def _tasa_mixta(v: Any) -> tuple[float | None, str | None]:
    """Separa una tasa que viene como texto en un numero y su tipo.

    En los swaps la CMF informa la tasa del contrato como texto:
    ``'FIJA 5.11'``, ``'VARIABLE 3.2'``. Pasarla por float() devuelve None y
    deja los 69.639 swaps sin tasa, que es justo el producto mas grande del
    libro. Se parte en el numero y la etiqueta, y se guardan los dos.
    """
    if v is None:
        return None, None
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return float(v), None
    s = str(v).strip()
    if not s:
        return None, None
    m = _RE_TASA.search(s)
    num = float(m.group(1).replace(",", ".")) if m else None
    # Lo que queda tras sacar el numero trae basura de formato ('FIJA %',
    # 'FIJA  %'); se deja solo la etiqueta.
    etiqueta = _RE_TASA.sub(" ", s)
    etiqueta = re.sub(r"[^A-Za-z_]+", " ", etiqueta).strip().upper() or None
    return num, etiqueta


def _first_tasa(f: Mapping[str, Any],
                nombres: Iterable[str]) -> tuple[float | None, str | None, str | None]:
    """Primera tasa con valor, con su tipo y el campo del que salio."""
    for n in nombres:
        num, etiqueta = _tasa_mixta(f.get(n))
        if num is not None:
            return num, etiqueta, n
    return None, None, None


def _first(f: Mapping[str, Any], nombres: Iterable[str]) -> tuple[float | None, str | None]:
    """Primer campo con valor, junto con el nombre del que salio."""
    for n in nombres:
        v = _num(f.get(n))
        if v is not None and v != 0:
            return v, n
    for n in nombres:                      # si todos son cero, igual se reporta
        if f.get(n) is not None:
            return _num(f.get(n)), n
    return None, None


#: Familias de tasa flotante que aparecen en la pata de un swap. Todo lo que
#: no es FIJA se considera flotante para decidir quien paga fijo.
_FLOTANTES = ("ICP", "LIBOR", "SOFR", "TAB", "VAR", "CAMARA", "TPM", "EURIBOR")


def _es_fija(etiqueta: str | None) -> bool | None:
    if not etiqueta:
        return None
    e = etiqueta.upper()
    if "FIJA" in e or "FIJO" in e:
        return True
    return False if any(k in e for k in _FLOTANTES) else None


#: Codificacion oficial de TIPO_CONTRATO en B.7 (anexo, pagina 137):
#: el subyacente sobre el que esta hecho el contrato.
SUBYACENTE = {"1": "TASA_O_INFLACION", "2": "MONEDA_EXTRANJERA", "3": "ACCIONES_O_INDICES"}


def _clasificar_swap(tipo_contrato: str | None,
                     moneda_larga: str | None, moneda_corta: str | None,
                     fija_larga: bool | None, fija_corta: bool | None) -> tuple[str, str]:
    """Separa IRS de CCS y dice quien paga fijo.

    La separacion sale de TIPO_CONTRATO, que es la codificacion oficial del
    anexo: 01 es tasa o inflacion, 02 es moneda extranjera. NO se deduce de
    las etiquetas de moneda de cada pata, y vale la pena explicar por que,
    porque es la trampa obvia:

    El 91% de los swaps informa MONEDA_POSICION_CORTA = 'PROM', que no es una
    moneda sino una convencion de precio. En esos registros el nocional largo
    dividido por T.C._FUTURO_CONTRATO da un numero redondo exacto (933.420 /
    933,42 = 1.000), o sea que la pata corta esta expresada contra un tipo de
    cambio, no contra una moneda declarada. Clasificar por el texto de la
    moneda acierta de casualidad en este caso y falla en cuanto un informante
    escriba otra cosa.

    Que la mayoria sean CCS no es raro: las aseguradoras chilenas calzan en UF
    y compran bonos en moneda extranjera, asi que el cross currency es su
    cobertura natural. Por la misma razon abundan los fijo-contra-fijo.
    """
    cod = str(tipo_contrato).strip().lstrip("0") if tipo_contrato is not None else ""
    if cod == "1":
        subtipo = "IRS"
    elif cod == "2":
        subtipo = "CCS"
    else:
        # Sin codificacion utilizable se cae a las monedas, y si tampoco
        # alcanzan se dice que no se sabe en vez de inventar un subtipo.
        ml = (moneda_larga or "").strip().upper()
        mc = (moneda_corta or "").strip().upper()
        if ml and mc and ml in _UNIDADES and mc in _UNIDADES:
            subtipo = "IRS" if ml == mc else "CCS"
        else:
            subtipo = "SWAP_SIN_CLASIFICAR"

    if fija_larga is True and fija_corta is False:
        rol = "RECIBE_FIJA"
    elif fija_larga is False and fija_corta is True:
        rol = "PAGA_FIJA"
    elif fija_larga is True and fija_corta is True:
        rol = "FIJA_CONTRA_FIJA"
    elif fija_larga is False and fija_corta is False:
        rol = "FLOTANTE_CONTRA_FLOTANTE"
    else:
        rol = "SIN_DETERMINAR"
    return subtipo, rol


#: Unidades de denominacion reconocibles. 'PROM' no esta a proposito: es una
#: convencion de precio, no una moneda.
_UNIDADES = {"UF", "$$", "CLP", "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD",
             "UVR", "UDI", "BRL", "MXN", "PEN", "COP", "NOK", "SEK", "DKK"}


def _duracion(plazo_meses: float | None, tir: float | None,
              cupon: float | None, dias_reales: float | None = None) -> float | None:
    """Duracion modificada APROXIMADA de un bono bullet, en anios.

    OJO CON LA ESCALA: PLAZO_AL_VENCIMIENTO del anexo viene en MESES, no en
    dias. La mediana informada es 200 contra 6.084 dias reales hasta el
    vencimiento -- un factor de 30,4. Dividirlo por 365 da duraciones de medio
    anio para una cartera de aseguradora de vida, que es absurdo.

    Se prefiere el plazo calculado desde la fecha de vencimiento cuando esta,
    porque no depende de la unidad que use el informante.

    El anexo no trae el calendario de cupones -- vive en B.10, sin transcribir --
    asi que esto es la formula cerrada de un bullet con cupon anual. Para un
    papel amortizable la duracion real es MENOR. Sirve para ordenar y comparar,
    no para calcular un hedge, y por eso la columna se llama `_aprox`.
    """
    if dias_reales and dias_reales > 0:
        n = dias_reales / 365.0
    elif plazo_meses and plazo_meses > 0:
        n = plazo_meses / 12.0
    else:
        return None
    y = (tir or cupon or 0) / 100.0
    if y <= -0.99:
        return None
    c = (cupon or 0) / 100.0
    if c <= 0 or abs(y) < 1e-9:
        return round(n / (1 + y), 4)
    try:
        mac = ((1 + y) / y) - ((1 + y + n * (c - y)) / (c * ((1 + y) ** n - 1) + y))
        d = mac / (1 + y)
    except (ZeroDivisionError, OverflowError, ValueError):
        return None
    return round(d, 4) if 0 < d < 100 else None


def _fin_de_mes(periodo: int) -> _dt.date:
    a, m = divmod(int(periodo), 100)
    return (_dt.date(a + (m == 12), (m % 12) + 1, 1) - _dt.timedelta(days=1))


def _uf_por_periodo() -> dict[int, tuple[float, str]]:
    import json
    if not UF_JSON.exists():
        _LOG.warning("Sin serie UF (%s): dim_periodo ira sin valor de UF", UF_JSON)
        return {}
    doc = json.loads(UF_JSON.read_text(encoding="utf-8"))
    out: dict[int, tuple[float, str]] = {}
    for per, d in (doc.get("cierre_mes") or {}).items():
        if isinstance(d, dict):
            out[int(per)] = (float(d["valor"]), str(d["fecha"]))
        else:
            out[int(per)] = (float(d), "")
    return out


# ---------------------------------------------------------------------------
# construccion de filas
# ---------------------------------------------------------------------------

class RowBuilder:
    """Convierte un ParsedRecord en la fila del hecho que le corresponde."""

    def __init__(self, resolver: EntityResolver, validator: ArithmeticValidator) -> None:
        self.resolver = resolver
        self.validator = validator

    def _base(self, rec: ParsedRecord, zip_origen: str, descargado: str) -> dict[str, Any]:
        return {
            "periodo_informacion": rec.periodo,
            "fecha_descarga": descargado,
            "zip_origen": zip_origen,
            "source_file": rec.source_file,
            "line_no": rec.line_no,
            "rut_compania": rec.rut_compania,
        }

    def _contraparte(self, f: Mapping[str, Any]) -> dict[str, Any]:
        r = self.resolver.resolve(
            rut=f.get("RUT_CONTRAPARTE_NACIONAL"),
            dv=f.get("DV_CONTRAPARTE_NACIONAL"),
            name=f.get("NOMBRE") or f.get("NOMBRE_CONTRAPARTE_GARANTIA"),
            identifier=f.get("LEI_CONTRAPARTE_EXTRANJERA"),
        )
        row = r.to_row()
        row["contraparte_rut"] = _num(f.get("RUT_CONTRAPARTE_NACIONAL"))
        row["contraparte_lei"] = _txt(f.get("LEI_CONTRAPARTE_EXTRANJERA"))
        return row

    def derivado(self, rec: ParsedRecord, zip_origen: str, descargado: str,
                 veredicto: Any) -> dict[str, Any]:
        f = rec.fields
        nocional, origen = _first(f, _NOCIONAL.get(rec.record_type, ()))
        tasa_c, tasa_tipo, origen_tasa = _first_tasa(f, _TASA_CONTRATO.get(rec.record_type, ()))
        tasa_m, _, _ = _first_tasa(f, _TASA_MERCADO.get(rec.record_type, ()))
        largo, _ = _first(f, ("NOCIONAL_POSICION_LARGA(monto)", "ACTIVO_OBJETO_POSICION_LARGA(monto)"))
        corto, _ = _first(f, ("NOCIONAL_POSICION_CORTA(monto)", "ACTIVO_OBJETO_POSICION_CORTA(monto)"))
        ml = _txt(f.get("MONEDA_POSICION_LARGA"))
        mc = _txt(f.get("MONEDA_POSICION_CORTA"))
        _, tipo_larga, _ = _first_tasa(f, ("TASA_A_FUTURO_CONTRATO_POSICION_LARGA",))
        _, tipo_corta, _ = _first_tasa(f, ("TASA_A_FUTURO_CONTRATO_POSICION_CORTA",))
        tasa_larga, _, _ = _first_tasa(f, ("TASA_A_FUTURO_CONTRATO_POSICION_LARGA",))
        tasa_corta, _, _ = _first_tasa(f, ("TASA_A_FUTURO_CONTRATO_POSICION_CORTA",))
        fija_larga, fija_corta = _es_fija(tipo_larga), _es_fija(tipo_corta)
        producto = PRODUCTO.get(rec.record_type, rec.record_type)
        if rec.record_type == "5":
            subtipo, rol_fija = _clasificar_swap(
                f.get("TIPO_CONTRATO"), ml, mc, fija_larga, fija_corta)
        else:
            subtipo, rol_fija = producto, None

        # MTM exacto del contrato: el anexo lo trae con signo en un campo
        # propio. Reconstruirlo como activo menos pasivo es una aproximacion;
        # este es el numero que la compania declara.
        mtm_contrato = _num(f.get("VALOR_RAZONABLE_DEL_CONTRATO_A_LA_FECHA_DE_LA_INFORMACION_(M$)")
                            or f.get("VALOR_RAZONABLE_DEL_CONTRATO_A_LA_FECHA_DE_LA_INFORMACION"))

        row = self._base(rec, zip_origen, descargado)
        row.update({
            "producto": producto,
            "subtipo": subtipo,
            "subyacente_contrato": SUBYACENTE.get(
                str(f.get("TIPO_CONTRATO")).strip().lstrip("0"), None),
            "rol_tasa_fija": rol_fija,
            "moneda_larga": ml,
            "moneda_corta_swap": mc,
            "par_monedas": (f"{ml}/{mc}" if ml and mc and ml != mc else None),
            "pata_larga_tipo": tipo_larga,
            "pata_corta_tipo": tipo_corta,
            "pata_larga_tasa": tasa_larga,
            "pata_corta_tasa": tasa_corta,
            "spread_patas_pb": (round((tasa_larga - tasa_corta) * 100, 2)
                                if tasa_larga is not None and tasa_corta is not None else None),
            "mtm_contrato_m": mtm_contrato,
            "valor_presente_largo_m": _num(f.get("VALOR_PRESENTE_")),
            "valor_presente_corto_m": _num(f.get("VALOR_PRESENTE_POSICION_CORTA(M$)")),
            "tipo_cambio_contrato": _num(f.get("T.C._FUTURO_CONTRATO")),
            "tipo_cambio_mercado": _num(f.get("TIPO_DE_CAMBIO_MERCADO")),
            "efecto_resultados_m": _num(f.get("EFECTO_EN_RESULTADOS_REALIZADOS")),
            "anexo_garantia": _txt(f.get("ANEXO_GARANTIA")),
            "identificador_garantia": _txt(f.get("IDENTIFICADOR_GARANTIA")),
            "origen_informacion": _txt(f.get("ORIGEN_DE_LA_INFORMACION")),
            "nombre_cartera": _txt(f.get("NOMBRE_CARTERA")),
            # --- pactos: la linea de financiamiento --------------------------
            "tasa_pacto": _num(f.get("TASA_PACTO")),
            "tir_pacto_compra": _num(f.get("TIR_COMPRA")),
            "activo_subyacente": _txt(f.get("ACTIVO_OBJETO(nombre)")),
            "serie_subyacente": _txt(f.get("SERIE_ACTIVO_OBJETO")),
            "monto_subyacente_m": _num(f.get("ACTIVO_OBJETO(monto)(M$)")),
            "record_type": rec.record_type,
            "folio_operacion": _txt(f.get("FOLIO_OPERACION")),
            "item_operacion": _txt(f.get("ITEM_OPERACION")),
            "tipo_operacion": _txt(f.get("TIPO_OPERACION")),
            "objetivo_contrato": _txt(f.get("OBJETIVO_CONTRATO")),
            "fecha_operacion": f.get("FECHA_DE_LA_OPERACION"),
            "fecha_vencimiento": f.get("FECHA_DE_VENCIMIENTO_DEL_CONTRATO"),
            "moneda": _txt(f.get("MONEDA") or f.get("MONEDA_POSICION_LARGA")),
            "moneda_corta": _txt(f.get("MONEDA_POSICION_CORTA")),
            "nocional_m": nocional,
            "nocional_origen": origen,
            "nocional_largo_m": largo,
            "nocional_corto_m": corto,
            "mtm_activo_m": _num(f.get("MONTO(activo)")),
            "mtm_pasivo_m": _num(f.get("MONTO(pasivo)")),
            "margen_m": _num(f.get("MONTO_ACTIVOS_EN_MARGEN")),
            "tasa_precio_contrato": tasa_c,
            "tasa_precio_origen": origen_tasa,
            "tasa_tipo": tasa_tipo,
            "tasa_precio_mercado": tasa_m,
            "precio_spot": _num(f.get("PRECIO_SPOT_DEL_ACTIVO_SUBYACENTE") or f.get("PRECIO_SPOT_")),
            "tasa_descuento": _num(f.get("TASA_DESCUENTO_DE_FLUJOS")),
            "tir_compra": _num(f.get("TIR_COMPRA")),
            "clasificacion_riesgo": _txt(f.get("CLASIFICACION_DE_RIESGO")),
            "tipo_contrato": _txt(f.get("TIPO_CONTRATO")),
            "tipo_contraparte": _txt(f.get("TIPO_CONTRAPARTE")),
            "cm_compensacion_bilateral": _txt(f.get("CM_COMPENSACION_BILATERAL")),
            "tipo_documentacion": _txt(f.get("TIPO_DOCUMENTACION")),
            "activo_objeto_largo": _txt(f.get("ACTIVO_OBJETO_POSICION_LARGA(nombre)")),
            "activo_objeto_corto": _txt(f.get("ACTIVO_OBJETO_POSICION_CORTA_(nombre)")),
            "relacionado": _txt(f.get("RELACIONADO")),
            "nacionalidad_contraparte": _txt(f.get("NACIONALIDAD")),
            "clasif_valoriz_eeff": _txt(f.get("METOD_CLASIF_VALORIZ_EEFF")),
            "veredicto": veredicto.verdict.value,
        })
        row.update(self._contraparte(f))
        return row

    def renta_fija(self, rec: ParsedRecord, zip_origen: str, descargado: str,
                   veredicto: Any) -> dict[str, Any]:
        f = rec.fields
        row = self._base(rec, zip_origen, descargado)
        row.update({
            "nemotecnico": _txt(f.get("NEMOTECNICO")),
            "tipo_instrumento": _txt(f.get("TIPO_INSTRUMENTO")),
            "serie": _txt(f.get("SERIE")),
            "pais": _txt(f.get("PAIS")),
            "emisor_rut": _num(f.get("NRO_RUT")),
            "emisor_dv": _txt(f.get("DIG_RUT")),
            "unidad_monetaria": _txt(f.get("UNIDAD_MONETARIA")),
            "valor_nominal": _num(f.get("VALOR_NOMINAL")),
            "valor_nominal_vigente": _num(f.get("VALOR_NOMINAL_VIGENTE")),
            "valor_compra": _num(f.get("VALOR_COMPRA")),
            "valor_comercial_mp": _num(f.get("VALOR_COMERCIAL_MP")),
            "valor_comercial_um": _num(f.get("VALOR_COMERCIAL_UM")),
            "valor_final": _num(f.get("VALOR_FINAL_B1")),
            "tasa_emision": _num(f.get("TASA_EMISION")),
            "fecha_emision": f.get("FECHA_EMISION"),
            "fecha_compra": f.get("FECHA_COMPRA"),
            "fecha_vencimiento": f.get("FECHA_VENCIMIENTO"),
            "clasif_valoriz_eeff": _txt(f.get("METOD_CLASIF_VALORIZ_EEFF")),
            "clasificacion_riesgo": _txt(f.get("CLASIFICACION_DE_RIESGO")),
            "clasificacion_inversion": _txt(f.get("CLASIFICACION_INVERSION")),
            "incremento_riesgo": _txt(f.get("INCREMENTO_RIESGO")),
            "plazo_meses": _num(f.get("PLAZO_AL_VENCIMIENTO")),
            "tasa_base": _num(f.get("TASA_BASE")),
            "spread_emision": _num(f.get("SPREAD_A_LA_EMISION")),
            "tir_compra": _num(f.get("TIR_COMPRA")),
            "tir_mercado": _num(f.get("TIR_MERCADO")),
            "tir_sin_costo": _num(f.get("TIR_SIN_COSTO")),
            "tasa_pacto": _num(f.get("TASA_PACTO")),
            "fuente_precios": _txt(f.get("FUENTE_PRECIOS")),
            "valor_par": _num(f.get("VALOR_PAR")),
            "porcentaje_valor_par": _num(f.get("PORCENTAJE_VALOR_PAR")),
            "duracion_modificada_aprox": _duracion(
                _num(f.get("PLAZO_AL_VENCIMIENTO")),
                _num(f.get("TIR_MERCADO")) or _num(f.get("TIR_COMPRA")),
                _num(f.get("TASA_EMISION")),
                dias_reales=((f["FECHA_VENCIMIENTO"] - _fin_de_mes(rec.periodo)).days
                             if f.get("FECHA_VENCIMIENTO") and rec.periodo else None)),
            "prohibicion": _txt(f.get("PROHIBICION")),
            "custodia": _txt(f.get("CUSTODIA_INV")),
            "veredicto": veredicto.verdict.value,
        })
        return row

    def equity(self, rec: ParsedRecord, zip_origen: str, descargado: str,
               veredicto: Any) -> dict[str, Any]:
        """Renta variable y cuotas de fondos de inversion (B.2).

        Aqui esta la exposicion a equity y el AUM declarado de las filiales:
        participacion porcentual, patrimonio y resultado de la sociedad.
        """
        f = rec.fields
        row = self._base(rec, zip_origen, descargado)
        row.update({
            "tipo_instrumento": _txt(f.get("TIPO_INSTRUMENTO")),
            "nemotecnico": _txt(f.get("NEMOTECNICO")),
            "serie": _txt(f.get("SERIE")),
            "emisor_rut": _num(f.get("RUT")),
            "emisor_dv": _txt(f.get("VERIFICADOR")),
            "rut_fondo": _num(f.get("RUT_FONDO_P")),
            "nombre_fondo": _txt(f.get("NOMBRE_FONDO_P") or f.get("NOMBRE_DEL_FONDO")),
            "unidades": _num(f.get("UNIDADES")),
            "presencia_bursatil": _num(f.get("PRES_BURSATIL")),
            "unidad_monetaria": _txt(f.get("UNIDAD_MONETARIA")),
            "valor_costo": _num(f.get("VALOR_COSTO")),
            "valor_libro": _num(f.get("VALOR_LIBRO")),
            "valor_bolsa": _num(f.get("VALOR_BOLSA")),
            "valor_razonable": _num(f.get("VALOR_RAZONABLE")),
            "deterioro": _num(f.get("DETERIORO")),
            "valor_final": _num(f.get("VALOR_FINAL")),
            "participacion_pct": _num(f.get("PARTICIPACION_PORCENTUAL")
                                      or f.get("PORCENTAJE_PARTICIPACION")),
            "patrimonio_filial": _num(f.get("PATRIMONIO_FILIAL")),
            "resultado_filial": _num(f.get("RESULTADO_DE_LA_SOCIEDAD_FILIAL")),
            "cuotas_suscritas": _num(f.get("CUOTAS_SUSCRITAS")),
            "clasificacion_riesgo": _txt(f.get("CLASIFICACION_DE_RIESGO")),
            "filial_coligada": _txt(f.get("FILIAL_COLIGADA")),
            "tipo_fondo": _txt(f.get("TIPO_FONDO_ACC")),
            "segmento_fondo": _txt(f.get("SEGMENTO_FONDO")),
            "subyacente": _txt(f.get("SUBYACENTE")),
            "relacionado": _txt(f.get("RELACIONADO")),
            "custodio": _txt(f.get("NOMBRE_CUSTODIO")),
            "nombre_cartera": _txt(f.get("NOMBRE_CARTERA")),
            "clasif_valoriz_eeff": _txt(f.get("METOD_CLASIF_VALORIZ_EEFF")),
            "veredicto": veredicto.verdict.value,
        })
        return row

    def fondo(self, rec: ParsedRecord, zip_origen: str, descargado: str,
              veredicto: Any) -> dict[str, Any]:
        """Cuotas de fondos mutuos (B.3)."""
        f = rec.fields
        row = self._base(rec, zip_origen, descargado)
        row.update({
            "tipo_instrumento": _txt(f.get("TIPO_INSTRUMENTO")),
            "nemotecnico": _txt(f.get("NEMOTECNICO")),
            "tipo_fondo": _txt(f.get("TIPO_FONDO")),
            "serie": _txt(f.get("SERIE")),
            "rut_administradora": _num(f.get("RUT_ADMINISTRADORA")),
            "unidades": _num(f.get("UNIDADES")),
            "unidad_monetaria": _txt(f.get("UNIDAD_MONETARIA")),
            "valor_cuota": _num(f.get("VALOR_CUOTA")),
            "valor_inversion": _num(f.get("VALOR_DE_INVERSION")),
            "valor_razonable": _num(f.get("VALOR_RAZONABLE")),
            "deterioro": _num(f.get("DETERIORO")),
            "valor_final": _num(f.get("VALOR_FINAL")),
            "clasificacion_riesgo": _txt(f.get("CLASIFICACION_DE_RIESGO")),
            "relacionado": _txt(f.get("RELACIONADO")),
            "nombre_fondo": _txt(f.get("NOMBRE_DEL_FONDO")),
            "nombre_cartera": _txt(f.get("NOMBRE_CARTERA")),
            "custodio": _txt(f.get("NOMBRE_CUSTODIO")),
            "clasif_valoriz_eeff": _txt(f.get("METOD_CLASIF_VALORIZ_EEFF")),
            "veredicto": veredicto.verdict.value,
        })
        return row

    def garantia(self, rec: ParsedRecord, zip_origen: str, descargado: str,
                 veredicto: Any) -> dict[str, Any]:
        f = rec.fields
        row = self._base(rec, zip_origen, descargado)
        row.update({
            # Quien postea a quien: POSICION_COMPANIA dice si la aseguradora
            # entrega o recibe el colateral. Sin ese campo la tabla no
            # distingue las dos direcciones y el mapa no sirve.
            "posicion_compania": _txt(f.get("POSICION_COMPANIA")),
            "tipo_garantia": _txt(f.get("TIPO_GARANTIA")),
            "tipo_activo": _txt(f.get("TIPO_ACTIVO_EN_GARANTIA")),
            "identificador_garantia": _txt(f.get("IDENTIFICADOR_GARANTIA")),
            "folio_instrumento": _txt(f.get("FOLIO_INSTRUMENTO_EN_GARANTIA")),
            "item_instrumento": _txt(f.get("ITEM_INSTRUMENTO_EN_GARANTIA")),
            "codigo_instrumento": _txt(f.get("CODIGO_IDENTIFICACION_INSTRUMENTO_EN_GARANTIA")),
            "valor_nominal_instrumento": _num(f.get("VALOR_NOMINAL_INSTRUMENTO_EN_GARANTIA")),
            "moneda": _txt(f.get("MONEDA_DENOMINACION_ACTIVO_EN_GARANTIA")),
            "clasificacion_riesgo_pais": _txt(f.get("CLASIFICACION_DE_RIESGO_PAIS")),
            "valor_contable_um": _num(f.get("VALOR_CONTABLE_ACTIVO_EN_GARANTIA_UM")),
            "valor_contable_m": _num(f.get("VALOR_CONTABLE_ACTIVO_EN_GARANTIA_M$")),
            "valor_razonable_um": _num(f.get("VALOR_RAZONABLE_ACTIVO_EN_GARANTIA_UM")),
            "monto_m": _num(f.get("VALOR_RAZONABLE_ACTIVO_EN_GARANTIA_M$")),
            "cuenta_eeff": _txt(f.get("CUENTA_EEFF_DE_REGISTRO_DE_LA_GARANTIA")),
            "relacionado": _txt(f.get("RELACIONADO")),
            "nacionalidad_contraparte": _txt(f.get("NACIONALIDAD_CONTRAPARTE_GARANTIA")),
            "veredicto": veredicto.verdict.value,
        })
        row.update(self._contraparte(f))
        return row

    def cuarentena(self, rec: ParsedRecord, zip_origen: str, descargado: str,
                   veredicto: Any) -> dict[str, Any]:
        row = self._base(rec, zip_origen, descargado)
        row.update({
            "archivo": rec.letter,
            "anexo": rec.anexo,
            "record_type": rec.record_type,
            "veredicto": veredicto.verdict.value,
            "motivos": " | ".join(veredicto.reasons)[:2000],
            # La linea cruda es el punto de esta tabla: sin ella la cuarentena
            # es una queja, no una pista.
            "raw": rec.raw,
        })
        return row


# ---------------------------------------------------------------------------
# carga
# ---------------------------------------------------------------------------

class Loader:
    """Recorre los ZIP y escribe Parquet particionado por periodo."""

    HECHOS = ("fact_derivado", "fact_renta_fija", "fact_garantia", "fact_cuarentena",
              "fact_equity", "fact_fondo", "dim_compania_src")

    def __init__(self, out: Path, *, layouts: Path = LAYOUTS, entities: Path = ENTITIES) -> None:
        self.out = out
        self.engine = FixedWidthEngine.from_yaml(layouts)
        self.validator = ArithmeticValidator()
        self.resolver = EntityResolver.from_yaml(entities)
        self.build = RowBuilder(self.resolver, self.validator)
        self.contadores: dict[str, int] = {h: 0 for h in self.HECHOS}
        self.saltados_por_layout = 0

    # -- recorrido -----------------------------------------------------------

    def _zip_rows(self, zpath: Path) -> Iterator[tuple[str, dict[str, Any]]]:
        descargado = _dt.datetime.fromtimestamp(
            zpath.stat().st_mtime, _dt.timezone.utc).date().isoformat()
        with zipfile.ZipFile(zpath) as z:
            for info in sorted(z.infolist(), key=lambda i: i.filename):
                if info.is_dir():
                    continue
                base = Path(info.filename).name
                try:
                    spec, _rut, _per = self.engine.describe(base)
                except UnknownFileTypeError:
                    continue
                if spec.letter not in ("I", "P", "G", "A", "F"):
                    continue
                for rec in self.engine.parse_file(base, data=z.read(info)):
                    # La generacion vieja (pre 202412) tiene otro largo de
                    # registro. El motor ya la marca; aqui simplemente no entra
                    # al warehouse: el scope es la generacion nueva.
                    if rec.fields.get("_layout_mismatch"):
                        self.saltados_por_layout += 1
                        continue
                    tabla_fila = self._fila(rec, zpath.name, descargado)
                    if tabla_fila:
                        yield tabla_fila

    def _fila(self, rec: ParsedRecord, zip_origen: str,
              descargado: str) -> tuple[str, dict[str, Any]] | None:
        L, t = rec.letter, rec.record_type
        # El nombre de la aseguradora vive en el registro de identificacion,
        # no en el detalle. Sin el, el Whitespace Map muestra RUT desnudos.
        if t == "1":
            return "dim_compania_src", {
                "rut_compania": rec.rut_compania,
                "nombre": _txt(rec.fields.get("NOMBRE")),
                "dv": _txt(rec.fields.get("VERIFICADOR")),
                "periodo_informacion": rec.periodo,
            }
        if L == "P" and t in PRODUCTO:
            v = self.validator.validate(L, t, rec.fields,
                                        untrusted=rec.untrusted, periodo=rec.periodo)
            if v.verdict is Verdict.QUARANTINE:
                return "fact_cuarentena", self.build.cuarentena(rec, zip_origen, descargado, v)
            return "fact_derivado", self.build.derivado(rec, zip_origen, descargado, v)
        if L == "I" and t == "2":
            v = self.validator.validate(L, t, rec.fields,
                                        untrusted=rec.untrusted, periodo=rec.periodo)
            if v.verdict is Verdict.QUARANTINE:
                return "fact_cuarentena", self.build.cuarentena(rec, zip_origen, descargado, v)
            return "fact_renta_fija", self.build.renta_fija(rec, zip_origen, descargado, v)
        if L == "A" and t == "2":
            v = self.validator.validate(L, t, rec.fields,
                                        untrusted=rec.untrusted, periodo=rec.periodo)
            return "fact_equity", self.build.equity(rec, zip_origen, descargado, v)
        if L == "F" and t == "2":
            v = self.validator.validate(L, t, rec.fields,
                                        untrusted=rec.untrusted, periodo=rec.periodo)
            return "fact_fondo", self.build.fondo(rec, zip_origen, descargado, v)
        if L == "G" and t == "2":
            v = self.validator.validate(L, t, rec.fields,
                                        untrusted=rec.untrusted, periodo=rec.periodo)
            return "fact_garantia", self.build.garantia(rec, zip_origen, descargado, v)
        return None

    # -- escritura -----------------------------------------------------------

    def cargar(self, zips: list[Path]) -> dict[str, int]:
        """Escribe un Parquet por (hecho, periodo)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        # {(hecho, periodo): [filas]}. Un mes completo de todas las companias
        # son ~150k filas: cabe de sobra, y escribir por periodo deja cada
        # particion en un solo archivo en vez de mil fragmentos.
        for zp in zips:
            buffers: dict[tuple[str, int], list[dict[str, Any]]] = {}
            _LOG.info("--- %s", zp.name)
            for tabla, fila in self._zip_rows(zp):
                per = fila.get("periodo_informacion") or 0
                buffers.setdefault((tabla, per), []).append(fila)
                self.contadores[tabla] += 1
            for (tabla, per), filas in sorted(buffers.items()):
                destino = self.out / tabla / f"periodo={per}"
                destino.mkdir(parents=True, exist_ok=True)
                # El nombre lleva el ZIP de origen: dos publicaciones del mismo
                # periodo conviven en la particion en vez de pisarse.
                slug = re.sub(r"[^A-Za-z0-9]+", "_", zp.stem).strip("_")
                pq.write_table(
                    pa.Table.from_pylist(filas),
                    destino / f"{slug}.parquet",
                    compression="zstd",
                )
        return dict(self.contadores)


# ---------------------------------------------------------------------------
# esquema estrella en DuckDB
# ---------------------------------------------------------------------------

DDL = """
-- ===========================================================================
--  Esquema estrella sobre el Parquet particionado.
--  Los hechos son VIEW sobre los archivos: no se duplica un byte y agregar
--  un periodo nuevo es dejar caer su carpeta, sin recargar nada.
-- ===========================================================================

CREATE OR REPLACE VIEW raw_derivado   AS SELECT * FROM read_parquet('{root}/fact_derivado/*/*.parquet',   union_by_name=true, hive_partitioning=true);
CREATE OR REPLACE VIEW raw_renta_fija AS SELECT * FROM read_parquet('{root}/fact_renta_fija/*/*.parquet', union_by_name=true, hive_partitioning=true);
CREATE OR REPLACE VIEW raw_garantia   AS SELECT * FROM read_parquet('{root}/fact_garantia/*/*.parquet',   union_by_name=true, hive_partitioning=true);
CREATE OR REPLACE VIEW raw_equity     AS SELECT * FROM read_parquet('{root}/fact_equity/*/*.parquet',     union_by_name=true, hive_partitioning=true);
CREATE OR REPLACE VIEW raw_fondo      AS SELECT * FROM read_parquet('{root}/fact_fondo/*/*.parquet',      union_by_name=true, hive_partitioning=true);
CREATE OR REPLACE VIEW raw_cuarentena AS SELECT * FROM read_parquet('{root}/fact_cuarentena/*/*.parquet', union_by_name=true, hive_partitioning=true);

-- --- bitemporal ------------------------------------------------------------
-- Ultima publicacion de cada periodo. La CMF republica (202608 existe en tres
-- versiones distintas), asi que "el dato de junio" depende de cuando preguntes.
CREATE OR REPLACE VIEW publicacion_vigente AS
WITH todas AS (
    SELECT periodo_informacion, zip_origen, fecha_descarga, COUNT(*) AS filas
    FROM raw_derivado GROUP BY 1,2,3
    UNION ALL
    SELECT periodo_informacion, zip_origen, fecha_descarga, COUNT(*)
    FROM raw_renta_fija GROUP BY 1,2,3
)
SELECT periodo_informacion, zip_origen, fecha_descarga, SUM(filas) AS filas,
       ROW_NUMBER() OVER (PARTITION BY periodo_informacion
                          ORDER BY fecha_descarga DESC, SUM(filas) DESC) AS recencia
FROM todas GROUP BY 1,2,3;

-- --- dimensiones -----------------------------------------------------------
CREATE OR REPLACE TABLE dim_periodo AS
SELECT DISTINCT
    d.periodo_informacion                                   AS periodo,
    CAST(d.periodo_informacion / 100 AS INTEGER)            AS anio,
    CAST(d.periodo_informacion % 100 AS INTEGER)            AS mes,
    LAST_DAY(STRPTIME(CAST(d.periodo_informacion AS VARCHAR) || '01', '%Y%m%d')) AS fecha_cierre,
    u.uf_cierre,
    u.uf_fecha
FROM (SELECT DISTINCT periodo_informacion FROM raw_derivado
      UNION SELECT DISTINCT periodo_informacion FROM raw_renta_fija) d
LEFT JOIN uf_cierre_mes u ON u.periodo = d.periodo_informacion;

CREATE OR REPLACE TABLE dim_compania AS
SELECT
    rut_compania,
    -- El nombre mas reciente gana: una compania se fusiona o cambia de razon
    -- social y no queremos el de hace dos anios.
    ARG_MAX(nombre, periodo_informacion) AS nombre,
    ANY_VALUE(dv)                        AS dv,
    COUNT(*)                             AS publicaciones
FROM read_parquet('{root}/dim_compania_src/*/*.parquet', union_by_name=true, hive_partitioning=true)
WHERE rut_compania IS NOT NULL
GROUP BY rut_compania;

CREATE OR REPLACE TABLE dim_contraparte AS
SELECT
    contraparte_key,
    ANY_VALUE(contraparte_nombre)  AS nombre,
    ANY_VALUE(contraparte_grupo)   AS grupo,
    ANY_VALUE(contraparte_tipo)    AS tipo,
    ANY_VALUE(contraparte_pais)    AS pais,
    ANY_VALUE(resolucion_metodo)   AS metodo,
    MAX(resolucion_confianza)      AS confianza,
    COUNT(*)                       AS operaciones
FROM (SELECT * FROM raw_derivado UNION ALL BY NAME SELECT * FROM raw_garantia)
GROUP BY contraparte_key;

CREATE OR REPLACE TABLE dim_instrumento AS
SELECT
    nemotecnico,
    ANY_VALUE(tipo_instrumento) AS tipo_instrumento,
    ANY_VALUE(unidad_monetaria) AS unidad_monetaria,
    ANY_VALUE(emisor_rut)       AS emisor_rut,
    ANY_VALUE(pais)             AS pais,
    MAX(fecha_vencimiento)      AS fecha_vencimiento,
    COUNT(*)                    AS observaciones
FROM raw_renta_fija
WHERE nemotecnico IS NOT NULL
GROUP BY nemotecnico;

-- --- hechos vigentes -------------------------------------------------------
-- Lo que uno quiere el 99% del tiempo: cada periodo en su ultima publicacion.
CREATE OR REPLACE VIEW fact_derivado AS
SELECT r.* FROM raw_derivado r
JOIN publicacion_vigente v
  ON v.periodo_informacion = r.periodo_informacion
 AND v.zip_origen = r.zip_origen AND v.recencia = 1;

CREATE OR REPLACE VIEW fact_renta_fija AS
SELECT r.* FROM raw_renta_fija r
JOIN publicacion_vigente v
  ON v.periodo_informacion = r.periodo_informacion
 AND v.zip_origen = r.zip_origen AND v.recencia = 1;

CREATE OR REPLACE VIEW fact_equity     AS SELECT * FROM raw_equity;
CREATE OR REPLACE VIEW fact_fondo      AS SELECT * FROM raw_fondo;
CREATE OR REPLACE VIEW fact_garantia   AS SELECT * FROM raw_garantia;
CREATE OR REPLACE VIEW fact_cuarentena AS SELECT * FROM raw_cuarentena;
"""


def construir_duckdb(out: Path, db: Path) -> dict[str, int]:
    """Crea el esquema estrella sobre el Parquet ya escrito."""
    import duckdb
    import json

    con = duckdb.connect(str(db))
    # La UF entra como tabla porque dim_periodo la necesita y es chica.
    uf = _uf_por_periodo()
    con.execute("CREATE OR REPLACE TABLE uf_cierre_mes "
                "(periodo INTEGER, uf_cierre DOUBLE, uf_fecha VARCHAR)")
    if uf:
        con.executemany("INSERT INTO uf_cierre_mes VALUES (?, ?, ?)",
                        [(p, v, f) for p, (v, f) in sorted(uf.items())])

    con.execute(DDL.format(root=out.as_posix()))

    conteos: dict[str, int] = {}
    for t in ("fact_derivado", "fact_renta_fija", "fact_equity", "fact_fondo",
              "fact_garantia", "fact_cuarentena",
              "dim_periodo", "dim_compania", "dim_contraparte", "dim_instrumento"):
        try:
            conteos[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception as e:                      # noqa: BLE001
            _LOG.error("no se pudo contar %s: %s", t, e)
            conteos[t] = -1
    con.close()
    return conteos


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Carga la cartera a Parquet + DuckDB.")
    p.add_argument("--data", type=Path, required=True, help="Carpeta con los ZIP mensuales.")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Raiz del warehouse.")
    p.add_argument("--periodo", help="Filtra un periodo AAAAMM.")
    p.add_argument("--db", type=Path, default=None, help="Ruta del archivo DuckDB.")
    p.add_argument("--solo-esquema", action="store_true",
                   help="No recarga el Parquet; solo reconstruye el esquema.")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="  [%(levelname)s] %(message)s")
    db = a.db or (a.out / "cmf1835.duckdb")
    a.out.mkdir(parents=True, exist_ok=True)

    if not a.solo_esquema:
        zips = sorted(z for z in a.data.rglob("*.zip") if z.is_file())
        if a.periodo:
            zips = [z for z in zips if a.periodo in z.name]
        if not zips:
            print(f"No hay ZIP en {a.data}", file=sys.stderr)
            return 2
        print(f"Cargando {len(zips)} ZIP a {a.out}")
        loader = Loader(a.out)
        conteos = loader.cargar(zips)
        print("\nFilas escritas por hecho:")
        for t, n in conteos.items():
            print(f"   {t:18} {n:>12,}")
        if loader.saltados_por_layout:
            print(f"   {'(generacion vieja)':18} {loader.saltados_por_layout:>12,} saltados")

    print(f"\nConstruyendo esquema estrella en {db}")
    conteos = construir_duckdb(a.out, db)
    print("\nObjetos del warehouse:")
    for t, n in conteos.items():
        print(f"   {t:18} {n:>12,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
