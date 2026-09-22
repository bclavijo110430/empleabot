"""Llenado del formulario de Easy Apply de LinkedIn.

Estrategia por cada paso del formulario:
1. Selecciona o sube el CV (radio con ``aria-label`` == PDF, o file chooser).
2. Extrae todas las preguntas del paso (texto, selects, radios) con sus
   opciones, omitiendo lo ya rellenado por LinkedIn.
3. Envía el lote de preguntas al LLM junto con el perfil y el contexto del CV.
4. Aplica las respuestas como texto o selección de opción.
5. Avanza (Siguiente/Revisar) hasta enviar la solicitud.

La interfaz de LinkedIn usa clases CSS con hashes y modales sin
``role="dialog"``, por eso el formulario se detecta por su contenido.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from src.ai_handler import AIHandler
from src.job_parser import Job
from src.utils import logger, random_pause, take_screenshot

# Mapeo de palabras clave del label -> clave en profile.json (fallback sin LLM)
PROFILE_FIELD_MAP: list[tuple[tuple[str, ...], str]] = [
    (("phone", "telefono", "teléfono", "mobile", "móvil"), "phone"),
    (("email", "correo", "e-mail"), "email"),
    (("first name", "primer nombre", "nombre"), "first_name"),
    (("last name", "apellido", "surname"), "last_name"),
    (("city", "ciudad", "localidad"), "city"),
    (("salary", "salario", "expectativa", "remuneración", "remuneracion"), "expected_salary"),
    (("years of experience", "años de experiencia", "anos de experiencia"), "years_of_experience"),
    (("linkedin",), "linkedin_url"),
    (("github",), "github_url"),
    (("website", "portfolio", "página web", "pagina web"), "website"),
]

SUBMIT_KEYWORDS = ("submit application", "enviar solicitud", "enviar candidatura")
REVIEW_KEYWORDS = ("review", "revisar")
NEXT_KEYWORDS = ("continue to next step", "next", "continuar", "siguiente")
UPLOAD_KEYWORDS = (
    "cargar currículum",
    "cargar curriculum",
    "upload resume",
    "cargar cv",
    "subir currículum",
    "subir resume",
)
APPLY_KEYWORDS = ("easy apply", "solicitud sencilla")
CONSENT_KEYWORDS = ("agree", "consent", "acepto", "confirmo", "terms", "autorizo")
SUCCESS_PHRASES = (
    "solicitud enviada",
    "tu solicitud se envió",
    "solicitud se envió",
    "candidatura enviada",
    "tu candidatura se envió",
    "application was sent",
    "your application was sent",
    "application sent",
    "ya te has inscrito",
    "already applied",
)
ERROR_PHRASES = ("no se pudo enviar", "something went wrong", "se produjo un error", "no pudimos enviar")
ALREADY_APPLIED_PHRASES = (
    "empleo solicitado",
    "currículum enviado",
    "curriculum enviado",
    "solicitud enviada",
    "candidatura enviada",
    "ya te has inscrito",
    "already applied",
    "application submitted",
    "you applied",
)
RESUME_FILE_RE = re.compile(r"\.(pdf|docx?|rtf)$", re.IGNORECASE)

# Cache del perfil propio de LinkedIn (se resuelve una vez por ejecución).
_SELF_LINKEDIN_URL = ""


class EasyApplyFormFiller:
    """Completa y envía una solicitud Easy Apply."""

    def __init__(
        self,
        page: Any,
        settings: Any,
        profile: dict,
        ai: AIHandler,
        cv_meta: dict | None = None,
    ) -> None:
        self.page = page
        self.settings = settings
        self.profile = profile
        self.ai = ai
        self.cv_context = self._build_cv_context(cv_meta)
        self.routed_via = ""
        self.self_linkedin_url = ""

    @staticmethod
    def _build_cv_context(cv_meta: dict | None) -> str:
        """Resume los metadatos del CV elegido para dárselos al LLM."""
        if not cv_meta:
            return ""
        parts = [f"Archivo: {cv_meta.get('file', '')}"]
        for key in ("title", "seniority", "summary"):
            if cv_meta.get(key):
                parts.append(f"{key}: {cv_meta[key]}")
        skills = cv_meta.get("skills")
        if skills:
            parts.append("skills: " + ", ".join(str(skill) for skill in skills))
        tags = cv_meta.get("tags")
        if tags:
            parts.append("tags: " + ", ".join(str(tag) for tag in tags))
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    async def apply(self, job: Job, cv_path: Path | None) -> tuple[bool, str]:
        """Devuelve (éxito, mensaje_de_error)."""
        if await self._already_applied():
            return False, "ya postulado previamente"

        # Si el perfil no trae LinkedIn, usamos el del usuario logueado para
        # poder responder el campo obligatorio "LinkedIn Profile".
        if not self.profile.get("linkedin_url"):
            self.self_linkedin_url = await self._fetch_self_linkedin_url(job.url)
            if self.self_linkedin_url:
                self.profile["linkedin_url"] = self.self_linkedin_url

        if not await self._open_modal():
            return False, "no se pudo abrir el formulario de Easy Apply"

        container = await self._find_form_container()
        last_label: str | None = None
        stuck = 0
        for step in range(1, self.settings.max_form_steps + 1):
            await random_pause(self.settings)

            await self._handle_resume(cv_path)
            await self._fill_current_step(container, job)

            label = await self._find_progress_button_label(container)
            if label is None:
                await take_screenshot(self.page, f"sin-boton-paso-{step}")
                return False, f"no se encontró botón de avance en el paso {step}"

            if label == last_label:
                stuck += 1
            else:
                stuck = 0
                last_label = label
            if stuck >= 4:
                await take_screenshot(self.page, f"paso-bloqueado-{step}")
                return False, f"el formulario no avanza (paso '{label}')"

            action = _classify_button(label)
            button = await self._find_progress_button(container)
            if button is None:
                return False, "el botón de avance desapareció"
            if not await self._safe_click(button, timeout=self.settings.action_timeout_ms):
                return False, f"no se pudo pulsar '{label}' (bloqueado por un overlay)"
            logger.info("Paso %d (%s)", step, label.split("|")[0])
            await random_pause(self.settings)

            if action == "submit":
                if await self._confirm_success():
                    self.routed_via = getattr(self.ai, "last_routed_via", "")
                    return True, ""
                return False, "no se confirmó el envío de la solicitud"

            if not await self._form_present():
                if await self._confirm_success():
                    self.routed_via = getattr(self.ai, "last_routed_via", "")
                    return True, ""
                return False, "el formulario se cerró antes de enviar"

            container = await self._find_form_container() or container

        return False, "se superó el número máximo de pasos del formulario"

    # ------------------------------------------------------------------ #
    # Extracción y respuesta de preguntas
    # ------------------------------------------------------------------ #
    async def _fill_current_step(self, container: Any, job: Job) -> None:
        """Extrae las preguntas del paso, las responde con el LLM y las aplica."""
        scope = container or self.page
        await self._auto_check_consents(scope)

        questions, targets = await self._collect_questions(scope)
        if not questions:
            return
        logger.info("Preguntas detectadas en el paso: %d", len(questions))
        for question in questions:
            logger.debug("  [%s] %s", question["type"], question["question"][:80])

        answers = self.ai.answer_questions_batch(
            questions, self.profile, job.to_context(), self.cv_context
        )
        if not answers:
            logger.warning("El lote no obtuvo respuesta; se resolverán una a una.")
            answers = await self._answer_individually(questions, job)

        for index, question in enumerate(questions):
            target = targets[index]
            answer = answers.get(str(index)) or answers.get(index) or ""
            if not answer:
                answer = self._profile_answer(question["question"]) or ""
            if not answer and question.get("options"):
                answer = await self._choose_option(question["options"], question["question"], job) or ""
            if not answer:
                logger.debug("Sin respuesta para: %s", question["question"][:60])
                continue
            await self._apply_answer(target, question, answer)

    async def _answer_individually(self, questions: list[dict], job: Job) -> dict[str, str]:
        """Fallback: consulta al LLM pregunta a pregunta."""
        result: dict[str, str] = {}
        for question in questions:
            answer = self.ai.answer_question(
                question["question"],
                self.profile,
                job.to_context(),
                field_type=question["type"],
                cv_context=self.cv_context,
            )
            if answer:
                result[str(question["id"])] = answer
        return result

    async def _collect_questions(self, scope: Any) -> tuple[list[dict], list[dict]]:
        """Recolecta preguntas (texto/select/radio) y cómo aplicarlas."""
        questions: list[dict] = []
        targets: list[dict] = []

        # 1. Grupos de radios (incluye [role='radiogroup'] y radios por name).
        for question_text, options in await self._collect_radio_groups(scope):
            questions.append(
                {
                    "id": len(questions),
                    "question": question_text,
                    "type": "radio",
                    "options": [label for _, label in options],
                }
            )
            targets.append({"kind": "radio", "options": options})

        # 1b. Grupos de checkboxes (selección múltiple).
        for question_text, options in await self._collect_checkbox_groups(scope):
            questions.append(
                {
                    "id": len(questions),
                    "question": question_text,
                    "type": "checkbox",
                    "options": [label for _, label in options],
                }
            )
            targets.append({"kind": "checkbox", "options": options})

        # 2. Campos de texto, número, textarea y select.
        fields = await scope.query_selector_all("input, select, textarea")
        for field in fields:
            try:
                input_type = (await field.get_attribute("type") or "text").lower()
                if input_type in ("file", "hidden", "submit", "button", "image", "radio", "checkbox"):
                    continue
                if not await field.is_visible() or not await field.is_enabled():
                    continue

                tag = (await field.evaluate("el => el.tagName.toLowerCase()")) or ""
                label = await self._field_label(field)

                if tag == "select":
                    current = (await field.input_value() or "").strip()
                    if current and not _is_placeholder_option(current):
                        continue  # LinkedIn ya lo seleccionó
                    raw_options = await field.evaluate(
                        "el => Array.from(el.options).map(o => o.text.trim()).filter(Boolean)"
                    )
                    options = [opt for opt in raw_options if not _is_placeholder_option(opt)]
                    if not options:
                        continue
                    questions.append(
                        {
                            "id": len(questions),
                            "question": label or "Selecciona una opción",
                            "type": "select",
                            "options": options,
                        }
                    )
                    targets.append({"kind": "select", "handle": field, "options": options})
                    continue

                try:
                    current = await field.input_value()
                except Exception:  # noqa: BLE001
                    current = ""
                if current and current.strip():
                    continue  # LinkedIn ya lo rellenó
                if not label.strip():
                    continue
                numeric = input_type == "number" or (
                    tag != "textarea" and _looks_numeric_question(label)
                )
                questions.append(
                    {
                        "id": len(questions),
                        "question": label,
                        "type": "number" if numeric else ("textarea" if tag == "textarea" else "text"),
                        "options": [],
                    }
                )
                targets.append(
                    {"kind": "text", "handle": field, "input_type": input_type, "numeric": numeric}
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Error recolectando un campo: %s", exc)

        return questions, targets

    async def _collect_radio_groups(self, scope: Any) -> list[tuple[str, list[tuple[Any, str]]]]:
        """Devuelve grupos de radios como (pregunta, [(handle, etiqueta), ...])."""
        groups: list[tuple[str, list[tuple[Any, str]]]] = []

        # Grupos nativos de LinkedIn.
        radiogroups = await scope.query_selector_all("[role='radiogroup']")
        for group in radiogroups:
            options = await self._radio_options(group)
            built = await self._build_radio_group(options)
            if built:
                groups.append(built)

        # Radios fuera de un radiogroup, agrupados por nombre.
        by_name: dict[str, list[Any]] = {}
        for radio in await scope.query_selector_all("input[type='radio'][name]"):
            try:
                if await radio.evaluate("el => !!el.closest(\"[role='radiogroup']\")"):
                    continue
                name = await radio.get_attribute("name") or ""
                by_name.setdefault(name, []).append(radio)
            except Exception:  # noqa: BLE001
                continue
        for name, radios in by_name.items():
            built = await self._build_radio_group(radios)
            if built:
                groups.append(built)

        return groups

    async def _radio_options(self, group: Any) -> list[Any]:
        options = await group.query_selector_all("[role='radio']")
        return options or await group.query_selector_all("input[type='radio']")

    async def _build_radio_group(
        self, elements: list[Any]
    ) -> tuple[str, list[tuple[Any, str]]] | None:
        if not elements:
            return None
        options: list[tuple[Any, str]] = []
        seen: set[str] = set()
        for element in elements:
            is_role_radio = await element.evaluate("el => el.getAttribute('role') === 'radio'")
            own_text = ((await element.inner_text()) or "").strip() if is_role_radio else ""
            aria = (await element.get_attribute("aria-label")) or ""
            label = own_text or aria.strip() or (await self._field_label(element)).strip()
            if not label or label in seen:
                continue
            seen.add(label)
            options.append((element, label))

        if not options:
            return None
        if any(RESUME_FILE_RE.search(label) for _, label in options):
            return None  # selección de CV, se gestiona aparte
        question = await self._radio_question(elements[0]) or "Selecciona una opción"
        return question, options

    async def _collect_checkbox_groups(
        self, scope: Any
    ) -> list[tuple[str, list[tuple[Any, str]]]]:
        """Devuelve grupos de checkboxes (selección múltiple) por nombre."""
        groups: list[tuple[str, list[tuple[Any, str]]]] = []
        by_name: dict[str, list[Any]] = {}
        for box in await scope.query_selector_all("input[type='checkbox'][name]"):
            try:
                if not await box.is_visible() or not await box.is_enabled():
                    continue
                name = await box.get_attribute("name") or ""
                by_name.setdefault(name, []).append(box)
            except Exception:  # noqa: BLE001
                continue

        for boxes in by_name.values():
            built = await self._build_checkbox_group(boxes)
            if built:
                groups.append(built)
        return groups

    async def _build_checkbox_group(
        self, elements: list[Any]
    ) -> tuple[str, list[tuple[Any, str]]] | None:
        options: list[tuple[Any, str]] = []
        seen: set[str] = set()
        for element in elements:
            label = ((await self._field_label(element)) or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            options.append((element, label))
        if not options:
            return None

        question = await self._checkbox_question(elements[0]) or ""
        if any(word in question.lower() for word in CONSENT_KEYWORDS):
            return None  # consentimientos se marcan automáticamente
        return question or "Selecciona las opciones que apliquen", options

    async def _checkbox_question(self, element: Any) -> str:
        """Obtiene el enunciado del grupo de checkboxes."""
        try:
            return (
                await element.evaluate(
                    """
                    (el) => {
                        const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 300);
                        const group = el.closest("[role='group'], [role='radiogroup'], fieldset");
                        if (group) {
                            const aria = group.getAttribute('aria-label');
                            if (aria) return clean(aria);
                            const labelledBy = group.getAttribute('aria-labelledby');
                            if (labelledBy) {
                                const ref = document.getElementById(labelledBy);
                                if (ref) return clean(ref.innerText);
                            }
                            const legend = group.querySelector('legend');
                            if (legend) return clean(legend.innerText);
                        }
                        let node = el.parentElement;
                        for (let i = 0; i < 6 && node; i += 1) {
                            const text = (node.innerText || '').trim();
                            if (text.length > 3 && text.length < 300) return clean(text);
                            node = node.parentElement;
                        }
                        return '';
                    }
                    """
                )
                or ""
            )
        except Exception:  # noqa: BLE001
            return ""

    async def _apply_answer(self, target: dict, question: dict, answer: str) -> None:
        """Aplica una respuesta como texto o selección según el tipo."""
        kind = target.get("kind")
        try:
            if kind == "text":
                value = answer
                if target.get("numeric") or target.get("input_type") == "number" or _looks_numeric(answer):
                    value = _extract_number(answer) or answer
                await target["handle"].fill(str(value))
            elif kind == "select":
                option = _best_option(answer, target["options"])
                if option is None:
                    logger.debug("Respuesta '%s' sin opción válida para el select.", answer)
                    return
                try:
                    await target["handle"].select_option(label=option)
                except Exception:  # noqa: BLE001
                    await target["handle"].select_option(value=option)
            elif kind == "radio":
                labels = [label for _, label in target["options"]]
                option = _best_option(answer, labels)
                handle = next((element for element, label in target["options"] if label == option), None)
                if handle is not None:
                    await self._click_element(handle)
                else:
                    logger.debug("Respuesta '%s' sin opción válida para el radio.", answer)
            elif kind == "checkbox":
                labels = [label for _, label in target["options"]]
                wanted = [part.strip() for part in answer.split(",") if part.strip()]
                matched = False
                for want in wanted:
                    option = _best_option(want, labels)
                    handle = next(
                        (element for element, label in target["options"] if label == option), None
                    )
                    if handle is not None:
                        await self._click_element(handle)
                        matched = True
                if not matched:
                    none_option = _best_option(
                        "none of the above", labels
                    ) or _best_option("ninguna de las anteriores", labels)
                    handle = next(
                        (element for element, label in target["options"] if label == none_option),
                        None,
                    )
                    if handle is not None:
                        await self._click_element(handle)
                    else:
                        logger.debug("Respuesta '%s' sin opción válida para el checkbox.", answer)
            logger.debug("[%s] '%s' -> '%s'", kind, question["question"][:50], str(answer)[:50])
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo aplicar '%s': %s", answer, exc)

    async def _auto_check_consents(self, scope: Any) -> None:
        for checkbox in await scope.query_selector_all("input[type='checkbox']"):
            try:
                if await checkbox.is_checked():
                    continue
                label = (await self._field_label(checkbox)).lower()
                if any(word in label for word in CONSENT_KEYWORDS):
                    await self._click_element(checkbox)
            except Exception:  # noqa: BLE001
                continue

    # ------------------------------------------------------------------ #
    # CV
    # ------------------------------------------------------------------ #
    async def _handle_resume(self, cv_path: Path | None) -> None:
        """Selecciona el CV ya subido o lo sube con el file chooser."""
        if cv_path is None:
            return
        name = cv_path.name

        existing = await self._find_resume_option(name)
        if existing is not None:
            checked = (await existing.get_attribute("aria-checked") or "").lower()
            if checked != "true":
                await self._click_element(existing)
            logger.info("CV seleccionado en el formulario: %s", name)
            return

        upload = await self._find_upload_button()
        if upload is not None:
            try:
                async with self.page.expect_file_chooser(timeout=10000) as chooser_info:
                    await upload.click()
                chooser = await chooser_info.value
                await chooser.set_files(str(cv_path))
                await asyncio.sleep(4)
                logger.info("CV subido en el formulario: %s", name)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo subir el CV con file chooser: %s", exc)

        file_input = await self.page.query_selector("input[type='file']")
        if file_input is not None:
            await self._assign_file(file_input, cv_path)

    async def _find_resume_option(self, filename: str) -> Any | None:
        handle = await self.page.evaluate_handle(
            """
            (name) => {
                const target = (name || '').toLowerCase();
                const radios = [...document.querySelectorAll("[role='radio'], input[type='radio']")];
                return radios.find((r) => (r.getAttribute('aria-label') || '').toLowerCase() === target) || null;
            }
            """,
            filename,
        )
        return handle.as_element()

    async def _find_upload_button(self) -> Any | None:
        handle = await self.page.evaluate_handle(
            """
            (words) => {
                const nodes = [...document.querySelectorAll('button, a, label')]
                    .filter((n) => n.offsetWidth || n.offsetHeight);
                return nodes.find((n) => {
                    const text = (n.innerText || '').toLowerCase();
                    return words.some((w) => text.includes(w));
                }) || null;
            }
            """,
            list(UPLOAD_KEYWORDS),
        )
        return handle.as_element()

    async def _assign_file(self, field: Any, cv_path: Path) -> None:
        try:
            existing = await field.evaluate("el => (el.files ? el.files.length : 0)")
            if existing:
                return
            await field.set_input_files(str(cv_path))
            logger.info("CV asignado por input file: %s", cv_path.name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo asignar el input file: %s", exc)

    # ------------------------------------------------------------------ #
    # Apertura y detección del formulario
    # ------------------------------------------------------------------ #
    async def _find_apply_button(self) -> Any | None:
        handle = await self.page.evaluate_handle(
            """
            (words) => {
                const nodes = [...document.querySelectorAll('button, a')]
                    .filter((n) => n.offsetWidth || n.offsetHeight);
                const matches = (n) => {
                    const text = ((n.innerText || '') + ' ' + (n.getAttribute('aria-label') || '')).toLowerCase();
                    return words.some((w) => text.includes(w));
                };
                const link = nodes.find((n) => (n.getAttribute('href') || '').includes('/apply/') && matches(n));
                return link || nodes.find(matches) || null;
            }
            """,
            list(APPLY_KEYWORDS),
        )
        return handle.as_element()

    async def _fetch_self_linkedin_url(self, return_url: str = "") -> str:
        """Obtiene la URL del perfil del usuario logueado (vía ``/in/me/``)."""
        global _SELF_LINKEDIN_URL
        if _SELF_LINKEDIN_URL:
            return _SELF_LINKEDIN_URL
        try:
            await self.page.goto(
                "https://www.linkedin.com/in/me/", wait_until="domcontentloaded"
            )
            await asyncio.sleep(2)
            current = (self.page.url or "").split("?")[0]
            match = re.search(r"/in/([^/]+)/?$", current)
            if match and match.group(1) != "me":
                _SELF_LINKEDIN_URL = current
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo obtener el perfil propio: %s", exc)
        finally:
            if return_url:
                try:
                    await self.page.goto(return_url, wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                except Exception:  # noqa: BLE001
                    pass
        return _SELF_LINKEDIN_URL

    async def _already_applied(self) -> bool:
        """Detecta si la oferta ya fue solicitada (p. ej. en el sitio externo)."""
        try:
            content = (await self.page.inner_text("body")).lower()
        except Exception:  # noqa: BLE001
            return False
        return any(phrase in content for phrase in ALREADY_APPLIED_PHRASES)

    async def _open_modal(self) -> bool:
        button = await self._find_apply_button()
        # LinkedIn expone el nuevo flujo SDUI como un enlace a /apply/; pulsarlo
        # resulta poco fiable (el overlay #interop-outlet lo intercepta), así que
        # navegamos directamente a esa URL, que abre el modal de postulación.
        # Si el botón no expone el enlace, derivamos la URL desde la oferta.
        href = await self._apply_href(button) if button is not None else ""
        if not href:
            href = await self._derive_apply_url()
        if href:
            # Algunas ofertas muestran primero un "recordatorio de seguridad":
            # al confirmarlo se cierra sin abrir el formulario, así que hay que
            # reintentar la navegación a la URL de postulación.
            for attempt in range(1, 4):
                logger.info(
                    "Abriendo el flujo de postulación (intento %d): %s", attempt, href
                )
                try:
                    await self.page.goto(href, wait_until="domcontentloaded")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("No se pudo navegar al flujo de postulación: %s", exc)
                if await self._wait_for_form(15):
                    return True

        if button is None:
            logger.warning("No se encontró el botón de Easy Apply en la oferta.")
            return False

        await self._remove_interop_overlay()
        for attempt in range(1, 4):
            if not await self._safe_click(button):
                logger.warning("No se pudo pulsar Easy Apply (intento %d).", attempt)
                await asyncio.sleep(1)
                continue
            if await self._wait_for_form(15):
                return True
            logger.warning("El formulario no apareció tras el intento %d.", attempt)
        return False

    async def _derive_apply_url(self) -> str:
        """Construye la URL de postulación a partir de la URL de la oferta."""
        url = (self.page.url or "").split("?")[0]
        match = re.search(r"/jobs/view/(\d+)", url)
        if not match:
            return ""
        return (
            f"https://www.linkedin.com/jobs/view/{match.group(1)}"
            "/apply/?openSDUIApplyFlow=true"
        )

    async def _wait_for_form(self, attempts: int = 20) -> bool:
        """Espera al formulario, confirmando diálogos intermedios de seguridad."""
        for _ in range(attempts):
            await asyncio.sleep(1)
            if await self._form_present():
                return True
            await self._dismiss_safety_dialog()
        return False

    async def _dismiss_safety_dialog(self) -> bool:
        """Pulsa "Continuar la solicitud" en el recordatorio de seguridad."""
        try:
            handle = await self.page.evaluate_handle(
                """
                () => {
                    const words = ['continuar la solicitud', 'continue to application',
                                   'continue application', 'continuar con la solicitud'];
                    const nodes = [...document.querySelectorAll('button, a')]
                        .filter((n) => n.offsetWidth || n.offsetHeight);
                    return nodes.find((n) => {
                        const text = ((n.innerText || '') + ' ' + (n.getAttribute('aria-label') || '')).toLowerCase();
                        return words.some((w) => text.includes(w));
                    }) || null;
                }
                """
            )
            element = handle.as_element()
            if element is None:
                return False
            logger.info("Confirmando el recordatorio de seguridad de LinkedIn.")
            return await self._safe_click(element)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _apply_href(button: Any) -> str:
        """Devuelve la URL de postulación del botón si es un enlace de Easy Apply."""
        try:
            href = await button.get_attribute("href")
        except Exception:  # noqa: BLE001
            return ""
        if not href or "/apply/" not in href:
            return ""
        if href.startswith("/"):
            return f"https://www.linkedin.com{href}"
        return href

    async def _form_present(self) -> bool:
        """Detecta si el modal de Easy Apply está abierto.

        Se exige un diálogo visible que contenga un campo de formulario y un
        botón de avance (o un input de archivo), para no confundirlo con
        botones "Siguiente" de carruseles de la propia página de la oferta.
        """
        try:
            return bool(
                await self.page.evaluate(
                    """
                    () => {
                        const progressWords = ['enviar solicitud', 'submit application', 'siguiente', 'next',
                                               'revisar', 'review', 'continuar', 'continue',
                                               'cargar currículum', 'upload resume'];
                        const hasProgress = (root) => [...root.querySelectorAll('button')].some((b) => {
                            const text = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).toLowerCase();
                            return progressWords.some((w) => text.includes(w));
                        });
                        const hasField = (root) => !!root.querySelector(
                            "input:not([type='hidden']):not([type='checkbox']), select, textarea, [role='radiogroup'], input[type='file']"
                        );
                        const dialogs = [...document.querySelectorAll(".artdeco-modal, [role='dialog']")]
                            .filter((d) => d.offsetWidth || d.offsetHeight);
                        return dialogs.some((d) => (hasField(d) && hasProgress(d)) || !!d.querySelector("input[type='file'], [role='radiogroup']"));
                    }
                    """
                )
            )
        except Exception:  # noqa: BLE001
            return False

    async def _find_form_container(self) -> Any | None:
        """Devuelve el contenedor del formulario (sin depender de clases)."""
        try:
            handle = await self.page.evaluate_handle(
                """
                () => {
                    const progressWords = ['enviar solicitud', 'submit application', 'revisar', 'review',
                                           'siguiente', 'next', 'continuar', 'continue'];
                    const progress = [...document.querySelectorAll('button')].find((b) => {
                        const text = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).toLowerCase();
                        return progressWords.some((w) => text.includes(w));
                    });
                    if (!progress) return null;

                    // Preferir el modal completo: contiene todos los campos del
                    // paso, incluidos los que quedan por encima del botón.
                    const dialog = progress.closest(".artdeco-modal, [role='dialog']");
                    if (dialog) return dialog;

                    const fieldSelector = "input:not([type='hidden']), select, textarea, [role='radiogroup'], [role='radio']";
                    let node = progress;
                    for (let i = 0; i < 15 && node; i += 1) {
                        if (node.tagName === 'BODY') break;
                        if (node !== progress && node.querySelector && node.querySelector(fieldSelector)) return node;
                        node = node.parentElement;
                    }
                    return progress.parentElement;
                }
                """
            )
            return handle.as_element()
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ #
    # Respuestas auxiliares
    # ------------------------------------------------------------------ #
    def _profile_answer(self, label: str) -> str | None:
        lowered = (label or "").lower()
        for keywords, key in PROFILE_FIELD_MAP:
            if any(keyword in lowered for keyword in keywords):
                value = self.profile.get(key)
                if value not in (None, "", []):
                    return str(value)
        default_answers = self.profile.get("default_answers", {})
        for key, value in default_answers.items():
            if key.replace("_", " ") in lowered:
                return str(value)
        return None

    async def _choose_option(self, options: list[str], question: str, job: Job) -> str | None:
        if not options:
            return None
        prompt = (
            f"Pregunta: {question}\n\n"
            "Opciones válidas (elige EXACTAMENTE una, sin añadir nada más):\n"
            + "\n".join(f"- {option}" for option in options)
        )
        answer = self.ai.answer_question(
            prompt, self.profile, job.to_context(), field_type="choice", cv_context=self.cv_context
        )
        option = _best_option(answer, options)
        if option:
            return option
        for candidate in options:
            if candidate.strip().lower() in ("no", "n/a", "none"):
                return candidate
        return options[0]

    async def _field_label(self, field: Any) -> str:
        try:
            return (
                await field.evaluate(
                    """
                    (el) => {
                        const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 300);
                        if (el.id) {
                            const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                            if (label) return clean(label.innerText);
                        }
                        const aria = el.getAttribute('aria-label');
                        if (aria) return clean(aria);
                        const labelledBy = el.getAttribute('aria-labelledby');
                        if (labelledBy) {
                            const ref = document.getElementById(labelledBy);
                            if (ref) return clean(ref.innerText);
                        }
                        const fieldset = el.closest('fieldset');
                        if (fieldset) {
                            const legend = fieldset.querySelector('legend');
                            if (legend) return clean(legend.innerText);
                        }
                        const label = el.closest('label');
                        if (label) return clean(label.innerText);
                        return clean(el.getAttribute('placeholder') || el.name || '');
                    }
                    """
                )
                or ""
            )
        except Exception:  # noqa: BLE001
            return ""

    async def _radio_question(self, element: Any) -> str:
        try:
            return (
                await element.evaluate(
                    """
                    (el) => {
                        const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 300);
                        const ownAria = el.getAttribute('aria-label');
                        if (ownAria) return clean(ownAria);
                        const group = el.closest("[role='radiogroup']");
                        if (group) {
                            const groupAria = group.getAttribute('aria-label');
                            if (groupAria) return clean(groupAria);
                            const labelledBy = group.getAttribute('aria-labelledby');
                            if (labelledBy) {
                                const ref = document.getElementById(labelledBy);
                                if (ref) return clean(ref.innerText);
                            }
                        }
                        const fieldset = el.closest('fieldset');
                        if (fieldset) {
                            const legend = fieldset.querySelector('legend');
                            if (legend) return clean(legend.innerText);
                        }
                        let node = el.parentElement;
                        for (let i = 0; i < 5 && node; i += 1) {
                            const text = (node.innerText || '').trim();
                            if (text.length > 3 && text.length < 300) return clean(text);
                            node = node.parentElement;
                        }
                        return '';
                    }
                    """
                )
                or ""
            )
        except Exception:  # noqa: BLE001
            return ""

    async def _click_element(self, element: Any) -> bool:
        """Hace clic en el elemento o en su label contenedor si está oculto."""
        if await self._safe_click(element, timeout=3000):
            return True
        try:
            handle = await element.evaluate_handle("el => el.closest('label') || el.parentElement")
            parent = handle.as_element()
            if parent is not None:
                return await self._safe_click(parent, timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _remove_interop_overlay(self) -> None:
        """Elimina el overlay de LinkedIn que intercepta los clics."""
        try:
            await self.page.evaluate(
                """
                () => {
                    const selectors = ['#interop-outlet', '[data-testid="interop-shadowdom"]'];
                    selectors.forEach((sel) =>
                        document.querySelectorAll(sel).forEach((node) => node.remove())
                    );
                }
                """
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo limpiar el overlay interop: %s", exc)

    async def _safe_click(self, element: Any, timeout: int = 5000) -> bool:
        """Hace clic sorteando overlays y sin depender de la acción nativa.

        LinkedIn inyecta ``#interop-outlet``, que intercepta los eventos de
        puntero y provoca timeouts en ``click()``. Se intenta el clic normal,
        luego se retira el overlay y, como último recurso, se despacha un clic
        sintético directamente sobre el elemento.
        """
        try:
            await element.scroll_into_view_if_needed(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass

        try:
            await element.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            pass

        await self._remove_interop_overlay()
        try:
            await element.click(timeout=timeout, force=True)
            return True
        except Exception:  # noqa: BLE001
            pass

        for dispatch in (True, False):
            try:
                if dispatch:
                    await element.dispatch_event("click")
                else:
                    await element.evaluate("el => el.click()")
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # ------------------------------------------------------------------ #
    # Botones y confirmación
    # ------------------------------------------------------------------ #
    async def _find_progress_button(self, container: Any) -> Any | None:
        ranked = await self._rank_progress_buttons(container or self.page)
        if not ranked and container is not None:
            ranked = await self._rank_progress_buttons(self.page)
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    async def _rank_progress_buttons(self, scope: Any) -> list[tuple[int, Any]]:
        ranked: list[tuple[int, Any]] = []
        try:
            buttons = await scope.query_selector_all("button")
        except Exception:  # noqa: BLE001
            return ranked
        for button in buttons:
            try:
                if not await button.is_visible() or not await button.is_enabled():
                    continue
                text = (await button.inner_text() or "").strip()
                aria = (await button.get_attribute("aria-label") or "").strip()
                priority = _button_priority(f"{text} {aria}".lower())
                if priority:
                    ranked.append((priority, button))
            except Exception:  # noqa: BLE001
                continue
        return ranked

    async def _find_progress_button_label(self, container: Any) -> str | None:
        button = await self._find_progress_button(container)
        if button is None:
            return None
        try:
            text = (await button.inner_text() or "").strip()
            aria = (await button.get_attribute("aria-label") or "").strip()
            return f"{text} {aria}".strip()
        except Exception:  # noqa: BLE001
            return None

    async def _confirm_success(self) -> bool:
        """Comprueba que la solicitud se envió correctamente."""
        for _ in range(15):
            await asyncio.sleep(1.5)
            try:
                content = (await self.page.inner_text("body")).lower()
            except Exception:  # noqa: BLE001
                content = ""
            if any(phrase in content for phrase in SUCCESS_PHRASES):
                await self._dismiss_post_apply()
                return True
            if any(phrase in content for phrase in ERROR_PHRASES):
                return False
            if not await self._form_present():
                await self._dismiss_post_apply()
                return True
        return False

    async def _dismiss_post_apply(self) -> None:
        """Cierra el diálogo de confirmación posterior al envío."""
        await asyncio.sleep(1)
        try:
            handle = await self.page.evaluate_handle(
                """
                () => {
                    const words = ['listo', 'done', 'cerrar', 'close', 'entendido', 'got it'];
                    const nodes = [...document.querySelectorAll('button, a')]
                        .filter((n) => n.offsetWidth || n.offsetHeight);
                    return nodes.find((n) => {
                        const text = (n.innerText || '').toLowerCase().trim();
                        return words.some((w) => text === w || text.includes(w));
                    }) || null;
                }
                """
            )
            element = handle.as_element()
            if element is not None:
                await element.click()
                await asyncio.sleep(1)
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo cerrar el diálogo posterior: %s", exc)

        for selector in ("button[aria-label='Dismiss']", "button[aria-label='Cerrar']", ".artdeco-modal__dismiss"):
            try:
                element = await self.page.query_selector(selector)
                if element and await element.is_visible():
                    await element.click()
                    await asyncio.sleep(1)
                    return
            except Exception:  # noqa: BLE001
                continue


def _classify_button(label: str) -> str:
    lowered = (label or "").lower()
    if any(keyword in lowered for keyword in SUBMIT_KEYWORDS):
        return "submit"
    if any(keyword in lowered for keyword in REVIEW_KEYWORDS):
        return "review"
    return "next"


# Botones de acción sobre entradas (editar/eliminar/añadir) pueden contener
# "siguiente" en su aria-label (p. ej. "Edite la siguiente entrada de
# experiencia") y no deben confundirse con el botón de avance del formulario.
EXCLUDE_KEYWORDS = (
    "eliminar",
    "quitar",
    "borrar",
    "delete",
    "remove",
    "discard",
    "editar",
    "edit",
    "añadir",
    "agregar",
    "add",
    "crear",
    "create",
)


def _button_priority(combined: str) -> int:
    if any(keyword in combined for keyword in EXCLUDE_KEYWORDS):
        return 0
    if any(keyword in combined for keyword in SUBMIT_KEYWORDS):
        return 1
    if any(keyword in combined for keyword in REVIEW_KEYWORDS):
        return 2
    if any(keyword in combined for keyword in NEXT_KEYWORDS):
        return 3
    return 0


def _best_option(answer: str | None, options: list[str]) -> str | None:
    """Empareja una respuesta con una opción disponible (exacta o parcial)."""
    if not answer:
        return None
    normalized = answer.strip().lower()
    for option in options:
        if option.strip().lower() == normalized:
            return option
    for option in options:
        candidate = option.strip().lower()
        if candidate and (normalized in candidate or candidate in normalized):
            return option
    return None


PLACEHOLDER_OPTIONS = (
    "selecciona una opción",
    "selecciona una opcion",
    "seleccione una opción",
    "seleccione una opcion",
    "seleccionar",
    "select an option",
    "select one",
    "choose an option",
    "please select",
    "elegir",
)


NUMERIC_QUESTION_HINTS = (
    "años",
    "anos",
    "years",
    "experiencia",
    "experience",
    "salario",
    "salarial",
    "salary",
    "aspiración",
    "aspiracion",
    "expectativa",
    "remuneración",
    "remuneracion",
    "pretensión",
    "pretension",
    "sueldo",
    "compensation",
    "wage",
    "monto",
    "cantidad",
    "número",
    "numero",
    "number",
)


def _looks_numeric_question(label: str) -> bool:
    """Heurística: la pregunta espera una respuesta numérica."""
    lowered = (label or "").lower()
    return any(hint in lowered for hint in NUMERIC_QUESTION_HINTS)


def _is_placeholder_option(text: str) -> bool:
    """Indica si el texto de una opción es un placeholder (sin elegir)."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    return any(placeholder in lowered for placeholder in PLACEHOLDER_OPTIONS)


def _looks_numeric(value: str) -> bool:
    stripped = (value or "").strip()
    return bool(stripped) and stripped.replace(".", "", 1).replace(",", "", 1).isdigit()


def _extract_number(value: str) -> str | None:
    digits = "".join(char for char in value if char.isdigit())
    return digits or None
