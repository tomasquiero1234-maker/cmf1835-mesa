"""
cmf1835.normalize.entities
==========================

Resolucion de contrapartes: de string sucio a entidad canonica.

Por que importa
---------------
El market share por banco es la vista mas usada del dashboard, y se calcula
agrupando por contraparte. Si "BANCO DE SANTANDER", "SANTANDER CHILE" y
"BCO SANTANDER" quedan como tres filas distintas, el share de Santander sale
partido en tres y la conclusion es falsa. Peor: es falsa de forma invisible.

La buena noticia es que el layout vigente de B.7 trae el RUT de la contraparte
(posiciones 48-57), que el anexo de 2016 no tenia. Eso convierte el problema de
un ejercicio de fuzzy matching en un join exacto. El texto pasa a ser el plan B.

Cascada de resolucion
---------------------
Se intenta en este orden y se registra siempre CUAL paso resolvio, para poder
auditar despues que porcentaje del nocional se agrupo por RUT y cual por texto:

  1. ``rut``        -- RUT + digito verificador validado con modulo 11.
  2. ``alias``      -- alias exacto tras normalizar (mayusculas, sin tildes,
                       sin sufijos societarios).
  3. ``identifier`` -- ISIN, CUSIP o LEI, para emisores y fondos extranjeros
                       que no tienen RUT.
  4. ``fuzzy``      -- similitud de tokens sobre el nombre normalizado, con
                       umbral alto y solo si no hubo ambiguedad.
  5. ``unresolved`` -- se conserva el nombre limpio y se emite al log de
                       revision. NUNCA se inventa una entidad.

El paso 5 es deliberado. Una contraparte sin resolver es un dato faltante
honesto; una contraparte mal resuelta es un error que se propaga silenciosamente
a todas las vistas.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field as _dc_field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

__all__ = [
    "Entity",
    "Resolution",
    "EntityResolver",
    "validate_rut",
    "normalize_name",
]

_LOG = logging.getLogger(__name__)

#: Sufijos societarios que no distinguen entidades y solo ensucian el match.
_LEGAL_SUFFIXES = (
    "SOCIEDAD ANONIMA", "S A", "SA", "SPA", "S P A", "LTDA", "LIMITADA",
    "SEGUROS DE VIDA", "COMPANIA DE SEGUROS", "CIA DE SEGUROS", "CIA",
    "N A", "NA", "PLC", "AG", "SE", "INC", "CORP", "CO", "LLC", "LP",
    "BRANCH", "SUCURSAL", "SUCURSAL CHILE", "CHILE", "NEW YORK", "LONDON",
)

#: Ruido que la CMF arrastra en campos de texto libre: fragmentos de RUT
#: pegados al nombre, guiones sueltos, dobles espacios.
_NOISE_RE = re.compile(r"\b\d{6,}[-]?[0-9K]?\b")
_NONWORD_RE = re.compile(r"[^A-Z0-9 ]+")
_SPACES_RE = re.compile(r"\s+")

#: Umbral de similitud para aceptar un match difuso. Alto a proposito: es
#: preferible dejar cien contrapartes sin resolver que fusionar dos bancos.
FUZZY_THRESHOLD = 0.92


def validate_rut(rut: int | str | None, dv: str | None) -> bool:
    """Valida un RUT chileno con el algoritmo de modulo 11.

    Sirve de red de seguridad contra offsets mal calibrados: si el campo que
    creemos que es el RUT no valida en una fraccion alta de los registros,
    entonces no es el RUT.

    Args:
        rut: Cuerpo del RUT sin digito verificador.
        dv: Digito verificador (``0``-``9`` o ``K``).

    Returns:
        True si el digito verificador corresponde al cuerpo.

    Examples:
        >>> validate_rut(97006000, "6")
        True
        >>> validate_rut(97006000, "1")
        False
    """
    if rut is None or dv is None:
        return False
    try:
        body = int(str(rut).strip())
    except (TypeError, ValueError):
        return False
    if body <= 0:
        return False

    total, factor = 0, 2
    for digit in reversed(str(body)):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1

    remainder = 11 - (total % 11)
    expected = {11: "0", 10: "K"}.get(remainder, str(remainder))
    return expected == str(dv).strip().upper()


def normalize_name(raw: str | None) -> str:
    """Normaliza un nombre para comparacion.

    Quita tildes, pasa a mayusculas, elimina fragmentos de RUT incrustados,
    descarta puntuacion y remueve sufijos societarios. El resultado no se
    guarda como nombre de la entidad; solo se usa como clave de match.

    Examples:
        >>> normalize_name("Banco de Chile S.A.")
        'BANCO DE CHILE'
        >>> normalize_name("K AMERICA 076123456 JP MORGAN CHASE N.A.")
        'K AMERICA JP MORGAN CHASE'
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKD", str(raw))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).upper()
    text = _NOISE_RE.sub(" ", text)
    text = _NONWORD_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text).strip()

    changed = True
    while changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True
    return text


