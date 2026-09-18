"""Adaptador del portal de empleo de LinkedIn (Easy Apply)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote_plus

from src.job_parser import Job, extract_job_details
from src.session_manager import ensure_authenticated
from src.utils import logger, random_pause

BASE_SEARCH = "https://www.linkedin.com/jobs/search/"
JOB_VIEW = "https://www.linkedin.com/jobs/view/"


class LinkedInPortal:
    """Navegación y extracción de ofertas de LinkedIn."""

    name = "linkedin"

    def __init__(self, page: Any, context: Any, settings: Any) -> None:
        self.page = page
        self.context = context
        self.settings = settings

    async def ensure_login(self) -> bool:
        """Garantiza una sesión válida (cookies, credenciales o login manual)."""
        return await ensure_authenticated(self.page, self.context, self.settings)

    @staticmethod
    def normalize_keywords(query: str) -> str:
        """Convierte una lista separada por comas en una consulta booleana OR.

        Ejemplo: ``pmo, gestion de proyectos, compras`` se transforma en
        ``pmo OR "gestion de proyectos" OR compras`` para LinkedIn.
        """
        if not query:
            return query
        if "," not in query:
            return query
        terms = [term.strip().strip('"') for term in query.split(",") if term.strip()]
        normalized = [f'"{term}"' if " " in term else term for term in terms]
        return " OR ".join(normalized)

    @staticmethod
    def build_search_url(
        query: str,
        location: str,
        remote: bool = True,
        easy_apply_only: bool = True,
        start: int = 0,
    ) -> str:
        """Construye la URL de búsqueda con filtros."""
        keywords = LinkedInPortal.normalize_keywords(query)
        params = [
            f"keywords={quote_plus(keywords)}",
            f"location={quote_plus(location)}",
            "f_TPR=r604800",  # última semana
            f"start={start}",
        ]
        if easy_apply_only:
            params.append("f_AL=true")
        if remote:
            params.append("f_WT=2")
        return f"{BASE_SEARCH}?{'&'.join(params)}"

    async def iter_job_urls(
        self,
        query: str,
        location: str,
        max_jobs: int,
        remote: bool = True,
        easy_apply_only: bool = True,
        max_pages: int = 5,
    ) -> AsyncIterator[str]:
        """Itera URLs de ofertas de los resultados de búsqueda."""
        seen: set[str] = set()
        yielded = 0

        for page_number in range(max_pages):
            if yielded >= max_jobs:
                break
            start = page_number * 25
            url = self.build_search_url(query, location, remote, easy_apply_only, start)
            logger.info("Buscando ofertas (página %d): %s", page_number + 1, url)
            try:
                await self.page.goto(url, wait_until="domcontentloaded")
                await random_pause(self.settings)
                await self._scroll_results()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error cargando búsqueda: %s", exc)
                continue

            links = await self._collect_links()
            if not links:
                logger.info("Sin resultados en la página %d.", page_number + 1)
                break

            for link in links:
                if link in seen:
                    continue
                seen.add(link)
                yielded += 1
                yield link
                if yielded >= max_jobs:
                    break

    async def _scroll_results(self, rounds: int = 4) -> None:
        """Hace scroll para forzar la carga diferida de tarjetas."""
        for _ in range(rounds):
            try:
                await self.page.mouse.wheel(0, 1800)
                await asyncio.sleep(1.0)
            except Exception:  # noqa: BLE001
                break

    async def _collect_links(self) -> list[str]:
        """Recoge enlaces de ofertas visibles en la página de resultados."""
        try:
            hrefs = await self.page.eval_on_selector_all(
                "a[href*='/jobs/view/']",
                "els => els.map(e => e.href)",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Error recogiendo enlaces: %s", exc)
            return []

        urls: list[str] = []
        for href in hrefs:
            clean = (href or "").split("?")[0]
            if "/jobs/view/" in clean and clean not in urls:
                urls.append(clean)
        return urls

    async def open_job(self, url: str) -> Job:
        """Abre una oferta y extrae sus detalles."""
        await self.page.goto(url, wait_until="domcontentloaded")
        try:
            await self.page.wait_for_function(
                """
                () => [...document.querySelectorAll('h1, h2, h3')].some(
                    (h) => /^(acerca del empleo|about the job|descripción del empleo)/i.test((h.innerText || '').trim())
                )
                """,
                timeout=10000,
            )
        except Exception:  # noqa: BLE001
            logger.debug("La descripción de %s no apareció a tiempo.", url)
        await random_pause(self.settings)
        return await extract_job_details(self.page, url)
