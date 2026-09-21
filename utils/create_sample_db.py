"""
utils.create_sample_db
======================

Genera una base de despliegue liviana con los ultimos meses.

Por que una muestra
-------------------
El warehouse completo son 124 MB repartidos en 147 archivos Parquet, casi
todo renta fija. No pasa ningun limite duro de GitHub -- el archivo mas
grande son 5,4 MB -- pero es dato DERIVADO y reproducible: sale de correr el
loader sobre los ZIP en unos doce minutos. Versionarlo significa clonarlo
entero en cada despliegue y arrastrarlo en cada push para siempre.

Ademas, la base completa monta los hechos como VIEW sobre rutas ABSOLUTAS de
Parquet. Eso funciona en el laptop que la genero y en ningun otro lado. La
muestra, en cambio, materializa TABLAS de verdad dentro de un unico archivo
.duckdb: es portable, se copia sola y no depende de donde quedo la carpeta.

Uso
---
    python -m utils.create_sample_db                 # ultimos 6 meses
    python -m utils.create_sample_db --meses 12
    python -m utils.create_sample_db --salida ./otro/sitio.duckdb
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_LOG = logging.getLogger("cmf1835.sample")

ORIGEN = ROOT / "warehouse" / "data" / "cmf1835.duckdb"
SALIDA = ROOT / "warehouse" / "sample" / "cmf1835_sample.duckdb"

#: Hechos y la columna por la que se recorta el periodo.
HECHOS = {
    "fact_derivado": "periodo_informacion",
    "fact_renta_fija": "periodo_informacion",
    "fact_extranjero_rf": "periodo_informacion",
    "fact_extranjero_rv": "periodo_informacion",
    "fact_equity": "periodo_informacion",
    "fact_fondo": "periodo_informacion",
    "fact_otras_inv": "periodo_informacion",
    "fact_control": "periodo_informacion",
    "fact_garantia": "periodo_informacion",
    "fact_cuarentena": "periodo_informacion",
    "fact_flujo": "periodo",
}

#: Dimensiones: van completas, son chicas y recortarlas rompe los joins.
DIMENSIONES = ("dim_periodo", "dim_compania", "dim_contraparte",
               "dim_instrumento", "uf_cierre_mes")


def tablas_existentes(con) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()} | \
           {r[0] for r in con.execute("SHOW TABLES").fetchall()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Base de despliegue con los ultimos meses.")
    p.add_argument("--origen", type=Path, default=ORIGEN)
    p.add_argument("--salida", type=Path, default=SALIDA)
    p.add_argument("--meses", type=int, default=6, help="Meses a conservar (default 6).")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="  [%(levelname)s] %(message)s")

    import duckdb

    if not a.origen.exists():
        print(f"No existe el warehouse en {a.origen}.\n"
              f"Corre primero: python -m warehouse.loader --data <carpeta de ZIP>",
              file=sys.stderr)
        return 2

    src = duckdb.connect(str(a.origen), read_only=True)
    presentes = tablas_existentes(src)

    periodos = [r[0] for r in src.execute(
        "SELECT DISTINCT periodo_informacion FROM fact_derivado "
        "ORDER BY periodo_informacion DESC").fetchall()]
    if not periodos:
        print("El warehouse de origen esta vacio.", file=sys.stderr)
        return 2
    conservar = sorted(periodos[:a.meses])
    lista = ", ".join(str(x) for x in conservar)
    print(f"Muestra: {len(conservar)} periodos ({conservar[0]} a {conservar[-1]})")

    a.salida.parent.mkdir(parents=True, exist_ok=True)
    if a.salida.exists():
        a.salida.unlink()
    dst = duckdb.connect(str(a.salida))
    dst.execute(f"ATTACH '{a.origen}' AS origen (READ_ONLY)")

    filas: dict[str, int] = {}
    for tabla, col in HECHOS.items():
        if tabla not in presentes:
            _LOG.info("sin %s en el origen; se omite", tabla)
            continue
        # CREATE TABLE, no VIEW: la muestra tiene que ser un archivo portable,
        # no un puntero a Parquet que solo existe en esta maquina.
        dst.execute(f"CREATE TABLE {tabla} AS "
                    f"SELECT * FROM origen.{tabla} WHERE {col} IN ({lista})")
        filas[tabla] = dst.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]

    for dim in DIMENSIONES:
        if dim not in presentes:
            continue
        dst.execute(f"CREATE TABLE {dim} AS SELECT * FROM origen.{dim}")
        filas[dim] = dst.execute(f"SELECT COUNT(*) FROM {dim}").fetchone()[0]

    # dim_periodo se recorta a la ventana para que el selector no ofrezca meses
    # que la muestra no tiene.
    if "dim_periodo" in filas:
        dst.execute(f"DELETE FROM dim_periodo WHERE periodo NOT IN ({lista})")
        filas["dim_periodo"] = dst.execute("SELECT COUNT(*) FROM dim_periodo").fetchone()[0]

    # Las vistas de clasificacion (clase de activo, apellido del instrumento)
    # se crean sobre las tablas, no se copian: si se generaran en el warehouse
    # completo y no se recrean aqui, la muestra de despliegue queda sin ellas
    # y el dashboard revienta en produccion con un CatalogException -- exactamente
    # lo que paso la primera vez que se genero esta muestra.
    from warehouse.clases import SQL_CLASES
    dst.execute(SQL_CLASES)

    dst.execute("DETACH origen")
    dst.execute("CHECKPOINT")
    dst.close()
    src.close()

    tam = a.salida.stat().st_size / 1e6
    print(f"\nArchivo : {a.salida}")
    print(f"Tamano  : {tam:,.1f} MB")
    print("\nFilas por tabla:")
    for t, n in sorted(filas.items()):
        print(f"   {t:20} {n:>12,}")
    if tam > 90:
        print(f"\nAVISO: {tam:,.0f} MB se acerca al limite de 100 MB por archivo de "
              f"GitHub. Baja --meses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
