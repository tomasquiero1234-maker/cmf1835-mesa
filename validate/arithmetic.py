"""
cmf1835.validate.arithmetic
===========================

Garantia de "cero alucinacion" por comprobacion, no por confianza.

La idea
-------
Un layout mal transcrito no produce basura evidente: produce numeros
*plausibles*. Un nocional desplazado un caracter sigue pareciendo un nocional.
La unica defensa real es que cada registro se valide contra sus propias
identidades financieras internas, que son redundantes por construccion.

Un forward de la CMF informa a la vez el nocional, el precio pactado, el monto
en pesos, el precio spot, el precio forward de mercado, la tasa de descuento y
el valor razonable. Eso esta sobredeterminado: si el layout es correcto, los
numeros tienen que cerrar entre si. Si no cierran, el layout esta mal o el dato
esta mal, y en ambos casos el registro no debe entrar al warehouse.

Ejemplo real (BCI Vida, 202608, forward de venta contra Scotiabank)::

    nocional 350 x precio pactado 924,05          = 323.417,5   (informado: 323.417,5)  OK
    (932,38 - 924,05) x 350 / (1 + 0,0468 x 109/360) = 2.875,1   (informado: 2.875,0)   OK

Con dos identidades independientes cuadrando al ultimo decimal, la probabilidad
de que los offsets esten mal por casualidad es despreciable. Esa es la prueba.

Politica
--------
Cada regla devuelve un ``CheckResult``. El veredicto del registro se resuelve
asi:

  - ``PASS``        : todas las reglas aplicables cerraron dentro de tolerancia.
  - ``QUARANTINE``  : alguna regla fallo, o el registro usa campos cuya
                      definicion todavia no esta confirmada.
  - ``UNVERIFIED``  : no habia informacion suficiente para probar nada. No es
                      un error, pero tampoco es una garantia, y se marca como
                      tal para que nadie construya P&L encima sin saberlo.

Las tolerancias son relativas y configurables porque la CMF aproxima montos al
entero superior (Seccion A.4.g) y el redondeo se propaga.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field as _dc_field
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "Verdict",
    "CheckResult",
    "RecordVerdict",
    "ArithmeticValidator",
    "Tolerances",
]

_LOG = logging.getLogger(__name__)


class Verdict(str, Enum):
    """Resultado de validar un registro."""

    PASS = "pass"
    QUARANTINE = "quarantine"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Tolerances:
    """Margenes de error aceptables.

    Attributes:
        relative: Error relativo maximo en identidades de producto
            (nocional x precio). 1e-6 absorbe el redondeo al entero superior.
        mtm_relative: Error relativo maximo al reconstruir el valor razonable.
            Mas holgado porque no conocemos la convencion exacta de conteo de
            dias que uso la compania (ACT/360 vs ACT/365 vs 30/360).
        mtm_absolute_floor: Piso absoluto en M$ bajo el cual no se exige
            precision relativa. Evita marcar como sospechoso un MTM de 3 M$.
    """

    relative: Decimal = Decimal("1e-6")
    mtm_relative: Decimal = Decimal("0.02")
    mtm_absolute_floor: Decimal = Decimal("10")


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Resultado de una regla individual."""

    rule: str
    ok: bool | None          # None = no aplicable / sin datos
    expected: Decimal | None = None
    observed: Decimal | None = None
    detail: str = ""

    @property
    def error(self) -> Decimal | None:
        """Error relativo observado, si ambos valores existen."""
        if self.expected is None or self.observed is None:
            return None
        if self.expected == 0:
            return abs(self.observed)
        return abs(self.observed - self.expected) / abs(self.expected)


@dataclass(slots=True)
class RecordVerdict:
    """Veredicto consolidado de un registro."""

    verdict: Verdict
    checks: list[CheckResult] = _dc_field(default_factory=list)
    reasons: list[str] = _dc_field(default_factory=list)

    @property
    def quarantined(self) -> bool:
        return self.verdict is Verdict.QUARANTINE

    def to_row(self) -> dict[str, Any]:
        """Fila para la tabla de auditoria del warehouse."""
        return {
            "verdict": self.verdict.value,
            "reglas_ok": sum(1 for c in self.checks if c.ok is True),
            "reglas_fallidas": sum(1 for c in self.checks if c.ok is False),
            "reglas_na": sum(1 for c in self.checks if c.ok is None),
            "motivos": "; ".join(self.reasons) or None,
        }


def _dec(value: Any) -> Decimal | None:
    """Convierte a Decimal lo que se pueda; None si no aplica."""
    if value is None or isinstance(value, (str, bool, _dt.date)):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


