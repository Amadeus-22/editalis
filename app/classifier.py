"""Turns a SourceItem into a Classification.

Classifiers are pure with respect to the application: no database access and
no message sending. The LLM classifier makes one API call per item and falls
back to the heuristic classifier on any API failure. Every LLM response goes
through `_sanitize()` before it is trusted.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

import anthropic

from app.domain import UFS, Area, Classification, EducationLevel, NoticeKind, SourceItem

logger = logging.getLogger(__name__)

MAX_VACANCIES = 100_000
MAX_SALARY_BRL = 200_000
MAX_ORGANIZATION_CHARS = 160


class Classifier(Protocol):
    def classify(self, item: SourceItem) -> Classification | None: ...


# ---------------------------------------------------------------------------
# Sanitization: the only path from untrusted JSON to a Classification.
# ---------------------------------------------------------------------------


def _sanitize(raw: Any) -> Classification | None:
    """Validate and normalize untrusted classifier output.

    Unknown enum values are dropped, numbers are range-checked and strings are
    trimmed. Returns None when the payload is not a JSON object at all.
    """
    if not isinstance(raw, dict):
        return None

    return Classification(
        is_public_exam=raw.get("is_public_exam") is True,
        kind=_enum_value(NoticeKind, raw.get("kind")) or NoticeKind.OTHER,
        organization=_clean_text(raw.get("organization"), MAX_ORGANIZATION_CHARS),
        ufs=tuple(sorted({uf for uf in _str_list(raw.get("ufs"), upper=True) if uf in UFS})),
        areas=_enum_tuple(Area, raw.get("areas")),
        education_levels=_enum_tuple(EducationLevel, raw.get("education_levels")),
        vacancies=_bounded_int(raw.get("vacancies"), 1, MAX_VACANCIES),
        max_salary_cents=_salary_cents(raw.get("max_salary_brl")),
        registration_deadline=_iso_date(raw.get("registration_deadline")),
    )


def _enum_value[E: StrEnum](enum: type[E], value: Any) -> E | None:
    if isinstance(value, str):
        try:
            return enum(value.strip().lower())
        except ValueError:
            return None
    return None


def _enum_tuple[E: StrEnum](enum: type[E], value: Any) -> tuple[E, ...]:
    members = {m for v in _str_list(value) if (m := _enum_value(enum, v)) is not None}
    return tuple(sorted(members, key=lambda m: m.value))


def _str_list(value: Any, *, upper: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return [v.upper() for v in items] if upper else items


def _clean_text(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _bounded_int(value: Any, low: int, high: int) -> int | None:
    if not _is_number(value) or value != int(value):
        return None
    number = int(value)
    return number if low <= number <= high else None


def _salary_cents(value: Any) -> int | None:
    if not _is_number(value) or not 0 < value <= MAX_SALARY_BRL:
        return None
    return round(value * 100)


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if 2000 <= parsed.year <= 2100 else None


# ---------------------------------------------------------------------------
# Heuristic classifier (no API key required).
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


_PUBLIC_EXAM = re.compile(r"concurso publico|processo seletivo")
_KIND_PATTERNS: tuple[tuple[NoticeKind, re.Pattern[str]], ...] = (
    (NoticeKind.RECTIFICATION, re.compile(r"retifica")),
    (NoticeKind.SUMMONS, re.compile(r"convoca")),
    (NoticeKind.RESULT, re.compile(r"homologa|resultado final")),
    (NoticeKind.OPENING, re.compile(r"abertura|edital de abertura|inscricoes")),
)
_AREA_PATTERNS: dict[Area, re.Pattern[str]] = {
    Area.HEALTH: re.compile(r"\b(saude|enfermeir|medic[oa]|hospital|farmaceutic|odontolog)"),
    Area.EDUCATION: re.compile(r"\b(professor|educacao|docente|pedagog)"),
    Area.SECURITY: re.compile(r"\b(policia|guarda municipal|bombeiro|penal|seguranca publica)"),
    Area.TAX: re.compile(r"\b(fiscal|auditor|receita|tributari)"),
    Area.LEGAL: re.compile(r"\b(procurador|juridic|advogad|tribunal|defensoria|promotor)"),
    Area.IT: re.compile(r"\b(tecnologia da informacao|informatica|analista de sistemas|ti\b)"),
    Area.ENGINEERING: re.compile(r"\b(engenheir|arquitet)"),
    Area.BANKING: re.compile(r"\b(banco do brasil|caixa economica|bancari|banrisul|bnb)"),
    Area.ADMINISTRATIVE: re.compile(r"\b(administrativ|assistente|auxiliar de|agente de)"),
}
_EDUCATION_PATTERNS: dict[EducationLevel, re.Pattern[str]] = {
    EducationLevel.HIGHER: re.compile(r"(nivel|ensino) superior|graduacao"),
    EducationLevel.TECHNICAL: re.compile(r"nivel tecnico|curso tecnico"),
    EducationLevel.HIGH_SCHOOL: re.compile(r"(nivel|ensino) medio"),
    EducationLevel.ELEMENTARY: re.compile(r"(nivel|ensino) fundamental"),
}
_VACANCIES = re.compile(r"(\d{1,3}(?:\.\d{3})*|\d+)\s+vagas")
_SALARY = re.compile(r"r\$\s?(\d{1,3}(?:\.\d{3})*,\d{2})")
_DEADLINE = re.compile(r"inscricoes.{0,120}?ate\s+(?:o dia\s+)?(\d{2})/(\d{2})/(\d{4})", re.S)
_UF_SUFFIX = re.compile(r"[/-]\s?(" + "|".join(UFS) + r")\b")


class HeuristicClassifier:
    """Keyword-based fallback. Cheap and predictable, but low recall."""

    def classify(self, item: SourceItem) -> Classification | None:
        original = f"{item.title}\n{item.content}"
        text = _normalize(original)
        if not _PUBLIC_EXAM.search(text):
            return _sanitize({"is_public_exam": False})

        kind = next((k for k, p in _KIND_PATTERNS if p.search(text)), NoticeKind.OTHER)
        ufs = set(_UF_SUFFIX.findall(original))
        if item.uf_hint:
            ufs.add(item.uf_hint)

        salaries = [float(s.replace(".", "").replace(",", ".")) for s in _SALARY.findall(text)]
        vacancies = [int(v.replace(".", "")) for v in _VACANCIES.findall(text)]
        deadline = _DEADLINE.search(text)

        return _sanitize(
            {
                "is_public_exam": True,
                "kind": kind.value,
                "organization": item.organization_hint,
                "ufs": sorted(ufs),
                "areas": [a.value for a, p in _AREA_PATTERNS.items() if p.search(text)],
                "education_levels": [
                    e.value for e, p in _EDUCATION_PATTERNS.items() if p.search(text)
                ],
                "vacancies": max(vacancies) if vacancies else None,
                "max_salary_brl": max(salaries) if salaries else None,
                "registration_deadline": (
                    f"{deadline[3]}-{deadline[2]}-{deadline[1]}" if deadline else None
                ),
            }
        )


# ---------------------------------------------------------------------------
# LLM classifier (Anthropic Messages API with structured output).
# ---------------------------------------------------------------------------


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_public_exam": {"type": "boolean"},
        "kind": {"type": "string", "enum": [k.value for k in NoticeKind]},
        "organization": _nullable({"type": "string"}),
        "ufs": {"type": "array", "items": {"type": "string", "enum": list(UFS)}},
        "areas": {"type": "array", "items": {"type": "string", "enum": [a.value for a in Area]}},
        "education_levels": {
            "type": "array",
            "items": {"type": "string", "enum": [e.value for e in EducationLevel]},
        },
        "vacancies": _nullable({"type": "integer"}),
        "max_salary_brl": _nullable({"type": "number"}),
        "registration_deadline": _nullable(
            {"type": "string", "description": "ISO date, YYYY-MM-DD"}
        ),
    },
    "required": [
        "is_public_exam",
        "kind",
        "organization",
        "ufs",
        "areas",
        "education_levels",
        "vacancies",
        "max_salary_brl",
        "registration_deadline",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You extract structured facts from Brazilian official publications (diários \
oficiais, exam board pages, RSS entries) for a service that alerts job seekers \
about public service exams (concursos públicos and processos seletivos).

Rules:
- is_public_exam is true only if the text is about a concurso público or \
processo seletivo for public positions.
- kind: "opening" for a new edital with registration, "rectification" for a \
retificação, "summons" for convocação/nomeação, "result" for results or \
homologação, otherwise "other".
- organization: the hiring body as written (e.g. "Prefeitura de Niterói"), \
or null.
- ufs: the states where the positions are located. Empty for nationwide exams.
- areas: every area that applies to the positions offered.
- education_levels: every education level required across the positions.
- vacancies: total number of vacancies, excluding cadastro de reserva, or null.
- max_salary_brl: the highest monthly base salary in reais, or null.
- registration_deadline: the last day of registration, or null.
Use null or an empty list when the text does not state a fact. Never guess."""


