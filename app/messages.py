"""Renders the WhatsApp alert text.

The text is user-facing and therefore in Brazilian Portuguese. Keep it factual:
no sales copy, no emoji spam, at most MAX_CHARS characters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.domain import Area, Classification, EducationLevel, NoticeKind

MAX_CHARS = 600
OPT_OUT_FOOTER = "Responda SAIR para não receber mais alertas."

KIND_LABELS: dict[NoticeKind, str] = {
    NoticeKind.OPENING: "Edital aberto",
    NoticeKind.RECTIFICATION: "Retificação",
    NoticeKind.SUMMONS: "Convocação",
    NoticeKind.RESULT: "Resultado",
    NoticeKind.OTHER: "Publicação",
}

EDUCATION_LABELS: dict[EducationLevel, str] = {
    EducationLevel.ELEMENTARY: "fundamental",
    EducationLevel.HIGH_SCHOOL: "médio",
    EducationLevel.TECHNICAL: "técnico",
    EducationLevel.HIGHER: "superior",
}


def format_brl(cents: int) -> str:
    formatted = f"{cents / 100:,.2f}"
    return "R$ " + formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def pick_affiliate_link(areas: Iterable[Area], links: Mapping[str, str]) -> str | None:
    """Return a course link only when one exists for an area of the notice."""
    return next((links[a.value] for a in areas if a.value in links), None)


def format_alert(notice: Classification, url: str, affiliate_link: str | None = None) -> str:
    organization = notice.organization or "Órgão não identificado"
    text = _render(notice, organization, url, affiliate_link)
    overflow = len(text) - MAX_CHARS
    if overflow > 0:
        keep = max(len(organization) - overflow - 1, 20)
        text = _render(notice, organization[:keep].rstrip() + "…", url, affiliate_link)
    if len(text) > MAX_CHARS and affiliate_link:
        text = _render(notice, organization, url, None)
    return text


def _render(notice: Classification, organization: str, url: str, affiliate_link: str | None) -> str:
    location = ", ".join(notice.ufs) if notice.ufs else "Nacional"
    lines = [f"*{KIND_LABELS[notice.kind]}*: {organization} ({location})"]

    details = []
    if notice.vacancies:
        details.append(f"Vagas: {notice.vacancies}")
    if notice.max_salary_cents:
        details.append(f"Salário: até {format_brl(notice.max_salary_cents)}")
    if details:
        lines.append(" | ".join(details))
    if notice.education_levels:
        levels = ", ".join(EDUCATION_LABELS[e] for e in notice.education_levels)
        lines.append(f"Escolaridade: {levels}")
    if notice.registration_deadline:
        lines.append(f"Inscrições até {notice.registration_deadline:%d/%m/%Y}")
    lines.append(url)

    footer = []
    if affiliate_link:
        footer.append(f"Curso preparatório: {affiliate_link}")
    footer.append(OPT_OUT_FOOTER)
    return "\n".join(lines) + "\n\n" + "\n".join(footer)
