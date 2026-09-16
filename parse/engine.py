"""
cmf1835.parse.engine
====================

Motor de parseo de ancho fijo dirigido enteramente por ``config/layouts/*.yaml``.

Principio de diseno
-------------------
El motor no sabe nada de renta fija, forwards ni swaps. Solo sabe cortar
strings por posiciones declaradas y aplicar las reglas de formato de la
Seccion A.4 del Anexo Tecnico de la CMF. Toda la semantica vive en el YAML.
Consecuencia: cuando la CMF cambie el formato (como hizo con la Circular
2354/2024, que llevo los registros de derivados de 489 a 587 caracteres),
se edita un archivo de texto y no se toca una linea de Python.

Dos decisiones que evitan corrupcion silenciosa
-----------------------------------------------
1. **Se corta por CARACTERES, nunca por bytes.** Los archivos vienen en UTF-8
   y el relleno de la CMF es por caracteres. Un registro de Colmena con la 'N'
   de "COMPANIA" mide 970 caracteres pero 971 bytes; cortarlo por bytes
   desplaza todos los campos posteriores en una posicion. Ese es exactamente
   el mecanismo que produce los "montos galacticos".
2. **Los campos no mapeados no se descartan.** Un registro cuyo layout aun no
   esta transcrito se emite igual, con la linea cruda en ``_raw``, en vez de
   hacer caer la ingesta. Nada se pierde.

Uso tipico
----------
>>> engine = FixedWidthEngine.from_yaml("config/layouts/cmf_v2024.yaml")
>>> for rec in engine.parse_file(Path("i202608v.96573600")):
...     print(rec.fields["NEMOTECNICO"], rec.fields["VALOR_FINAL_B1"])
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass, field as _dc_field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import yaml

__all__ = [
    "FieldSpec",
    "RecordTypeSpec",
    "FileSpec",
    "ParsedRecord",
    "FixedWidthEngine",
    "LayoutError",
    "UnknownFileTypeError",
]

_LOG = logging.getLogger(__name__)

#: Nombres de archivo del ZIP de la CMF: <tipo><fecha><grupo?>.<RUT>
#: p.ej. ``p202608v.96573600``. Unica expresion regular del modulo, y se usa
#: solo para el NOMBRE del archivo -- jamas para extraer datos del contenido.
#:
#: El sufijo de grupo es OPCIONAL: en 202412 la CMF publico los archivos de
#: derivados y garantias sin el (``g202412.70015730``) mientras que los de
#: renta fija del mismo ZIP si lo llevaban. Exigirlo descartaba en silencio
#: 350 de los 420 archivos de ese mes.
_FILENAME_RE = re.compile(
    r"^(?P<tipo>[A-Za-z])(?P<periodo>\d{6})(?P<grupo>[VvGg]?)\.(?P<rut>\d{6,9})$"
)


def _normalise_periodo(token: str) -> int | None:
    """Lleva el token de 6 digitos del nombre de archivo a un periodo AAAAMM.

    La CMF cambio de convencion en diciembre de 2024 y el historico trae las
    dos mezcladas, asi que el mismo mes puede venir escrito de dos formas:

        ``i231231v.70015730``  -> AAMMDD, fecha de cierre  -> 202312
        ``g202501v.70015730``  -> AAAAMM, periodo directo   -> 202501

    Sin normalizar, ``231231`` y ``202312`` conviven como periodos distintos y
    el diff mes contra mes por folio nunca cruza el limite de 2024/2025.

    Las dos lecturas no se pisan: ``202412`` como AAMMDD daria mes 24, y
    ``201231`` como AAAAMM daria mes 31. Se prueba AAAAMM primero y solo se
    acepta si el mes es valido.

    Returns:
        El periodo como entero AAAAMM, o None si el token no es una fecha
        plausible bajo ninguna de las dos convenciones.
    """
    # AAAAMM: cuatro digitos de anio "20xx" y un mes 01-12.
    year, month = int(token[:4]), int(token[4:6])
    if 2000 <= year <= 2100 and 1 <= month <= 12:
        return year * 100 + month

    # AAMMDD: fecha de cierre de mes.
    yy, mm, dd = int(token[:2]), int(token[2:4]), int(token[4:6])
    if 1 <= mm <= 12 and 1 <= dd <= 31:
        return (2000 + yy) * 100 + mm

    return None

#: PICTURE de COBOL segun Seccion A.4: ``9(13)V9(04)``, ``X(60)``, ``-9(03)V9(04)``.
_PICTURE_RE = re.compile(r"^(?P<sign>-?)(?P<int>[9X])\((?P<n>\d+)\)(?:V9\((?P<d>\d+)\))?$")

#: Procedencias que el motor considera NO confiables para analitica.
UNTRUSTED_PROVENANCE = frozenset({"needs_calibration", "unknown"})


class LayoutError(ValueError):
    """El YAML de layout es internamente inconsistente."""


class UnknownFileTypeError(KeyError):
    """El archivo no corresponde a ningun tipo declarado en el layout."""


# ---------------------------------------------------------------------------
# Especificaciones (inmutables, construidas una vez al cargar el YAML)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Un campo dentro de un tipo de registro.

    Attributes:
        name: Nombre del campo tal como aparece en el anexo tecnico.
        start: Offset inicial en caracteres, base 0, inclusivo.
        length: Largo en caracteres.
        kind: ``text``, ``num`` o ``date``.
        decimals: Decimales implicitos (la ``V`` del PICTURE no ocupa posicion).
        signed: Si el primer caracter es el signo (``-`` / ``+`` / espacio).
        provenance: De donde salio esta definicion. Ver cabecera del YAML.
        picture: PICTURE original, para auditoria.
    """

    name: str
    start: int
    length: int
    kind: str = "text"
    decimals: int = 0
    signed: bool = False
    provenance: str = "official"
    picture: str | None = None

    @property
    def stop(self) -> int:
        """Offset final, exclusivo."""
        return self.start + self.length

    @property
    def trusted(self) -> bool:
        """True si el campo puede usarse para analitica financiera."""
        return self.provenance not in UNTRUSTED_PROVENANCE

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FieldSpec":
        """Construye un FieldSpec desde una entrada del YAML.

        Si el YAML trae ``pic`` y ademas ``len``/``dec``, se verifica que
        coincidan. Una discrepancia significa que alguien transcribio mal
        el anexo, y es mejor enterarse al cargar que tres meses despues
        mirando un nocional absurdo.

        Raises:
            LayoutError: si el PICTURE y los offsets explicitos no cuadran.
        """
        name = str(raw["name"])
        picture = raw.get("pic")
        length = raw.get("len")
        decimals = raw.get("dec")
        signed = raw.get("signed")

        if picture:
            p_len, p_dec, p_signed = cls._parse_picture(str(picture), name)
            if length is not None and int(length) != p_len:
                raise LayoutError(
                    f"{name}: PICTURE {picture} implica largo {p_len} "
                    f"pero el YAML declara {length}"
                )
            length, decimals, signed = p_len, p_dec, p_signed

        if length is None:
            raise LayoutError(f"{name}: falta 'len' y no hay 'pic' del cual derivarlo")

        return cls(
            name=name,
            start=int(raw["start"]),
            length=int(length),
            kind=str(raw.get("kind", "text")),
            decimals=int(decimals or 0),
            signed=bool(signed),
            provenance=str(raw.get("prov", "official")),
            picture=str(picture) if picture else None,
        )

    @staticmethod
    def _parse_picture(picture: str, field_name: str) -> tuple[int, int, bool]:
        """Traduce un PICTURE de COBOL a (largo, decimales, con_signo)."""
        m = _PICTURE_RE.match(picture.strip())
        if not m:
            raise LayoutError(f"{field_name}: PICTURE no reconocido: {picture!r}")
        signed = m.group("sign") == "-"
        n = int(m.group("n"))
        d = int(m.group("d") or 0)
        return n + d + (1 if signed else 0), d, signed


