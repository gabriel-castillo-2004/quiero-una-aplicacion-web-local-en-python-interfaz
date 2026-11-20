# Aplicación web local en Python (Flask + Waitress en 127.0.0.1) con interfaz HTML/JS vanilla minimalista para descargar canciones y playlists de YouTube usando yt-dlp y ffmpeg. Reconcilia la simplicidad de un monolito local sin base de datos ni bundler con una arquitectura ligera de puertos y adaptadores, cola serializada por job_id, staging atómico, SSE con snapshot/heartbeats y endurecimiento local (CSRF y validación Host/Origin).

## Objective
Aplicación web local en Python (Flask + Waitress en 127.0.0.1) con interfaz HTML/JS vanilla minimalista para descargar canciones y playlists de YouTube usando yt-dlp y ffmpeg. Reconcilia la simplicidad de un monolito local sin base de datos ni bundler con una arquitectura ligera de puertos y adaptadores, cola serializada por job_id, staging atómico, SSE con snapshot/heartbeats y endurecimiento local (CSRF y validación Host/Origin).

## Rationale
Se adopta el enfoque de proceso único y UI vanilla sin Node.js ni Redis del Pragmatist, pero incorporando las salvaguardas críticas del Structuralist: (1) puertos explícitos para yt-dlp, sistema de archivos y bus de eventos que permiten pruebas deterministas sin red en pytest; (2) JobManager con cola FIFO serializada para evitar condiciones de carrera sobre download_archive y permitir cancelación cooperativa; (3) descarga en directorio temporal de staging con promoción atómica y limpieza de residuos (.part, .webp); (4) pipeline explícito de postprocesadores ffmpeg (extracción de audio, conversión de miniatura a JPEG, metadatos y carátula) con preflight al arranque; y (5) servidor WSGI Waitress en 127.0.0.1 con validación de Origin/Host, token CSRF y SSE resiliente con snapshot inicial y respaldo por polling.

## Deployment Target
local
