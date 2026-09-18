"""Gestión de sesión persistente, cookies y arranque del navegador."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from config import Settings
from src.utils import logger

LINKEDIN_FEED = "https://www.linkedin.com/feed/"
LINKEDIN_LOGIN = "https://www.linkedin.com/login"

# Mapeo de valores sameSite usados por extensiones de navegador -> Playwright
_SAMESITE_MAP = {
    "no_restriction": "None",
    "none": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
}


def apply_stealth() -> Any:
    """Devuelve un callable para aplicar playwright-stealth (compatible con varias versiones)."""
    try:
        from playwright_stealth import Stealth  # type: ignore

        stealth = Stealth()

        async def _apply(page: Any) -> None:
            await stealth.apply_stealth_async(page)

        return _apply
    except Exception:  # noqa: BLE001 - intentamos la API antigua
        pass

    try:
        from playwright_stealth import stealth_async  # type: ignore

        return stealth_async
    except Exception:  # noqa: BLE001
        logger.warning("playwright-stealth no disponible; se continúa sin sigilo.")


def load_storage_state(path: Path) -> dict | None:
    """Carga un storage_state de Playwright si existe y es válido."""
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        logger.info("Sesión cargada desde %s", path)
        return state
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("No se pudo leer la sesión (%s); se ignora.", exc)
        return None


async def save_storage_state(context: Any, path: Path) -> None:
    """Persiste cookies y localStorage del contexto actual."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await context.storage_state(path=str(path))
        logger.info("Sesión guardada en %s", path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo guardar la sesión: %s", exc)


def normalize_cookies(raw: Any) -> list[dict]:
    """Convierte cookies de distintas fuentes al formato de Playwright.

    Acepta un storage_state ({"cookies": [...]}) o una lista exportada por
    extensiones como Cookie-Editor o EditThisCookie.
    """
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]
    if not isinstance(raw, list):
        return []

    cookies: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name, value = item.get("name"), item.get("value")
        if not name or value is None:
            continue

        cookie: dict[str, Any] = {"name": name, "value": str(value)}
        url = item.get("url")
        domain = item.get("domain")
        if url:
            cookie["url"] = url
        elif domain:
            cookie["domain"] = domain
            cookie["path"] = item.get("path", "/")
        else:
            continue

        expires = item.get("expires", item.get("expirationDate"))
        if expires is not None:
            try:
                cookie["expires"] = float(expires)
            except (TypeError, ValueError):
                pass
        if item.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(item["httpOnly"])
        if item.get("secure") is not None:
            cookie["secure"] = bool(item["secure"])
        same_site = item.get("sameSite")
        if same_site:
            cookie["sameSite"] = _SAMESITE_MAP.get(str(same_site).lower(), "Lax")
        cookies.append(cookie)
    return cookies


async def apply_cookies_file(context: Any, path: Path | None) -> int:
    """Inyecta en el contexto las cookies de un archivo JSON. Devuelve el total."""
    if path is None:
        return 0
    if not path.exists():
        logger.warning("Archivo de cookies no encontrado: %s", path)
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("No se pudo leer %s: %s", path, exc)
        return 0

    cookies = normalize_cookies(raw)
    if not cookies:
        logger.warning("El archivo %s no contiene cookies válidas.", path)
        return 0
    try:
        await context.add_cookies(cookies)
        logger.info("Inyectadas %d cookies desde %s", len(cookies), path)
        return len(cookies)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron inyectar las cookies: %s", exc)
        return 0


async def launch_browser(playwright: Any, settings: Settings) -> Any:
    """Lanza Chromium y crea un contexto con la sesión guardada."""
    settings.ensure_dirs()
    browser = await playwright.chromium.launch(
        headless=settings.headless,
        slow_mo=settings.slow_mo,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    state = load_storage_state(settings.session_state_path)
    context = await browser.new_context(
        storage_state=state,
        user_agent=settings.user_agent,
        viewport={"width": settings.viewport_width, "height": settings.viewport_height},
        locale="es-ES",
        timezone_id="Europe/Madrid",
    )
    context.set_default_timeout(settings.action_timeout_ms)
    return browser, context


async def ensure_authenticated(page: Any, context: Any, settings: Settings) -> bool:
    """Garantiza que hay sesión iniciada en LinkedIn.

    Orden de intentos:
    1. Cookies de `LINKEDIN_COOKIES_FILE`.
    2. `storage_state` ya cargado por `launch_browser`.
    3. Login automático con `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD`.
    4. Login manual asistido (espera hasta 10 minutos).
    """
    if settings.cookies_file_path:
        await apply_cookies_file(context, settings.cookies_file_path)

    await page.goto(LINKEDIN_FEED, wait_until="domcontentloaded")
    if await _is_logged_in(page):
        logger.info("Sesión de LinkedIn activa.")
        return True

    if settings.linkedin_email and settings.linkedin_password:
        logger.info("Intentando login automático con las credenciales de .env...")
        if await _credential_login(page, settings):
            logger.info("Login con credenciales correcto.")
            await save_storage_state(context, settings.session_state_path)
            return True
        logger.warning(
            "Login automático no completado (posible 2FA, captcha o credenciales "
            "incorrectas). Se solicitará login manual."
        )

    logger.info("Abriendo LinkedIn para login manual...")
    await page.goto(LINKEDIN_LOGIN, wait_until="domcontentloaded")
    logger.info("Inicia sesión en el navegador. Esperando hasta 10 minutos...")

    for _ in range(120):  # 120 * 5s = 10 minutos
        await asyncio.sleep(5)
        if await _is_logged_in(page):
            logger.info("Login detectado. Continuando.")
            return True

    logger.error("Tiempo de espera de login agotado.")
    return False


async def _credential_login(page: Any, settings: Settings) -> bool:
    """Rellena el formulario de login con usuario y contraseña."""
    try:
        await page.goto(LINKEDIN_LOGIN, wait_until="domcontentloaded")
        await page.fill("#username", settings.linkedin_email)
        await page.fill("#password", settings.linkedin_password)
        await page.click("button[type='submit']")
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo completar el formulario de login: %s", exc)
        return False

    for _ in range(30):  # hasta 30s para feed o challenge
        await asyncio.sleep(1)
        url = page.url or ""
        if await _is_logged_in(page):
            return True
        if "checkpoint" in url or "captcha" in url or "challenge" in url:
            return False
    return await _is_logged_in(page)


async def _is_logged_in(page: Any) -> bool:
    """Detecta si la sesión está activa comprobando la URL."""
    url = page.url or ""
    if "/login" in url or "/authwall" in url or "checkpoint" in url:
        return False
    if "/feed" in url or "/jobs" in url or "/mynetwork" in url:
        return True
    try:
        await page.wait_for_selector("div.global-nav", timeout=3000)
        return True
    except Exception:  # noqa: BLE001
        return False
