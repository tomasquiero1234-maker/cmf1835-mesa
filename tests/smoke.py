"""Prueba de humo end-to-end contra el ZIP real 202608v."""
import logging, sys, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")

from parse.engine import FixedWidthEngine
from validate.arithmetic import ArithmeticValidator, summarise
from normalize.entities import EntityResolver, validate_rut

ROOT = Path(__file__).resolve().parents[1]
eng = FixedWidthEngine.from_yaml(ROOT / "config/layouts/cmf_v2024.yaml")
val = ArithmeticValidator()
res = EntityResolver.from_yaml(ROOT / "config/entities.yaml")

print("=" * 72)
print("1. COBERTURA DEL LAYOUT")
print("=" * 72)
for r in eng.coverage_report():
    if r["campos"]:
        print(f"  {r['archivo']}/{r['record_type']} {r['anexo']:<5} {r['nombre']:<15} "
              f"{r['campos']:3d} campos  {r['procedencias']}")

if len(sys.argv) > 1:
    zpath = Path(sys.argv[1])
else:
    _cands = sorted((ROOT.parent / "Archivos completos ZIP CMF").glob("202608v*.zip"))
    if not _cands:
        sys.exit("No encuentro un ZIP 202608v. Pasalo como argumento: python tests/smoke.py <ruta.zip>")
    # El mas grande = la republicacion mas completa de ese periodo.
    zpath = max(_cands, key=lambda q: q.stat().st_size)
print(f"ZIP de prueba: {zpath}")
print()
print("=" * 72)
print("2. INGESTA EN STREAMING DESDE EL ZIP")
print("=" * 72)
verdicts, rows, resolutions, quarantine = [], 0, [], []
with zipfile.ZipFile(zpath) as z:
    for info in sorted(z.infolist(), key=lambda i: i.filename):
        try:
            spec, rut, per = eng.describe(info.filename)
        except Exception as e:
            print(f"  SKIP {info.filename}: {e}"); continue
        if not any(t.mapped for t in spec.record_types.values()):
            continue
        data = z.read(info)
        n = 0
        for rec in eng.parse_file(info.filename, data=data):
            rows += 1; n += 1
            if rec.record_type in ("2", "3") and rec.letter in ("I", "P"):
                v = val.validate(rec.letter, rec.record_type, rec.fields,
                                 untrusted=rec.untrusted, periodo=rec.periodo)
                verdicts.append(v)
                if v.quarantined and len(quarantine) < 3:
                    quarantine.append((info.filename, rec.line_no, v))
            if rec.letter == "P" and rec.record_type == "3":
                resolutions.append(res.resolve(
                    rut=rec.fields.get("RUT_CONTRAPARTE_NACIONAL"),
                    dv=rec.fields.get("DV_CONTRAPARTE_NACIONAL"),
                    name=rec.fields.get("NOMBRE")))
        print(f"  {info.filename:<26} {spec.anexo:<5} {n:5d} registros")

print()
print("=" * 72)
print(f"3. VALIDACION ARITMETICA  ({rows} registros leidos)")
print("=" * 72)
print("  ", summarise(verdicts))
for fn, ln, v in quarantine:
    print(f"   cuarentena {fn}:{ln} -> {v.reasons[0][:96]}")

print()
print("=" * 72)
print("4. RESOLUCION DE CONTRAPARTES (derivados)")
print("=" * 72)
print("  ", res.coverage(resolutions))
for r in resolutions:
    print(f"   {r.display:<22} -> {r.key:<12} via {r.method} ({r.confidence:.2f})  grupo={r.to_row()['contraparte_grupo']}")

print()
print("=" * 72)
print("5. RUT modulo 11 sobre emisores de renta fija")
print("=" * 72)
ok = bad = 0
with zipfile.ZipFile(zpath) as z:
    for info in z.infolist():
        if not info.filename.lower().startswith("i"): continue
        for rec in eng.parse_file(info.filename, data=z.read(info)):
            if rec.record_type != "2": continue
            if validate_rut(rec.fields.get("NRO_RUT"), rec.fields.get("DIG_RUT")): ok += 1
            else: bad += 1
print(f"   validos={ok}  invalidos={bad}  -> {100*ok/(ok+bad):.2f}% cuadra modulo 11")
