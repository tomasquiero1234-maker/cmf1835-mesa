"""
utils.fetch_emisores
====================

Descarga la nomina de emisores de valores de la CMF y la deja en disco.

Por que hace falta
------------------
El anexo B.1 identifica al emisor de un papel local SOLO por su RUT. El layout
declara un campo NOMBRE_DEUDOR, pero viene vacio en el 100% de los registros
--se verifico sobre 1.425 registros de detalle del periodo 202608--, asi que no
hay un nombre que cargar: la unica identidad publicada es el numero.

Sin resolverlo, el libro de renta fija se lee como una lista de RUTs pelados y
no se puede preguntar "quien tiene papel de quien", que es justamente el uso.

Por que un archivo en disco y no una llamada en linea
-----------------------------------------------------
Misma razon que utils.fetch_uf: si el nombre del emisor dependiera de si habia
internet, la misma corrida daria resultados distintos segun la hora. Se
descarga una vez, se versiona con su fecha, y el warehouse lee del disco.

Fuente
------
CMF, "Listado de Emisores de valores" -- el mismo regulador que publica la
Circular 1835. Se baja el listado COMPLETO (Estado=TO: vigentes y no vigentes),
porque la cartera tiene papel de emisores que ya salieron del registro.

    https://www.cmfchile.cl/institucional/mercados/descargar_consulta
        ?mercado=V&Estado=TO&entidad=RVEMI

Cobertura conocida y su limite
------------------------------
Esta nomina resuelve 174 de los 322 RUTs emisores del libro local: 20% de los
papeles pero 46% del valor. Combinada con config/entities.yaml y con
dim_compania --las aseguradoras tambien emiten-- llega a 205 RUTs, 41% de los
papeles y 78% del valor.

El resto queda sin nombre A PROPOSITO: Tesoreria General (60805000), el Banco
Central y algunas mutuarias no estan en el registro de emisores de valores. Se
muestran con su RUT y marcados como no identificados. Inventarles un nombre
seria exactamente lo que este proyecto no hace.

Uso
---
    python -m utils.fetch_emisores              # baja y escribe el CSV
    python -m utils.fetch_emisores --verificar  # solo valida el que ya esta
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from normalize.entities import validate_rut  # noqa: E402

URL = ("https://www.cmfchile.cl/institucional/mercados/descargar_consulta"
       "?mercado=V&Estado=TO&entidad=RVEMI")

DESTINO = ROOT / "config" / "emisores_cmf.csv"

#: Por debajo de esto la descarga se considera fallida. El listado completo
#: traia 1.138 entidades al 2026-09-22; si un dia vuelve con 12, es que la CMF
#: cambio la pagina y hay que mirarlo, no sobrescribir el archivo bueno.
MINIMO_ESPERADO = 500

_RUT = re.compile(r"^(\d+)-([\dkK])$")


def descargar(url: str = URL, timeout: int = 90) -> str:
    pedido = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(pedido, timeout=timeout) as r:
        return r.read().decode("utf-8-sig", errors="replace")


def parsear(texto: str) -> list[tuple[int, str, str, str]]:
    """(rut, dv, nombre, estado), solo con RUT que cuadra modulo 11."""
    filas, descartados = [], []
    for row in csv.DictReader(io.StringIO(texto)):
        crudo = (row.get("R.U.T.") or "").strip()
        nombre = (row.get("Entidad") or "").strip()
        estado = next((str(v).strip() for k, v in row.items()
                       if k and k.lower().startswith("seleccione")), "")
        m = _RUT.match(crudo)
        if not m or not nombre:
            descartados.append(crudo or "(sin rut)")
            continue
        rut, dv = int(m.group(1)), m.group(2).upper()
        # El mismo control que se le aplica a los RUT del propio anexo: un
        # nombre colgado de un RUT invalido contamina la resolucion entera.
        if not validate_rut(rut, dv):
            descartados.append(crudo)
            continue
        filas.append((rut, dv, nombre, estado))
    if descartados:
        print(f"   descartados por RUT invalido o sin nombre: {len(descartados)}"
              f" -> {descartados[:5]}")
    return filas


def escribir(filas, destino: Path = DESTINO) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# Nomina de emisores de valores de la CMF.\n")
        fh.write(f"# fuente: {URL}\n")
        fh.write(f"# descargado: {dt.date.today().isoformat()}\n")
        fh.write(f"# filas: {len(filas)}\n")
        w = csv.writer(fh)
        w.writerow(["rut", "dv", "nombre", "estado"])
        w.writerows(sorted(filas))


def leer(origen: Path = DESTINO) -> dict[int, str]:
    """RUT -> razon social. Vacio si el archivo no esta."""
    if not origen.exists():
        return {}
    with origen.open(encoding="utf-8") as fh:
        lineas = [l for l in fh if not l.startswith("#")]
    return {int(r["rut"]): r["nombre"]
            for r in csv.DictReader(io.StringIO("".join(lineas)))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verificar", action="store_true",
                    help="no descarga; solo valida el archivo ya presente")
    a = ap.parse_args()

    if a.verificar:
        m = leer()
        print(f"{DESTINO}: {len(m):,} emisores")
        return 0 if len(m) >= MINIMO_ESPERADO else 1

    print(f"descargando  {URL}")
    filas = parsear(descargar())
    print(f"   entidades con RUT valido: {len(filas):,}")
    if len(filas) < MINIMO_ESPERADO:
        print(f"ABORTA: se esperaban al menos {MINIMO_ESPERADO} y vinieron "
              f"{len(filas)}. No se sobrescribe el archivo existente.")
        return 1
    escribir(filas)
    print(f"escrito      {DESTINO}")
    vig = sum(1 for f in filas if f[3].lower().startswith("vig"))
    print(f"             {vig:,} vigentes, {len(filas) - vig:,} no vigentes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
