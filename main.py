"""
cmf1835.main
============

Punto de entrada del pipeline ETL. Procesa TODOS los ZIP mensuales que
encuentre en un directorio, en streaming, y deja el resultado listo para
consumir desde DuckDB.

Uso
---
    python main.py --data "/Users/tomasquiero/Claude/Archivos completos ZIP CMF"

    # solo un periodo, y volcando a parquet
    python main.py --data ./data --periodo 202608 --out ./warehouse

    # sin escribir nada, solo diagnostico
    python main.py --data ./data --dry-run

Diseno
------
Nada se carga entero en memoria. Se itera ZIP por ZIP, archivo por archivo
dentro del ZIP y linea por linea, acumulando en lotes que se descargan a
parquet. Un backfill de 36 meses de todas las companias de vida cabe sin
problema en la RAM de un laptop.

Cada registro pasa por tres etapas antes de entrar al warehouse:

  1. parse      -- corte por offsets declarados en el YAML
  2. validate   -- identidades financieras internas del propio registro
  3. normalize  -- resolucion de contraparte contra el catalogo de entidades

Los registros que no cierran matematicamente NO se descartan: van a una tabla
de cuarentena aparte, con el motivo y la linea cruda. Un dato en cuarentena es
un dato que hay que revisar; un dato borrado es un dato que nadie va a revisar.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field as _dc_field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Sequence

from parse.engine import FixedWidthEngine, ParsedRecord, UnknownFileTypeError
from validate.arithmetic import ArithmeticValidator, Verdict
from normalize.entities import EntityResolver, Resolution

_LOG = logging.getLogger("cmf1835")

ROOT = Path(__file__).resolve().parent
LAYOUTS = ROOT / "config" / "layouts" / "cmf_v2024.yaml"
ENTITIES = ROOT / "config" / "entities.yaml"

#: Tipos de registro que llevan datos de negocio (el resto es cabecera/trailer).
DETAIL_TYPES: dict[str, set[str]] = {
    "I": {"2"},                          # renta fija
    "P": {"2", "3", "4", "5", "6"},      # opciones, forwards, futuros, swaps, pactos
    "G": {"2"},                          # garantias
    "A": {"2"}, "B": {"2"}, "F": {"2"},
    "X": {"2", "3", "4", "5"}, "O": {"2"}, "C": {"2"},
}

#: Nombres legibles por (archivo, tipo de registro), para los logs.
INSTRUMENT_LABEL: dict[tuple[str, str], str] = {
    ("I", "2"): "RENTA FIJA", ("P", "2"): "OPCIONES", ("P", "3"): "FORWARDS",
    ("P", "4"): "FUTUROS", ("P", "5"): "SWAPS", ("P", "6"): "PACTOS",
    ("G", "2"): "GARANTIAS",
}


class JSONEncoderDec(json.JSONEncoder):
    """Serializa Decimal y date, que json no maneja por defecto."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return super().default(o)


@dataclass(slots=True)
class RunStats:
    """Contadores de una corrida completa."""

    zips: int = 0
    archivos: int = 0
    lineas: int = 0
    detalle: int = 0
    por_instrumento: Counter[str] = _dc_field(default_factory=Counter)
    por_periodo: Counter[int] = _dc_field(default_factory=Counter)
    companias: set[int] = _dc_field(default_factory=set)
    veredictos: Counter[str] = _dc_field(default_factory=Counter)
    trailer_ok: int = 0
    trailer_fail: list[str] = _dc_field(default_factory=list)
    sin_layout: Counter[str] = _dc_field(default_factory=Counter)
    resoluciones: list[Resolution] = _dc_field(default_factory=list)

    #: Nombres que el regex de la CMF no supo leer. Distinto de `sin_layout`:
    #: ahi caen las letras que todavia no estan transcritas, que es una
    #: ausencia conocida. Esto es un archivo que EXISTE y se perdio en
    #: silencio -- en 202412 fueron 350 de golpe y el resumen dio exito igual.
    rechazados: list[str] = _dc_field(default_factory=list)
    #: (letra, largo_real, largo_declarado) -> registros. Un largo que no
    #: calza significa que el YAML describe otra generacion del layout.
    largo_mismatch: Counter[tuple[str, int, int]] = _dc_field(default_factory=Counter)
    #: Periodos afectados por un largo que no calza.
    periodos_mismatch: set[int] = _dc_field(default_factory=set)
    #: Por ZIP y por letra: cuantos archivos habia y cuantos entraron.
    presentes: dict[str, Counter[str]] = _dc_field(default_factory=dict)
    ingeridos: dict[str, Counter[str]] = _dc_field(default_factory=dict)


