# cmf1835 — Pipeline ETL Circular N°1835 CMF

Motor de ingesta de las carteras de inversión de aseguradoras chilenas.
Layouts declarativos, validación aritmética por registro, resolución de
contrapartes por RUT.

## Instalación

```bash
cd /Users/tomasquiero/Claude/cmf1835
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

```bash
# procesa TODOS los ZIP de la carpeta, recursivamente
python main.py --data "/Users/tomasquiero/Claude/Archivos completos ZIP CMF"

# un período puntual, con más muestras
python main.py --data "../Archivos completos ZIP CMF" --periodo 202608 --sample 20

# guardando estadísticas de la corrida
python main.py --data ../data --out ./warehouse
```

## Estado de los layouts

| Archivo | Anexo | Largo | Estado |
|---|---|---|---|
| I | B.1  Renta Fija | 970 | oficial + verificado contra datos |
| P | B.7  Derivados y Pactos | 587 | oficial + verificado contra datos |
| G | B.14 Garantías | 300 | oficial, sin muestra con datos |
| A, B, F, X, O, C, D, T, E, R, V | B.2–B.13 | — | pendientes de transcribir |

Los pendientes están declarados en el YAML con el número de página del anexo
consolidado. El motor los detecta, valida el trailer de control y conserva la
línea cruda en `_raw`: la ingesta nunca se cae por un layout faltante.

## Verificación (período 202608, 8 compañías)

```
Líneas leídas         : 1.478
Registros de detalle  : 1.430
Trailer de control    : 16/16 archivos cuadrados
Validación aritmética : 1.430 pass / 0 cuarentena / 0 sin verificar
Contrapartes por RUT  : 100%
RUT módulo 11         : 1.425/1.425 válidos
```

## Dos trampas que este código ya resuelve

**Ancho en caracteres, no en bytes.** Los archivos vienen en UTF-8 y el relleno
de la CMF es por caracteres. Un registro con Ñ mide 970 caracteres pero 971
bytes. Cortar por bytes desplaza el registro completo desde ese punto.

**El signo del PICTURE.** `-9(13)` son 14 caracteres, no 13. Los tres campos
finales de Forwards llevan signo, y es exactamente lo que cuadra el registro
en 587.

## Cómo se agrega un layout nuevo

Se edita el YAML. No se toca Python. El motor valida al cargar que los campos
cubran el registro sin solapes ni desbordes, y falla al arrancar si la
transcripción tiene un error — no tres meses después mirando un nocional raro.

## Próximos módulos

- `warehouse/` — carga a Parquet particionado + DuckDB
- `analytics/flows.py` — diff mes contra mes por folio: new / unwind / roll
- `app/dashboard.py` — Streamlit
