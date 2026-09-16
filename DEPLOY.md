# Despliegue en Streamlit Community Cloud

## Que se sube y que no

| | Se versiona | Por que |
|---|---|---|
| Codigo (`app/`, `warehouse/`, `analytics/`, `utils/`, `parse/`, `validate/`, `normalize/`) | si | |
| `config/` (layouts, entidades, serie UF) | si | Son la fuente de verdad del motor, pesan poco |
| `warehouse/sample/cmf1835_sample.duckdb` | si | ~44 MB, archivo unico y portable: es lo que lee la nube |
| `warehouse/data/` (124 MB, 147 Parquet) | **no** | Dato derivado; se regenera con el loader en ~13 min |
| Los ZIP de la CMF (~450 MB) | **no** | Publicos y pesados |

La app elige base sola: usa `warehouse/data/` si existe (local), si no cae a
`warehouse/sample/`. La variable `CMF1835_DB` manda sobre las dos.

## Subir a GitHub

```bash
cd "/Users/tomasquiero/Claude/Copia de cmf1835"
git add .
git commit -m "Dashboard Release"

# Crear el repo y empujar (requiere gh instalado y autenticado)
gh repo create cmf1835-mesa --private --source=. --remote=origin --push

# Sin gh: crear el repo vacio en github.com/new y despues
#   git remote add origin https://github.com/<usuario>/cmf1835-mesa.git
#   git branch -M main
#   git push -u origin main
```

## Conectar a Streamlit Community Cloud

1. Entrar a <https://share.streamlit.io> con la misma cuenta de GitHub.
2. **Create app** -> **Deploy a public app from a repo**.
3. Repository: `<usuario>/cmf1835-mesa` · Branch: `main` ·
   Main file path: `app/dashboard.py`.
4. **Advanced settings** -> Python version **3.11** o superior.
5. **Deploy**. El primer build tarda unos minutos.

La URL queda como `https://<algo>.streamlit.app`.

## Acceso para la mesa

Un repo **privado** en Streamlit Community Cloud despliega una app cuyo acceso
se controla por lista de correos: en la app, **Settings -> Sharing**, agregar
las cuentas de la mesa. Si el repo es publico, la app es publica para
cualquiera que tenga el link.

Esto importa: la cartera de inversiones de las aseguradoras es informacion
publica de la CMF, pero el cruce competitivo que arma este dashboard -- quien
opera con quien, a que precio, y donde no estamos -- es analisis propio. No lo
publiques abierto sin decidirlo a proposito.

## Actualizar los datos

La muestra es un archivo versionado; refrescarla es regenerarla y empujar:

```bash
python -m warehouse.loader --data "<carpeta de ZIP>"   # warehouse completo
python -m analytics.flows --todos                       # clasificacion de flujos
python -m utils.create_sample_db --meses 6              # muestra de despliegue
git add warehouse/sample && git commit -m "Datos a <periodo>" && git push
```

Streamlit Cloud redespliega solo con cada push a `main`.
