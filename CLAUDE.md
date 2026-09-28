# Editalis

WhatsApp alerts for Brazilian public service exams (concursos públicos),
segmented by profile (state/UF, area, education level, minimum salary).
Revenue: monthly subscription plus affiliate links to prep courses.
Audience: Brazilian exam candidates (concurseiros).

## Product goals
1. Hyper-segmented alerts, in real time (Pro plan) or with a 24h delay (Free).
2. Phase 2: tracker for rectifications (retificações) and summons (convocações) per organization.
3. Phase 3: PDF edital reader that produces a summary and a schedule.
Always prioritize item 1 until there are paying subscribers.

## Stack (do not change without justification)
- Python 3.12, FastAPI, SQLAlchemy 2 (SQLite in dev, Postgres in prod).
- httpx for HTTP, feedparser for RSS, APScheduler for the worker.
- Classification: Anthropic Messages API (`claude-sonnet-4-6`, set via `CLASSIFIER_MODEL`)
  with JSON-schema structured output. Without an API key, the heuristic classifier is used.
- WhatsApp: Meta Cloud API (official) or Evolution API (self-hosted). `DRY_RUN=1`
  logs to the console instead of sending.
- Payments: Asaas (recurring Pix). The webhook switches `plan` to `pro`.
- Deploy: Docker Compose on a VPS (api, worker, postgres).

## Architecture
A linear, idempotent pipeline: collect → classify → match → dispatch.

| Module | Responsibility |
|---|---|
| `app/sources/` | One adapter per data source; each yields `SourceItem`s and never touches the DB |
| `app/classifier.py` | `SourceItem` → `Classification` (LLM + heuristic fallback, `_sanitize()`) |
| `app/matcher.py` | Pure `matches(profile, notice)` |
| `app/messages.py` | Alert text (pt-BR, max 600 chars) and affiliate link choice |
| `app/sender.py` | Console / Meta Cloud / Evolution providers |
| `app/pipeline.py` | Stages, `build_sources()` and `run_cycle()` |
| `app/api.py` | Health, admin subscriber CRUD, WhatsApp webhook (opt-out) |
| `app/models.py` | `Subscriber`, `RawItem`, `Exam`, `Delivery` |

Code, comments, identifiers and enum values are in English. Only user-facing
WhatsApp text is in Portuguese.

## Engineering rules
- Every pipeline stage must be safe to run N times without duplicating anything.
  RawItem is unique per (source, external_id); Delivery is unique per
  (subscriber_id, exam_id). Never remove these constraints.
- A new data source = a new file in `app/sources/`, registered in
  `pipeline.build_sources()`. Do not mix source logic into the pipeline.
- The classifier and the matcher are pure functions: no DB I/O, no sending.
- Every LLM output goes through `_sanitize()`. Never trust raw JSON.
- Sending only happens in `dispatch()`, and only after the Delivery is written.
  Deliveries are marked `sending` before the provider call (at-most-once).
- A failing source must not bring down the cycle: catch, log, move on.
- Secrets only via `.env` (never commit it). `.env.example` documents every key.
- No scraping of competing portals (PCI, Ache) as the basis of the product.
  Accepted sources: Querido Diário, DOU/INLABS, exam board sites, state gazettes,
  RSS feeds whose use is permitted.

## Data sources
- Querido Diário: https://api.queridodiario.org.br/docs — confirm parameters before
  relying on it; the current adapter was written against the docs and has not been
  tested against the live API (it was returning 503 at the time).
- DOU/INLABS: free registration, daily XML download. Implement in `app/sources/dou.py`.
- Exam boards (Cebraspe, FGV, FCC, Vunesp, IBFC): scrape the open-exams pages, one
  adapter per board.

## Product rules
- Alert message: type, organization, UF, vacancies, salary, education level,
  deadline, link. At most ~600 characters. No emoji overload, no sales copy.
- Free receives alerts with a 24h delay; Pro in real time. The delay is evaluated
  in `dispatch()`, so upgrading releases pending alerts on the next cycle.
- Affiliate link in the footer only when there is a course for the notice's area.
- Users can leave at any time by replying SAIR (mandatory on the Meta API).
- Meta templates require approval; keep `WA_TEMPLATE_NAME` in sync.

## Working in this repository
- Before any change: `python tests_smoke.py` must pass.
- After changing pipeline/classifier/matcher/sender: update or add a case in `tests_smoke.py`.
- Lint/format: `ruff check . && ruff format .`
- Run the API: `uvicorn app.api:app --reload`. Run the worker: `python run.py --once`.
- Small commits, English messages, imperative mood ("add DOU source").
- Do not install new dependencies without adding them to `requirements.txt` with a pinned version.
- Schema migrations use Alembic (add it when moving off SQLite).

## Backlog (in order)
1. Asaas webhook to set `plan` (recurring Pix). The `plan` column already exists.
2. WhatsApp onboarding: webhook receives "RJ, saúde, superior" and updates the profile.
3. DOU/INLABS adapter.
4. Adapters for the 5 main exam boards.
5. Migrate to Postgres + Alembic; Docker Compose.
6. Rectification/summons tracker (the kind already exists in the classifier).
7. PDF edital upload endpoint → summary and schedule.

Done: 24h delay for the free plan in `dispatch()`.

## Out of scope for now
User web dashboard, mobile app, multiple languages, in-house courses,
Redis queue (only once classification starts delaying the cron).