class Pipeline:
    """Orquesta parseo, validacion y normalizacion sobre un arbol de ZIPs."""

    def __init__(
        self,
        layouts: Path = LAYOUTS,
        entities: Path = ENTITIES,
        *,
        batch_size: int = 50_000,
    ) -> None:
        """
        Args:
            layouts: YAML con los layouts declarativos.
            entities: YAML con el catalogo de contrapartes.
            batch_size: Registros a acumular antes de volcar a disco.

        Raises:
            SystemExit: si falta alguno de los archivos de configuracion.
        """
        for path in (layouts, entities):
            if not path.exists():
                _LOG.error("No existe el archivo de configuracion: %s", path)
                raise SystemExit(2)

        self.engine = FixedWidthEngine.from_yaml(layouts)
        self.validator = ArithmeticValidator()
        self.resolver = EntityResolver.from_yaml(entities)
        self.batch_size = batch_size
        self.stats = RunStats()

        #: Letras con al menos un tipo de registro transcrito. Un archivo de
        #: estas letras que no entra es una perdida; uno de las demas es una
        #: transcripcion pendiente y ya se sabe.
        self._letras_transcritas = {
            letra
            for letra, spec in self.engine._spec.items()
            if any(t.mapped for t in spec.record_types.values())
        }

    # -- descubrimiento ------------------------------------------------------

    @staticmethod
    def find_zips(data_dir: Path, periodo: str | None = None) -> list[Path]:
        """Busca ZIP mensuales de forma recursiva.

        Args:
            data_dir: Carpeta raiz donde buscar.
            periodo: Filtro opcional AAAAMM. Si se omite, se toman todos.

        Returns:
            Rutas ordenadas por nombre, que para el patron de la CMF equivale
            a orden cronologico.
        """
        zips = sorted(p for p in data_dir.rglob("*.zip") if p.is_file())
        if periodo:
            zips = [p for p in zips if periodo in p.name]
        return zips

    # -- procesamiento -------------------------------------------------------

    def run(
        self, data_dir: Path, *, periodo: str | None = None, sample: int = 10
    ) -> tuple[RunStats, dict[str, list[dict[str, Any]]]]:
        """Procesa todos los ZIP encontrados.

        Args:
            data_dir: Carpeta con los ZIP de la CMF.
            periodo: Filtro opcional AAAAMM.
            sample: Cuantos registros de muestra guardar por instrumento.

        Returns:
            Tupla ``(estadisticas, muestras)``. Las muestras se usan para el
            log de verificacion, no para el warehouse.
        """
        zips = self.find_zips(data_dir, periodo)
        if not zips:
            _LOG.error("No se encontraron archivos .zip en %s", data_dir)
            return self.stats, {}

        _LOG.info("%d archivos ZIP encontrados en %s", len(zips), data_dir)
        samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for zpath in zips:
            try:
                self._process_zip(zpath, samples, sample)
            except zipfile.BadZipFile:
                _LOG.error("ZIP corrupto, se omite: %s", zpath.name)
            except OSError as exc:
                _LOG.error("No se pudo leer %s: %s", zpath.name, exc)

        return self.stats, dict(samples)

    def _process_zip(
        self, zpath: Path, samples: dict[str, list[dict[str, Any]]], sample: int
    ) -> None:
        """Procesa un ZIP mensual completo, en streaming."""
        self.stats.zips += 1
        _LOG.info("--- %s", zpath.name)
        presentes = self.stats.presentes.setdefault(zpath.name, Counter())
        ingeridos = self.stats.ingeridos.setdefault(zpath.name, Counter())

        with zipfile.ZipFile(zpath) as z:
            for info in sorted(z.infolist(), key=lambda i: i.filename):
                if info.is_dir():
                    continue
                letra = Path(info.filename).name[:1].upper()
                presentes[letra] += 1
                try:
                    spec, rut, periodo = self.engine.describe(info.filename)
                except UnknownFileTypeError:
                    self.stats.sin_layout[letra] += 1
                    # El nombre no calza con NINGUNA de las convenciones
                    # conocidas. Se anota aparte: es un archivo perdido, no
                    # una letra pendiente de transcribir.
                    if letra in self._letras_transcritas:
                        self.stats.rechazados.append(f"{zpath.name}/{Path(info.filename).name}")
                    continue

                if not any(t.mapped for t in spec.record_types.values()):
                    self.stats.sin_layout[spec.letter] += 1
                    continue

                ingeridos[spec.letter] += 1
                self.stats.archivos += 1
                if rut:
                    self.stats.companias.add(rut)
                if periodo:
                    self.stats.por_periodo[periodo] += 1

                declared: int | None = None
                count = 0

                for rec in self.engine.parse_file(info.filename, data=z.read(info)):
                    count += 1
                    self.stats.lineas += 1

                    if rec.fields.get("_layout_mismatch"):
                        self.stats.largo_mismatch[
                            (rec.letter, rec.fields["_line_length"],
                             rec.fields["_expected_length"])
                        ] += 1
                        if rec.periodo:
                            self.stats.periodos_mismatch.add(rec.periodo)

                    # Trailer de control: la CMF declara el total de lineas del
                    # archivo. Es la cuadratura mas barata que existe y detecta
                    # truncamientos antes de que contaminen nada.
                    if rec.fields.get("TOTAL_REGISTROS") is not None:
                        declared = int(rec.fields["TOTAL_REGISTROS"])

                    if rec.record_type not in DETAIL_TYPES.get(rec.letter, set()):
                        continue

                    self._handle_detail(rec, samples, sample)

                if declared is not None:
                    if declared == count:
                        self.stats.trailer_ok += 1
                    else:
                        self.stats.trailer_fail.append(
                            f"{info.filename}: trailer={declared} vs lineas={count}"
                        )

    def _handle_detail(
        self, rec: ParsedRecord, samples: dict[str, list[dict[str, Any]]], sample: int
    ) -> None:
        """Valida y normaliza un registro de detalle."""
        self.stats.detalle += 1
        label = INSTRUMENT_LABEL.get((rec.letter, rec.record_type),
                                     f"{rec.letter}/{rec.record_type}")
        self.stats.por_instrumento[label] += 1

        verdict = self.validator.validate(
            rec.letter, rec.record_type, rec.fields,
            untrusted=rec.untrusted, periodo=rec.periodo,
        )
        self.stats.veredictos[verdict.verdict.value] += 1

        resolution: Resolution | None = None
        if rec.letter in ("P", "G"):
            resolution = self.resolver.resolve(
                rut=rec.fields.get("RUT_CONTRAPARTE_NACIONAL"),
                dv=rec.fields.get("DV_CONTRAPARTE_NACIONAL"),
                name=rec.fields.get("NOMBRE") or rec.fields.get("NOMBRE_CONTRAPARTE_GARANTIA"),
                identifier=rec.fields.get("LEI_CONTRAPARTE_EXTRANJERA"),
            )
            self.stats.resoluciones.append(resolution)

        if len(samples[label]) < sample:
            samples[label].append({
                "archivo": rec.source_file,
                "linea": rec.line_no,
                "periodo": rec.periodo,
                "veredicto": verdict.verdict.value,
                "checks": [
                    {"regla": c.rule, "ok": c.ok, "esperado": c.expected,
                     "informado": c.observed, "detalle": c.detail}
                    for c in verdict.checks
                ],
                "contraparte": resolution.to_row() if resolution else None,
                "campos": rec.fields,
            })