class LLMClassifier:
    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str,
        fallback: Classifier | None = None,
        max_chars: int = 20_000,
    ) -> None:
        self._client = client
        self._model = model
        self._fallback = fallback or HeuristicClassifier()
        self._max_chars = max_chars

    def classify(self, item: SourceItem) -> Classification | None:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._render(item)}],
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            )
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
            logger.warning("LLM classification failed for %s: %s", item.external_id, exc)
            return self._fallback.classify(item)

        if response.stop_reason != "end_turn":
            logger.warning(
                "LLM stopped with %s for %s; using fallback", response.stop_reason, item.external_id
            )
            return self._fallback.classify(item)

        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLM returned invalid JSON for %s", item.external_id)
            return self._fallback.classify(item)
        return _sanitize(raw)

    def _render(self, item: SourceItem) -> str:
        content = item.content
        if len(content) > self._max_chars:
            logger.info(
                "Truncating %s from %d to %d chars", item.external_id, len(content), self._max_chars
            )
            content = content[: self._max_chars]
        hints = []
        if item.uf_hint:
            hints.append(f"Publication state: {item.uf_hint}")
        if item.organization_hint:
            hints.append(f"Publisher: {item.organization_hint}")
        header = "\n".join([f"Title: {item.title}", f"URL: {item.url}", *hints])
        return f"{header}\n\n<document>\n{content}\n</document>"


def build_classifier(api_key: str, model: str, max_chars: int = 20_000) -> Classifier:
    if not api_key:
        logger.info("ANTHROPIC_API_KEY not set; using heuristic classifier")
        return HeuristicClassifier()
    return LLMClassifier(anthropic.Anthropic(api_key=api_key), model, max_chars=max_chars)