@dataclass(frozen=True, slots=True)
class Entity:
    """Entidad canonica del catalogo.

    Attributes:
        key: Identificador estable interno. No cambia aunque la entidad
            se fusione o cambie de razon social.
        name: Nombre para mostrar.
        parent: Clave de la matriz, si es filial. Permite agregar riesgo de
            contraparte a nivel de grupo sin perder el detalle de la filial.
        kind: ``banco``, ``aseguradora``, ``afp``, ``fondo``, ``corporativo``,
            ``soberano``, ``otro``.
        country: Codigo ISO de dos letras.
        ruts: RUTs asociados. Una entidad puede tener varios (fusiones).
        aliases: Nombres alternativos ya normalizados.
        identifiers: LEI, ISIN o CUSIP para entidades sin RUT.
    """

    key: str
    name: str
    parent: str | None = None
    kind: str = "otro"
    country: str | None = None
    ruts: frozenset[int] = frozenset()
    aliases: frozenset[str] = frozenset()
    identifiers: frozenset[str] = frozenset()

    @property
    def group_key(self) -> str:
        """Clave a usar para agregar exposicion a nivel de grupo."""
        return self.parent or self.key


@dataclass(slots=True)
class Resolution:
    """Resultado de resolver una contraparte."""

    entity: Entity | None
    method: str                 # rut | alias | identifier | fuzzy | unresolved
    confidence: float
    raw_name: str | None = None
    normalized: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        """Clave a usar en el warehouse. Si no resolvio, queda el texto limpio."""
        if self.entity:
            return self.entity.key
        return f"UNRESOLVED::{self.normalized}" if self.normalized else "UNRESOLVED::"

    @property
    def display(self) -> str:
        return self.entity.name if self.entity else (self.raw_name or "").strip()

    def to_row(self) -> dict[str, Any]:
        """Fila para la dimension de contraparte."""
        return {
            "contraparte_key": self.key,
            "contraparte_nombre": self.display,
            "contraparte_grupo": self.entity.group_key if self.entity else None,
            "contraparte_tipo": self.entity.kind if self.entity else None,
            "contraparte_pais": self.entity.country if self.entity else None,
            "resolucion_metodo": self.method,
            "resolucion_confianza": round(self.confidence, 4),
            "resolucion_nota": self.note or None,
        }


