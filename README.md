# Concurso Alerts

Segmented WhatsApp alerts for Brazilian public service exams (*concursos públicos*).
Subscribers pick states, areas, education level and a minimum salary. The service
watches official sources and sends each person only the notices that match.

```
 sources ──► collect ──► classify ──► match ──► dispatch ──► WhatsApp
 (QD, RSS)   RawItem     Concurso     Delivery   Meta / Evolution
```

Each stage is idempotent: re-running a cycle never duplicates items or messages.
Uniqueness is enforced by database constraints, not by application logic.

## Features

- **Collection** from Querido Diário (municipal gazettes) and allowed RSS feeds, one adapter per source.
- **Classification** with Claude (JSON-schema structured output). A keyword heuristic runs when no API key is set or the API fails. Every LLM response is validated by `_sanitize()`.
- **Matching** by UF, area, education level and salary floor, written as pure functions.
- **Delivery** through the Meta Cloud API (templates) or Evolution API, with a dry-run console mode.
- **Plans:** Pro gets alerts in real time and Free gets them 24h later. An upgrade releases pending alerts on the next cycle.
- **Compliance:** replying `SAIR` opts out and cancels queued alerts, and webhook signatures are verified.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # DRY_RUN=1 by default

python tests_smoke.py           # must pass before any change
python run.py --once            # one pipeline cycle
uvicorn app.api:app --reload    # API on http://localhost:8000/docs
```

Create a subscriber (set `ADMIN_TOKEN` in `.env` first):

```bash
curl -X POST localhost:8000/subscribers \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"phone": "5521999990001", "ufs": ["RJ"], "areas": ["health"],
       "education": "higher", "min_salary_brl": 4000}'
```

## Project layout

```
app/
  api.py           FastAPI app: health, admin subscribers, WhatsApp webhook
  classifier.py    LLM + heuristic classifiers, _sanitize()
  config.py        Settings (pydantic-settings, .env)
  db.py            Engine/session factories, insert_ignore()
  domain.py        Enums and dataclasses shared across layers
  matcher.py       Pure subscriber ↔ notice matching
  messages.py      pt-BR alert text (≤ 600 chars)
  models.py        SQLAlchemy models and idempotency constraints
  pipeline.py      collect / classify / match / dispatch, run_cycle()
  sender.py        Console, Meta Cloud and Evolution senders
  sources/         One module per data source
run.py             Worker (APScheduler) entrypoint
tests_smoke.py     End-to-end smoke tests (no network)
```

## Configuration

`.env.example` documents every variable. The main ones:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLite in dev, Postgres in prod |
| `ANTHROPIC_API_KEY`, `CLASSIFIER_MODEL` | LLM classification (optional) |
| `DRY_RUN`, `WA_PROVIDER` | Console vs. real delivery; `meta` or `evolution` |
| `FREE_DELAY_HOURS` | Delay for the free plan (default 24) |
| `AFFILIATE_LINKS` | JSON `{area: url}` for course links in the footer |

## Development

```bash
ruff check . && ruff format .
python tests_smoke.py     # or: pytest
```

See [CLAUDE.md](CLAUDE.md) for engineering rules and the backlog.
