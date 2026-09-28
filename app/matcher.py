"""Decides whether a classified notice is relevant to a subscriber. Pure functions only."""

from __future__ import annotations

from app.domain import EDUCATION_RANK, Classification, NoticeKind, SubscriberProfile

# Phase 1 only alerts on new exams. Rectifications and summons come in Phase 2.
DELIVERABLE_KINDS: frozenset[NoticeKind] = frozenset({NoticeKind.OPENING})


def matches(
    profile: SubscriberProfile,
    notice: Classification,
    kinds: frozenset[NoticeKind] = DELIVERABLE_KINDS,
) -> bool:
    return (
        notice.is_public_exam
        and notice.kind in kinds
        and _uf_matches(profile, notice)
        and _area_matches(profile, notice)
        and _education_matches(profile, notice)
        and _salary_matches(profile, notice)
    )


def _uf_matches(profile: SubscriberProfile, notice: Classification) -> bool:
    # A notice without UFs is nationwide.
    return not profile.ufs or not notice.ufs or bool(profile.ufs.intersection(notice.ufs))


def _area_matches(profile: SubscriberProfile, notice: Classification) -> bool:
    # A subscriber with area preferences only gets notices whose area is known.
    return not profile.areas or bool(profile.areas.intersection(notice.areas))


def _education_matches(profile: SubscriberProfile, notice: Classification) -> bool:
    if profile.education is None or not notice.education_levels:
        return True
    lowest_required = min(EDUCATION_RANK[level] for level in notice.education_levels)
    return lowest_required <= EDUCATION_RANK[profile.education]


def _salary_matches(profile: SubscriberProfile, notice: Classification) -> bool:
    if profile.min_salary_cents is None or notice.max_salary_cents is None:
        return True
    return notice.max_salary_cents >= profile.min_salary_cents
