"""Configuración central del bot.

Carga variables desde el archivo `.env` (si existe) y expone un objeto
`settings` con valores validados mediante Pydantic. Las rutas del proyecto
se definen como constantes de módulo.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
CV_DIR = DATA_DIR / "cv"
SESSIONS_DIR = DATA_DIR / "sessions"

LOGS_DIR = BASE_DIR / "logs"
SCREENSHOTS_DIR = LOGS_DIR / "screenshots"

DB_PATH = LOGS_DIR / "applications.db"
EXPORT_JSON_PATH = LOGS_DIR / "applications_export.json"

PROFILE_PATH = DATA_DIR / "profile.json"
CVS_META_PATH = DATA_DIR / "cvs.json"

# Proveedores de inferencia soportados (ambos exponen una API OpenAI-compatible).
LLM_PROVIDERS = ("freellmapi", "ollama")


def normalize_text(value: str) -> str:
    """Normaliza texto para comparar: minúsculas, sin acentos ni puntuación."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class LLMConfig:
    """Parámetros resueltos del proveedor de inferencia activo."""

    provider: str
    base_url: str
    api_key: str
    model: str
    timeout: float
    max_retries: int


class Settings(BaseSettings):
    """Ajustes del bot, sobreescribibles vía variables de entorno."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Proveedor de inferencia ---
    # "freellmapi" (router local en el puerto 3001) u "ollama" (servidor local).
    llm_provider: str = "freellmapi"

    # --- FreeLLMAPI (implementación local) ---
    freelm_base_url: str = "http://localhost:3001/v1"
    freelm_api_key: str = "freellmapi-unified-key"
    freelm_model: str = "auto"
    freelm_timeout: float = 60.0
    freelm_max_retries: int = 3

    # --- Ollama (inferencia local) ---
    # El endpoint es OpenAI-compatible; la API key la ignora Ollama pero el
    # cliente la exige. Descarga un modelo con: `ollama pull <modelo>`.
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_api_key: str = "ollama"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout: float = 120.0
    ollama_max_retries: int = 3

    @field_validator("llm_provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        """Normaliza y valida el proveedor de inferencia elegido."""
        provider = (value or "freellmapi").strip().lower()
        if provider not in LLM_PROVIDERS:
            raise ValueError(
                f"LLM_PROVIDER inválido: {value!r}. Usa uno de {list(LLM_PROVIDERS)}."
            )
        return provider

    # --- Navegador ---
    headless: bool = False
    slow_mo: int = 0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    viewport_width: int = 1440
    viewport_height: int = 900
    action_timeout_ms: int = 20000

    # --- Comportamiento humano / anti-bloqueo ---
    min_delay: float = 1.2
    max_delay: float = 4.0

    # --- Búsqueda en LinkedIn ---
    search_query: str = "pmo, gestion de proyectos, compras, logistica"
    search_location: str = "España"
    remote: bool = True
    easy_apply_only: bool = True

    # --- Sesión / credenciales de LinkedIn (opcionales) ---
    # Si se dejan vacías, el bot pedirá login manual la primera vez.
    linkedin_email: str = ""
    linkedin_password: str = ""
    # Ruta a un JSON de cookies (exportado por Cookie-Editor, EditThisCookie
    # o un storage_state de Playwright). Relativa a la raíz del proyecto.
    linkedin_cookies_file: str = ""

    # --- Empresas bloqueadas ---
    # Lista separada por comas. No se postulará a ofertas de estas empresas.
    # Ejemplo: BLOCKED_COMPANIES=inetum,acme corp
    blocked_companies: str = "inetum"

    # --- Roles bloqueados ---
    # Lista separada por comas. No se postulará a ofertas cuyo título coincida
    # con alguno de estos roles (comparación sin acentos ni mayúsculas).
    # Ejemplo: BLOCKED_ROLES=desarrollador full stack,frontend developer
    blocked_roles: str = ""

    # --- Límites ---
    max_applications: int = 25
    max_form_steps: int = 20

    # --- Expectativa salarial (se inyecta en el perfil) ---
    # currency: "USD" o "COP"; period: "mensual", "anual", "quincenal", etc.
    expected_salary: str = "1000"
    salary_currency: str = "USD"
    salary_period: str = "mensual"

    @property
    def active_llm(self) -> LLMConfig:
        """Devuelve los parámetros del proveedor de inferencia seleccionado."""
        if self.llm_provider == "ollama":
            return LLMConfig(
                provider="ollama",
                base_url=self.ollama_base_url,
                api_key=self.ollama_api_key,
                model=self.ollama_model,
                timeout=self.ollama_timeout,
                max_retries=self.ollama_max_retries,
            )
        return LLMConfig(
            provider="freellmapi",
            base_url=self.freelm_base_url,
            api_key=self.freelm_api_key,
            model=self.freelm_model or "auto",
            timeout=self.freelm_timeout,
            max_retries=self.freelm_max_retries,
        )

    @property
    def blocked_companies_list(self) -> list[str]:
        """Empresas bloqueadas normalizadas (minúsculas, sin espacios extra)."""
        return [
            item.strip().lower()
            for item in self.blocked_companies.split(",")
            if item.strip()
        ]

    def is_company_blocked(self, company: str) -> bool:
        """Indica si una empresa está en la lista de bloqueo."""
        normalized = (company or "").strip().lower()
        if not normalized:
            return False
        return any(
            blocked in normalized or normalized in blocked
            for blocked in self.blocked_companies_list
        )

    @property
    def blocked_roles_list(self) -> list[str]:
        """Roles bloqueados normalizados (minúsculas, sin acentos)."""
        return [
            normalize_text(item)
            for item in self.blocked_roles.split(",")
            if item.strip()
        ]

    def is_role_blocked(self, *texts: str) -> bool:
        """Indica si el título (u otro texto) de la oferta coincide con un rol bloqueado.

        La comparación ignora mayúsculas, acentos y guiones, y exige que el rol
        aparezca como palabra completa (p. ej. "java" no bloquea "javascript").
        """
        roles = self.blocked_roles_list
        if not roles:
            return False
        for text in texts:
            haystack = normalize_text(text)
            if not haystack:
                continue
            for role in roles:
                if re.search(rf"\b{re.escape(role)}\b", haystack):
                    return True
        return False

    @property
    def search_query_terms(self) -> list[str]:
        """Términos individuales de la búsqueda, normalizados."""
        return [
            value
            for value in (
                normalize_text(item) for item in self.search_query.split(",")
            )
            if value
        ]

    def title_matches_search(self, title: str) -> bool:
        """Indica si el título contiene alguno de los términos de la búsqueda.

        Estos títulos se consideran deseados y nunca se bloquean por rol.
        """
        haystack = normalize_text(title)
        if not haystack:
            return False
        return any(
            re.search(rf"\b{re.escape(term)}\b", haystack)
            for term in self.search_query_terms
        )

    @property
    def session_state_path(self) -> Path:
        """Ruta del storage_state de LinkedIn."""
        return SESSIONS_DIR / "linkedin_storage_state.json"

    @property
    def cookies_file_path(self) -> Path | None:
        """Ruta absoluta del archivo de cookies, si se configuró."""
        if not self.linkedin_cookies_file:
            return None
        path = Path(self.linkedin_cookies_file)
        return path if path.is_absolute() else BASE_DIR / path

    def ensure_dirs(self) -> None:
        """Crea las carpetas de datos/logs si no existen."""
        for directory in (DATA_DIR, CV_DIR, SESSIONS_DIR, LOGS_DIR, SCREENSHOTS_DIR):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
