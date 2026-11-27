# yt-music-dl

Aplicación web local en Python (Flask + Waitress) para descargar canciones y playlists de YouTube en formato MP3, M4A o audio original usando `yt-dlp` y `ffmpeg`.

## Requisitos previos

- **Python 3.11+**
- **ffmpeg** y **ffprobe** disponibles en el `PATH` del sistema (necesarios para extraer/convertir audio, incrustar metadatos y miniaturas).

## Instalación en un entorno virtual limpio

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Ejecución de la aplicación

Con el entorno virtual activado, inicia el servidor local (por defecto en `http://127.0.0.1:8081`):

### Windows (PowerShell)

```powershell
$env:PYTHONPATH = "src"
python -m yt_music_dl.app
```

### Linux / macOS

```bash
PYTHONPATH=src python -m yt_music_dl.app
```

Luego abre `http://127.0.0.1:8081` en tu navegador. Puedes personalizar la dirección y el puerto con las variables de entorno `HOST` (por defecto `127.0.0.1`) y `PORT` (por defecto `8081`).

## Ejecución de pruebas

Para ejecutar la suite de pruebas con `pytest`:

```bash
pip install pytest
pytest
```