class ArithmeticValidator:
    """Aplica las identidades financieras que cada anexo permite comprobar."""

    def __init__(
        self,
        tolerances: Tolerances | None = None,
        *,
        day_count: Decimal = Decimal(360),
    ) -> None:
        """
        Args:
            tolerances: Margenes de error. Se usan los por defecto si se omite.
            day_count: Base de conteo de dias para descontar el MTM. La CMF no
                declara la convencion, asi que se prueban ACT/360 y ACT/365 y
                se acepta la que cierre mejor; este valor es solo el punto de
                partida.
        """
        self.tol = tolerances or Tolerances()
        self.day_count = day_count

    # -- despacho ------------------------------------------------------------

    def validate(
        self, letter: str, record_type: str, fields: Mapping[str, Any],
        *, untrusted: Sequence[str] = (), periodo: int | None = None,
    ) -> RecordVerdict:
        """Valida un registro segun su anexo y tipo.

        Args:
            letter: Letra del archivo (``I``, ``P``, ...).
            record_type: Codigo del tipo de registro.
            fields: Campos ya parseados.
            untrusted: Campos cuya definicion no esta confirmada.
            periodo: Periodo AAAAMM, necesario para las reglas con fechas.

        Returns:
            Veredicto consolidado.
        """
        rules = self._rules_for(letter, record_type)
        if not rules:
            return RecordVerdict(
                Verdict.UNVERIFIED,
                reasons=[f"Sin reglas definidas para {letter}/{record_type}"],
            )

        checks = [r(fields, periodo) for r in rules]
        failed = [c for c in checks if c.ok is False]
        passed = [c for c in checks if c.ok is True]

        reasons: list[str] = []
        for c in failed:
            err = c.error
            reasons.append(
                f"{c.rule}: esperado {c.expected} vs informado {c.observed}"
                + (f" (error {err:.2%})" if err is not None else "")
                + (f" -- {c.detail}" if c.detail else "")
            )

        # Un campo cuya posicion no esta confirmada contamina cualquier
        # conclusion que dependa de el. Se cuarentena aunque la aritmetica
        # haya cerrado: cerrar por casualidad tambien es posible.
        risky = [u for u in untrusted if u in fields and fields[u] is not None]
        if risky:
            reasons.append(
                "Campos sin definicion confirmada: " + ", ".join(sorted(risky)[:6])
            )

        if failed or risky:
            return RecordVerdict(Verdict.QUARANTINE, checks, reasons)
        if not passed:
            return RecordVerdict(
                Verdict.UNVERIFIED, checks,
                ["Ninguna identidad pudo evaluarse con los datos presentes"],
            )
        return RecordVerdict(Verdict.PASS, checks, [])

    def _rules_for(
        self, letter: str, record_type: str
    ) -> list[Callable[[Mapping[str, Any], int | None], CheckResult]]:
        key = (letter.upper(), str(record_type))
        table: dict[tuple[str, str], list[Callable[..., CheckResult]]] = {
            ("P", "3"): [self.check_forward_notional, self.check_forward_mtm],
            ("I", "2"): [self.check_bond_tenor, self.check_bond_nominal_vigente],
        }
        return table.get(key, [])

    # -- B.7 registro 3: FORWARDS -------------------------------------------

    #: Nombres oficiales del anexo (B.7 registro tipo 3). El activo objeto se
    #: informa en unidades de su propia moneda; los nocionales, en M$.
    F_LONG_NAME = "ACTIVO_OBJETO_POSICION_LARGA(nombre)"
    F_SHORT_NAME = "ACTIVO_OBJETO_POSICION_CORTA_(nombre)"
    F_LONG_UNITS = "ACTIVO_OBJETO_POSICION_LARGA(unidades)"
    F_SHORT_UNITS = "ACTIVO_OBJETO_POSICION_CORTA(unidades)"
    F_LONG_NOTIONAL = "NOCIONAL_POSICION_LARGA(monto)"
    F_SHORT_NOTIONAL = "NOCIONAL_POSICION_CORTA(monto)"
    F_MTM = "VALOR_RAZONABLE_DEL_CONTRATO_A_LA_FECHA_DE_LA_INFORMACION"

    #: Codigo de moneda de la CMF para el peso chileno.
    CLP = "$$"

    #: Factor para pasar de pesos a M$ (miles de pesos), Seccion A.4.g.
    M_PESOS = Decimal(1000)

    @classmethod
    def _fx_leg(cls, f: Mapping[str, Any]) -> tuple[Decimal | None, str | None]:
        """Identifica la pata en moneda extranjera y devuelve (unidades, moneda).

        En un forward de tipo de cambio una pata esta en pesos y la otra en
        divisa. El nocional en divisa es el que multiplica al precio, asi que
        hay que saber cual es cual antes de validar nada. La pata se identifica
        por el codigo de moneda, no por la posicion, porque el orden se invierte
        entre compra (FWC) y venta (FWV).
        """
        long_ccy = f.get(cls.F_LONG_NAME)
        short_ccy = f.get(cls.F_SHORT_NAME)
        if long_ccy and long_ccy != cls.CLP:
            return _dec(f.get(cls.F_LONG_UNITS)), long_ccy
        if short_ccy and short_ccy != cls.CLP:
            return _dec(f.get(cls.F_SHORT_UNITS)), short_ccy
        return None, None

    def check_forward_notional(
        self, f: Mapping[str, Any], periodo: int | None = None
    ) -> CheckResult:
        """Identidades de nocional del forward.

        Se comprueban dos a la vez, porque involucran cuatro campos repartidos
        a lo largo del registro (unidades en 289-320, nocionales en 321-346,
        precios en 353-396). Si ambas cierran, todo ese tramo esta alineado::

            nocional de la pata en pesos = unidades_fx x precio_contrato / 1000
            nocional de la pata en divisa = unidades_fx x precio_spot   / 1000

        Verificado sobre los cinco forwards de BCI Vida en 202608: cierran al
        peso en los cinco, en ambos sentidos (FWC y FWV).
        """
        rule = "forward.nocional = unidades_fx x precio / 1000"
        units, ccy = self._fx_leg(f)
        price_k = _dec(f.get("PRECIO_FORWARD_CONTRATO"))
        spot = _dec(f.get("PRECIO_SPOT_DEL_ACTIVO_SUBYACENTE"))
        n_long = _dec(f.get(self.F_LONG_NOTIONAL))
        n_short = _dec(f.get(self.F_SHORT_NOTIONAL))

        if None in (units, price_k, spot, n_long, n_short) or not units:
            return CheckResult(rule, None, detail="Faltan unidades, precios o nocionales")

        at_contract = units * price_k / self.M_PESOS
        at_spot = units * spot / self.M_PESOS
        reported = {n_long, n_short}

        def close(expected: Decimal) -> bool:
            return any(
                abs(r - expected) <= max(abs(expected) * self.tol.relative, Decimal(1))
                for r in reported
            )

        if close(at_contract) and close(at_spot):
            return CheckResult(rule, True, at_contract, n_short,
                               f"pata {ccy}: {units} unidades")
        return CheckResult(
            rule, False, at_contract, n_short,
            f"pata {ccy}: contrato={at_contract} spot={at_spot} "
            f"vs informados {sorted(reported)}",
        )

    def check_forward_mtm(
        self, f: Mapping[str, Any], periodo: int | None = None
    ) -> CheckResult:
        """Reconstruye el valor razonable del forward desde el propio registro.

        La identidad, con todos los insumos tomados del mismo registro::

            MTM = s x (fwd_mercado - fwd_contrato) x unidades_fx / 1000
                      / (1 + tasa_descuento x dias / base)

        donde ``s`` es +1 si la compania compro la divisa (FWC) y -1 si la
        vendio (FWV). Es la comprobacion mas dura del archivo: cruza seis
        campos, dos fechas y el signo, y no hay forma de que cierre por azar.

        Verificado sobre 202608: 20.145, 2.875, -24.296, 4.206 y -222.566 M$
        reproducidos con error menor a 1 M$ en los cinco casos.
        """
        rule = "forward.mtm = s x (fwd_mercado - fwd_contrato) x nocional descontado"
        units, _ = self._fx_leg(f)
        fwd_k = _dec(f.get("PRECIO_FORWARD_CONTRATO"))
        fwd_m = _dec(f.get("PRECIO_FORWARD_MERCADO"))
        rate = _dec(f.get("TASA_DESCUENTO_DE_FLUJOS"))
        mtm = _dec(f.get(self.F_MTM))
        maturity = f.get("FECHA_DE_VENCIMIENTO_DEL_CONTRATO")
        operation = (f.get("TIPO_OPERACION") or "").strip().upper()

        if None in (units, fwd_k, fwd_m, mtm) or not isinstance(maturity, _dt.date):
            return CheckResult(rule, None, detail="Faltan precios, unidades, MTM o vencimiento")

        valuation = self._period_end(periodo)
        if valuation is None or maturity <= valuation:
            return CheckResult(rule, None, detail="Contrato vencido o periodo desconocido")

        # FWC: la compania compra la divisa y gana si el mercado sube.
        # FWV: la vendio, y el signo del valor razonable se invierte.
        if operation.startswith("FWC"):
            sign = Decimal(1)
        elif operation.startswith("FWV"):
            sign = Decimal(-1)
        else:
            return CheckResult(rule, None, detail=f"Sentido no reconocido: {operation!r}")

        days = Decimal((maturity - valuation).days)
        gross = sign * (fwd_m - fwd_k) * units / self.M_PESOS

        # La CMF no declara la convencion de conteo de dias; se prueban las dos
        # usuales y basta que una cierre. El objetivo es validar el layout, no
        # replicar el motor de valorizacion de la compania.
        best: CheckResult | None = None
        for basis in (Decimal(360), Decimal(365)):
            expected = gross if rate is None else gross / (Decimal(1) + (rate / 100) * days / basis)
            limit = max(abs(expected) * self.tol.mtm_relative, self.tol.mtm_absolute_floor)
            result = CheckResult(rule, abs(mtm - expected) <= limit, expected, mtm,
                                 f"{operation}, ACT/{basis}, {days} dias")
            if result.ok:
                return result
            if best is None or (result.error or Decimal(9)) < (best.error or Decimal(9)):
                best = result
        return best or CheckResult(rule, None)

    # -- B.1 registro 2: RENTA FIJA -----------------------------------------

    def check_bond_tenor(
        self, f: Mapping[str, Any], periodo: int | None = None
    ) -> CheckResult:
        """PLAZO_AL_VENCIMIENTO = meses entre el cierre y el vencimiento.

        Es la identidad mas barata de B.1 y sirve como canario: cruza un campo
        numerico corto con una fecha que esta 500 caracteres mas alla. Si ambos
        coinciden, todo el tramo intermedio esta alineado.
        """
        rule = "renta_fija.plazo_al_vencimiento = meses(cierre -> vencimiento)"
        declared = _dec(f.get("PLAZO_AL_VENCIMIENTO"))
        maturity = f.get("FECHA_VENCIMIENTO")
        valuation = self._period_end(periodo)

        if declared is None or not isinstance(maturity, _dt.date) or valuation is None:
            return CheckResult(rule, None, detail="Faltan plazo, vencimiento o periodo")

        months = (maturity.year - valuation.year) * 12 + (maturity.month - valuation.month)
        months = max(months, 0)
        # Tolerancia de un mes: la CMF no documenta si trunca o redondea.
        return CheckResult(
            rule, abs(int(declared) - months) <= 1,
            Decimal(months), declared, "tolerancia +/- 1 mes",
        )

    def check_bond_nominal_vigente(
        self, f: Mapping[str, Any], periodo: int | None = None
    ) -> CheckResult:
        """El nominal vigente nunca puede superar al nominal original.

        Solo se informa para BE, EC, MHA, MHB y BS; en el resto va en ceros,
        y entonces la regla no aplica en vez de fallar.
        """
        rule = "renta_fija.valor_nominal_vigente <= valor_nominal"
        nominal = _dec(f.get("VALOR_NOMINAL"))
        current = _dec(f.get("VALOR_NOMINAL_VIGENTE"))

        if nominal is None or current is None or current == 0:
            return CheckResult(rule, None, detail="No aplica a este tipo de instrumento")
        return CheckResult(
            rule, current <= nominal * (Decimal(1) + self.tol.relative),
            nominal, current, "el vigente descuenta amortizaciones",
        )

    # -- utilidades ----------------------------------------------------------

    @staticmethod
    def _period_end(periodo: int | None) -> _dt.date | None:
        """Ultimo dia del periodo AAAAMM, que es la fecha de valorizacion."""
        if not periodo:
            return None
        try:
            year, month = divmod(int(periodo), 100)
            if not (1 <= month <= 12):
                return None
            nxt = _dt.date(year + (month == 12), (month % 12) + 1, 1)
            return nxt - _dt.timedelta(days=1)
        except (ValueError, TypeError):
            return None


def summarise(verdicts: Iterable[RecordVerdict]) -> dict[str, int]:
    """Cuenta veredictos. Este es el numero que se mira antes de publicar.

    Un salto en ``quarantine`` de un mes al siguiente casi siempre significa
    que la CMF cambio el formato, no que el mercado cambio de comportamiento.
    """
    out = {v.value: 0 for v in Verdict}
    for v in verdicts:
        out[v.verdict.value] += 1
    return out
