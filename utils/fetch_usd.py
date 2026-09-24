"""
utils.fetch_usd
===============

Descarga la serie del dolar observado y la deja en disco.

Para que
--------
El reporte Excel expresa todo en USD. Los montos de la Circular 1835 vienen en
miles de pesos (M$), asi que cada monto se convierte como

    USD = M$ x 1.000 / dolar observado al cierre del periodo

y cada periodo usa el dolar de SU propio cierre: es la foto del balance en
USD, que es lo que se pidio. La consecuencia, que el reporte declara, es que la
variacion entre dos cierres incluye el efecto cambiario.

Que dolar es "el de cierre"
---------------------------
El ultimo dolar observado publicado con fecha menor o igual al ultimo dia del
mes. Si el mes cierra en fin de semana o feriado, rige el ultimo publicado
antes. Ejemplo: diciembre 2025 cierra el 31, pero el ultimo observado
publicado es el del 30 (911,18).

Por que un archivo en disco y no una llamada en linea
-----------------------------------------------------
La misma razon que utils.fetch_uf: si la conversion dependiera de si habia
internet, el mismo reporte daria numeros distintos segun la hora. Se descarga
una vez, se versiona con su fecha, y el exportador lee del disco.

Fuentes y control
-----------------
1. ``mindicador.cl``   -- serie diaria del dolar observado, un GET por anio.
2. ``api.gael.cloud``  -- ancla independiente: el dolar observado de hoy. Se
                          verifico que publica el MISMO numero que mindicador
                          (959,39 el 2026-09-24 en ambas), asi que una
                          diferencia es un error de parseo, no de definicion.

Si la serie y el ancla difieren en mas de 0,05% la descarga aborta y el archivo
bueno no se sobrescribe.

Uso
---
    python -m utils.fetch_usd               # 2023 -> hoy
    python -m utils.fetch_usd --verificar   # solo lee y resume lo que hay
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import re
import sys
import urllib.error
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Mismo cliente HTTP y mismo parser de montos chilenos que la serie UF: si uno
# se corrige, el otro hereda la correccion.
from utils.fetch_uf import _get, _monto_cl  # noqa: E402

_LOG = logging.getLogger("cmf1835.usd")

DESTINO = ROOT / "config" / "series" / "usd.json"

#: Desvio maximo aceptado contra el ancla. Dos fuentes que publican el mismo
#: dolar observado no deberian diferir en nada; 0,05% solo absorbe redondeo.
DESVIO_MAXIMO = Decimal("0.0005")


class USDError(RuntimeError):
    """La serie descargada no es confiable."""


def desde_mindicador(anio: int) -> dict[str, str]:
    """Serie diaria del dolar observado de un anio."""
    data = json.loads(_get(f"https://mindicador.cl/api/dolar/{anio}").decode("utf-8"))
    out: dict[str, str] = {}
    for punto in data.get("serie", []):
        fecha = str(punto["fecha"])[:10]
        valor = punto.get("valor")
        if valor:
            out[fecha] = str(Decimal(str(valor)))
    return out


def ancla_independiente() -> tuple[str, Decimal] | None:
    """Dolar observado de hoy segun una fuente distinta."""
    try:
        d = json.loads(_get("https://api.gael.cloud/general/public/monedas/USD").decode("utf-8"))
        valor = _monto_cl(str(d.get("Valor", "")))
        fecha = str(d.get("Fecha", ""))[:10]
        if valor and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
            return fecha, valor
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        _LOG.info("ancla independiente no disponible: %s", e)
    return None


def verificar_contra_ancla(serie: dict[str, str]) -> str:
    """Contrasta la serie contra el ancla. Devuelve una linea de resumen.

    Raises:
        USDError: si el desvio supera DESVIO_MAXIMO.
    """
    ancla = ancla_independiente()
    if not ancla:
        return "sin ancla independiente disponible: serie no contrastada"
    fecha, valor = ancla
    if fecha not in serie:
        return f"el ancla es del {fecha} y la serie no lo cubre: sin contraste"
    propio = Decimal(serie[fecha])
    desvio = abs(propio - valor) / valor
    if desvio > DESVIO_MAXIMO:
        raise USDError(
            f"la serie no cuadra con el ancla: {fecha} vale {propio} aqui y "
            f"{valor} en api.gael.cloud ({desvio * 100:.4f}%). No se guarda.")
    return f"ancla OK: {fecha} {propio} vs {valor} ({desvio * 100:.4f}%)"


def cierres_de_mes(serie: dict[str, str]) -> dict[str, dict[str, str]]:
    """{AAAAMM: {fecha, valor}} con el ultimo observado publicado del mes.

    Se exige que el ultimo publicado caiga DENTRO del mes: un mes sin ningun
    dato no hereda el dolar del mes anterior, queda fuera.
    """
    por_mes: dict[str, tuple[str, str]] = {}
    for fecha in sorted(serie):
        por_mes[fecha[:4] + fecha[5:7]] = (fecha, serie[fecha])
    return {p: {"fecha": f, "valor": v} for p, (f, v) in sorted(por_mes.items())}


def descargar(desde: int, hasta: int) -> dict[str, str]:
    serie: dict[str, str] = {}
    for anio in range(desde, hasta + 1):
        parte = desde_mindicador(anio)
        if not parte:
            raise USDError(f"mindicador no devolvio datos para {anio}")
        serie.update(parte)
        _LOG.info("%s: %s dias", anio, len(parte))
    return serie


def guardar(serie: dict[str, str], control: str, destino: Path = DESTINO) -> dict:
    doc = {
        "indicador": "dolar_observado",
        "unidad": "CLP por USD",
        "fuente": "mindicador.cl (dolar observado, Banco Central de Chile)",
        "convencion_cierre": "ultimo observado publicado con fecha <= ultimo dia del mes",
        "control": control,
        "descargado_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "cierres": cierres_de_mes(serie),
        "dias": dict(sorted(serie.items())),
    }
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return doc


def leer(origen: Path = DESTINO) -> dict[int, tuple[float, str]]:
    """{AAAAMM: (dolar, fecha_del_dato)}. Vacio si el archivo no esta."""
    if not origen.exists():
        return {}
    doc = json.loads(origen.read_text(encoding="utf-8"))
    return {int(p): (float(c["valor"]), c["fecha"]) for p, c in doc["cierres"].items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Serie del dolar observado a disco.")
    ap.add_argument("--desde", type=int, default=2023)
    ap.add_argument("--hasta", type=int, default=_dt.date.today().year)
    ap.add_argument("--verificar", action="store_true",
                    help="no descarga; resume el archivo presente")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    if a.verificar:
        c = leer()
        if not c:
            print(f"no hay serie en {DESTINO}")
            return 1
        print(f"{DESTINO}: {len(c)} cierres de mes, {min(c)} a {max(c)}")
        return 0

    serie = descargar(a.desde, a.hasta)
    control = verificar_contra_ancla(serie)
    print(f"  {control}")
    doc = guardar(serie, control)
    print(f"escrito {DESTINO}: {len(serie)} dias, {len(doc['cierres'])} cierres de mes")
    for p in ("202312", "202412", "202512"):
        if p in doc["cierres"]:
            c = doc["cierres"][p]
            print(f"   cierre {p}: {c['valor']} ({c['fecha']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
