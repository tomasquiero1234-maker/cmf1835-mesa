"""
analytics.flows
===============

Diff de cartera mes contra mes sobre TODOS los derivados: opciones,
forwards, futuros, swaps y pactos. No solo forwards.

Grano
-----
Una POSICION es ``(rut_compania, folio_operacion)``. El folio es el numero de
papeleta de la mesa, unico dentro de la compania. Un swap puede traer dos
items (una pata por lado); los items se agregan, porque lo que se abre y se
cierra es la operacion, no la pata.

Las cinco categorias
--------------------
Cada posicion del universo ``T-1 union T`` cae en exactamente una:

    ACTIVE     esta en T y en T-1
    NEW        esta en T y no en T-1
    UNWIND     estaba en T-1, no esta en T, y vencia DESPUES del cierre de T
               -> se cerro antes de tiempo
    MATURITY   estaba en T-1, no esta en T, y vencia EN o ANTES del cierre
               -> se murio sola
    ROLL       par (una que muere, una que nace) con la misma compania, la
               misma contraparte, el mismo producto y nocional parecido

ROLL tiene precedencia: si una posicion que desaparece queda emparejada con
una que aparece, las dos se marcan ROLL en vez de UNWIND y NEW. Sin esa
precedencia las categorias se solapan y el 100% deja de sumar 100%.

La diferencia entre UNWIND y MATURITY es la que le importa a la mesa:
un vencimiento es calendario, un unwind es una decision del cliente. Meterlos
en la misma bolsa borra justamente la senal.

Sobre la heuristica de ROLL
---------------------------
Es una heuristica y se comporta como tal: empareja por contraparte, producto
y cercania de nocional dentro de una tolerancia, exigiendo que el match sea
MUTUAMENTE el mejor de los dos lados. Eso la hace determinista y evita que
una posicion que muere se lleve tres que nacen. Cada fila marcada ROLL deja
escrito con quien se emparejo y con que diferencia de nocional, para que
alguien pueda discutirla mirando numeros.

Uso
---
    python -m analytics.flows --periodo 202608
    python -m analytics.flows --todos
    python -m analytics.flows --sql          # imprime el SQL y no ejecuta nada
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_LOG = logging.getLogger("cmf1835.flows")

DEFAULT_DB = ROOT / "warehouse" / "data" / "cmf1835.duckdb"

#: Tolerancia relativa de nocional para considerar que dos operaciones son
#: la misma renovada. 10% deja pasar un roll que ajusta tamano sin dejar
#: pasar dos operaciones sin relacion.
TOL_NOCIONAL = 0.10


# ---------------------------------------------------------------------------
#  SQL
# ---------------------------------------------------------------------------

SQL_POSICIONES = """
-- ---------------------------------------------------------------------------
-- Posiciones por periodo: se agregan los items de un mismo folio.
-- Cubre los cinco productos de B.7, no solo forwards.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_posicion AS
SELECT
    periodo_informacion                                    AS periodo,
    rut_compania,
    folio_operacion,
    ANY_VALUE(producto)                                    AS producto,
    ANY_VALUE(contraparte_key)                             AS contraparte_key,
    ANY_VALUE(contraparte_grupo)                           AS contraparte_grupo,
    ANY_VALUE(contraparte_nombre)                          AS contraparte_nombre,
    ANY_VALUE(moneda)                                      AS moneda,
    MAX(fecha_vencimiento)                                 AS fecha_vencimiento,
    SUM(COALESCE(nocional_m, 0))                           AS nocional_m,
    SUM(COALESCE(mtm_activo_m, 0) - COALESCE(mtm_pasivo_m, 0)) AS mtm_neto_m,
    COUNT(*)                                               AS items
FROM fact_derivado
WHERE folio_operacion IS NOT NULL AND folio_operacion <> ''
GROUP BY 1, 2, 3;
"""

# El diff. Se parametriza por los dos periodos y la tolerancia de nocional.
SQL_FLUJO = """
WITH t AS (
    SELECT * FROM v_posicion WHERE periodo = {t}
),
tm AS (
    SELECT * FROM v_posicion WHERE periodo = {tm}
),
cierre AS (
    SELECT LAST_DAY(STRPTIME('{t}' || '01', '%Y%m%d')) AS fecha_cierre_t
),

