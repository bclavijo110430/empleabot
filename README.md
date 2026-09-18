# Bot de postulación automatizada (LinkedIn Easy Apply + FreeLLMAPI)

Bot en Python que busca ofertas en LinkedIn, analiza cada vacante con un LLM
local (servido por **FreeLLMAPI** u **Ollama**), selecciona el CV en PDF más
adecuado, rellena el formulario de Easy Apply y registra cada postulación en
SQLite y JSON.

## Requisitos

- Python 3.10+
- Un proveedor de inferencia local (elige uno con `LLM_PROVIDER`):
  - [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) escuchando en
    `http://localhost:3001` con claves de proveedor configuradas, o
  - [Ollama](https://ollama.com/) escuchando en `http://localhost:11434` con un
    modelo descargado (`ollama pull qwen2.5:7b`).
- Chromium de Playwright.

## Instalación

### Automática en Linux/macOS (recomendada)

```bash
chmod +x install.sh
./install.sh
```

El script crea el entorno virtual `.venv`, instala las dependencias de
`requirements.txt`, descarga Chromium de Playwright, genera el `.env` y
prepara las carpetas `data/` y `logs/`.

Opciones:

```bash
./install.sh --with-deps   # instala dependencias del sistema de Playwright (sudo)
./install.sh --force       # recrea el venv desde cero
```

### Automática en Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Opciones:

```powershell
.\install.ps1 -Force        # recrea el venv desde cero
.\install.ps1 -Python py    # usa un interprete concreto
```

Si PowerShell bloquea el script por la política de ejecución, usa la forma
`powershell -ExecutionPolicy Bypass -File .\install.ps1`.

### Manual

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
cp .env.example .env               # Windows: Copy-Item .env.example .env
```

Edita `.env` y coloca tu token unificado de FreeLLMAPI en `FREELM_API_KEY`.
El modelo por defecto es `auto`, que deja que el router elija el mejor
proveedor disponible.

## Proveedor de inferencia

`LLM_PROVIDER` escoge el backend local: `freellmapi` (por defecto) u `ollama`.
Ambos exponen una API compatible con OpenAI, así que no hay dependencias extra.

### FreeLLMAPI

```env
LLM_PROVIDER=freellmapi
FREELM_BASE_URL=http://localhost:3001/v1
FREELM_API_KEY=freellmapi-tu-token-unificado
FREELM_MODEL=auto
```

### Ollama

Instala [Ollama](https://ollama.com/), descarga un modelo y apunta el bot a su
endpoint OpenAI-compatible:

```bash
ollama pull qwen2.5:7b
```

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TIMEOUT=120
```

La `OLLAMA_API_KEY` se ignora, pero el cliente la exige (por defecto `ollama`).
Usa modelos con buen soporte de JSON/instrucciones (`qwen2.5`, `llama3.1`,
`mistral`).

## Configuración

### Perfil — `data/profile.json`
Rellena tus datos. Se usa como contexto para la IA y para los campos comunes.
Puedes generarlo automáticamente desde tus CVs (ver *Análisis de CVs*).
`data/profile.json` no se versiona: usa `data/profile.example.json` como
referencia.

### CVs — `data/cv/*.pdf` + `data/cvs.json`
Coloca tus PDFs en `data/cv/` y describe cada uno en `data/cvs.json`
(`file`, `title`, `summary`, `skills`, `seniority`). El bot pedirá al LLM que
elija el mejor encaje según la oferta. `data/cvs.json` tampoco se versiona:
usa `data/cvs.example.json` como referencia.

## Análisis de CVs

Genera automáticamente `data/cvs.json` y `data/profile.json` a partir de los
PDFs en `data/cv/`:

```bash
python main.py --analyze-cvs
```

El comando extrae el texto de cada PDF, pide al LLM los metadatos de cada CV y
un perfil consolidado del candidato, y **sobrescribe** ambos JSON. Requiere el
proveedor de inferencia activo (FreeLLMAPI u Ollama). La expectativa salarial y
las respuestas por defecto se toman de `.env` / los valores predefinidos.

> `data/cvs.json` y `data/profile.json` están en `.gitignore` porque contienen
> datos personales; solo se versionan las plantillas `.example.json`.

## Uso

```bash
# Ejecución normal (navegador visible la primera vez para iniciar sesión)
python main.py

# Buscar otro puesto/ubicación y limitar postulaciones
python main.py --query "Backend Engineer" --location "Remoto" --limit 10

# Análisis sin enviar (elige CV y muestra decisiones)
python main.py --dry-run

# Sin ventana
python main.py --headless
```

La primera ejecución abre LinkedIn para que inicies sesión manualmente; al
cerrar se guarda la sesión en `data/sessions/linkedin_storage_state.json` y no
volverá a pedirla.

## Autenticación (3 vías)

El bot intenta autenticarse en este orden: cookies importadas → sesión guardada
→ credenciales → login manual.

### Vía A — Login manual (recomendado, sin configurar nada)
Deja `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD` y `LINKEDIN_COOKIES_FILE` vacías.
Al ejecutar se abrirá el navegador; inicia sesión tú mismo (soporta 2FA/captcha)
y el bot guardará la sesión automáticamente.

### Vía B — Credenciales en `.env`
```env
LINKEDIN_EMAIL=tu.email@example.com
LINKEDIN_PASSWORD=tu-contraseña
```
Útil para ejecuciones desatendidas. Si LinkedIn pide verificación en dos pasos
o captcha, el login automático fallará y se pedirá login manual.

### Vía C — Cookies exportadas
1. Instala en tu navegador una extensión como **Cookie-Editor**.
2. Inicia sesión en LinkedIn y exporta las cookies como JSON.
3. Guarda el archivo en `data/sessions/cookies.json` (o la ruta que prefieras)
   y configúralo:
```env
LINKEDIN_COOKIES_FILE=data/sessions/cookies.json
```

La sesión más fiable se obtiene con la Vía A; las Vías B y C son cómodas para
automatizar, pero dependen de que LinkedIn no exija verificación adicional.

## Estructura

```
├── main.py                 # Orquestador + CLI
├── config.py               # Configuración y rutas (Pydantic)
├── install.sh              # Instalador de prerequisitos (Linux/macOS)
├── install.ps1             # Instalador de prerequisitos (Windows)
├── src/
│   ├── ai_handler.py       # Cliente LLM local (FreeLLMAPI u Ollama)
│   ├── cv_selector.py      # Selección de CV
│   ├── form_filler.py      # Llenado del formulario Easy Apply
│   ├── job_parser.py       # Extracción de ofertas
│   ├── session_manager.py  # Sesión persistente + navegador
│   ├── storage.py          # SQLite + export JSON
│   ├── utils.py            # Delays, screenshots, logging
│   └── portals/linkedin.py # Adaptador de LinkedIn
├── data/                   # Perfil, CVs y sesiones (no versionado)
└── logs/                   # applications.db, export JSON y capturas
```

## Notas de seguridad y buenas prácticas

- Retardos aleatorios entre acciones y límite de postulaciones por corrida.
- Capturas de pantalla automáticas ante fallos en `logs/screenshots/`.
- Deduplicación por `job_id`: nunca repite una oferta ya postulada.
- El uso de automatización debe respetar los términos de servicio de LinkedIn.
  Úsalo bajo tu responsabilidad y con volúmenes moderados.
