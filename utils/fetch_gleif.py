"""
utils.fetch_gleif
=================

Nombre legal oficial de cada LEI que aparece en el warehouse, desde GLEIF.

Por que hace falta
------------------
En el B.7 la aseguradora identifica a una contraparte extranjera con su LEI y,
aparte, escribe un nombre a mano. El catalogo propio (config/entities.yaml)
agrupa entidades y resuelve por alias antes que por LEI, asi que:

  - 5.931 operaciones informadas como "CITIBANK N.A." con el LEI de Citibank,
    N.A. quedaron atribuidas a la agencia en Chile;
  - 231 operaciones informadas como "DEUTSCHE BANK LONDON" traen el LEI de
    Credivalores-Crediservicios S.A.S.;
  - 716 informadas como "BANK OF NOVA SCOTIA" traen el LEI de un fideicomiso
    de acciones para empleados de ese banco.

Un LEI identifica a UNA persona juridica. GLEIF es el registro oficial y
publico de LEIs, asi que su nombre legal es la fuente para separar entidades de
verdad, que es lo que pide el reporte.

Validacion
----------
Antes de consultar se valida el digito verificador del LEI (ISO 17442, ISO
7064 MOD 97-10). Un LEI que no cuadra no es un identificador: se guarda como
INVALIDO y no se consulta.

Uso
---
    python -m utils.fetch_gleif               # consulta los LEI del warehouse
    python -m utils.fetch_gleif --verificar   # resume lo que hay en disco
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINO = ROOT / "config" / "gleif_lei.csv"
DB = ROOT / "warehouse" / "data" / "cmf1835.duckdb"

API = "https://api.gleif.org/api/v1/lei-records"
LOTE = 100                       # LEIs por consulta; GLEIF acepta hasta 200

_LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
COLUMNAS = ["lei", "estado_lei", "nombre_legal", "pais", "categoria", "estado_entidad"]


def lei_valido(lei: str) -> bool:
    """ISO 17442: letras a numeros (A=10..Z=35) y el total mod 97 debe dar 1."""
    lei = lei.strip().upper()
    if not _LEI.match(lei):
        return False
    numero = "".join(str(int(c, 36)) for c in lei)
    return int(numero) % 97 == 1


def leis_del_warehouse(db: Path = DB) -> list[str]:
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    filas = con.execute("""
        SELECT DISTINCT upper(trim(contraparte_lei)) FROM raw_derivado
        WHERE NULLIF(trim(contraparte_lei), '') IS NOT NULL
        UNION
        SELECT DISTINCT upper(trim(contraparte_lei)) FROM raw_garantia
        WHERE NULLIF(trim(contraparte_lei), '') IS NOT NULL
    """).fetchall()
    return sorted(r[0] for r in filas)


def consultar(leis: list[str]) -> dict[str, dict[str, str]]:
    """{lei: fila} para los LEI que GLEIF conoce."""
    out: dict[str, dict[str, str]] = {}
    for i in range(0, len(leis), LOTE):
        lote = leis[i:i + LOTE]
        url = (f"{API}?filter%5Blei%5D={urllib.parse.quote(','.join(lote))}"
               f"&page%5Bsize%5D={LOTE}")
        pedido = urllib.request.Request(url, headers={"Accept": "application/vnd.api+json"})
        with urllib.request.urlopen(pedido, timeout=60) as r:
            doc = json.loads(r.read().decode("utf-8"))
        for rec in doc.get("data", []):
            a = rec["attributes"]
            ent = a["entity"]
            out[a["lei"]] = {
                "lei": a["lei"],
                "estado_lei": a.get("registration", {}).get("status", ""),
                "nombre_legal": ent["legalName"]["name"],
                "pais": ent.get("legalAddress", {}).get("country", ""),
                "categoria": ent.get("category") or "",
                "estado_entidad": ent.get("status", ""),
            }
    return out


def escribir(filas: list[dict[str, str]], destino: Path = DESTINO) -> None:
    with destino.open("w", encoding="utf-8", newline="") as fh:
        fh.write("# Nombre legal de cada LEI del warehouse, segun GLEIF.\n")
        fh.write(f"# fuente: {API}\n")
        fh.write(f"# consultado: {_dt.date.today().isoformat()}\n")
        w = csv.DictWriter(fh, fieldnames=COLUMNAS)
        w.writeheader()
        w.writerows(sorted(filas, key=lambda f: f["lei"]))


def leer(origen: Path = DESTINO) -> dict[str, dict[str, str]]:
    """{lei: fila}. Vacio si el archivo no esta."""
    if not origen.exists():
        return {}
    with origen.open(encoding="utf-8") as fh:
        lineas = [l for l in fh if not l.startswith("#")]
    return {f["lei"]: f for f in csv.DictReader(io.StringIO("".join(lineas)))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nombres legales de LEI desde GLEIF.")
    ap.add_argument("--verificar", action="store_true")
    a = ap.parse_args(argv)

    if a.verificar:
        m = leer()
        print(f"{DESTINO}: {len(m)} LEI")
        return 0 if m else 1

    leis = leis_del_warehouse()
    validos = [l for l in leis if lei_valido(l)]
    invalidos = [l for l in leis if not lei_valido(l)]
    print(f"LEI distintos en el warehouse: {len(leis)}  (validos {len(validos)}, "
          f"checksum invalido {len(invalidos)}: {invalidos})")

    encontrados = consultar(validos)
    filas = list(encontrados.values())
    for l in validos:
        if l not in encontrados:
            filas.append({"lei": l, "estado_lei": "NO_ENCONTRADO", "nombre_legal": "",
                          "pais": "", "categoria": "", "estado_entidad": ""})
    for l in invalidos:
        filas.append({"lei": l, "estado_lei": "INVALIDO", "nombre_legal": "",
                      "pais": "", "categoria": "", "estado_entidad": ""})
    escribir(filas)
    print(f"escrito {DESTINO}: {len(encontrados)} encontrados en GLEIF, "
          f"{len(validos) - len(encontrados)} no encontrados")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
