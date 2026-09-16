"""
utils.fetch_uf
==============

Descarga la serie historica de la Unidad de Fomento y la deja en disco.

Por que un archivo en disco y no una llamada en linea desde el validador
--------------------------------------------------------------------------
El motor valida cada registro contra sus propias identidades internas. Meter
una llamada de red adentro de esa validacion significaria que el veredicto de
un registro depende de si habia internet a esa hora: la misma linea daria PASS
hoy y UNVERIFIED manana. La serie se descarga UNA vez, se versiona, y el
validador la lee del disco. Una corrida es reproducible o no sirve.

Fuentes
-------
Se prueban en orden y se usa la primera que responda:

1. ``mindicador.cl``  -- JSON limpio, sin autenticacion, un GET por anio.
2. ``sii.cl``         -- tabla HTML oficial del Servicio de Impuestos
                         Internos. Mas fea de parsear, pero es la fuente
                         autoritativa y no pide llave.

Las dos entregan el MISMO numero porque las dos publican el valor fijado por
el Banco Central. Si alguna vez discrepan, es un error de la fuente y hay que
mirarlo, no promediarlo.

Uso
---
    python -m utils.fetch_uf                   # 2023 -> hoy
    python -m utils.fetch_uf --desde 2020      # mas historia
    python -m utils.fetch_uf --force           # reescribe aunque exista
    python -m utils.fetch_uf --fuente sii      # forzar una fuente
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Iterable

_LOG = logging.getLogger("cmf1835.uf")

ROOT = Path(__file__).resolve().parents[1]
DESTINO = ROOT / "config" / "series" / "uf.json"

_TIMEOUT = 30
_UA = "cmf1835/1.0 (motor ETL Circular 1835; contacto: mesa de dinero)"

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


class UFError(RuntimeError):
    """La serie no se pudo obtener de ninguna fuente."""


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------

def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read()


def _monto_cl(texto: str) -> Decimal | None:
    """Convierte '39.643,59' (formato chileno) a Decimal('39643.59').

    El punto es separador de miles y la coma es el decimal. Hacerlo al reves
    multiplica la UF por mil y el error no se nota hasta que un nominal
    reajustado sale absurdo.
    """
    t = re.sub(r"[^\d.,]", "", texto or "").strip()
    if not t:
        return None
    t = t.replace(".", "").replace(",", ".")
    try:
        v = Decimal(t)
    except Exception:
        return None
    return v if v > 0 else None


# ---------------------------------------------------------------------------
# fuentes
# ---------------------------------------------------------------------------

def desde_mindicador(anio: int) -> dict[str, str]:
    """Serie diaria de un anio desde mindicador.cl."""
    data = json.loads(_get(f"https://mindicador.cl/api/uf/{anio}").decode("utf-8"))
    out: dict[str, str] = {}
    for punto in data.get("serie", []):
        # La fecha viene en ISO con zona; el dia es lo unico que importa.
        fecha = str(punto["fecha"])[:10]
        valor = punto.get("valor")
        if valor:
            out[fecha] = str(Decimal(str(valor)))
    return out


def desde_sii(anio: int) -> dict[str, str]:
    """Serie diaria de un anio desde la tabla oficial del SII.

    La pagina trae una tabla por mes y, OJO, el titulo del mes va DESPUES de
    su tabla en el HTML, no antes. Ademas arriba hay un menu de navegacion
    que repite los doce nombres de mes. Anclar cada tabla al encabezado
    anterior -- que es lo intuitivo -- corre la serie entera un mes, y el
    error no se nota porque la serie sigue siendo continua y creciente: solo
    queda 0,2% desviada, que es justo del orden de un mes de reajuste.

    Por eso el mes se toma del PRIMER encabezado que aparece DESPUES de la
    tabla, y por eso `descargar()` contrasta el resultado contra una fuente
    independiente antes de darlo por bueno.
    """
    html = _get(f"https://www.sii.cl/valores_y_fechas/uf/uf{anio}.htm").decode("latin-1")
    out: dict[str, str] = {}

    encabezados = [
        (m.start(), _MESES[m.group(1).lower()])
        for m in re.finditer(
            r">\s*(Enero|Febrero|Marzo|Abril|Mayo|Junio|Julio|Agosto|"
            r"Septiembre|Setiembre|Octubre|Noviembre|Diciembre)\s*<",
            html, re.I)
    ]
    for tabla in re.finditer(r"<table.*?</table>", html, re.S):
        siguientes = [mes for pos, mes in encabezados if pos > tabla.start()]
        if not siguientes:
            continue                      # tabla de cierre, sin mes propio
        mes = siguientes[0]
        for fila in re.findall(r"<tr[^>]*>(.*?)</tr>", tabla.group(0), re.S):
            celdas = [
                re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", fila, re.S)
            ]
            # Las celdas van en pares (dia, valor).
            for i in range(0, len(celdas) - 1, 2):
                dia_txt, val_txt = celdas[i], celdas[i + 1]
                if not re.fullmatch(r"\d{1,2}", dia_txt.strip()):
                    continue
                valor = _monto_cl(val_txt)
                if valor is None:
                    continue              # el SII deja vacios varios dia 31
                try:
                    fecha = _dt.date(anio, mes, int(dia_txt))
                except ValueError:
                    continue
                out[fecha.isoformat()] = str(valor)
    return out


def ancla_independiente() -> tuple[str, Decimal] | None:
    """UF de hoy segun una fuente distinta, para contrastar la serie.

    Un desfase de un mes en el parseo del SII deja una serie que se ve
    perfectamente sana -- continua, creciente, del orden de magnitud correcto --
    y solo esta 0,2% mal. La unica forma barata de cazarlo es preguntarle el
    valor de hoy a alguien mas.

    Returns:
        ``(fecha_iso, valor)`` o None si la fuente no responde.
    """
    try:
        d = json.loads(_get("https://api.gael.cloud/general/public/monedas/UF").decode("utf-8"))
        valor = _monto_cl(str(d.get("Valor", "")))
        fecha = str(d.get("Fecha", ""))[:10]
        if valor and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
            return fecha, valor
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        _LOG.info("ancla independiente no disponible: %s", e)
    return None


FUENTES = {"mindicador": desde_mindicador, "sii": desde_sii}


# ---------------------------------------------------------------------------
# orquestacion
# ---------------------------------------------------------------------------

def descargar(desde: int, hasta: int, fuentes: Iterable[str]) -> tuple[dict[str, str], str]:
    """Baja la serie diaria completa probando cada fuente en orden.

    Returns:
        ``(serie, nombre_fuente)`` -- serie es ``{'AAAA-MM-DD': 'valor'}``.

    Raises:
        UFError: si ninguna fuente respondio para ningun anio.
    """
    for nombre in fuentes:
        fn = FUENTES[nombre]
        serie: dict[str, str] = {}
        fallo = False
        for anio in range(desde, hasta + 1):
            try:
                trozo = fn(anio)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                _LOG.warning("fuente %s, anio %s: %s", nombre, anio, e)
                fallo = True
                break
            if not trozo:
                _LOG.warning("fuente %s, anio %s: sin datos", nombre, anio)
            serie.update(trozo)
            _LOG.info("  %s %s: %d dias", nombre, anio, len(trozo))
        if serie and not fallo:
            _verificar_contra_ancla(serie, nombre)
            return serie, nombre
        if serie and fallo:
            _LOG.warning("fuente %s incompleta (%d dias); se prueba la siguiente",
                         nombre, len(serie))
    raise UFError(f"ninguna fuente respondio (probadas: {list(fuentes)})")


def _verificar_contra_ancla(serie: dict[str, str], fuente: str) -> None:
    """Contrasta la serie recien bajada contra una fuente independiente.

    Raises:
        UFError: si la diferencia supera 0,05%. Un desfase de un mes en el
            parseo da ~0,2%, asi que el umbral lo caza sin saltar por el
            redondeo normal entre fuentes.
    """
    ancla = ancla_independiente()
    if not ancla:
        _LOG.warning("sin ancla independiente: la serie %s no se pudo contrastar", fuente)
        return
    fecha, valor_ancla = ancla
    if fecha not in serie:
        _LOG.warning("el ancla es del %s y la serie no lo cubre; sin contraste", fecha)
        return
    propio = Decimal(serie[fecha])
    desvio = abs(propio - valor_ancla) / valor_ancla
    if desvio > Decimal("0.0005"):
        raise UFError(
            f"la serie de {fuente} no cuadra con la fuente de contraste: "
            f"{fecha} vale {propio} aqui y {valor_ancla} alla "
            f"({desvio * 100:.4f}% de diferencia). Un desfase de un mes en el "
            f"parseo da exactamente este sintoma; revisar antes de usarla."
        )
    _LOG.info("ancla OK: %s %s vs %s (%.4f%%)", fecha, propio, valor_ancla, desvio * 100)


def cierres_de_mes(serie: dict[str, str]) -> dict[str, dict[str, str]]:
    """Ultimo valor PUBLICADO de cada mes, indexado por periodo AAAAMM.

    Es el numero que necesita el validador: la CMF valoriza la cartera al
    cierre del mes informado.

    Se guarda tambien la fecha de la que salio el valor, porque no siempre es
    el ultimo dia del calendario. La tabla del SII deja la celda vacia en
    varios 31 (mayo, julio, octubre y diciembre, entre otros), asi que para
    202505 el cierre sale del 30 y no del 31. La diferencia es un dia de
    devengo -- del orden de 0,017% -- pero se deja anotada en vez de
    interpolar un valor que nadie publico.

    Returns:
        ``{'202505': {'fecha': '2025-05-30', 'valor': '39075.41'}}``
    """
    ultimo: dict[str, tuple[str, str]] = {}
    for fecha, valor in serie.items():
        periodo = fecha[:4] + fecha[5:7]
        if periodo not in ultimo or fecha > ultimo[periodo][0]:
            ultimo[periodo] = (fecha, valor)
    return {
        per: {"fecha": f, "valor": v}
        for per, (f, v) in sorted(ultimo.items())
    }


def guardar(serie: dict[str, str], fuente: str, destino: Path) -> dict:
    destino.parent.mkdir(parents=True, exist_ok=True)
    fechas = sorted(serie)
    doc = {
        "indicador": "UF",
        "unidad": "CLP por UF",
        "fuente": fuente,
        "descargado_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "dias": len(serie),
        "rango": {"desde": fechas[0], "hasta": fechas[-1]} if fechas else {},
        "cierre_mes": cierres_de_mes(serie),
        "diaria": {f: serie[f] for f in fechas},
    }
    destino.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    # Parquet es opcional: si pyarrow no esta, el JSON ya sirve.
    try:
        import pyarrow as pa, pyarrow.parquet as pq
        tabla = pa.table({
            "fecha": pa.array([_dt.date.fromisoformat(f) for f in fechas], pa.date32()),
            "valor": pa.array([float(serie[f]) for f in fechas], pa.float64()),
        })
        pq.write_table(tabla, destino.with_suffix(".parquet"))
        doc["parquet"] = str(destino.with_suffix(".parquet"))
    except ImportError:
        _LOG.info("pyarrow no instalado: se escribe solo JSON")
    return doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Descarga la serie historica de la UF.")
    p.add_argument("--desde", type=int, default=2023, help="Primer anio (default 2023).")
    p.add_argument("--hasta", type=int, default=_dt.date.today().year, help="Ultimo anio.")
    p.add_argument("--fuente", choices=sorted(FUENTES), action="append",
                   help="Forzar una fuente. Repetible; define el orden.")
    p.add_argument("--destino", type=Path, default=DESTINO)
    p.add_argument("--force", action="store_true", help="Reescribe aunque ya exista.")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="  [%(levelname)s] %(message)s")

    if a.destino.exists() and not a.force:
        doc = json.loads(a.destino.read_text(encoding="utf-8"))
        print(f"Ya existe {a.destino} ({doc.get('dias')} dias, "
              f"{doc.get('rango',{}).get('desde')} a {doc.get('rango',{}).get('hasta')}). "
              f"Usa --force para rebajarla.")
        return 0

    orden = a.fuente or ["mindicador", "sii"]
    print(f"Descargando UF {a.desde}-{a.hasta}; fuentes en orden: {orden}")
    try:
        serie, fuente = descargar(a.desde, a.hasta, orden)
    except UFError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    doc = guardar(serie, fuente, a.destino)
    cm = doc["cierre_mes"]
    print(f"\nFuente usada     : {fuente}")
    print(f"Dias descargados : {doc['dias']:,}")
    print(f"Rango            : {doc['rango']['desde']} a {doc['rango']['hasta']}")
    print(f"Cierres de mes   : {len(cm)}")
    print(f"Archivo          : {a.destino}")
    if "parquet" in doc:
        print(f"Parquet          : {doc['parquet']}")
    # Cierres cuya fecha no es el ultimo dia del calendario: la fuente no
    # publico ese dia. Se avisa explicitamente para que nadie lo descubra
    # despues mirando un reajuste raro.
    import calendar
    desfasados = [
        (per, d["fecha"]) for per, d in cm.items()
        if int(d["fecha"][8:10]) != calendar.monthrange(int(per[:4]), int(per[4:6]))[1]
    ]

    print("\nCierre de mes (primeros y ultimos 6):")
    items = list(cm.items())
    for per, d in items[:6]:
        print(f"   {per}  {d['valor']:>12}   (al {d['fecha']})")
    print("   ...")
    for per, d in items[-6:]:
        print(f"   {per}  {d['valor']:>12}   (al {d['fecha']})")
    if desfasados:
        print(f"\n{len(desfasados)} periodo(s) sin publicacion el ultimo dia del mes; "
              f"se usa el ultimo dia publicado:")
        for per, f in desfasados[:8]:
            print(f"   {per} -> {f}")
        if len(desfasados) > 8:
            print(f"   ... y {len(desfasados)-8} mas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
