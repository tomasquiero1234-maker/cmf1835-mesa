"""
tests.fidelidad
===============

Prueba que el warehouse sea FIEL a lo que publica la CMF.

Que prueba y que no
-------------------
NO prueba que la CMF refleje la realidad del mercado. Eso es imposible desde
aqui: si una aseguradora no informa un forward, no hay forma de saberlo
mirando el archivo. Esa brecha se mide contra libros propios, no contra el
propio dato.

SI prueba que entre el archivo de la CMF y la tabla del warehouse no se
perdio, se invento ni se corrompio nada. Cuatro controles:

  1. CONTABILIDAD DE LINEAS
     Cada linea no vacia del ZIP termina en un destino explicable: detalle
     cargado, cabecera/trailer, letra no cargada, largo que no calza con el
     layout, o nombre de archivo no reconocido. La suma de destinos tiene que
     dar exactamente el total de lineas crudas. Sin residuo.

  2. ORIGEN CONTRA WAREHOUSE
     Las lineas de detalle contadas en el ZIP tienen que ser exactamente las
     filas que el warehouse atribuye a ese ZIP, sumando hechos y cuarentena.

  3. ROUND-TRIP DE CAMPOS
     Se vuelve al ZIP, se reparsea la linea y se reconstruye la fila con el
     mismo RowBuilder del loader. Cada valor tiene que coincidir con lo que
     quedo guardado en Parquet. Esto caza corrupcion entre parseo y escritura.

  4. SIN DUPLICADOS NI FANTASMAS
     (zip_origen, source_file, line_no) es unico: una linea del origen no
     puede aparecer dos veces, y ninguna fila puede citar una linea que en el
     archivo no existe.

Uso
---
    python -m tests.fidelidad                      # todos los ZIP
    python -m tests.fidelidad --periodo 202608
    python -m tests.fidelidad --muestra 500        # filas por round-trip
"""

from __future__ import annotations

import argparse
import random
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parse.engine import FixedWidthEngine, UnknownFileTypeError
from warehouse.loader import Loader, DEFAULT_OUT

DATA = Path("/Users/tomasquiero/Claude/Archivos completos ZIP CMF")
DB = DEFAULT_OUT / "cmf1835.duckdb"

#: Tablas del warehouse que reciben detalle, en el orden en que el loader las
#: escribe. La cuarentena cuenta: una linea en cuarentena NO es una linea
#: perdida, es una linea guardada con su veredicto y su texto crudo.
TABLAS = ("raw_derivado", "raw_renta_fija", "raw_extranjero_rf", "raw_extranjero_rv",
          "raw_equity", "raw_fondo", "raw_otras_inv", "raw_control",
          "raw_garantia", "raw_cuarentena")


def contabilidad(zpath: Path, loader: Loader) -> tuple[Counter, int]:
    """Clasifica cada linea cruda del ZIP en un destino. Devuelve (destinos, total)."""
    eng = loader.engine
    c: Counter[str] = Counter()
    total = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            base = Path(info.filename).name
            raw = z.read(info)
            # Se cuenta sobre el texto crudo, sin pasar por el motor: si el
            # motor perdiera lineas, contarlas con el motor lo ocultaria.
            lineas = [l for l in raw.decode("utf-8", errors="replace").split("\n") if l.strip()]
            total += len(lineas)
            try:
                spec, _rut, _per = eng.describe(base)
            except UnknownFileTypeError:
                c["nombre_no_reconocido"] += len(lineas)
                continue
            letra = spec.letter
            if letra not in loader.LETRAS_CARGADAS:
                c[f"letra_no_cargada[{letra}]"] += len(lineas)
                continue
            for rec in eng.parse_file(base, data=raw):
                if rec.fields.get("_layout_mismatch"):
                    c["largo_no_calza"] += 1
                elif rec.record_type in loader.DETALLE.get(letra, set()):
                    c["detalle_cargado"] += 1
                else:
                    c["cabecera_o_trailer"] += 1
    return c, total


