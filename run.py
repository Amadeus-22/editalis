"""Worker entrypoint.

python run.py --once   # run a single collect -> classify -> match -> dispatch cycle
python run.py          # run forever, every SCHEDULE_MINUTES
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from functools import partial

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler

from app import __version__
from app.classifier import build_classifier
from app.config import configure_logging, get_settings
from app.db import get_engine, init_db, make_session_factory
from app.pipeline import build_sources, run_cycle
from app.sender import build_sender

logger = logging.getLogger("worker")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Public Exam Alerts worker")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    engine = get_engine(settings.database_url)
    init_db(engine)

    headers = {"User-Agent": f"public-exam-alerts/{__version__}"}
    with httpx.Client(headers=headers, timeout=30) as http:
        cycle = partial(
            run_cycle,
            make_session_factory(engine),
            build_sources(settings, http),
            build_classifier(
                settings.anthropic_api_key,
                settings.classifier_model,
                settings.classifier_max_chars,
            ),
            build_sender(settings, http),
            settings,
        )
        if args.once:
            cycle()
            return 0

        scheduler = BlockingScheduler(timezone=UTC)
        scheduler.add_job(
            cycle,
            "interval",
            minutes=settings.schedule_minutes,
            next_run_time=datetime.now(UTC),
            max_instances=1,
            coalesce=True,
        )
        logger.info("Worker started; running every %d min", settings.schedule_minutes)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