class EntityResolver:
    """Resuelve contrapartes contra un catalogo declarativo.

    El catalogo (``config/entities.yaml``) es la unica fuente de verdad sobre
    quien es quien. Se versiona en git como cualquier otro codigo, porque una
    fusion bancaria cambia retroactivamente todas las series historicas y hay
    que poder rastrear cuando se aplico el cambio.
    """

    def __init__(self, entities: Iterable[Entity]) -> None:
        self._entities: list[Entity] = list(entities)
        self._by_key = {e.key: e for e in self._entities}
        self._by_rut: dict[int, Entity] = {}
        self._by_alias: dict[str, Entity] = {}
        self._by_identifier: dict[str, Entity] = {}
        self._unresolved: Counter[str] = Counter()

        for e in self._entities:
            for rut in e.ruts:
                if rut in self._by_rut and self._by_rut[rut].key != e.key:
                    _LOG.warning(
                        "RUT %s asignado a %s y %s; gana el primero",
                        rut, self._by_rut[rut].key, e.key,
                    )
                    continue
                self._by_rut[rut] = e
            for alias in {normalize_name(e.name), *e.aliases}:
                if alias:
                    self._by_alias.setdefault(alias, e)
            for ident in e.identifiers:
                self._by_identifier.setdefault(ident.strip().upper(), e)

        # Se valida que las matrices referenciadas existan. Un `parent` colgado
        # rompe silenciosamente la agregacion por grupo.
        for e in self._entities:
            if e.parent and e.parent not in self._by_key:
                _LOG.warning("Entidad %s apunta a matriz inexistente %s", e.key, e.parent)

    # -- carga ---------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> "EntityResolver":
        """Carga el catalogo de entidades.

        Formato esperado::

            entities:
              - key: SANTANDER
                name: Banco Santander-Chile
                kind: banco
                country: CL
                ruts: [97036000]
                aliases: [BANCO DE SANTANDER, SANTANDER CHILE]
              - key: FALABELLA_RETAIL
                name: Falabella Retail S.A.
                parent: FALABELLA
                kind: corporativo
        """
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entities: list[Entity] = []
        for item in raw.get("entities", []):
            entities.append(Entity(
                key=str(item["key"]).strip().upper(),
                name=str(item.get("name", item["key"])).strip(),
                parent=(str(item["parent"]).strip().upper() if item.get("parent") else None),
                kind=str(item.get("kind", "otro")),
                country=(str(item["country"]).upper() if item.get("country") else None),
                ruts=frozenset(int(r) for r in (item.get("ruts") or [])),
                aliases=frozenset(
                    a for a in (normalize_name(x) for x in (item.get("aliases") or [])) if a
                ),
                identifiers=frozenset(
                    str(i).strip().upper() for i in (item.get("identifiers") or [])
                ),
            ))
        _LOG.info("Catalogo %s: %d entidades", path.name, len(entities))
        return cls(entities)

    # -- resolucion ----------------------------------------------------------

    def resolve(
        self,
        *,
        rut: int | str | None = None,
        dv: str | None = None,
        name: str | None = None,
        identifier: str | None = None,
    ) -> Resolution:
        """Resuelve una contraparte por la cascada descrita en el modulo.

        Args:
            rut: Cuerpo del RUT de la contraparte, si el registro lo trae.
            dv: Digito verificador.
            name: Nombre tal como viene en el archivo, sin limpiar.
            identifier: LEI, ISIN o CUSIP, para entidades extranjeras.

        Returns:
            Resolucion, siempre. Nunca lanza: una contraparte que no resuelve
            es un dato, no una excepcion.
        """
        normalized = normalize_name(name)

        # 1. RUT. El unico metodo que no puede equivocarse.
        if rut is not None:
            try:
                body = int(str(rut).strip())
            except (TypeError, ValueError):
                body = 0
            if body > 0:
                entity = self._by_rut.get(body)
                checksum_ok = validate_rut(body, dv) if dv is not None else None
                if entity is not None:
                    note = "" if checksum_ok is not False else "digito verificador no cuadra"
                    return Resolution(entity, "rut", 1.0 if checksum_ok is not False else 0.9,
                                      name, normalized, note)
                if checksum_ok:
                    # RUT valido pero desconocido: es una entidad real que falta
                    # en el catalogo, no un error de parseo. Vale la pena avisar.
                    _LOG.info("RUT %s-%s valido pero ausente del catalogo (%s)",
                              body, dv, (name or "").strip())

        # 2. Alias exacto.
        if normalized and (entity := self._by_alias.get(normalized)):
            return Resolution(entity, "alias", 0.98, name, normalized)

        # 3. Identificador internacional.
        if identifier and (entity := self._by_identifier.get(identifier.strip().upper())):
            return Resolution(entity, "identifier", 0.97, name, normalized)

        # 4. Fuzzy, solo si hay un unico candidato claramente mejor.
        if normalized:
            match = self._best_fuzzy(normalized)
            if match:
                entity, score, runner_up = match
                if score - runner_up < 0.03:
                    self._unresolved[normalized] += 1
                    return Resolution(
                        None, "unresolved", 0.0, name, normalized,
                        f"ambiguo entre candidatos (mejor {score:.2f}, segundo {runner_up:.2f})",
                    )
                return Resolution(entity, "fuzzy", score, name, normalized,
                                  f"similitud {score:.2f}")

        # 5. Sin resolver. Se conserva el nombre limpio y se acumula para revision.
        if normalized:
            self._unresolved[normalized] += 1
        return Resolution(None, "unresolved", 0.0, name, normalized)

    def _best_fuzzy(self, normalized: str) -> tuple[Entity, float, float] | None:
        """Devuelve (mejor entidad, score, score del segundo) o None."""
        scored: list[tuple[float, Entity]] = []
        tokens = set(normalized.split())
        if not tokens:
            return None

        for alias, entity in self._by_alias.items():
            alias_tokens = set(alias.split())
            if not alias_tokens:
                continue
            # Pre-filtro barato: sin tokens en comun no hay nada que comparar.
            overlap = len(tokens & alias_tokens) / len(tokens | alias_tokens)
            if overlap < 0.3:
                continue
            score = SequenceMatcher(None, normalized, alias).ratio()
            scored.append((score, entity))

        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_entity = scored[0]
        if best_score < FUZZY_THRESHOLD:
            return None
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        return best_entity, best_score, runner_up

    # -- observabilidad ------------------------------------------------------

    def unresolved_report(self, top: int = 50) -> list[dict[str, Any]]:
        """Contrapartes sin resolver, de mayor a menor frecuencia.

        Esta lista es el backlog de mantenimiento del catalogo. Revisarla cada
        mes es lo que impide que el market share se degrade con el tiempo.
        """
        return [
            {"nombre_normalizado": n, "ocurrencias": c}
            for n, c in self._unresolved.most_common(top)
        ]

    def coverage(self, resolutions: Iterable[Resolution]) -> dict[str, Any]:
        """Resume por que metodo se resolvio cada contraparte.

        El numero que importa es el porcentaje resuelto por ``rut``: mientras
        mas alto, menos depende el market share de heuristicas de texto.
        """
        counts = Counter(r.method for r in resolutions)
        total = sum(counts.values()) or 1
        return {
            "total": total,
            "por_metodo": dict(counts),
            "pct_por_rut": round(100 * counts.get("rut", 0) / total, 2),
            "pct_sin_resolver": round(100 * counts.get("unresolved", 0) / total, 2),
        }
