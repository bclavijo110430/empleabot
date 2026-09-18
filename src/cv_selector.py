"""Selección del CV más adecuado para una oferta."""
from __future__ import annotations

import json
from pathlib import Path

from config import CV_DIR, CVS_META_PATH
from src.ai_handler import AIHandler
from src.job_parser import Job
from src.utils import logger


def load_cvs_meta(path: Path = CVS_META_PATH) -> list[dict]:
    """Carga los metadatos de los CVs disponibles."""
    if not path.exists():
        logger.warning("No se encontró %s. No hay CVs para seleccionar.", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Error leyendo %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.error("El formato de %s debe ser una lista de objetos.", path)
        return []
    return [item for item in data if isinstance(item, dict)]


def choose_cv(job: Job, cvs: list[dict], ai: AIHandler) -> Path | None:
    """Devuelve la ruta del PDF elegido, o None si no hay ninguno disponible."""
    if not cvs:
        return None

    existing = [c for c in cvs if (CV_DIR / c.get("file", "")).exists()]
    if not existing:
        logger.error("Ningún PDF de %s existe en disco.", CV_DIR)
        return None

    filename = ai.select_best_cv(job, existing)
    if not filename:
        filename = existing[0]["file"]
        logger.warning("Sin selección de LLM; usando %s por defecto.", filename)

    path = CV_DIR / filename
    if not path.exists():
        logger.error("El CV elegido no existe: %s", path)
        return None
    return path