-- Presentes en los dos periodos.
ambos AS (
    SELECT t.*, tm.nocional_m AS nocional_prev_m, tm.mtm_neto_m AS mtm_prev_m
    FROM t JOIN tm USING (rut_compania, folio_operacion)
),
-- Aparecen en T.
nacen AS (
    SELECT t.* FROM t
    LEFT JOIN tm USING (rut_compania, folio_operacion)
    WHERE tm.folio_operacion IS NULL
),
-- Estaban en T-1 y ya no estan.
mueren AS (
    SELECT tm.* FROM tm
    LEFT JOIN t USING (rut_compania, folio_operacion)
    WHERE t.folio_operacion IS NULL
),

-- --- heuristica de ROLL ----------------------------------------------------
-- Candidatos: misma compania, misma contraparte, mismo producto y nocional
-- dentro de la tolerancia. Se exige que el emparejamiento sea el mejor por
-- LOS DOS lados (rk_muere = 1 y rk_nace = 1); asi una que muere no se lleva
-- tres que nacen, y el resultado no depende del orden de las filas.
candidatos AS (
    SELECT
        d.rut_compania,
        d.folio_operacion            AS folio_muere,
        n.folio_operacion            AS folio_nace,
        d.nocional_m                 AS nocional_muere_m,
        n.nocional_m                 AS nocional_nace_m,
        d.contraparte_key,
        d.producto,
        CASE WHEN ABS(d.nocional_m) > 0
             THEN ABS(n.nocional_m - d.nocional_m) / ABS(d.nocional_m) END AS dif_rel,
        ROW_NUMBER() OVER (PARTITION BY d.rut_compania, d.folio_operacion
                           ORDER BY ABS(n.nocional_m - d.nocional_m), n.folio_operacion) AS rk_muere,
        ROW_NUMBER() OVER (PARTITION BY d.rut_compania, n.folio_operacion
                           ORDER BY ABS(n.nocional_m - d.nocional_m), d.folio_operacion) AS rk_nace
    FROM mueren d
    JOIN nacen n
      ON  n.rut_compania     = d.rut_compania
      AND n.contraparte_key  = d.contraparte_key
      AND n.producto         = d.producto
      AND ABS(n.nocional_m - d.nocional_m) <= ABS(d.nocional_m) * {tol}
      AND ABS(d.nocional_m) > 0
),
rolls AS (
    SELECT * FROM candidatos WHERE rk_muere = 1 AND rk_nace = 1
),

