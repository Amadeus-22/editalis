"""Smoke tests for the whole pipeline. Run with `python tests_smoke.py` (or `pytest`).

Uses an in-memory SQLite database, fake sources and a recording sender;
no network access is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.billing import apply_asaas_event, plan_for_event
from app.classifier import HeuristicClassifier, LLMClassifier, _sanitize
from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.domain import (
    Area,
    Classification,
    DeliveryStatus,
    EducationLevel,
    NoticeKind,
    Plan,
    SourceItem,
    SubscriberProfile,
)
from app.matcher import matches
from app.messages import MAX_CHARS, OPT_OUT_FOOTER, format_alert, format_brl
from app.models import Delivery, Exam, ProcessedWebhookEvent, RawItem, Subscriber
from app.pipeline import run_cycle
from app.sender import SendError, SendResult, flatten
from app.sources.querido_diario import QueridoDiarioSource

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

NOTICE_TEXT = (
    "EDITAL DE ABERTURA Nº 01/2026 - CONCURSO PÚBLICO. A Prefeitura de Niterói/RJ torna "
    "pública a abertura de inscrições para 120 vagas de Enfermeiro e Técnico de Enfermagem, "
    "nível superior e nível médio, com salário de até R$ 8.500,00. As inscrições ficam "
    "abertas até 30/10/2026."
)


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeSource:
    items: list[SourceItem]
    name: str = "fake"

    def fetch(self, now: datetime) -> Iterator[SourceItem]:
        yield from self.items


class BrokenSource:
    name = "broken"

    def fetch(self, now: datetime) -> Iterator[SourceItem]:
        raise httpx.ConnectError("boom")


@dataclass
class RecordingSender:
    sent: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    def send(self, to: str, text: str) -> SendResult:
        if self.fail:
            raise SendError("provider down")
        self.sent.append((to, text))
        return SendResult(provider_message_id=f"msg-{len(self.sent)}")


def make_item(external_id: str = "1", text: str = NOTICE_TEXT) -> SourceItem:
    return SourceItem(
        source="fake",
        external_id=external_id,
        title="Diário Oficial de Niterói",
        url=f"https://example.org/{external_id}",
        content=text,
        uf_hint="RJ",
        organization_hint="Prefeitura de Niterói",
    )


def make_db() -> sessionmaker[Session]:
    engine = make_engine("sqlite://", poolclass=StaticPool)
    init_db(engine)
    return make_session_factory(engine)


def add_subscriber(factory: sessionmaker[Session], phone: str, **fields: Any) -> int:
    defaults: dict[str, Any] = {"ufs": ["RJ"], "areas": ["health"], "plan": Plan.PRO.value}
    with factory() as session:
        subscriber = Subscriber(phone=phone, **(defaults | fields))
        session.add(subscriber)
        session.commit()
        return subscriber.id


def count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, dry_run=True, **overrides)


def cycle(factory, sources, sender, now=NOW, **setting_overrides):
    return run_cycle(
        factory, sources, HeuristicClassifier(), sender, settings(**setting_overrides), now=now
    )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_sanitize_rejects_non_objects() -> None:
    assert _sanitize(None) is None
    assert _sanitize(["not", "a", "dict"]) is None


def test_sanitize_normalizes_untrusted_values() -> None:
    result = _sanitize(
        {
            "is_public_exam": "yes",  # not a real boolean -> False
            "kind": "OPENING",
            "organization": "  Prefeitura   de  X  ",
            "ufs": ["rj", "XX", "sp", 3],
            "areas": ["health", "astrology"],
            "education_levels": ["higher", "phd"],
            "vacancies": 10**9,
            "max_salary_brl": 8500.5,
            "registration_deadline": "30/10/2026",
        }
    )
    assert result is not None
    assert result.is_public_exam is False
    assert result.kind is NoticeKind.OPENING
    assert result.organization == "Prefeitura de X"
    assert result.ufs == ("RJ", "SP")
    assert result.areas == (Area.HEALTH,)
    assert result.education_levels == (EducationLevel.HIGHER,)
    assert result.vacancies is None
    assert result.max_salary_cents == 850_050
    assert result.registration_deadline is None


def test_heuristic_classifier_extracts_fields() -> None:
    result = HeuristicClassifier().classify(make_item())
    assert result is not None and result.is_public_exam
    assert result.kind is NoticeKind.OPENING
    assert result.ufs == ("RJ",)
    assert Area.HEALTH in result.areas
    assert set(result.education_levels) == {EducationLevel.HIGHER, EducationLevel.HIGH_SCHOOL}
    assert result.vacancies == 120
    assert result.max_salary_cents == 850_000
    assert result.registration_deadline == date(2026, 10, 30)


def test_heuristic_classifier_ignores_unrelated_text() -> None:
    result = HeuristicClassifier().classify(make_item(text="Decreto sobre coleta de lixo."))
    assert result is not None and not result.is_public_exam


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response, self.error, self.calls = response, error, []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_llm(response: Any = None, error: Exception | None = None) -> tuple[LLMClassifier, Any]:
    messages = FakeMessages(response, error)
    client = SimpleNamespace(messages=messages)
    return LLMClassifier(client, "claude-sonnet-4-6"), messages  # type: ignore[arg-type]


def llm_response(payload: str, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=payload)]
    )


def test_llm_output_goes_through_sanitize() -> None:
    payload = json.dumps(
        {
            "is_public_exam": True,
            "kind": "opening",
            "organization": "Prefeitura de Niterói",
            "ufs": ["RJ", "ZZ"],
            "areas": ["health"],
            "education_levels": ["higher"],
            "vacancies": -5,
            "max_salary_brl": 8500,
            "registration_deadline": "2026-10-30",
        }
    )
    classifier, messages = fake_llm(llm_response(payload))
    result = classifier.classify(make_item())
    assert result is not None
    assert result.ufs == ("RJ",)
    assert result.vacancies is None
    assert result.registration_deadline == date(2026, 10, 30)
    request = messages.calls[0]
    assert request["model"] == "claude-sonnet-4-6"
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_llm_falls_back_to_heuristic_on_errors() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    for classifier, _ in (
        fake_llm(error=anthropic.APIConnectionError(request=request)),
        fake_llm(llm_response("not json")),
        fake_llm(llm_response("{}", stop_reason="max_tokens")),
    ):
        result = classifier.classify(make_item())
        assert result is not None and result.vacancies == 120  # heuristic result


# ---------------------------------------------------------------------------
# Matcher and message
# ---------------------------------------------------------------------------

NOTICE = Classification(
    is_public_exam=True,
    kind=NoticeKind.OPENING,
    organization="Prefeitura de Niterói",
    ufs=("RJ",),
    areas=(Area.HEALTH,),
    education_levels=(EducationLevel.HIGH_SCHOOL, EducationLevel.HIGHER),
    vacancies=120,
    max_salary_cents=850_000,
    registration_deadline=date(2026, 10, 30),
)


def test_matcher_rules() -> None:
    assert matches(SubscriberProfile(), NOTICE)
    assert matches(SubscriberProfile(ufs=frozenset({"RJ", "SP"})), NOTICE)
    assert not matches(SubscriberProfile(ufs=frozenset({"SP"})), NOTICE)
    assert matches(SubscriberProfile(ufs=frozenset({"SP"})), replace(NOTICE, ufs=()))
    assert not matches(SubscriberProfile(areas=frozenset({Area.IT})), NOTICE)
    assert matches(SubscriberProfile(education=EducationLevel.HIGH_SCHOOL), NOTICE)
    assert not matches(SubscriberProfile(education=EducationLevel.ELEMENTARY), NOTICE)
    assert matches(SubscriberProfile(min_salary_cents=800_000), NOTICE)
    assert not matches(SubscriberProfile(min_salary_cents=900_000), NOTICE)
    assert not matches(SubscriberProfile(), replace(NOTICE, kind=NoticeKind.RECTIFICATION))


def test_alert_message_format() -> None:
    text = format_alert(NOTICE, "https://example.org/edital")
    assert text.startswith("*Edital aberto*: Prefeitura de Niterói (RJ)")
    assert "Vagas: 120 | Salário: até R$ 8.500,00" in text
    assert "Escolaridade: médio, superior" in text
    assert "Inscrições até 30/10/2026" in text
    assert text.endswith(OPT_OUT_FOOTER)
    assert "Curso" not in text
    assert "Curso preparatório: https://aff" in format_alert(NOTICE, "u", "https://aff")
    assert format_brl(123_456_789) == "R$ 1.234.567,89"


def test_alert_message_respects_length_limit() -> None:
    long_notice = replace(NOTICE, organization="X" * 900)
    assert len(format_alert(long_notice, "https://example.org", "https://aff")) <= MAX_CHARS


def test_flatten_is_template_safe() -> None:
    flat = flatten("a\n\nb    c\tд")
    assert "\n" not in flat and "\t" not in flat and "    " not in flat


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def test_pipeline_is_idempotent() -> None:
    factory = make_db()
    add_subscriber(factory, "5521999990001")
    sender = RecordingSender()
    source = FakeSource([make_item("1"), make_item("1")])  # duplicate in the same batch

    for _ in range(3):
        cycle(factory, [source], sender)

    assert count(factory, RawItem) == 1
    assert count(factory, Exam) == 1
    assert count(factory, Delivery) == 1
    assert len(sender.sent) == 1
    phone, text = sender.sent[0]
    assert phone == "5521999990001" and "Prefeitura de Niterói" in text


def test_failing_source_does_not_abort_cycle() -> None:
    factory = make_db()
    add_subscriber(factory, "5521999990001")
    sender = RecordingSender()
    report = cycle(factory, [BrokenSource(), FakeSource([make_item()])], sender)
    assert report.collected == 1
    assert len(sender.sent) == 1


def test_free_plan_is_delayed_and_pro_is_immediate() -> None:
    factory = make_db()
    add_subscriber(factory, "5521999990001", plan=Plan.PRO.value)
    add_subscriber(factory, "5521999990002", plan=Plan.FREE.value)
    sender = RecordingSender()
    source = FakeSource([make_item()])

    cycle(factory, [source], sender)
    assert [phone for phone, _ in sender.sent] == ["5521999990001"]

    cycle(factory, [source], sender, now=NOW + timedelta(hours=23))
    assert len(sender.sent) == 1

    cycle(factory, [source], sender, now=NOW + timedelta(hours=24))
    assert [phone for phone, _ in sender.sent] == ["5521999990001", "5521999990002"]


def test_delivery_is_recorded_before_send_and_not_retried_after_failure() -> None:
    factory = make_db()
    add_subscriber(factory, "5521999990001")
    failing = RecordingSender(fail=True)
    cycle(factory, [FakeSource([make_item()])], failing)
    with factory() as session:
        delivery = session.scalars(select(Delivery)).one()
        assert delivery.status == DeliveryStatus.FAILED and delivery.attempts == 1

    healthy = RecordingSender()
    cycle(factory, [], healthy)
    assert healthy.sent == []


def test_closed_registration_is_skipped() -> None:
    factory = make_db()
    add_subscriber(factory, "5521999990001", plan=Plan.FREE.value)
    sender = RecordingSender()
    cycle(factory, [FakeSource([make_item()])], sender)
    cycle(factory, [], sender, now=datetime(2026, 11, 5, tzinfo=UTC))
    assert sender.sent == []
    with factory() as session:
        assert session.scalars(select(Delivery)).one().status == DeliveryStatus.SKIPPED


def test_querido_diario_adapter_parses_and_paginates() -> None:
    pages = [
        {
            "total_gazettes": 2,
            "gazettes": [
                {
                    "territory_id": "3303302",
                    "territory_name": "Niterói",
                    "state_code": "RJ",
                    "date": "2026-09-27",
                    "url": "https://qd.example/1.pdf",
                    "excerpts": ["CONCURSO PÚBLICO ..."],
                }
            ],
        },
        {"total_gazettes": 2, "gazettes": [{"territory_id": "x", "date": "2026-09-27"}]},
    ]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=pages[len(calls) - 1])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    items = list(QueridoDiarioSource(http, "https://qd.example", "exam").fetch(NOW))
    assert len(calls) == 2
    assert calls[0].url.params["published_since"] == "2026-09-26"
    assert len(items) == 1  # the second gazette has no URL and is skipped
    assert items[0].uf_hint == "RJ" and items[0].external_id.startswith("3303302:2026-09-27:")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def make_client(factory: sessionmaker[Session], **setting_overrides: Any):
    from fastapi.testclient import TestClient

    from app.api import app, get_session, get_settings

    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings(**setting_overrides)
    return TestClient(app)


def test_api_subscriber_crud_requires_admin_token() -> None:
    factory = make_db()
    client = make_client(factory, admin_token="secret")
    body = {"phone": "5521999990001", "ufs": ["rj"], "areas": ["health"], "min_salary_brl": 3000}

    assert client.post("/subscribers", json=body).status_code == 401
    created = client.post("/subscribers", json=body, headers={"X-Admin-Token": "secret"})
    assert created.status_code == 201, created.text
    assert created.json()["ufs"] == ["RJ"] and created.json()["min_salary_cents"] == 300_000
    duplicate = client.post("/subscribers", json=body, headers={"X-Admin-Token": "secret"})
    assert duplicate.status_code == 409

    subscriber_id = created.json()["id"]
    patched = client.patch(
        f"/subscribers/{subscriber_id}", json={"plan": "pro"}, headers={"X-Admin-Token": "secret"}
    )
    assert patched.json()["plan"] == "pro"


def test_webhook_opt_out_cancels_pending_deliveries() -> None:
    factory = make_db()
    add_subscriber(factory, "5521999990001", plan=Plan.FREE.value)
    sender = RecordingSender()
    cycle(factory, [FakeSource([make_item()])], sender)  # free: delivery stays pending

    client = make_client(factory, wa_app_secret="app-secret")
    payload = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "5521999990001",
                                        "type": "text",
                                        "text": {"body": " sair "},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"app-secret", payload, hashlib.sha256).hexdigest()

    assert client.post("/webhooks/whatsapp", content=payload).status_code == 401
    response = client.post(
        "/webhooks/whatsapp", content=payload, headers={"X-Hub-Signature-256": signature}
    )
    assert response.status_code == 200

    cycle(factory, [], sender, now=NOW + timedelta(days=2))
    assert sender.sent == []
    with factory() as session:
        assert session.scalars(select(Subscriber)).one().active is False
        assert session.scalars(select(Delivery)).one().status == DeliveryStatus.SKIPPED


def test_webhook_verification_handshake() -> None:
    client = make_client(make_db(), wa_verify_token="verify-me")
    params = {"hub.mode": "subscribe", "hub.challenge": "42", "hub.verify_token": "verify-me"}
    assert client.get("/webhooks/whatsapp", params=params).text == "42"
    params["hub.verify_token"] = "wrong"
    assert client.get("/webhooks/whatsapp", params=params).status_code == 403


def asaas_event(event_id: str, event: str, subscriber_id: int) -> dict[str, Any]:
    return {
        "id": event_id,
        "event": event,
        "payment": {"id": "pay_1", "externalReference": str(subscriber_id)},
    }


def test_asaas_plan_mapping() -> None:
    assert plan_for_event("PAYMENT_RECEIVED") is Plan.PRO
    assert plan_for_event("SUBSCRIPTION_DELETED") is Plan.FREE
    assert plan_for_event("PAYMENT_CREATED") is None


def test_asaas_webhook_switches_plan_idempotently() -> None:
    factory = make_db()
    subscriber_id = add_subscriber(factory, "5521999990001", plan=Plan.FREE.value)
    client = make_client(factory, asaas_webhook_token="asaas-secret")
    headers = {"asaas-access-token": "asaas-secret"}
    upgrade = asaas_event("evt_1", "PAYMENT_RECEIVED", subscriber_id)

    assert client.post("/webhooks/asaas", json=upgrade).status_code == 401
    assert client.post("/webhooks/asaas", json=upgrade, headers=headers).json() == {
        "outcome": "applied"
    }
    assert client.post("/webhooks/asaas", json=upgrade, headers=headers).json() == {
        "outcome": "duplicate"
    }
    with factory() as session:
        assert session.get(Subscriber, subscriber_id).plan == Plan.PRO
        assert session.scalar(select(func.count()).select_from(ProcessedWebhookEvent)) == 1

    downgrade = asaas_event("evt_2", "PAYMENT_OVERDUE", subscriber_id)
    client.post("/webhooks/asaas", json=downgrade, headers=headers)
    unknown = asaas_event("evt_3", "PAYMENT_RECEIVED", 999)
    outcome = client.post("/webhooks/asaas", json=unknown, headers=headers).json()
    assert outcome == {"outcome": "unknown_subscriber"}
    with factory() as session:
        assert session.get(Subscriber, subscriber_id).plan == Plan.FREE


def test_upgrade_releases_pending_free_alerts() -> None:
    factory = make_db()
    subscriber_id = add_subscriber(factory, "5521999990001", plan=Plan.FREE.value)
    sender = RecordingSender()
    cycle(factory, [FakeSource([make_item()])], sender)
    assert sender.sent == []

    with factory() as session:
        apply_asaas_event(session, asaas_event("evt_1", "PAYMENT_CONFIRMED", subscriber_id), NOW)
        session.commit()
    cycle(factory, [], sender, now=NOW + timedelta(minutes=30))
    assert len(sender.sent) == 1


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def main() -> int:
    tests: list[tuple[str, Callable[[], None]]] = [
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, test in tests:
        try:
            test()
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