@dataclass(frozen=True, slots=True)
class RecordTypeSpec:
    """Un tipo de registro (identificacion, detalle, totales, ...)."""

    code: str
    name: str
    fields: tuple[FieldSpec, ...]
    control_field: str | None = None
    status: str | None = None

    @property
    def mapped(self) -> bool:
        """True si hay campos declarados para este tipo de registro."""
        return bool(self.fields)


@dataclass(frozen=True, slots=True)
class FileSpec:
    """Un archivo del ZIP (B.1, B.7, B.14, ...)."""

    letter: str
    anexo: str
    description: str
    record_length: int | None
    record_length_status: str
    record_types: Mapping[str, RecordTypeSpec]
    status: str | None = None


@dataclass(slots=True)
class ParsedRecord:
    """Resultado de parsear una linea.

    Attributes:
        source_file: Nombre del archivo dentro del ZIP.
        letter: Letra del archivo (``I``, ``P``, ``G``, ...).
        anexo: Anexo tecnico correspondiente (``B.1``, ``B.7``, ...).
        record_type: Codigo del tipo de registro (primer caracter).
        line_no: Numero de linea, base 1.
        rut_compania: RUT de la aseguradora, propagado desde el nombre del archivo.
        periodo: Periodo AAAAMM, propagado desde el nombre del archivo.
        fields: Diccionario campo -> valor tipado.
        untrusted: Campos presentes cuya definicion aun no esta confirmada.
        raw: Linea cruda. Se conserva siempre: permite recalibrar offsets
            sin volver a leer el ZIP original.
    """

    source_file: str
    letter: str
    anexo: str
    record_type: str
    line_no: int
    rut_compania: int | None
    periodo: int | None
    fields: dict[str, Any] = _dc_field(default_factory=dict)
    untrusted: tuple[str, ...] = ()
    raw: str = ""

    def to_row(self) -> dict[str, Any]:
        """Aplana el registro a una fila lista para DuckDB / Parquet."""
        row: dict[str, Any] = {
            "source_file": self.source_file,
            "archivo": self.letter,
            "anexo": self.anexo,
            "record_type": self.record_type,
            "line_no": self.line_no,
            "rut_compania": self.rut_compania,
            "periodo": self.periodo,
        }
        row.update(self.fields)
        return row


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------