-- --- clasificacion ---------------------------------------------------------
clasificado AS (
    SELECT {t} AS periodo, 'ACTIVE' AS categoria, a.rut_compania, a.folio_operacion,
           a.producto, a.contraparte_key, a.contraparte_grupo, a.contraparte_nombre,
           a.moneda, a.fecha_vencimiento, a.nocional_m, a.nocional_prev_m,
           a.mtm_neto_m, NULL AS folio_par, NULL::DOUBLE AS dif_rel_nocional
    FROM ambos a

    UNION ALL
    SELECT {t}, CASE WHEN r.folio_nace IS NOT NULL THEN 'ROLL' ELSE 'NEW' END,
           n.rut_compania, n.folio_operacion, n.producto, n.contraparte_key,
           n.contraparte_grupo, n.contraparte_nombre, n.moneda, n.fecha_vencimiento,
           n.nocional_m, NULL, n.mtm_neto_m, r.folio_muere, r.dif_rel
    FROM nacen n
    LEFT JOIN rolls r
      ON r.rut_compania = n.rut_compania AND r.folio_nace = n.folio_operacion

    UNION ALL
    SELECT {t},
           CASE
               WHEN r.folio_muere IS NOT NULL THEN 'ROLL'
               -- Sin fecha de vencimiento no se puede probar que vencio, asi
               -- que se trata como cierre anticipado y queda contado aparte.
               WHEN d.fecha_vencimiento IS NULL THEN 'UNWIND'
               WHEN d.fecha_vencimiento > (SELECT fecha_cierre_t FROM cierre) THEN 'UNWIND'
               ELSE 'MATURITY'
           END,
           d.rut_compania, d.folio_operacion, d.producto, d.contraparte_key,
           d.contraparte_grupo, d.contraparte_nombre, d.moneda, d.fecha_vencimiento,
           NULL, d.nocional_m, d.mtm_neto_m, r.folio_nace, r.dif_rel
    FROM mueren d
    LEFT JOIN rolls r
      ON r.rut_compania = d.rut_compania AND r.folio_muere = d.folio_operacion
)
SELECT * FROM clasificado;
"""

SQL_TABLA = """
CREATE TABLE IF NOT EXISTS fact_flujo (
    periodo             INTEGER,
    categoria           VARCHAR,     -- ACTIVE | NEW | UNWIND | MATURITY | ROLL
    rut_compania        BIGINT,
    folio_operacion     VARCHAR,
    producto            VARCHAR,     -- OPCION | FORWARD | FUTURO | SWAP | PACTO
    contraparte_key     VARCHAR,
    contraparte_grupo   VARCHAR,
    contraparte_nombre  VARCHAR,
    moneda              VARCHAR,
    fecha_vencimiento   DATE,
    nocional_m          DOUBLE,      -- nocional en T   (NULL si la posicion murio)
    nocional_prev_m     DOUBLE,      -- nocional en T-1 (NULL si la posicion nacio)
    mtm_neto_m          DOUBLE,
    folio_par           VARCHAR,     -- con quien se emparejo, si es ROLL
    dif_rel_nocional    DOUBLE       -- que tan parecido era el nocional
);
"""


# ---------------------------------------------------------------------------
#  motor
# ---------------------------------------------------------------------------

class FlowEngine:
    """Calcula el diff mes contra mes sobre el warehouse."""

    def __init__(self, db: Path = DEFAULT_DB, *, tol: float = TOL_NOCIONAL) -> None:
        import duckdb
        if not Path(db).exists():
            raise SystemExit(f"No existe el warehouse en {db}. "
                             f"Corre primero: python -m warehouse.loader --data <zips>")
        self.con = duckdb.connect(str(db))
        self.tol = tol
        self.con.execute(SQL_POSICIONES)
        self.con.execute(SQL_TABLA)

    def periodos(self) -> list[int]:
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT periodo FROM v_posicion ORDER BY periodo").fetchall()]

    @staticmethod
    def anterior(periodo: int) -> int:
        a, m = divmod(int(periodo), 100)
        return (a - 1) * 100 + 12 if m == 1 else a * 100 + (m - 1)

    def sql(self, t: int) -> str:
        return SQL_FLUJO.format(t=t, tm=self.anterior(t), tol=self.tol)

    def calcular(self, t: int, *, materializar: bool = True):
        """Clasifica todas las posiciones de T-1 union T."""
        tm = self.anterior(t)
        disponibles = set(self.periodos())
        if t not in disponibles:
            raise SystemExit(f"El periodo {t} no esta en el warehouse. Hay: {sorted(disponibles)}")
        if tm not in disponibles:
            raise SystemExit(f"Falta el periodo anterior {tm}: sin T-1 no hay diff.")
        q = self.sql(t)
        if materializar:
            self.con.execute(f"DELETE FROM fact_flujo WHERE periodo = {t}")
            self.con.execute(f"INSERT INTO fact_flujo {q}")
            return self.con.execute(
                f"SELECT * FROM fact_flujo WHERE periodo = {t}").fetch_df()
        return self.con.execute(q).fetch_df()

    def resumen(self, t: int):
        return self.con.execute(f"""
            SELECT categoria,
                   COUNT(*)                                   AS posiciones,
                   ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct,
                   ROUND(SUM(COALESCE(nocional_m, nocional_prev_m)) / 1e6, 1) AS nocional_mm_m,
                   COUNT(DISTINCT contraparte_grupo)          AS grupos
            FROM fact_flujo WHERE periodo = {t}
            GROUP BY categoria ORDER BY posiciones DESC
        """).fetch_df()

    def por_producto(self, t: int):
        return self.con.execute(f"""
            SELECT producto, categoria, COUNT(*) AS n
            FROM fact_flujo WHERE periodo = {t}
            GROUP BY 1, 2
            ORDER BY producto, n DESC
        """).fetch_df()

    def cobertura_informantes(self, t: int) -> tuple[int, int, list[int]]:
        """Companias que informaron en T-1 y no en T.

        La CMF publica por tramos: 202608 existe en versiones de 96, 209 y 240
        archivos sobre 396. Si media plaza todavia no informo, sus posiciones
        del mes anterior "desaparecen" y el diff las lee como cierres. No es un
        error del motor -- es que la pregunta no se puede responder todavia --
        pero hay que gritarlo, porque un 83% de UNWIND se ve igual de creible
        que un 0,5% si nadie avisa.
        """
        tm = self.anterior(t)
        en_t = {r[0] for r in self.con.execute(
            f"SELECT DISTINCT rut_compania FROM v_posicion WHERE periodo = {t}").fetchall()}
        en_tm = {r[0] for r in self.con.execute(
            f"SELECT DISTINCT rut_compania FROM v_posicion WHERE periodo = {tm}").fetchall()}
        return len(en_t), len(en_tm), sorted(en_tm - en_t)

    def verificar(self, t: int) -> tuple[bool, str]:
        """Las 5 categorias tienen que cubrir el universo, sin solapes.

        Es la prueba que importa: si la suma de las categorias no es igual al
        tamano de ``T-1 union T``, la clasificacion perdio o duplico folios y
        cualquier lectura de la mesa sale mal.
        """
        tm = self.anterior(t)
        universo = self.con.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT rut_compania, folio_operacion FROM v_posicion WHERE periodo = {t}
                UNION
                SELECT rut_compania, folio_operacion FROM v_posicion WHERE periodo = {tm}
            )""").fetchone()[0]
        clasificadas, distintas, cats = self.con.execute(f"""
            SELECT COUNT(*), COUNT(DISTINCT (rut_compania, folio_operacion)),
                   COUNT(DISTINCT categoria)
            FROM fact_flujo WHERE periodo = {t}""").fetchone()
        ok = universo == clasificadas == distintas
        return ok, (f"universo(T-1 U T)={universo:,}  clasificadas={clasificadas:,}  "
                    f"folios distintos={distintas:,}  categorias={cats}")


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Diff de cartera mes contra mes.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--periodo", type=int, help="Periodo T (AAAAMM).")
    p.add_argument("--todos", action="store_true", help="Todos los periodos con T-1 disponible.")
    p.add_argument("--tol", type=float, default=TOL_NOCIONAL,
                   help=f"Tolerancia de nocional para ROLL (default {TOL_NOCIONAL}).")
    p.add_argument("--sql", action="store_true", help="Imprime el SQL y termina.")
    p.add_argument("--detalle", type=int, default=0, help="Muestra N filas de ejemplo.")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")

    if a.sql:
        print(SQL_POSICIONES)
        print(SQL_TABLA)
        print(SQL_FLUJO.format(t=a.periodo or "{t}", tm=FlowEngine.anterior(a.periodo)
                               if a.periodo else "{t-1}", tol=a.tol))
        return 0

    eng = FlowEngine(a.db, tol=a.tol)
    disponibles = eng.periodos()
    objetivos = ([t for t in disponibles if eng.anterior(t) in set(disponibles)]
                 if a.todos else [a.periodo] if a.periodo else [])
    if not objetivos:
        print(f"Indica --periodo o --todos. Periodos en el warehouse: {disponibles}")
        return 2

    for t in objetivos:
        eng.calcular(t)
        ok, detalle = eng.verificar(t)
        print("=" * 74)
        print(f"FLUJOS {eng.anterior(t)} -> {t}      (tolerancia ROLL {a.tol:.0%})")
        print("=" * 74)
        print(eng.resumen(t).to_string(index=False))
        print(f"\n  cobertura: {detalle}   {'OK' if ok else '!! NO CIERRA'}")

        n_t, n_tm, faltan = eng.cobertura_informantes(t)
        if faltan:
            print(f"\n  !! AVISO: {len(faltan)} compania(s) informaron en {eng.anterior(t)} "
                  f"y no en {t} ({n_t} vs {n_tm} informantes).")
            print(f"     Sus posiciones aparecen como cerradas sin que nadie las haya cerrado.")
            print(f"     RUT sin informar: {faltan[:10]}{' ...' if len(faltan) > 10 else ''}")
            if len(faltan) > n_tm * 0.1:
                print(f"     La lectura de UNWIND/MATURITY de este periodo NO es utilizable "
                      f"hasta que la CMF complete la publicacion.")
        if a.detalle:
            print("\n  ejemplos:")
            df = eng.con.execute(f"""
                SELECT categoria, producto, contraparte_grupo, folio_operacion,
                       folio_par, ROUND(dif_rel_nocional, 4) AS dif,
                       ROUND(COALESCE(nocional_m, nocional_prev_m)/1e3, 1) AS noc_mm
                FROM fact_flujo WHERE periodo = {t}
                ORDER BY categoria, COALESCE(nocional_m, nocional_prev_m) DESC
                LIMIT {a.detalle}""").fetch_df()
            print(df.to_string(index=False))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
