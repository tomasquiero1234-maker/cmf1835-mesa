"""python -m reportes [--periodo AAAAMM] [--salida ruta.xlsx] [--libro-bbva ruta.xlsx]"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reportes.conciliacion import LIBRO_BBVA  # noqa: E402
from reportes.exportar import generar  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Exporta el stock de inversiones a Excel.")
    ap.add_argument("--periodo", type=int, help="AAAAMM; por defecto el mas reciente")
    ap.add_argument("--salida", type=Path, help="ruta del .xlsx")
    ap.add_argument("--libro-bbva", type=Path, default=LIBRO_BBVA,
                    help="Excel con el libro de BBVA con Confuturo, para la conciliacion")
    a = ap.parse_args(argv)
    t = time.time()
    ruta = generar(a.periodo, a.salida, a.libro_bbva)
    print(f"Excel generado en {time.time() - t:.1f}s: {ruta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
