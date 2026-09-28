"""Parse free-text profile messages such as "RJ, saúde, superior, acima de 5 mil".

Pure functions only: the webhook decides what to do with the parsed result.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.domain import EDUCATION_RANK, UFS, Area, EducationLevel

STATE_NAMES: dict[str, str] = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES", "goias": "GO",
    "maranhao": "MA", "mato grosso do sul": "MS", "mato grosso": "MT",
    "minas gerais": "MG", "para": "PA", "paraiba": "PB", "parana": "PR",
    "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO",
    "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE",
    "tocantins": "TO",
}  # fmt: skip

AREA_KEYWORDS: dict[str, Area] = {
    "saude": Area.HEALTH,
    "educacao": Area.EDUCATION,
    "professor": Area.EDUCATION,
    "seguranca": Area.SECURITY,
    "policial": Area.SECURITY,
    "policia": Area.SECURITY,
    "fiscal": Area.TAX,
    "tributaria": Area.TAX,
    "juridica": Area.LEGAL,
    "direito": Area.LEGAL,
    "ti": Area.IT,
    "tecnologia": Area.IT,
    "informatica": Area.IT,
    "engenharia": Area.ENGINEERING,
    "bancaria": Area.BANKING,
    "banco": Area.BANKING,
    "administrativa": Area.ADMINISTRATIVE,
    "administrativo": Area.ADMINISTRATIVE,
    "administracao": Area.ADMINISTRATIVE,
}

EDUCATION_KEYWORDS: dict[str, EducationLevel] = {
    "fundamental": EducationLevel.ELEMENTARY,
    "medio": EducationLevel.HIGH_SCHOOL,
    "tecnico": EducationLevel.TECHNICAL,
    "superior": EducationLevel.HIGHER,
    "graduacao": EducationLevel.HIGHER,
}

_SEPARATORS = re.compile(r"[,;/\n]+|\s+e\s+")
_SALARY = re.compile(r"(?:r\$\s*)?(\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2})?\s*(mil|k)?")


@dataclass(frozen=True, slots=True)
class ProfileUpdate:
    ufs: tuple[str, ...] = ()
    areas: tuple[Area, ...] = ()
    education: EducationLevel | None = None
    min_salary_cents: int | None = None

    def is_empty(self) -> bool:
        return not (self.ufs or self.areas or self.education or self.min_salary_cents)


def parse_profile(text: str) -> ProfileUpdate | None:
    """Return the preferences found in `text`, or None if nothing was recognized.

    When several education levels are given, the highest one wins: it is the
    subscriber's own level, and lower requirements match it anyway.
    """
    ufs: set[str] = set()
    areas: set[Area] = set()
    levels: set[EducationLevel] = set()
    salary: int | None = None

    for raw in _SEPARATORS.split(_normalize(text)):
        token = raw.strip(" .!")
        if not token:
            continue
        if token.upper() in UFS:
            ufs.add(token.upper())
        elif token in STATE_NAMES:
            ufs.add(STATE_NAMES[token])
        elif (salary_cents := _salary_cents(token)) is not None:
            salary = salary_cents
        else:
            words = token.split()
            areas.update(AREA_KEYWORDS[w] for w in words if w in AREA_KEYWORDS)
            levels.update(EDUCATION_KEYWORDS[w] for w in words if w in EDUCATION_KEYWORDS)

    update = ProfileUpdate(
        ufs=tuple(sorted(ufs)),
        areas=tuple(sorted(areas, key=lambda a: a.value)),
        education=max(levels, key=EDUCATION_RANK.__getitem__) if levels else None,
        min_salary_cents=salary,
    )
    return None if update.is_empty() else update


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _salary_cents(token: str) -> int | None:
    token = token.removeprefix("acima de ").removeprefix("a partir de ").removeprefix("minimo ")
    match = _SALARY.fullmatch(token.strip())
    if not match:
        return None
    value = int(match[1].replace(".", ""))
    if match[2]:
        value *= 1000
    return value * 100 if 500 <= value <= 200_000 else None
