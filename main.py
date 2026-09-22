"""Punto de entrada del bot de postulación automatizada.

Uso:
    python main.py --query "Python Developer" --location "España" --limit 10
    python main.py --headless --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from config import (
    EXPORT_JSON_PATH,
    PROFILE_PATH,
    Settings,
    settings as default_settings,
)
from src.ai_handler import AIHandler
from src.cv_analyzer import analyze_all
from src.cv_selector import choose_cv, load_cvs_meta
from src.form_filler import EasyApplyFormFiller
from src.job_parser import job_id_from_url
from src.portals.linkedin import LinkedInPortal
from src.session_manager import apply_stealth, launch_browser, save_storage_state
from src.storage import Application, Storage
from src.utils import logger, random_pause, take_screenshot


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define y parsea los argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(description="Bot de postulación a ofertas de empleo.")
    parser.add_argument("--query", help="Puesto a buscar (sobrescribe SEARCH_QUERY).")
    parser.add_argument("--location", help="Ubicación (sobrescribe SEARCH_LOCATION).")
    parser.add_argument("--limit", type=int, help="Máximo de postulaciones en esta corrida.")
    parser.add_argument("--headless", action="store_true", help="Ejecuta el navegador sin ventana.")
    parser.add_argument("--no-remote", action="store_true", help="Desactiva el filtro remoto.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analiza y elige CV sin enviar postulaciones.",
    )
    parser.add_argument(
        "--analyze-cvs",
        action="store_true",
        help="Analiza los PDFs de data/cv/ y regenera data/cvs.json y data/profile.json.",
    )
    return parser.parse_args(argv)


def load_profile(path: Path) -> dict:
    """Carga el perfil del candidato desde JSON."""
    if not path.exists():
        logger.error("No existe %s. Copia y edita el perfil de ejemplo.", path)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Perfil inválido (%s): %s", path, exc)
        return {}


async def run(args: argparse.Namespace) -> int:
    """Ejecuta el flujo completo del bot."""
    settings = Settings()
    if args.headless:
        settings.headless = True
    if args.query:
        settings.search_query = args.query
    if args.location:
        settings.search_location = args.location
    if args.limit is not None:
        settings.max_applications = args.limit
    if args.no_remote:
        settings.remote = False

    settings.ensure_dirs()

    profile = load_profile(PROFILE_PATH)
    if not profile:
        return 1

    # La expectativa salarial manda desde la configuración (.env).
    profile["expected_salary"] = settings.expected_salary
    profile["salary_currency"] = settings.salary_currency
    profile["salary_period"] = settings.salary_period
    logger.info(
        "Expectativa salarial: %s %s %s",
        settings.expected_salary,
        settings.salary_currency,
        settings.salary_period,
    )

    cvs = load_cvs_meta()
    if not cvs:
        logger.error("No hay CVs configurados en data/cvs.json.")
        return 1

    storage = Storage()
    storage.init_db()
    ai = AIHandler(settings)

    applied = 0
    attempted = 0

    async with async_playwright() as playwright:
        browser, context = await launch_browser(playwright, settings)
        page = await context.new_page()
        stealth = apply_stealth()
        if stealth:
            await stealth(page)

        portal = LinkedInPortal(page, context, settings)
        if not await portal.ensure_login():
            logger.error("No se pudo iniciar sesión. Abortando.")
            await browser.close()
            return 1

        try:
            async for url in portal.iter_job_urls(
                query=settings.search_query,
                location=settings.search_location,
                max_jobs=max(settings.max_applications * 3, 30),
                remote=settings.remote,
                easy_apply_only=settings.easy_apply_only,
            ):
                if applied >= settings.max_applications:
                    logger.info("Límite de postulaciones alcanzado (%d).", applied)
                    break

                job_id = job_id_from_url(url)
                if storage.already_applied(job_id):
                    logger.info("Ya postulado previamente: %s", url)
                    continue

                attempted += 1
                try:
                    job = await portal.open_job(url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("No se pudo abrir %s: %s", url, exc)
                    storage.record(
                        Application(
                            job_id=job_id,
                            title="",
                            company="",
                            location="",
                            url=url,
                            cv_used="",
                            status="failed",
                            error=f"apertura: {exc}",
                        )
                    )
                    continue

                if settings.is_company_blocked(job.company):
                    logger.info(
                        "Empresa bloqueada (%s), se omite: %s",
                        job.company or "?",
                        job.title or url,
                    )
                    storage.record(
                        Application(
                            job_id=job_id,
                            title=job.title,
                            company=job.company,
                            location=job.location,
                            url=url,
                            cv_used="",
                            status="skipped",
                            error="empresa bloqueada",
                        )
                    )
                    continue

                # Los títulos que coinciden con la búsqueda deseada nunca se
                # bloquean por rol. El resto se valida contra BLOCKED_ROLES:
                # primero coincidencia literal y, si no, similitud vía LLM.
                if settings.blocked_roles_list and not settings.title_matches_search(
                    job.title
                ):
                    blocked = settings.is_role_blocked(job.title)
                    if not blocked:
                        blocked = (
                            ai.is_similar_blocked_role(
                                job.title, settings.blocked_roles_list
                            )
                            is True
                        )
                    if blocked:
                        logger.info(
                            "Rol bloqueado, se omite: %s @ %s",
                            job.title or "?",
                            job.company or "?",
                        )
                        storage.record(
                            Application(
                                job_id=job_id,
                                title=job.title,
                                company=job.company,
                                location=job.location,
                                url=url,
                                cv_used="",
                                status="skipped",
                                error="rol bloqueado",
                            )
                        )
                        continue

                if settings.easy_apply_only and not job.easy_apply:
                    logger.info("Sin Easy Apply, se omite: %s", job.title or url)
                    storage.record(
                        Application(
                            job_id=job_id,
                            title=job.title,
                            company=job.company,
                            location=job.location,
                            url=url,
                            cv_used="",
                            status="skipped",
                            error="sin Easy Apply",
                        )
                    )
                    continue

                cv_path = choose_cv(job, cvs, ai)
                cv_meta = next(
                    (item for item in cvs if item.get("file") == cv_path.name), None
                ) if cv_path else None
                logger.info(
                    "Postulando a '%s' @ '%s' con CV %s",
                    job.title or "?",
                    job.company or "?",
                    cv_path.name if cv_path else "-",
                )

                if args.dry_run:
                    applied += 1
                    logger.info("[dry-run] Se omitiría el envío.")
                    continue

                filler = EasyApplyFormFiller(page, settings, profile, ai, cv_meta=cv_meta)
                try:
                    success, error = await filler.apply(job, cv_path)
                except Exception as exc:  # noqa: BLE001
                    await take_screenshot(page, f"error-{job_id}")
                    success, error = False, str(exc)

                if success:
                    applied += 1
                else:
                    await take_screenshot(page, f"fallo-{job_id}")

                if success:
                    status = "applied"
                elif error == "ya postulado previamente":
                    status = "skipped"
                else:
                    status = "failed"

                storage.record(
                    Application(
                        job_id=job_id,
                        title=job.title,
                        company=job.company,
                        location=job.location,
                        url=url,
                        cv_used=cv_path.name if cv_path else "",
                        status=status,
                        error=error,
                        routed_via=getattr(ai, "last_routed_via", ""),
                    )
                )
                await random_pause(settings)

        except KeyboardInterrupt:
            logger.info("Ejecución interrumpida por el usuario.")
        finally:
            await save_storage_state(context, settings.session_state_path)
            await browser.close()

    export_path = storage.export_json(EXPORT_JSON_PATH)
    stats = storage.stats()
    logger.info(
        "Resumen: %d postulaciones nuevas, %d ofertas analizadas de %d vistas.",
        applied,
        attempted,
        stats.get("applied", 0) + stats.get("failed", 0) + stats.get("skipped", 0),
    )
    logger.info("Registro exportado en %s", export_path)
    return 0


def main() -> int:
    """Entrypoint sincrónico."""
    args = parse_args()
    if args.analyze_cvs:
        return analyze_all(Settings())
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
