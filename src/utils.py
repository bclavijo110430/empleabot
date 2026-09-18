"""Utilidades transversales: logging, retardos human-like y capturas."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from config import SCREENSHOTS_DIR, Settings

_LOGGER_NAME = "jobbot"


def setup_logger(level: int = logging.INFO) -> logging.Logger:
    """Devuelve un logger configurado una sola vez."""
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logger()


async def human_delay(min_seconds: float, max_seconds: float) -> None:
    """Espera un tiempo aleatorio dentro del rango para simular a una persona."""
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


async def random_pause(settings: Settings) -> None:
    """Pausa aleatoria usando los límites configurados."""
    await human_delay(settings.min_delay, settings.max_delay)


def slugify(value: str, max_length: int = 60) -> str:
    """Convierte texto en un identificador seguro para nombres de archivo."""
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value[:max_length] or "sin-titulo"


async def take_screenshot(page: Any, name: str) -> Path | None:
    """Guarda una captura de pantalla y devuelve la ruta (o None si falla)."""
    if page is None:
        return None
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOTS_DIR / f"{timestamp}_{slugify(name)}.png"
    try:
        await page.screenshot(path=str(path), full_page=False)
        logger.info("Captura guardada en %s", path)
        return path
    except Exception as exc:  # noqa: BLE001 - nunca debe romper el flujo
        logger.warning("No se pudo guardar la captura: %s", exc)
        return None


def truncate(text: str, length: int = 6000) -> str:
    """Recorta un texto largo preservando el final relevante."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= length:
        return text
    return text[:length] + "\n...[recortado]..."
