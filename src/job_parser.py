"""Extracción de datos de una oferta de LinkedIn.

La interfaz de LinkedIn usa clases CSS con hashes generados (p. ej.
``e5d9f935 edbd809c``), por lo que los selectores semánticos clásicos ya no
son fiables. La extracción se apoya en:
- ``document.title`` para el título y la empresa.
- Anclas de texto ("Acerca del empleo" / "About the job") para la descripción.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.utils import logger

DESCRIPTION_ANCHORS = ("acerca del empleo", "about the job", "descripción del empleo")


@dataclass
class Job:
    """Representa una oferta de empleo."""

    job_id: str
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    easy_apply: bool = False
    metadata: dict = field(default_factory=dict)

    def to_context(self, max_length: int = 2000) -> str:
        """Texto compacto de la oferta para enviar al LLM."""
        text = f"{self.title} en {self.company} ({self.location})"
        if self.description:
            text += f"\n\n{self.description}"
        return text[:max_length]


def job_id_from_url(url: str) -> str:
    """Extrae el identificador numérico de una URL de oferta de LinkedIn."""
    match = re.search(r"(\d{6,})", url or "")
    return match.group(1) if match else (url or "")


def parse_page_title(raw: str) -> tuple[str, str]:
    """Extrae (puesto, empresa) del título del documento.

    Ejemplo: ``"Scrum Master - Remote | INDI Staffing Services | LinkedIn"``.
    """
    if not raw:
        return "", ""
    parts = [part.strip() for part in raw.split("|") if part.strip()]
    if parts and parts[-1].lower() == "linkedin":
        parts = parts[:-1]
    title = parts[0] if parts else ""
    company = parts[1] if len(parts) > 1 else ""
    return title, company


async def extract_job_details(page: Any, url: str) -> Job:
    """Lee los datos visibles de la página de detalle de una oferta."""
    job = Job(job_id=job_id_from_url(url), url=url)

    job.title, job.company = parse_page_title(await page.title())
    job.description = await _extract_description(page)
    job.location = await _extract_location(page, job.title)
    job.easy_apply = await is_easy_apply(page)

    logger.info(
        "Oferta: %s | %s | %s | EasyApply=%s | desc=%d chars",
        job.title or "?",
        job.company or "?",
        job.location or "?",
        job.easy_apply,
        len(job.description),
    )
    return job


async def is_easy_apply(page: Any) -> bool:
    """Detecta si la oferta permite Easy Apply."""
    try:
        return bool(
            await page.evaluate(
                """
                () => {
                    const nodes = [...document.querySelectorAll('button, a')];
                    return nodes.some((n) => {
                        const text = ((n.innerText || '') + ' ' + (n.getAttribute('aria-label') || '')).toLowerCase();
                        return text.includes('easy apply') || text.includes('solicitud sencilla');
                    });
                }
                """
            )
        )
    except Exception:  # noqa: BLE001
        return False


async def _extract_description(page: Any) -> str:
    """Extrae la descripción anclándose al encabezado de la sección."""
    try:
        return (
            await page.evaluate(
                """
                () => {
                    const anchors = ['acerca del empleo', 'about the job', 'descripción del empleo'];
                    const clean = (t) => (t || '')
                        .replace(/\\u00a0/g, ' ')
                        .replace(/[ \\t]+/g, ' ')
                        .trim();
                    const headings = [...document.querySelectorAll('h1, h2, h3, h4')];
                    const heading = headings.find((h) => {
                        const text = clean(h.innerText).toLowerCase();
                        return anchors.some((a) => text === a || text.startsWith(a));
                    });
                    if (!heading) return '';

                    let node = heading.parentElement;
                    for (let i = 0; i < 4 && node; i += 1) {
                        if (clean(node.innerText).length > 300) break;
                        node = node.parentElement;
                    }
                    if (!node) return '';

                    let text = clean(node.innerText);
                    text = text.replace(/^(acerca del empleo|about the job|descripción del empleo)\\s*/i, '');
                    text = text.replace(/\\s*(ver más|see more|\\.\\.\\.|…)\\s*$/i, '');
                    return text;
                }
                """
            )
            or ""
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("No se pudo extraer la descripción: %s", exc)
        return ""


async def _extract_location(page: Any, title: str) -> str:
    """Obtiene la ubicación desde la tarjeta superior de la oferta."""
    if not title:
        return ""
    try:
        return (
            await page.evaluate(
                """
                (title) => {
                    const clean = (t) => (t || '').replace(/\\u00a0/g, ' ').trim();
                    const nodes = [...document.querySelectorAll('div, span, p')].filter((n) => {
                        const text = clean(n.innerText);
                        return text.startsWith(title) && text.length < 200;
                    });
                    if (!nodes.length) return '';
                    nodes.sort((a, b) => a.innerText.length - b.innerText.length);
                    let node = nodes[0];
                    for (let i = 0; i < 6 && node; i += 1) {
                        const line = clean(node.innerText)
                            .split('\\n')
                            .map((l) => l.trim())
                            .find((l) => l.includes('·'));
                        if (line) return line.split('·')[0].trim();
                        node = node.parentElement;
                    }
                    return '';
                }
                """,
                title,
            )
            or ""
        )
    except Exception:  # noqa: BLE001
        return ""
