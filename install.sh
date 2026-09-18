#!/usr/bin/env bash
#
# install.sh - Prepara el entorno del bot de postulación.
#
# Crea el entorno virtual, instala las dependencias de Python y descarga
# el navegador Chromium de Playwright.
#
# Uso:
#   ./install.sh                  # instalación estándar
#   ./install.sh --with-deps      # además instala dependencias del sistema (sudo)
#   ./install.sh --force          # recrea el venv desde cero
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Utilidades de salida
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { printf "${BLUE}[INFO]${NC} %s\n" "$1"; }
ok()    { printf "${GREEN}[ OK ]${NC} %s\n" "$1"; }
warn()  { printf "${YELLOW}[WARN]${NC} %s\n" "$1"; }
error() { printf "${RED}[FAIL]${NC} %s\n" "$1" >&2; }

# ---------------------------------------------------------------------------
# Ir al directorio del proyecto (donde vive este script)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
info "Directorio del proyecto: $SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Argumentos
# ---------------------------------------------------------------------------
WITH_DEPS=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --with-deps) WITH_DEPS=1 ;;
        --force)     FORCE=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 12
            exit 0
            ;;
        *) warn "Argumento desconocido: $arg" ;;
    esac
done

# ---------------------------------------------------------------------------
# Localizar Python 3.10+
# ---------------------------------------------------------------------------
find_python() {
    for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
    error "No se encontró Python 3.10 o superior. Instálalo y vuelve a intentarlo."
    exit 1
fi
ok "Python detectado: $($PYTHON_BIN --version) ($PYTHON_BIN)"

# ---------------------------------------------------------------------------
# Entorno virtual
# ---------------------------------------------------------------------------
VENV_DIR=".venv"

if [ "$FORCE" -eq 1 ] && [ -d "$VENV_DIR" ]; then
    warn "--force: eliminando venv existente..."
    rm -rf "$VENV_DIR"
fi

if [ -d "$VENV_DIR" ]; then
    info "El entorno virtual ya existe en $VENV_DIR (se reutiliza)."
else
    info "Creando entorno virtual en $VENV_DIR..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "Entorno virtual creado."
fi

VENV_PY="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PY" ]; then
    error "No se encontró el intérprete del venv ($VENV_PY)."
    exit 1
fi

# ---------------------------------------------------------------------------
# Dependencias de Python
# ---------------------------------------------------------------------------
info "Actualizando pip..."
"$VENV_PY" -m pip install --upgrade pip setuptools wheel >/dev/null

info "Instalando dependencias de requirements.txt..."
if [ ! -f "requirements.txt" ]; then
    error "No se encontró requirements.txt."
    exit 1
fi
"$VENV_PY" -m pip install -r requirements.txt
ok "Dependencias de Python instaladas."

# ---------------------------------------------------------------------------
# Navegador de Playwright
# ---------------------------------------------------------------------------
info "Descargando Chromium de Playwright (puede tardar unos minutos)..."
"$VENV_PY" -m playwright install chromium
ok "Chromium instalado para Playwright."

if [ "$WITH_DEPS" -eq 1 ]; then
    info "Instalando dependencias de sistema de Playwright..."
    if [ "$(id -u)" -eq 0 ]; then
        "$VENV_PY" -m playwright install-deps chromium
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$VENV_PY" -m playwright install-deps chromium
    else
        warn "Se requiere root/sudo para --with-deps. Omitido."
    fi
    ok "Dependencias del sistema procesadas."
else
    warn "Si Chromium falla al arrancar, ejecuta: ./install.sh --with-deps"
fi

# ---------------------------------------------------------------------------
# Archivo .env
# ---------------------------------------------------------------------------
if [ -f ".env" ]; then
    info "El archivo .env ya existe; no se sobrescribe."
elif [ -f ".env.example" ]; then
    cp .env.example .env
    ok "Creado .env a partir de .env.example."
    warn "Edita .env: elige LLM_PROVIDER (freellmapi u ollama) y tus credenciales."
else
    warn "No se encontró .env.example; crea el .env manualmente."
fi

# ---------------------------------------------------------------------------
# Carpetas de datos
# ---------------------------------------------------------------------------
mkdir -p data/cv data/sessions logs/screenshots
ok "Carpetas data/ y logs/ preparadas."

# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------
echo
printf "${GREEN}==================================================${NC}\n"
printf "${GREEN} Instalación completada${NC}\n"
printf "${GREEN}==================================================${NC}\n"
echo
echo "Próximos pasos:"
echo "  1. Activa el entorno:     source .venv/bin/activate"
echo "  2. Edita .env:            FREELM_API_KEY y (opcional) LinkedIn"
echo "  3. Coloca tus CVs en:     data/cv/*.pdf"
echo "  4. Genera perfil y CVs:   python main.py --analyze-cvs"
echo "  5. Ejecuta una prueba:    python main.py --dry-run"
echo
