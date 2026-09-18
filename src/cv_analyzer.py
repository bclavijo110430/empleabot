"""Análisis de los CVs en PDF para generar ``data/cvs.json`` y ``data/profile.json``.

Extrae el texto de cada PDF en ``data/cv/``, pide al LLM los metadatos de cada
CV y un perfil consolidado del candidato, y escribe ambos JSON.

Uso:
    python main.py --analyze-cvs
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config import CV_DIR, CVS_META_PATH, PROFILE_PATH, Settings
from src.ai_handler import AIHandler
from src.utils import logger

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - dependencia declarada en requirements.txt
    PdfReader = None  # type: ignore[assignment]


DEFAULT_ANSWERS = {
    "authorized_to_work": "Sí",
    "require_sponsorship": "No",
    "gender": "Prefiero no decirlo",
    "disability": "Prefiero no decirlo",
    "veteran": "Prefiero no decirlo",
    "how_did_you_hear": "LinkedIn",
}

PROFILE_TEMPLATE: dict[str, Any] = {
    "full_name": "",
    "first_name": "",
    "last_name": "",
    "email": "",
    "phone": "",
    "location": "",
    "city": "",
    "country": "",
    "linkedin_url": "",
    "github_url": "",
    "website": "",
    "headline": "",
    "summary": "",
    "years_of_experience": 0,
    "current_company": "",
    "current_title": "",
    "notice_period": "",
    "willing_to_relocate": False,
    "remote_preference": "",
    "work_authorization": "",
    "needs_sponsorship": False,
    "languages": {},
    "skills": [],
    "education": [],
    "certifications": [],
    "experience": [],
}

COP_PER_USD = 4000.0


def extract_pdf_text(path: Path) -> str:
    """Extrae y normaliza el texto de un PDF."""
    if PdfReader is None:
        raise RuntimeError(
            "Falta la dependencia pypdf. Instálala con: pip install -r requirements.txt"
        )
    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - una página corrupta no debe abortar
            logger.warning("No se pudo leer una página de %s: %s", path.name, exc)
    return _normalize_text("\n".join(pages))


def analyze_all(settings: Settings) -> int:
    """Analiza los CVs y sobrescribe ``cvs.json`` y ``profile.json``.

    Devuelve 0 si termina correctamente, 1 ante cualquier error bloqueante.
    """
    settings.ensure_dirs()
    pdfs = _list_pdfs()
    if not pdfs:
        logger.error("No hay PDFs en %s. Coloca tu CV antes de analizar.", CV_DIR)
        return 1

    ai = AIHandler(settings)

    texts: dict[str, str] = {}
    for pdf in pdfs:
        try:
            text = extract_pdf_text(pdf)
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo leer %s: %s", pdf.name, exc)
            continue
        if not text:
            logger.warning("Sin texto extraíble en %s (¿PDF escaneado?).", pdf.name)
            continue
        texts[pdf.name] = text
        logger.info("Texto extraído de %s (%d caracteres).", pdf.name, len(text))

    if not texts:
        logger.error("No se pudo extraer texto de ningún CV. Abortando.")
        return 1

    # --- data/cvs.json -------------------------------------------------- #
    cvs: list[dict] = []
    for name, text in texts.items():
        meta = ai.extract_cv_metadata(name, text)
        if meta:
            cvs.append(meta)
            logger.info("CV catalogado: %s", name)
        else:
            logger.warning("Sin metadatos para %s; se omite de cvs.json.", name)

    if not cvs:
        logger.error("El LLM no generó metadatos de CV; no se sobrescribe %s.", CVS_META_PATH)
    else:
        _write_json(CVS_META_PATH, cvs)
        logger.info("Escrito %s con %d CV(s).", CVS_META_PATH, len(cvs))

    # --- data/profile.json ---------------------------------------------- #
    profile = ai.extract_profile(texts)
    if not profile:
        logger.error("El LLM no generó el perfil; no se sobrescribe %s.", PROFILE_PATH)
        return 1

    profile = _normalize_profile(profile)
    profile["expected_salary"] = settings.expected_salary
    profile["salary_currency"] = settings.salary_currency
    profile["salary_period"] = settings.salary_period
    profile["salary_usd_monthly"] = _salary_usd_monthly(settings)
    profile["default_answers"] = dict(DEFAULT_ANSWERS)

    _write_json(PROFILE_PATH, profile)
    logger.info("Escrito %s.", PROFILE_PATH)
    return 0


def _list_pdfs() -> list[Path]:
    """Devuelve los PDFs de ``data/cv`` ordenados por nombre."""
    return sorted(p for p in CV_DIR.glob("*.pdf") if p.is_file())


def _normalize_text(text: str) -> str:
    """Unifica espacios y saltos de línea sobrantes."""
    text = (text or "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_profile(data: dict) -> dict:
    """Completa el perfil con un valor por defecto para cada clave conocida."""
    profile: dict[str, Any] = {}
    for key, default in PROFILE_TEMPLATE.items():
        value = data.get(key)
        profile[key] = default if _is_empty(value) else value
    # Conserva cualquier clave extra que haya devuelto el LLM.
    for key, value in data.items():
        if key not in profile and not _is_empty(value):
            profile[key] = value
    return profile


def _is_empty(value: Any) -> bool:
    """Indica si un valor del LLM debe considerarse vacío."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple)):
        return len(value) == 0
    return False


def _salary_usd_monthly(settings: Settings) -> int | None:
    """Estima la expectativa salarial en USD mensuales (redondeo a entero)."""
    raw = str(settings.expected_salary or "").replace(",", "").replace(".", "").strip()
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return None

    currency = (settings.salary_currency or "").upper()
    if currency == "COP":
        amount /= COP_PER_USD

    period = (settings.salary_period or "").lower()
    if period in {"anual", "annual", "año", "ano", "year", "yearly"}:
        amount /= 12.0
    elif period in {"quincenal", "fortnight", "biweekly", "catorcenal"}:
        amount *= 2.0
    elif period in {"semanal", "weekly", "semana"}:
        amount *= 4.0

    return int(round(amount))


def _write_json(path: Path, data: Any) -> None:
    """Escribe JSON legible en UTF-8."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