def round_trip(zpath: Path, loader: Loader, con, n_muestra: int) -> tuple[int, int, list[str]]:
    """Reparsea filas al azar desde el ZIP y las compara con el Parquet.

    Se muestrea por ARCHIVO, no por fila: tomar una muestra aleatoria sobre
    2,1 millones de filas de Parquet obliga a escanearlas todas. Elegir unos
    pocos archivos del ZIP y verificar todas sus lineas prueba lo mismo y
    cuesta segundos.
    """
    import math, datetime as _dt

    with zipfile.ZipFile(zpath) as z:
        miembros = [i for i in z.infolist()
                    if not i.is_dir() and _cargable(Path(i.filename).name, loader)]
        if not miembros:
            return 0, 0, []
        random.shuffle(miembros)

        descargado = _dt.datetime.fromtimestamp(
            zpath.stat().st_mtime, _dt.timezone.utc).date().isoformat()
        ok = mal = 0
        fallos: list[str] = []

        for info in miembros:
            if ok + mal >= n_muestra:
                break
            sf = Path(info.filename).name
            esperados: dict[tuple[str, int], dict] = {}
            for rec in loader.engine.parse_file(sf, data=z.read(info)):
                # El loader descarta estos ANTES de clasificarlos: son de la
                # generacion vieja del layout y no se les asigna ningun campo.
                # El auditor tiene que replicar exactamente ese contrato, o
                # reclama por filas que el loader nunca prometio escribir.
                if rec.fields.get("_layout_mismatch"):
                    continue
                destino = loader._fila(rec, zpath.name, descargado)
                if destino is None:
                    continue
                tabla, fila = destino
                # dim_compania_src sale del registro de identificacion, no es
                # detalle y no tiene vista raw_: se verifica por separado.
                if tabla == "dim_compania_src":
                    continue
                esperados[(tabla, rec.line_no)] = fila
            if not esperados:
                continue

            # Una sola consulta por (archivo, tabla), no una por fila.
            for tabla in {t for t, _ in esperados}:
                vista = "raw_" + tabla.removeprefix("fact_")
                real = con.execute(
                    f"SELECT * FROM {vista} WHERE zip_origen=? AND source_file=?",
                    [zpath.name, sf]).fetch_df()
                porlinea = {int(r.line_no): r for _, r in real.iterrows()}
                for (t, ln), esp in esperados.items():
                    if t != tabla:
                        continue
                    if ln not in porlinea:
                        fallos.append(f"{sf}:{ln} no esta en {tabla} (linea PERDIDA)")
                        mal += 1
                        continue
                    g = porlinea[ln]
                    diff = []
                    for k, v in esp.items():
                        if k not in g.index:
                            diff.append(f"falta columna {k}")
                            continue
                        w = g[k]
                        # pandas representa el nulo de varias formas segun el
                        # dtype: None, nan, NaT y pd.NA. Las cuatro son "no hay
                        # dato" y ninguna es una diferencia contra un None.
                        wn = _es_nulo(w)
                        if v is None and wn:
                            continue
                        if isinstance(v, float) and isinstance(w, (int, float)) and not wn:
                            if abs(v - float(w)) > 1e-6:
                                diff.append(f"{k}: origen={v} warehouse={w}")
                        elif not _igual(v, w):
                            diff.append(f"{k}: origen={v!r} warehouse={w!r}")
                    if diff:
                        mal += 1
                        fallos.append(f"{sf}:{ln} -> " + "; ".join(diff[:3]))
                    else:
                        ok += 1
    return ok, mal, fallos


def _igual(v, w) -> bool:
    """Compara valores tolerando la representacion, no el contenido.

    Parquet devuelve las fechas como Timestamp de pandas y el parser las
    entrega como date. Son el mismo dia; compararlas como texto da
    '2019-09-01' contra '2019-09-01 00:00:00' y marca una diferencia que no
    existe. Se normalizan las dos a fecha antes de comparar.
    """
    import datetime as _d
    import pandas as pd

    def _fecha(x):
        if isinstance(x, _d.datetime):
            return x.date()
        if isinstance(x, _d.date):
            return x
        if isinstance(x, pd.Timestamp):
            return x.date()
        return None

    fv, fw = _fecha(v), _fecha(w)
    if fv is not None and fw is not None:
        return fv == fw
    return str(v) == str(w)


def _es_nulo(w) -> bool:
    """True si el valor del warehouse representa ausencia de dato."""
    import pandas as pd
    try:
        r = pd.isna(w)
    except (TypeError, ValueError):
        return w is None
    return bool(r) if not hasattr(r, "__len__") else False