class FixedWidthEngine:
    """Parser de ancho fijo configurado por YAML.

    El motor es inmutable y sin estado entre archivos, asi que una sola
    instancia puede reutilizarse para todo un backfill historico y es
    seguro compartirla entre hilos.
    """

    def __init__(self, spec: Mapping[str, FileSpec], defaults: Mapping[str, Any]) -> None:
        self._spec = dict(spec)
        self._defaults = dict(defaults)
        self._encodings: Sequence[str] = tuple(
            defaults.get("encoding_candidates", ("utf-8", "cp1252", "latin-1"))
        )
        self._comma_is_zero = bool(defaults.get("comma_is_zero", True))

    # -- construccion --------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> "FixedWidthEngine":
        """Carga y valida un archivo de layouts.

        Args:
            path: Ruta al YAML de layouts.

        Returns:
            Motor listo para usar.

        Raises:
            LayoutError: si algun tipo de registro tiene campos solapados o
                que se salen del largo declarado.
        """
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise LayoutError(f"No se pudo leer el layout {path}: {exc}") from exc

        defaults = raw.get("defaults", {}) or {}
        files: dict[str, FileSpec] = {}

        for letter, block in (raw.get("files") or {}).items():
            block = block or {}
            rec_len = block.get("record_length")
            types: dict[str, RecordTypeSpec] = {}

            for code, tspec in (block.get("record_types") or {}).items():
                tspec = tspec or {}
                fields = tuple(
                    FieldSpec.from_mapping(f) for f in (tspec.get("fields") or [])
                )
                if fields and rec_len:
                    cls._assert_consistent(str(letter), str(code), fields, int(rec_len))
                types[str(code)] = RecordTypeSpec(
                    code=str(code),
                    name=str(tspec.get("name", f"TIPO_{code}")),
                    fields=fields,
                    control_field=(tspec.get("control") or {}).get("field"),
                    status=tspec.get("status"),
                )

            files[str(letter).upper()] = FileSpec(
                letter=str(letter).upper(),
                anexo=str(block.get("anexo", "?")),
                description=str(block.get("descripcion", "")),
                record_length=int(rec_len) if rec_len else None,
                record_length_status=str(block.get("record_length_status", "unknown")),
                record_types=types,
                status=block.get("status"),
            )

        _LOG.info("Layout %s cargado: %d archivos declarados", path.name, len(files))
        return cls(files, defaults)

    @staticmethod
    def _assert_consistent(
        letter: str, code: str, fields: Sequence[FieldSpec], record_length: int
    ) -> None:
        """Verifica que los campos cubran el registro sin solapes ni desbordes.

        Un solape es casi siempre un error de transcripcion del anexo, y es
        justo el tipo de error que produce cifras plausibles pero falsas.
        """
        occupied = bytearray(record_length)
        for f in fields:
            if f.stop > record_length:
                raise LayoutError(
                    f"{letter}/{code}: campo {f.name} termina en {f.stop}, "
                    f"fuera del registro de {record_length}"
                )
            for i in range(f.start, f.stop):
                if occupied[i]:
                    raise LayoutError(
                        f"{letter}/{code}: campo {f.name} se solapa en la posicion {i}"
                    )
                occupied[i] = 1
        if (gap := record_length - sum(occupied)):
            _LOG.warning(
                "%s/%s: %d caracteres sin mapear (se conservan en _raw)",
                letter, code, gap,
            )

    # -- lectura -------------------------------------------------------------

    def describe(self, filename: str) -> tuple[FileSpec, int | None, int | None]:
        """Identifica archivo, RUT y periodo a partir del nombre.

        La CMF codifica la aseguradora y el periodo en el NOMBRE del archivo,
        no siempre de forma consistente con el contenido, asi que ambos se
        propagan a cada registro para poder cruzarlos despues.

        Args:
            filename: Nombre base del archivo, p.ej. ``p202608v.96573600``.

        Returns:
            Tupla ``(FileSpec, rut, periodo)``. RUT y periodo pueden ser None
            si el nombre no calza con el patron esperado.

        Raises:
            UnknownFileTypeError: si la letra no esta declarada en el layout.
        """
        m = _FILENAME_RE.match(Path(filename).name)
        if not m:
            raise UnknownFileTypeError(f"Nombre de archivo no reconocido: {filename!r}")
        letter = m.group("tipo").upper()
        if letter not in self._spec:
            raise UnknownFileTypeError(
                f"Archivo tipo {letter!r} no declarado en el layout ({filename})"
            )
        periodo = _normalise_periodo(m.group("periodo"))
        if periodo is None:
            _LOG.warning(
                "Periodo no interpretable en %s (token %r): el registro entra sin periodo",
                filename, m.group("periodo"))
        return self._spec[letter], int(m.group("rut")), periodo

    def decode(self, data: bytes) -> str:
        """Decodifica respetando el orden de candidatos del YAML.

        Los ZIP recientes vienen en UTF-8, pero los historicos pueden venir
        en CP1252. Se prueba en orden y se registra cual funciono.
        """
        for enc in self._encodings:
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        _LOG.warning("Ningun encoding limpio; se decodifica UTF-8 con reemplazo")
        return data.decode("utf-8", errors="replace")

    def parse_file(
        self, path: str | Path, *, data: bytes | None = None
    ) -> Iterator[ParsedRecord]:
        """Parsea un archivo completo, linea por linea.

        Args:
            path: Ruta o nombre del archivo (se usa para identificar el tipo).
            data: Bytes crudos. Si se omite, se leen desde ``path``. Pasarlos
                explicitamente permite leer directo del ZIP en streaming, sin
                escribir nada a disco.

        Yields:
            Un ``ParsedRecord`` por linea no vacia.
        """
        path = Path(path)
        spec, rut, periodo = self.describe(path.name)

        if data is None:
            data = path.read_bytes()
        text = self.decode(data)

        for i, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            yield self.parse_line(line, spec, path.name, i, rut, periodo)

    def parse_line(
        self,
        line: str,
        spec: FileSpec,
        source_file: str,
        line_no: int,
        rut: int | None,
        periodo: int | None,
    ) -> ParsedRecord:
        """Parsea una linea contra el layout de su tipo de registro.

        El tipo de registro es siempre el primer caracter. Si ese tipo no
        esta mapeado todavia, el registro se emite igual con la linea cruda:
        la ingesta nunca se cae por un layout incompleto.
        """
        code = line[:1]
        rtype = spec.record_types.get(code)

        rec = ParsedRecord(
            source_file=source_file,
            letter=spec.letter,
            anexo=spec.anexo,
            record_type=code,
            line_no=line_no,
            rut_compania=rut,
            periodo=periodo,
            raw=line,
        )

        if rtype is None or not rtype.mapped:
            rec.fields["_unmapped"] = True
            return rec

        # Guarda de version de layout. La CMF cambio el largo de registro en
        # 202412 (B.1 930->970, B.7 489->587) y el YAML describe una sola
        # generacion. Cortar una linea de 489 con offsets de 587 no falla:
        # `line[start:stop]` devuelve vacio pasado el final y el registro sale
        # lleno de None con algunos campos a medio truncar -- exactamente el
        # tipo de dato inventado que este motor no puede emitir.
        #
        # Si el largo no calza, el layout NO aplica a esta linea. Se emite con
        # la linea cruda y sin campos, igual que un tipo sin transcribir.
        if spec.record_length and len(line) != spec.record_length:
            rec.fields["_layout_mismatch"] = True
            rec.fields["_line_length"] = len(line)
            rec.fields["_expected_length"] = spec.record_length
            rec.untrusted = ("_layout_mismatch",)
            _LOG.debug(
                "%s:%d largo %d != %d declarado para %s: layout no aplica",
                source_file, line_no, len(line), spec.record_length, spec.letter,
            )
            return rec

        untrusted: list[str] = []
        for f in rtype.fields:
            chunk = line[f.start:f.stop]
            try:
                rec.fields[f.name] = self._cast(chunk, f)
            except Exception:  # noqa: BLE001 - un campo malo no bota el registro
                _LOG.debug(
                    "%s:%d campo %s no parseable: %r", source_file, line_no, f.name, chunk
                )
                rec.fields[f.name] = None
                rec.fields[f"{f.name}__raw"] = chunk
            if not f.trusted:
                untrusted.append(f.name)

        rec.untrusted = tuple(untrusted)
        return rec

    # -- conversion de tipos -------------------------------------------------

    def _cast(self, chunk: str, f: FieldSpec) -> Any:
        """Convierte un trozo crudo al tipo declarado.

        Reglas de la Seccion A.4 del anexo:
          - texto ausente = espacios, numerico ausente = ceros;
          - la coma vale cero;
          - el signo va en el primer caracter cuando el PICTURE lo declara;
          - los decimales son implicitos.
        """
        if f.kind == "text":
            value = chunk.strip()
            return value or None

        if self._comma_is_zero:
            chunk = chunk.replace(",", "0")

        if f.kind == "date":
            return self._cast_date(chunk)

        sign = 1
        body = chunk
        if f.signed:
            head, body = chunk[:1], chunk[1:]
            if head == "-":
                sign = -1
            elif head not in {"+", " ", ""}:
                body = chunk  # sin signo explicito: el campo completo es el numero

        body = body.strip() or "0"
        if not body.isdigit():
            raise ValueError(f"{f.name}: no numerico: {chunk!r}")

        if f.decimals:
            try:
                return sign * (Decimal(body) / (10 ** f.decimals))
            except InvalidOperation as exc:
                raise ValueError(f"{f.name}: decimal invalido: {chunk!r}") from exc
        return sign * int(body)

    @staticmethod
    def _cast_date(chunk: str) -> _dt.date | None:
        """Convierte AAAAMMDD. Los ceros significan 'sin informacion'."""
        body = chunk.strip()
        if not body or set(body) == {"0"}:
            return None
        try:
            return _dt.datetime.strptime(body, "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError(f"Fecha invalida: {chunk!r}") from exc

    # -- utilidades ----------------------------------------------------------

    @property
    def files(self) -> Mapping[str, FileSpec]:
        """Layouts cargados, por letra de archivo."""
        return dict(self._spec)

    def coverage_report(self) -> list[dict[str, Any]]:
        """Resume que tan completo esta el layout.

        Util para saber contra que se esta trabajando antes de confiar en un
        numero: distingue lo transcrito del anexo de lo deducido a ojo.
        """
        rows: list[dict[str, Any]] = []
        for letter, spec in sorted(self._spec.items()):
            for code, rtype in sorted(spec.record_types.items()):
                by_prov: dict[str, int] = {}
                for f in rtype.fields:
                    by_prov[f.provenance] = by_prov.get(f.provenance, 0) + 1
                rows.append({
                    "archivo": letter,
                    "anexo": spec.anexo,
                    "record_type": code,
                    "nombre": rtype.name,
                    "campos": len(rtype.fields),
                    "mapeado": rtype.mapped,
                    "procedencias": by_prov,
                })
        return rows
