# install.ps1 - Prepara el entorno del bot de postulacion en Windows.
#
# Crea el entorno virtual, instala las dependencias de Python y descarga
# el navegador Chromium de Playwright.
#
# Uso (PowerShell):
#   .\install.ps1                 # instalacion estandar
#   .\install.ps1 -Force          # recrea el venv desde cero
#   .\install.ps1 -Python py      # usa un interprete concreto
#
# Si Windows bloquea la ejecucion de scripts:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$Force,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Utilidades de salida
# ---------------------------------------------------------------------------
function Write-Info  { param([string]$Message) Write-Host "[INFO] $Message" -ForegroundColor Cyan }
function Write-Ok    { param([string]$Message) Write-Host "[ OK ] $Message" -ForegroundColor Green }
function Write-Warn  { param([string]$Message) Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Write-Err   { param([string]$Message) Write-Host "[FAIL] $Message" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# Ir al directorio del proyecto (donde vive este script)
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
Write-Info "Directorio del proyecto: $ScriptDir"

# ---------------------------------------------------------------------------
# Localizar Python 3.10+
# ---------------------------------------------------------------------------
function Test-Python {
    param([string]$Exe, [string[]]$Pre)
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $false }
    $cmdArgs = @()
    if ($Pre) { $cmdArgs += $Pre }
    $cmdArgs += @("-c", "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)")
    try {
        & $Exe @cmdArgs 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Get-PythonCommand {
    if ($Python) {
        if (Test-Python -Exe $Python -Pre @()) {
            return @{ Exe = $Python; Pre = @() }
        }
        Write-Warn "El interprete indicado (-Python $Python) no sirve o es < 3.10."
    }
    $candidates = @(
        @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"),
        @("py", "-3.10"), @("py", "-3"),
        @("python"), @("python3")
    )
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $pre = @()
        if ($candidate.Count -gt 1) { $pre = $candidate[1..($candidate.Count - 1)] }
        if (Test-Python -Exe $exe -Pre $pre) {
            return @{ Exe = $exe; Pre = $pre }
        }
    }
    return $null
}

$py = Get-PythonCommand
if ($null -eq $py) {
    Write-Err "No se encontro Python 3.10 o superior. Instalalo desde https://www.python.org/downloads/ y pulsa Reintentar."
    exit 1
}

$versionArgs = @()
if ($py.Pre) { $versionArgs += $py.Pre }
$versionArgs += @("--version")
$pyVersion = (& $py.Exe @versionArgs 2>&1) -join " "
Write-Ok "Python detectado: $pyVersion"

# ---------------------------------------------------------------------------
# Entorno virtual
# ---------------------------------------------------------------------------
$VenvDir = ".venv"

if ($Force -and (Test-Path $VenvDir)) {
    Write-Warn "-Force: eliminando venv existente..."
    Remove-Item -Recurse -Force $VenvDir
}

if (Test-Path $VenvDir) {
    Write-Info "El entorno virtual ya existe en $VenvDir (se reutiliza)."
} else {
    Write-Info "Creando entorno virtual en $VenvDir..."
    $venvArgs = @()
    if ($py.Pre) { $venvArgs += $py.Pre }
    $venvArgs += @("-m", "venv", $VenvDir)
    & $py.Exe @venvArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Fallo al crear el entorno virtual."
        exit 1
    }
    Write-Ok "Entorno virtual creado."
}

$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Err "No se encontro el interprete del venv ($VenvPy)."
    exit 1
}

# ---------------------------------------------------------------------------
# Dependencias de Python
# ---------------------------------------------------------------------------
Write-Info "Actualizando pip..."
& $VenvPy -m pip install --upgrade pip setuptools wheel | Out-Null

if (-not (Test-Path "requirements.txt")) {
    Write-Err "No se encontro requirements.txt."
    exit 1
}

Write-Info "Instalando dependencias de requirements.txt..."
& $VenvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Err "Fallo al instalar las dependencias de Python."
    exit 1
}
Write-Ok "Dependencias de Python instaladas."

# ---------------------------------------------------------------------------
# Navegador de Playwright
# ---------------------------------------------------------------------------
Write-Info "Descargando Chromium de Playwright (puede tardar unos minutos)..."
& $VenvPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Err "Fallo al descargar Chromium."
    exit 1
}
Write-Ok "Chromium instalado para Playwright."

if ($WithDeps) {
    Write-Warn "-WithDeps: 'playwright install-deps' solo existe en Linux; en Windows las dependencias ya vienen con el sistema. Omitido."
}

# ---------------------------------------------------------------------------
# Archivo .env
# ---------------------------------------------------------------------------
if (Test-Path ".env") {
    Write-Info "El archivo .env ya existe; no se sobrescribe."
} elseif (Test-Path ".env.example") {
    Copy-Item ".env.example" ".env"
    Write-Ok "Creado .env a partir de .env.example."
    Write-Warn "Edita .env: elige LLM_PROVIDER (freellmapi u ollama) y tus credenciales."
} else {
    Write-Warn "No se encontro .env.example; crea el .env manualmente."
}

# ---------------------------------------------------------------------------
# Carpetas de datos
# ---------------------------------------------------------------------------
foreach ($dir in @("data\cv", "data\sessions", "logs\screenshots")) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}
Write-Ok "Carpetas data\ y logs\ preparadas."

# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " Instalacion completada" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Proximos pasos:"
Write-Host "  1. Activa el entorno:     .\.venv\Scripts\Activate.ps1"
Write-Host "  2. Edita .env:            FREELM_API_KEY y (opcional) LinkedIn"
Write-Host "  3. Coloca tus CVs en:     data\cv\*.pdf"
Write-Host "  4. Genera perfil y CVs:   python main.py --analyze-cvs"
Write-Host "  5. Ejecuta una prueba:    python main.py --dry-run"
Write-Host ""
