# descargadepdf

Respaldo automático de materiales de Moodle/Aula USM a partir de un HTML exportado del curso, ejecutado en GitHub Actions usando cookies de sesión en un secret.

## Qué hace
- Lee un HTML exportado desde Moodle.
- Extrae y clasifica enlaces (`resource/view`, `folder/view`, `url/view`, `pluginfile`, PDFs directos, etc.).
- Resuelve redirecciones con `requests.Session` usando `MOODLE_COOKIES`.
- Descarga PDFs reales (directos o encontrados dentro de páginas intermedias).
- Evita duplicados por URL final, hash SHA-256 y nombre+tamaño.
- Genera trazabilidad en CSV/JSON.
- Empaqueta el resultado en ZIP como artifact del workflow.

## Estructura del repositorio
- `scripts/download_moodle_pdfs.py`: script principal CLI.
- `.github/workflows/main.yml`: workflow manual (`workflow_dispatch`).
- `requirements.txt`: dependencias Python.
- `output/` (generado en ejecución):
  - `pdfs/`
  - `logs/download_log.csv`
  - `logs/failed_urls.csv`
  - `logs/download_summary.json`

## Secret requerido
Crear secret del repositorio:
- **Nombre:** `MOODLE_COOKIES`
- **Valor:** cookies válidas de sesión Moodle en formato Netscape `cookies.txt` o formato `name=value; name2=value2`.

> No publiques tus cookies en commits ni en issues.

## Cómo usar en GitHub Actions
1. Sube al repo el HTML exportado del curso (por ejemplo: `html/curso_exportado.html`).
2. Ve a **Actions** → **Respaldo Moodle PDFs** → **Run workflow**.
3. Completa inputs:
   - `html_file`: ruta del HTML dentro del repo.
   - `output_dir`: carpeta de salida (ej. `output`).
   - `delay`: segundos entre requests (ej. `0.4`).
   - `only_pdf`: `true` o `false`.
4. Ejecuta.
5. Descarga artifacts al finalizar:
   - `respaldo-moodle-zip`
   - `respaldo-moodle-output`

## Uso local (opcional)
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export MOODLE_COOKIES="(cookies)"
python scripts/download_moodle_pdfs.py \
  --html html/curso_exportado.html \
  --out output \
  --delay 0.4 \
  --max-workers 6
```

Argumentos disponibles:
- `--html` (requerido)
- `--out` (requerido)
- `--max-workers` (opcional)
- `--delay` (opcional)
- `--only-pdf` (opcional)
- `--base-url` (opcional)
- `--verbose` (opcional)

## Limitaciones importantes
- GitHub Actions **no puede leer** cookies locales del navegador del usuario.
- La autenticación depende totalmente de `MOODLE_COOKIES` válidas.
- Si la sesión expira, la descarga puede fallar.
- Algunos recursos externos pueden no ser descargables.
- No todo enlace de Moodle corresponde a un PDF.
