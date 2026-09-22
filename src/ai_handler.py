"""Integración con el LLM local.

Soporta dos proveedores seleccionables vía ``LLM_PROVIDER``:

- ``freellmapi``: router local compatible con OpenAI en
  ``http://localhost:3001/v1``. El modelo virtual ``auto`` deja que el router
  elija el mejor proveedor disponible; el header ``X-Routed-Via`` indica qué
  modelo sirvió.
- ``ollama``: servidor local de Ollama, cuyo endpoint OpenAI-compatible vive en
  ``http://localhost:11434/v1``. No emite ``X-Routed-Via``, así que se registra
  el proveedor y modelo configurados.
"""
from __future__ import annotations

import json
import time
from typing import Any

from openai import OpenAI

from config import CV_DIR, Settings
from src.utils import logger, truncate


class AIHandler:
    """Wrapper de alto nivel sobre el cliente OpenAI-compatible."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        llm = settings.active_llm
        self.provider = llm.provider
        self.model = llm.model
        self.max_retries = llm.max_retries
        self.client = OpenAI(
            base_url=llm.base_url,
            api_key=llm.api_key,
            timeout=llm.timeout,
        )
        self.last_routed_via: str = ""
        logger.info("Proveedor de inferencia: %s (modelo=%s)", self.provider, self.model)

    # ------------------------------------------------------------------ #
    # Llamada base
    # ------------------------------------------------------------------ #
    def _chat(self, messages: list[dict[str, str]], json_mode: bool = False) -> str:
        """Llama al proveedor con reintentos y backoff ante 429/5xx."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.2}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        attempts = max(1, self.max_retries)
        for attempt in range(1, attempts + 1):
            try:
                return self._request(kwargs, json_mode=json_mode)
            except Exception as exc:  # noqa: BLE001 - degradación controlada
                if attempt >= attempts:
                    logger.warning("LLM falló tras %d intentos: %s", attempt, exc)
                    return ""
                wait = 2 ** attempt
                logger.warning("LLM error (%s). Reintentando en %ss...", exc, wait)
                time.sleep(wait)
        return ""

    def _request(self, kwargs: dict[str, Any], json_mode: bool = False) -> str:
        """Ejecuta una llamada única y devuelve el contenido de texto."""
        try:
            if hasattr(self.client.chat.completions, "with_raw_response"):
                raw = self.client.chat.completions.with_raw_response.create(**kwargs)
                routed_via = _extract_routed_via(raw)
                response = raw.parse()
            else:
                response = self.client.chat.completions.create(**kwargs)
                routed_via = _extract_routed_via(response)
        except Exception as exc:  # noqa: BLE001
            # Algunos servidores (Ollama antiguo) no soportan response_format.
            if json_mode and _is_response_format_error(exc):
                logger.warning("El proveedor no soporta response_format; reintentando sin JSON mode.")
                kwargs.pop("response_format", None)
                return self._request(kwargs, json_mode=False)
            raise

        self.last_routed_via = routed_via or f"{self.provider}:{self.model}"
        if routed_via:
            logger.debug("LLM servido por %s", routed_via)
        return (response.choices[0].message.content or "").strip()

    # ------------------------------------------------------------------ #
    # Selección de CV
    # ------------------------------------------------------------------ #
    def select_best_cv(self, job: Any, cvs: list[dict]) -> str | None:
        """Elige el PDF más adecuado para la oferta.

        Devuelve el nombre de archivo (no la ruta) o ``None`` si no hay CVs.
        """
        available = [c for c in cvs if (CV_DIR / c.get("file", "")).exists()]
        if not available:
            return None
        if len(available) == 1:
            return available[0]["file"]

        catalog = [
            {
                "file": c.get("file"),
                "title": c.get("title"),
                "seniority": c.get("seniority"),
                "summary": c.get("summary"),
                "skills": c.get("skills", []),
            }
            for c in available
        ]
        system = (
            "Eres un experto en reclutamiento. Selecciona el CV que mejor encaje con la "
            "oferta. Responde SIEMPRE con un objeto JSON con las claves 'cv' (nombre de "
            "archivo exacto de la lista) y 'reason' (motivo breve)."
        )
        user = (
            f"OFERTA\nTítulo: {job.title}\nEmpresa: {job.company}\n"
            f"Ubicación: {job.location}\n\nDescripción:\n{truncate(job.description, 6000)}\n\n"
            f"CVs DISPONIBLES:\n{json.dumps(catalog, ensure_ascii=False, indent=2)}\n\n"
            "Devuelve el JSON ahora."
        )
        content = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
        )
        data = _parse_json(content)
        if isinstance(data, dict):
            chosen = data.get("cv")
            if chosen in {c["file"] for c in available}:
                logger.info("CV elegido por el LLM: %s (%s)", chosen, data.get("reason", ""))
                return chosen
        fallback = available[0]["file"]
        logger.warning("Selección de CV no válida, usando fallback: %s", fallback)
        return fallback

    # ------------------------------------------------------------------ #
    # Validación de roles bloqueados
    # ------------------------------------------------------------------ #
    def is_similar_blocked_role(self, job_title: str, blocked_roles: list[str]) -> bool | None:
        """Determina con el LLM si el título corresponde a un rol a evitar.

        Va más allá de la coincidencia literal: reconoce sinónimos, traducciones
        y variantes del mismo tipo de rol (p. ej. ``developer`` ≈
        ``desarrollador`` ≈ ``software engineer``). Devuelve ``True``/``False`` o
        ``None`` si el LLM no responde, para que el llamador decida.
        """
        if not job_title or not blocked_roles:
            return False
        system = (
            "Eres un experto en reclutamiento. Determina si una oferta de empleo "
            "pertenece al mismo tipo de rol que alguno de los roles que el candidato "
            "quiere EVITAR, aunque el título use sinónimos, otros idiomas o variantes "
            "(p. ej. 'developer' ≈ 'desarrollador' ≈ 'programador' ≈ "
            "'software engineer'). Responde SIEMPRE con un objeto JSON con la forma "
            '{"blocked": true|false, "reason": "motivo breve"}. Marca blocked=true '
            "solo si el puesto es del mismo tipo de rol que algún rol a evitar; no lo "
            "marques por compartir sector, herramientas o empresa."
        )
        user = (
            f"ROLES A EVITAR:\n{json.dumps(blocked_roles, ensure_ascii=False)}\n\n"
            f"TÍTULO DE LA OFERTA: {job_title}\n\n"
            "Devuelve el JSON ahora."
        )
        content = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
        )
        data = _parse_json(content)
        if isinstance(data, dict) and "blocked" in data:
            blocked = bool(data.get("blocked"))
            logger.info(
                "Validación de rol por LLM: %s (%s)",
                "bloqueado" if blocked else "permitido",
                data.get("reason", ""),
            )
            return blocked
        logger.warning("El LLM no validó el rol de '%s'.", job_title)
        return None

    # ------------------------------------------------------------------ #
    # Análisis de CVs (generación de cvs.json y profile.json)
    # ------------------------------------------------------------------ #
    def extract_cv_metadata(self, filename: str, cv_text: str) -> dict:
        """Extrae los metadatos de un CV para ``data/cvs.json``.

        Devuelve un dict con las claves ``file``, ``title``, ``seniority``,
        ``summary``, ``skills`` y ``tags``. Retorna ``{}`` si el LLM falla o
        no devuelve un JSON válido.
        """
        if not cv_text.strip():
            return {}
        system = (
            "Eres un experto en reclutamiento que cataloga currículums. "
            "Analiza el CV y devuelve EXCLUSIVAMENTE un objeto JSON válido, sin texto "
            "adicional, con esta forma exacta:\n"
            "{\n"
            '  "title": "titular u objetivo profesional del CV",\n'
            '  "seniority": "uno de: junior, mid, senior, lead",\n'
            '  "summary": "resumen del perfil en 2 a 4 frases",\n'
            '  "skills": ["habilidad", "..."],\n'
            '  "tags": ["etiqueta-en-minusculas-con-guiones", "..."]\n'
            "}\n"
            "Usa el idioma del CV. No inventes datos que no aparezcan. "
            "En 'skills' incluye competencias técnicas y herramientas concretas. "
            "En 'tags' incluye entre 4 y 8 palabras clave para clasificar el CV."
        )
        user = (
            f"CURSO O ARCHIVO DEL CV: {filename}\n\n"
            f"CONTENIDO DEL CV:\n{truncate(cv_text, 12000)}\n\n"
            "Devuelve el JSON ahora."
        )
        content = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
        )
        data = _parse_json(content)
        if not isinstance(data, dict):
            logger.warning("El LLM no devolvió metadatos válidos para %s.", filename)
            return {}

        result = {
            "file": filename,
            "title": _clean_str(data.get("title")),
            "seniority": _normalize_seniority(data.get("seniority")),
            "summary": _clean_str(data.get("summary")),
            "skills": _string_list(data.get("skills")),
            "tags": _string_list(data.get("tags")),
        }
        if not result["title"] and not result["summary"]:
            logger.warning("Metadatos vacíos para %s; se descartan.", filename)
            return {}
        return result

    def extract_profile(self, cv_texts: dict[str, str]) -> dict:
        """Extrae el perfil del candidato a partir de uno o más CVs.

        ``cv_texts`` mapea nombre de archivo -> texto del CV. Devuelve un dict
        con el esquema de ``data/profile.json`` (algunos campos pueden ir
        vacíos o en ``None`` si no aparecen en el CV).
        """
        usable = {name: text for name, text in cv_texts.items() if text.strip()}
        if not usable:
            return {}
        system = (
            "Eres un experto en reclutamiento que convierte currículums en un perfil "
            "estructurado. Devuelve EXCLUSIVAMENTE un objeto JSON válido, sin texto "
            "adicional, con esta forma:\n"
            "{\n"
            '  "full_name": "", "first_name": "", "last_name": "",\n'
            '  "email": "", "phone": "", "location": "", "city": "", "country": "",\n'
            '  "linkedin_url": "", "github_url": "", "website": "",\n'
            '  "headline": "titular u objetivo profesional",\n'
            '  "summary": "resumen profesional",\n'
            '  "years_of_experience": 0,\n'
            '  "current_company": "", "current_title": "",\n'
            '  "notice_period": "", "willing_to_relocate": true,\n'
            '  "remote_preference": "", "work_authorization": "", "needs_sponsorship": false,\n'
            '  "languages": {"es": "Nativo"},\n'
            '  "skills": ["..."],\n'
            '  "education": [{"degree": "", "institution": "", "year": null}],\n'
            '  "certifications": [{"name": "", "institution": "", "year": null}],\n'
            '  "experience": [{"company": "", "title": "", "location": "", "start": "", "end": "", "highlights": ["..."]}]\n'
            "}\n"
            "Reglas: NO inventes datos; si un dato no aparece en el CV usa cadena vacía, "
            "lista vacía, null o false según corresponda. Respeta el orden cronológico "
            "inverso en 'experience' (lo más reciente primero). "
            "'years_of_experience' es un número entero estimado a partir del historial. "
            "Escribe en el idioma de los CVs."
        )
        parts = [
            f"=== CV: {name} ===\n{truncate(text, 12000)}" for name, text in usable.items()
        ]
        user = (
            "CURRÍCULUMS A CONSOLIDAR:\n\n" + "\n\n".join(parts) + "\n\nDevuelve el JSON ahora."
        )
        content = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
        )
        data = _parse_json(content)
        if not isinstance(data, dict):
            logger.warning("El LLM no devolvió un perfil válido.")
            return {}
        return data

    # ------------------------------------------------------------------ #
    # Preguntas dinámicas del formulario
    # ------------------------------------------------------------------ #
    def answer_question(
        self,
        question: str,
        profile: dict,
        job_context: str = "",
        field_type: str = "text",
        cv_context: str = "",
    ) -> str:
        """Genera una respuesta coherente al perfil para una pregunta del formulario.

        Incluye el contexto del CV y reglas salariales fijas. Si la pregunta
        trae opciones (desplegable / opción múltiple), el LLM debe devolver
        exactamente una de ellas.
        """
        if not question:
            return ""
        default_answers = profile.get("default_answers", {})
        system = (
            "Eres un asistente que rellena solicitudes de empleo en nombre del candidato. "
            "Responde SIEMPRE en el mismo idioma de la pregunta, de forma breve, honesta y profesional. "
            "Si el campo es numérico responde solo con el número. Si es de sí/no responde 'Sí' o 'No' "
            "(o 'Yes'/'No' si la pregunta está en inglés). Si es un desplegable u opción múltiple, "
            "responde EXACTAMENTE con una de las opciones ofrecidas, sin texto adicional. "
            "REGLAS SALARIALES (obligatorias): la expectativa salarial del candidato es "
            f"{profile.get('expected_salary', '')} {profile.get('salary_currency', '')} "
            f"{profile.get('salary_period', '')}. Si preguntan por otro periodo, convierte "
            "proporcionalmente (anual = mensual x12, quincenal = mensual /2). Si ofrecen rangos, "
            "elige el que contenga esa cifra. Si las opciones están en otra moneda, conviértela con "
            "una tasa aproximada (1 USD ≈ 4000 COP) antes de decidir. "
            "No inventes títulos ni empresas. "
            "Devuelve únicamente la respuesta, sin explicaciones ni comillas."
        )
        cv_section = f"CONTEXTO DEL CV SELECCIONADO:\n{truncate(cv_context, 2000)}\n\n" if cv_context else ""
        user = (
            f"PERFIL DEL CANDIDATO (JSON):\n"
            f"{json.dumps(profile, ensure_ascii=False, indent=2)}\n\n"
            f"{cv_section}"
            f"RESPUESTAS POR DEFECTO CONFIGURADAS:\n{json.dumps(default_answers, ensure_ascii=False)}\n\n"
            f"CONTEXTO DE LA OFERTA:\n{truncate(job_context, 2000)}\n\n"
            f"PREGUNTA DEL FORMULARIO (tipo {field_type}): {question}\n\nRespuesta:"
        )
        answer = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        if not answer:
            logger.debug("Sin respuesta del LLM para: %s", question)
        return answer.strip().strip('"')

    def answer_questions_batch(
        self,
        questions: list[dict],
        profile: dict,
        job_context: str = "",
        cv_context: str = "",
    ) -> dict[str, str]:
        """Responde un lote de preguntas del formulario en una sola llamada.

        Cada pregunta es un dict con ``id``, ``question``, ``type`` y ``options``.
        Devuelve un mapeo ``{id: respuesta}``; vacío si el LLM falla.
        """
        if not questions:
            return {}

        salary = (
            f"{profile.get('expected_salary', '')} {profile.get('salary_currency', '')} "
            f"{profile.get('salary_period', '')}"
        ).strip()
        default_answers = profile.get("default_answers", {})
        system = (
            "Eres un asistente que rellena solicitudes de empleo en nombre del candidato. "
            "Responde en el mismo idioma de cada pregunta, de forma breve, honesta y profesional. "
            "Si la pregunta es de sí/no responde 'Sí' o 'No' (o 'Yes'/'No' en inglés). "
            "Si la pregunta trae 'options', responde EXACTAMENTE con una de esas opciones. "
            "Si el tipo es 'checkbox' (selección múltiple, 'select all that apply'), responde con "
            "una o varias opciones de la lista separadas por comas, usando el texto exacto de cada "
            "opción. Si ninguna aplica y existe la opción 'None of the above'/'Ninguna', usa esa. "
            "Si es numérica responde solo con el número. "
            f"REGLAS SALARIALES (obligatorias): la expectativa salarial es {salary}. "
            "Si piden otro periodo, convierte proporcionalmente (anual = mensual x12, quincenal = mensual /2). "
            "Si ofrecen rangos, elige el que contenga esa cifra. Si las opciones están en otra moneda, "
            "conviértela con una tasa aproximada (1 USD ≈ 4000 COP) antes de decidir. "
            "No inventes títulos ni empresas. "
            'Devuelve EXCLUSIVAMENTE un JSON válido con la forma {"answers":[{"id": <id>, "answer": "<respuesta>"}]} '
            "usando el mismo id de cada pregunta y sin texto adicional fuera del JSON."
        )

        cv_section = f"CONTEXTO DEL CV SELECCIONADO:\n{truncate(cv_context, 2000)}\n\n" if cv_context else ""
        user = (
            f"PERFIL DEL CANDIDATO (JSON):\n"
            f"{json.dumps(profile, ensure_ascii=False, indent=2)}\n\n"
            f"{cv_section}"
            f"RESPUESTAS POR DEFECTO CONFIGURADAS:\n{json.dumps(default_answers, ensure_ascii=False)}\n\n"
            f"CONTEXTO DE LA OFERTA:\n{truncate(job_context, 2000)}\n\n"
            f"PREGUNTAS DEL FORMULARIO (JSON):\n"
            f"{json.dumps(questions, ensure_ascii=False)}\n\n"
            "Responde con el JSON de respuestas:"
        )

        content = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
        )
        data = _parse_json(content)
        if not isinstance(data, dict):
            logger.warning("El LLM no devolvió un JSON válido para el lote de preguntas.")
            return {}

        items = data.get("answers") or data.get("respuestas") or data.get("answers_list") or []
        if isinstance(data, list):
            items = data
        if not isinstance(items, list):
            return {}

        result: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("id")
            if key is None:
                continue
            result[str(key)] = str(item.get("answer", "")).strip()
        logger.info("LLM respondió %d de %d preguntas del formulario.", len(result), len(questions))
        return result


