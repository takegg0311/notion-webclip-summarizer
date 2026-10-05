"""One-shot CLI suitable for cron and Cloud Run Jobs."""

import argparse
import logging

import httpx
from dotenv import load_dotenv

from .config import Settings
from .llm import LlmClientImpl
from .notion import NotionClientImpl
from .service import SummarizerImpl


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize pending Notion web clips")
    parser.add_argument("--limit", type=int, help="Maximum pages per run")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    try:
        settings = Settings.from_env()
        with httpx.Client(timeout=60) as client:
            failures = SummarizerImpl(
                settings, NotionClientImpl(settings, client), LlmClientImpl(settings)
            ).run(limit=args.limit)
        raise SystemExit(1 if failures else 0)
    except Exception:
        logging.getLogger(__name__).exception("Batch initialization failed")
        raise SystemExit(1)