def _cargable(base: str, loader: Loader) -> bool:
    try:
        spec, _r, _p = loader.engine.describe(base)
    except UnknownFileTypeError:
        return False
    return spec.letter in loader.LETRAS_CARGADAS


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Audita la fidelidad del warehouse contra los ZIP.")
    p.add_argument("--data", type=Path, default=DATA)
    p.add_argument("--db", type=Path, default=DB)
    p.add_argument("--periodo", help="Solo un periodo AAAAMM.")
    p.add_argument("--muestra", type=int, default=300, help="Filas por ZIP para round-trip.")
    a = p.parse_args(argv)

    import duckdb
    if not a.db.exists():
        print(f"No existe el warehouse en {a.db}", file=sys.stderr)
        return 2
    con = duckdb.connect(str(a.db), read_only=True)
    loader = Loader(DEFAULT_OUT)

    zips = sorted(z for z in a.data.rglob("*.zip") if z.is_file())
    if a.periodo:
        zips = [z for z in zips if a.periodo in z.name]

    print("=" * 78)
    print("AUDITORIA DE FIDELIDAD -- warehouse contra los archivos de la CMF")
    print("=" * 78)
    print(f"{'ZIP':22} {'lineas':>9} {'detalle':>9} {'warehouse':>10} {'dif':>6}  estado")
    print("-" * 78)

    problemas: list[str] = []
    tot_lineas = tot_detalle = tot_wh = 0
    resumen_destinos: Counter[str] = Counter()

    for zp in zips:
        c, total = contabilidad(zp, loader)
        suma = sum(c.values())
        detalle = c["detalle_cargado"]
        wh = sum(con.execute(f"SELECT COUNT(*) FROM {t} WHERE zip_origen=?",
                             [zp.name]).fetchone()[0] for t in TABLAS)
        resumen_destinos.update(c)
        tot_lineas += total; tot_detalle += detalle; tot_wh += wh

        estado = []
        if suma != total:
            estado.append(f"CONTABILIDAD NO CIERRA ({total - suma:+,})")
            problemas.append(f"{zp.name}: lineas={total:,} destinos={suma:,}")
        if detalle != wh:
            estado.append(f"ORIGEN != WAREHOUSE ({detalle - wh:+,})")
            problemas.append(f"{zp.name}: detalle={detalle:,} warehouse={wh:,}")
        print(f"{zp.name:22} {total:>9,} {detalle:>9,} {wh:>10,} {detalle - wh:>6,}  "
              f"{'OK' if not estado else ' | '.join(estado)}")

    print("-" * 78)
    print(f"{'TOTAL':22} {tot_lineas:>9,} {tot_detalle:>9,} {tot_wh:>10,} {tot_detalle - tot_wh:>6,}")

    print("\nDestino de cada linea (agregado):")
    for k, v in sorted(resumen_destinos.items(), key=lambda kv: -kv[1]):
        print(f"   {k:34} {v:>12,}")
    print(f"   {'SUMA':34} {sum(resumen_destinos.values()):>12,}")
    print(f"   {'LINEAS CRUDAS':34} {tot_lineas:>12,}")

    # --- duplicados y fantasmas ---------------------------------------------
    print("\nDuplicados ((zip, archivo, linea) repetido):")
    dups = 0
    for t in TABLAS:
        n = con.execute(f"""SELECT COUNT(*) FROM (
            SELECT zip_origen, source_file, line_no, COUNT(*) c
            FROM {t} GROUP BY 1,2,3 HAVING COUNT(*) > 1)""").fetchone()[0]
        if n:
            print(f"   {t}: {n:,} claves repetidas")
            problemas.append(f"{t}: {n} duplicados")
            dups += n
    if not dups:
        print("   ninguno")

    # --- round trip ----------------------------------------------------------
    print(f"\nRound-trip de campos ({a.muestra} filas por ZIP):")
    ok = mal = 0
    for zp in zips:
        o, m, f = round_trip(zp, loader, con, a.muestra)
        ok += o; mal += m
        for x in f[:3]:
            problemas.append(f"{zp.name} {x}")
    print(f"   coinciden : {ok:,}")
    print(f"   difieren  : {mal:,}")

    print("\n" + "=" * 78)
    if problemas:
        print(f"RESULTADO: {len(problemas)} problema(s)")
        for x in problemas[:20]:
            print(f"   - {x}")
        return 1
    print("RESULTADO: el warehouse es fiel al origen.")
    print("   Toda linea del ZIP tiene destino explicable, el conteo cuadra,")
    print("   no hay duplicados y los campos reconstruidos coinciden.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
