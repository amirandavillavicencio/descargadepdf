# descargadepdf (modo local)

Este proyecto está preparado para probar **localmente** (Windows o Linux) la descarga de PDFs desde Moodle/Aula USM usando únicamente cookies frescas exportadas de una sesión iniciada.

## Requisitos

- Python 3.10+
- Archivo de cookies Netscape actualizado (por ejemplo `aula.usm.cl_cookies.txt`)
- Dependencias:

```bash
pip install -r requirements.txt
```

## Archivos clave

- `scripts/download_moodle_pdfs.py`: script principal.
- `scripts/run_local_example.sh`: ejemplo de ejecución local.
- `aula.usm.cl_cookies.txt`: ejemplo/plantilla de cookies Netscape.

## Uso local (Linux/macOS)

```bash
python scripts/download_moodle_pdfs.py \
  --url "https://aula.usm.cl/course/view.php?id=4401&section=23#tabs-tree-start" \
  --cookies "aula.usm.cl_cookies.txt" \
  --out "output"
```

También puedes usar:

```bash
bash scripts/run_local_example.sh
```

## Uso local (Windows PowerShell)

```powershell
python .\scripts\download_moodle_pdfs.py --url "https://aula.usm.cl/course/view.php?id=4401&section=23#tabs-tree-start" --cookies "aula.usm.cl_cookies.txt" --out "output"
```



## Modo INTENSIVOS CIAC (descarga por carpeta)

Para priorizar el botón **"Descargar carpeta"** en carpetas de años 2020-2024 dentro de sub-solapas de INTENSIVOS CIAC, usa:

```bash
python scripts/download_moodle_pdfs.py \
  --url "https://aula.usm.cl/course/view.php?id=4401&section=23#tabs-tree-start" \
  --cookies "aula.usm.cl_cookies.txt" \
  --out "output_intensivos" \
  --intensivos-ciac
```

Cuando una carpeta no expone el enlace de descarga ZIP, el script cae automáticamente a descarga manual de PDFs desde esa carpeta.

## Qué valida el script

1. Carga cookies en formato Netscape desde `--cookies`.
2. Abre la URL autenticada (`--url`) y detecta claramente si ocurrió:
   - redirección a `/login/index.php`
   - redirección a `/auth/oauth2/login.php`
   - HTML de login en lugar del curso
3. Procesa enlaces tipo:
   - `mod/resource/view.php`
   - `mod/folder/view.php`
   - `mod/url/view.php`
   - `pluginfile.php`
4. Si recibe HTML, busca dentro enlaces PDF reales antes de marcar error.
5. Evita duplicados por hash SHA-256.

## Logs y salida

Con `--out output`, se generan:

- `output/pdfs/` → PDFs descargados
- `output/logs/download_log.csv`
- `output/logs/failed_urls.csv`
- `output/logs/summary.json`

Además, por cada enlace procesado se imprime en consola:

- `url original`
- `tipo detectado`
- `http_status`
- `final_url`
- `content_type`
- `reason`

## Notas importantes

- No usa Selenium.
- No usa navegador automatizado.
- No usa login Microsoft automático.
- No usa usuario/contraseña.
- Depende 100% de cookies frescas válidas.