# ---------------------------------------------------------------------- #
# Helpers de parseo
# ---------------------------------------------------------------------- #
def _is_response_format_error(exc: Exception) -> bool:
    """Detecta si el error se debe a un ``response_format`` no soportado."""
    message = str(exc).lower()
    return "response_format" in message or "json" in message and "format" in message


def _parse_json(text: str) -> Any:
    """Extrae JSON de una respuesta que puede venir con cercas de código."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _clean_str(value: Any) -> str:
    """Normaliza un valor escalar a cadena limpia."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip()


def _string_list(value: Any) -> list[str]:
    """Convierte un valor del LLM en una lista de cadenas sin duplicados."""
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_str(item)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _normalize_seniority(value: Any) -> str:
    """Restringe la seniority a uno de los valores admitidos."""
    text = _clean_str(value).lower()
    aliases = {
        "junior": "junior",
        "jr": "junior",
        "trainee": "junior",
        "mid": "mid",
        "intermedio": "mid",
        "medio": "mid",
        "semi senior": "mid",
        "semi-senior": "mid",
        "senior": "senior",
        "sr": "senior",
        "lead": "lead",
        "líder": "lead",
        "lider": "lead",
    }
    if text in ("junior", "mid", "senior", "lead"):
        return text
    return aliases.get(text, "junior")


def _extract_routed_via(response: Any) -> str:
    """Obtiene el header X-Routed-Via de la respuesta del router."""
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("x-routed-via")
            if value:
                return value
        except Exception:  # noqa: BLE001
            pass
    raw = getattr(response, "_response", None)
    if raw is not None and getattr(raw, "headers", None):
        return raw.headers.get("x-routed-via", "") or ""
    return ""
