# Editalis

**Segmented WhatsApp alerts for Brazilian public service exams.**

[![CI](https://github.com/Amadeus-22/editalis/actions/workflows/ci.yml/badge.svg)](https://github.com/Amadeus-22/editalis/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.12-3776ab?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?style=flat-square&logo=fastapi&logoColor=white)](requirements.txt)
[![code style: ruff](https://img.shields.io/badge/code%20style-ruff-d7ff64?style=flat-square)](pyproject.toml)

Editalis watches official publications for new public service exams (*concursos
públicos*) and sends each subscriber only the notices that match their profile:
state, area, education level and minimum salary.

---

## Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [API](#api)
- [Project structure](#project-structure)
- [Design principles](#design-principles)
- [Development](#development)
- [Roadmap](#roadmap)

---

## How it works

```
 sources ──► collect ──► classify ──► match ──► dispatch ──► WhatsApp
 (QD, RSS)   RawItem     Exam         Delivery   Meta / Evolution
```

| Stage | Input → output | Guarantee |
|---|---|---|
| **collect** | Source adapters → `RawItem` | Unique per `(source, external_id)` |
| **classify** | `RawItem` → `Exam` (Claude or heuristic) | One `Exam` per raw item; LLM output always sanitized |
| **match** | `Exam` × active subscribers → `Delivery` | Unique per `(subscriber_id, exam_id)` |
| **dispatch** | Due `Delivery` → WhatsApp message | Recorded before sending; at-most-once delivery |

Every stage can run any number of times without duplicating items or messages.
Uniqueness is enforced by database constraints, not by application logic.

Sample alert, as the subscriber sees it (user-facing text is in Portuguese):

```
*Edital aberto*: Prefeitura de Niterói (RJ)
Vagas: 120 | Salário: até R$ 8.500,00
Escolaridade: médio, superior
Inscrições até 30/10/2026
https://example.org/edital

Responda SAIR para não receber mais alertas.
```

---

## Features

| Area | What it does |
|---|---|
| **Collection** | Querido Diário (municipal gazettes) and allowed RSS feeds, one adapter per source. A failing source is logged and skipped. |
| **Classification** | Claude with JSON-schema structured output; keyword heuristic when there is no API key or the API fails. |
| **Matching** | Pure functions over UF, area, education level and salary floor. |
| **Delivery** | Meta Cloud API (approved templates) or self-hosted Evolution API; console output with `DRY_RUN=1`. |
| **Plans** | Pro receives alerts in real time; Free 24h later. Upgrading releases pending alerts on the next cycle. |
| **Compliance** | Replying `SAIR` opts out and cancels queued alerts; webhook signatures are verified. |
| **Relevance** | Alerts whose registration closed before dispatch are skipped. Affiliate links appear only when a course matches the exam's area. |

---

## Getting started

**Requirements:** Python 3.12.

```bash
git clone git@github.com:Amadeus-22/editalis.git && cd editalis
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env              # DRY_RUN=1: messages are printed, not sent
```

Run it:

```bash
python tests_smoke.py             # smoke tests, no network needed
python run.py --once              # one full pipeline cycle
python run.py                     # worker, every SCHEDULE_MINUTES
uvicorn app.api:app --reload      # API docs at http://localhost:8000/docs
```

Create a subscriber (set `ADMIN_TOKEN` in `.env` first):

```bash
curl -X POST localhost:8000/subscribers \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"phone": "5521999990001", "ufs": ["RJ"], "areas": ["health"],
       "education": "higher", "min_salary_brl": 4000}'
```

---

## Configuration

All settings come from environment variables or `.env`. [`.env.example`](.env.example)
documents every key. The most important ones:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./editalis.db` | SQLite in development, Postgres in production |
| `ADMIN_TOKEN` | *(empty)* | Required for the `/subscribers` endpoints |
| `ANTHROPIC_API_KEY` | *(empty)* | Enables LLM classification; empty uses the heuristic |
| `CLASSIFIER_MODEL` | `claude-sonnet-4-6` | Model used for classification |
| `DRY_RUN` | `1` | Print messages instead of sending them |
| `WA_PROVIDER` | `meta` | `meta` (Cloud API) or `evolution` |
| `WA_TEMPLATE_NAME` | `editalis_alert` | Approved Meta template with one body variable |
| `FREE_DELAY_HOURS` | `24` | Delay applied to the free plan |
| `SCHEDULE_MINUTES` | `30` | Worker interval |
| `RSS_FEEDS` | *(empty)* | Comma-separated feeds whose terms allow this use |
| `ASAAS_WEBHOOK_TOKEN` | *(empty)* | Token Asaas sends on every webhook call |
| `AFFILIATE_LINKS` | `{}` | JSON `{area: url}` for the course link in the footer |

Valid areas: `administrative`, `banking`, `education`, `engineering`, `health`,
`it`, `legal`, `security`, `tax`, `other`. Education levels: `elementary`,
`high_school`, `technical`, `higher`.

---

## API

Interactive documentation is served at `/docs`.

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | — | Liveness plus database check |
| `POST` | `/subscribers` | `X-Admin-Token` | Create a subscriber (`409` if the phone exists) |
| `GET` | `/subscribers/{id}` | `X-Admin-Token` | Read a subscriber |
| `PATCH` | `/subscribers/{id}` | `X-Admin-Token` | Update profile, plan or status |
| `GET` | `/webhooks/whatsapp` | Verify token | Meta subscription handshake |
| `POST` | `/webhooks/whatsapp` | `X-Hub-Signature-256` | Inbound messages; handles `SAIR` opt-out |
| `POST` | `/webhooks/asaas` | `asaas-access-token` | Payment/subscription events; switches the plan (idempotent per event) |

Phone numbers use E.164 without the `+`: `55` + area code + number.

---

## Project structure

```
app/
├── api.py            FastAPI app: health, admin subscribers, webhooks
├── billing.py        Asaas events → subscriber plan
├── classifier.py     LLM and heuristic classifiers, _sanitize()
├── config.py         Settings (pydantic-settings)
├── db.py             Engine/session factories, insert_ignore()
├── domain.py         Enums and dataclasses shared across layers
├── matcher.py        Pure subscriber ↔ exam matching
├── messages.py       pt-BR alert text (≤ 600 characters)
├── models.py         SQLAlchemy models and idempotency constraints
├── pipeline.py       collect / classify / match / dispatch, run_cycle()
├── sender.py         Console, Meta Cloud and Evolution senders
├── subscribers.py    Opt-out handling
└── sources/          One module per data source
    ├── querido_diario.py
    └── rss.py
run.py                Worker entrypoint (APScheduler)
tests_smoke.py        End-to-end smoke tests
```

---

## Design principles

- **Idempotent by construction.** Unique constraints on `RawItem` and `Delivery` make re-runs safe; `insert_ignore()` handles conflicts on SQLite and Postgres.
- **At-most-once delivery.** A delivery is marked `sending` and committed before the provider call, so a crash can never send the same alert twice.
- **Pure core.** The classifier and matcher do no database I/O and send nothing; sending happens only in `dispatch()`.
- **Untrusted LLM output.** Every model response passes through `_sanitize()`: enums are checked, numbers are range-checked, and invalid fields are dropped.
- **Isolated sources.** Each source is its own module registered in `pipeline.build_sources()`. One failing source never stops a cycle.
- **Only permitted data.** Official gazettes, exam board sites and feeds whose terms allow reuse. Competing portals are never scraped.

---

## Development

```bash
ruff check . && ruff format .     # lint and format
python tests_smoke.py             # or: pytest
```

CI runs ruff and the smoke tests on every push and pull request. Before opening
a PR, the tests must pass. Any change to the pipeline, classifier, matcher or
sender needs a matching test case. Engineering rules for contributors and AI
agents are in [CLAUDE.md](CLAUDE.md).

> **Note:** the Querido Diário adapter follows the public API documentation but
> has not yet been verified against the live service.

---

## Roadmap

- [x] Idempotent collect → classify → match → dispatch pipeline
- [x] 24h delay for the free plan
- [x] `SAIR` opt-out via WhatsApp webhook
- [x] Asaas webhook for recurring Pix subscriptions
- [ ] Profile onboarding over WhatsApp (`"RJ, saúde, superior"`)
- [ ] DOU/INLABS source
- [ ] Exam board adapters: Cebraspe, FGV, FCC, Vunesp, IBFC
- [ ] Postgres + Alembic, Docker Compose deployment
- [ ] Rectification and summons tracker
- [ ] PDF edital reader: summary and schedule
