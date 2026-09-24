"""python -m reportes [--periodo AAAAMM] [--salida ruta.xlsx]"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reportes.exportar import generar  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Exporta el stock de inversiones a Excel.")
    ap.add_argument("--periodo", type=int, help="AAAAMM; por defecto el mas reciente")
    ap.add_argument("--salida", type=Path, help="ruta del .xlsx")
    a = ap.parse_args(argv)
    t = time.time()
    ruta = generar(a.periodo, a.salida)
    print(f"Excel generado en {time.time() - t:.1f}s: {ruta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
