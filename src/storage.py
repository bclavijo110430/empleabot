"""Persistencia de postulaciones en SQLite con exportación a JSON."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from config import DB_PATH, EXPORT_JSON_PATH
from src.utils import logger


@dataclass
class Application:
    """Resultado de una postulación."""

    job_id: str
    title: str
    company: str
    location: str
    url: str
    cv_used: str
    status: str  # "applied" | "failed" | "skipped"
    error: str = ""
    routed_via: str = ""
    applied_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class Storage:
    """Acceso a la base de datos SQLite de postulaciones."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        """Crea la tabla de postulaciones si no existe."""
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    title TEXT,
                    company TEXT,
                    location TEXT,
                    url TEXT,
                    cv_used TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    routed_via TEXT,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_job_id "
                "ON applications(job_id) WHERE status = 'applied'"
            )
            connection.commit()

    def already_applied(self, job_id: str) -> bool:
        """Indica si una oferta ya fue postulada con éxito."""
        if not job_id:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM applications WHERE job_id = ? AND status = 'applied' LIMIT 1",
                (job_id,),
            ).fetchone()
        return row is not None

    def record(self, application: Application) -> None:
        """Inserta el resultado de una postulación."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO applications
                    (job_id, title, company, location, url, cv_used, status, error, routed_via, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application.job_id,
                    application.title,
                    application.company,
                    application.location,
                    application.url,
                    application.cv_used,
                    application.status,
                    application.error,
                    application.routed_via,
                    application.applied_at,
                ),
            )
            connection.commit()
        logger.info(
            "[%s] %s @ %s (CV: %s)",
            application.status.upper(),
            application.title or "?",
            application.company or "?",
            application.cv_used or "-",
        )

    def stats(self) -> dict[str, int]:
        """Devuelve un resumen por estado."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM applications GROUP BY status"
            ).fetchall()
        return {row["status"]: row["total"] for row in rows}

    def export_json(self, path: Path = EXPORT_JSON_PATH) -> Path:
        """Exporta todas las postulaciones a un archivo JSON."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM applications ORDER BY applied_at DESC"
            ).fetchall()
        data = [dict(row) for row in rows]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Exportadas %d postulaciones a %s", len(data), path)
        return path


def application_to_dict(application: Application) -> dict:
    """Helper de serialización."""
    return asdict(application)