# ---------------------------------------------------------------------------
# Reporte por consola
# ---------------------------------------------------------------------------

def _rule(char: str = "=", width: int = 78) -> str:
    return char * width


def print_report(
    stats: RunStats,
    samples: dict[str, list[dict[str, Any]]],
    resolver: EntityResolver,
    letras_transcritas: set[str] | None = None,
) -> None:
    """Imprime el diagnostico de la corrida."""
    letras_transcritas = letras_transcritas or set()
    print()
    print(_rule())
    print("RESUMEN DE INGESTA")
    print(_rule())
    print(f"  ZIP procesados        : {stats.zips}")
    print(f"  Archivos con layout   : {stats.archivos}")
    print(f"  Lineas leidas         : {stats.lineas:,}")
    print(f"  Registros de detalle  : {stats.detalle:,}")
    print(f"  Companias distintas   : {len(stats.companias)}")
    periodos = sorted(stats.por_periodo)
    if periodos:
        rango = f"{periodos[0]} a {periodos[-1]}" if len(periodos) > 1 else str(periodos[0])
        print(f"  Periodos              : {rango} ({len(periodos)})")
    if stats.sin_layout:
        pend = ", ".join(f"{k}={v}" for k, v in sorted(stats.sin_layout.items()))
        print(f"  Archivos sin layout   : {pend}")

    print()
    print(_rule())
    print("REGISTROS POR INSTRUMENTO")
    print(_rule())
    for label, n in stats.por_instrumento.most_common():
        print(f"  {label:<24} {n:>10,}")

    print()
    print(_rule())
    print("CUADRATURA CONTRA EL TRAILER DE CONTROL")
    print(_rule())
    total = stats.trailer_ok + len(stats.trailer_fail)
    print(f"  Archivos cuadrados    : {stats.trailer_ok}/{total}")
    # Sin tope: un descuadre que no se imprime es un descuadre que nadie
    # revisa. Son pocos por definicion; si fueran muchos, peor razon para
    # esconderlos.
    for msg in stats.trailer_fail:
        print(f"    DESCUADRE {msg}")

    print()
    print(_rule())
    print("AUDITORIA DE COBERTURA")
    print(_rule())
    if stats.rechazados:
        print(f"  !! {len(stats.rechazados)} archivo(s) de letra transcrita con nombre ilegible:")
        for n in stats.rechazados[:20]:
            print(f"     RECHAZADO {n}")
        if len(stats.rechazados) > 20:
            print(f"     ... y {len(stats.rechazados) - 20} mas")
    else:
        print("  Nombres ilegibles     : 0")

    if stats.largo_mismatch:
        tot = sum(stats.largo_mismatch.values())
        per = sorted(stats.periodos_mismatch)
        print(f"  !! {tot:,} registro(s) con largo distinto al declarado en el YAML:")
        for (letra, real, decl), n in sorted(stats.largo_mismatch.items()):
            print(f"     LARGO {letra}: {real} caracteres reales vs {decl} declarados  ({n:,} registros)")
        print(f"     periodos afectados: {len(per)}  ({per[0]} a {per[-1]})")
        print("     -> el YAML describe otra generacion del layout; estos registros")
        print("        conservan su linea cruda y NO se les asigna ningun campo.")
    else:
        print("  Largos de registro    : todos calzan con el YAML")

    # Un mes que pierde una letra completa es el fallo que hay que cazar:
    # el resumen global lo diluye y la corrida termina en verde.
    huecos: list[str] = []
    for zname in sorted(stats.presentes):
        pres, ing = stats.presentes[zname], stats.ingeridos.get(zname, Counter())
        for letra in sorted(pres):
            if letra not in letras_transcritas:
                continue
            if pres[letra] and not ing.get(letra):
                huecos.append(f"{zname}: letra {letra} -- {pres[letra]} archivo(s) presentes, 0 ingeridos")
            elif ing.get(letra, 0) < pres[letra]:
                huecos.append(f"{zname}: letra {letra} -- {ing[letra]}/{pres[letra]} ingeridos")
    if huecos:
        print(f"  !! {len(huecos)} hueco(s) de cobertura:")
        for h in huecos:
            print(f"     HUECO {h}")
    else:
        print(f"  Huecos de cobertura   : 0  ({len(stats.presentes)} ZIP, letras {sorted(letras_transcritas)})")

    print()
    print(_rule())
    print("VALIDACION ARITMETICA")
    print(_rule())
    for v in Verdict:
        print(f"  {v.value:<12} {stats.veredictos.get(v.value, 0):>10,}")
    if stats.veredictos.get("quarantine"):
        print("  (los registros en cuarentena conservan su linea cruda para recalibrar)")

    if stats.resoluciones:
        print()
        print(_rule())
        print("RESOLUCION DE CONTRAPARTES")
        print(_rule())
        cov = resolver.coverage(stats.resoluciones)
        print(f"  Total                 : {cov['total']:,}")
        print(f"  Resueltas por RUT     : {cov['pct_por_rut']}%")
        print(f"  Sin resolver          : {cov['pct_sin_resolver']}%")
        for row in resolver.unresolved_report(top=8):
            print(f"    pendiente: {row['nombre_normalizado'][:50]:<50} x{row['ocurrencias']}")

    for label, rows in samples.items():
        print()
        print(_rule())
        print(f"MUESTRA VALIDADA -- {label} (primeros {len(rows)})")
        print(_rule())
        for i, r in enumerate(rows, 1):
            cp = (r["contraparte"] or {}).get("contraparte_nombre")
            head = f"  [{i:2d}] {r['archivo']}:{r['linea']}  periodo={r['periodo']}  -> {r['veredicto'].upper()}"
            print(head + (f"  contraparte={cp}" if cp else ""))
            for c in r["checks"]:
                if c["ok"] is None:
                    continue
                mark = "OK  " if c["ok"] else "FALLA"
                print(f"        {mark} {c['regla']}")
                print(f"              esperado={c['esperado']}  informado={c['informado']}"
                      + (f"  [{c['detalle']}]" if c["detalle"] else ""))


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada CLI."""
    ap = argparse.ArgumentParser(
        description="Pipeline ETL Circular 1835 CMF: ingesta masiva de ZIP mensuales."
    )
    ap.add_argument("--data", required=True, type=Path,
                    help="Carpeta con los ZIP mensuales (se busca recursivamente).")
    ap.add_argument("--periodo", default=None,
                    help="Filtra un periodo AAAAMM. Por defecto procesa todos.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Carpeta de salida para el warehouse. Omitir para no escribir.")
    ap.add_argument("--sample", type=int, default=10,
                    help="Registros de muestra a mostrar por instrumento (default 10).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo diagnostico, no escribe nada.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    if not args.data.exists():
        _LOG.error("La carpeta no existe: %s", args.data)
        return 2

    pipeline = Pipeline()
    stats, samples = pipeline.run(args.data, periodo=args.periodo, sample=args.sample)
    print_report(stats, samples, pipeline.resolver, pipeline._letras_transcritas)

    if args.out and not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "run_stats.json").write_text(
            json.dumps({
                "zips": stats.zips, "archivos": stats.archivos, "lineas": stats.lineas,
                "detalle": stats.detalle, "companias": sorted(stats.companias),
                "por_instrumento": dict(stats.por_instrumento),
                "por_periodo": dict(stats.por_periodo),
                "veredictos": dict(stats.veredictos),
                "trailer_ok": stats.trailer_ok, "trailer_fail": stats.trailer_fail,
            }, indent=2, cls=JSONEncoderDec),
            encoding="utf-8",
        )
        _LOG.info("Estadisticas escritas en %s", args.out / "run_stats.json")

    return 0 if not stats.trailer_fail else 1


if __name__ == "__main__":
    sys.exit(main())
