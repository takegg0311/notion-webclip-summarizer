"""Batch orchestration. Failed pages remain pending and do not stop other pages."""

import logging
from collections import Counter
from datetime import UTC, datetime

from .config import Settings
from .llm import LlmClientImpl
from .notion import NotionClientImpl, read_text

LOGGER = logging.getLogger(__name__)
MAX_CANDIDATE_TAGS = 300


class SummarizerImpl:
    def __init__(self, settings: Settings, notion: NotionClientImpl, llm: LlmClientImpl) -> None:
        self.settings = settings
        self.notion = notion
        self.llm = llm

    def run(self, limit: int | None = None) -> int:
        self.notion.validate_schema()
        candidates: Counter[str] = Counter()
        recent = self.notion.query_pages(
            {"property": self.settings.tags_property, "multi_select": {"is_not_empty": True}},
            "descending",
            self.settings.recent_count,
        )
        for page in recent:
            candidates.update(
                tag["name"]
                for tag in page["properties"][self.settings.tags_property]["multi_select"]
            )
        # Snapshot IDs before updating the filtered collection so pagination is stable.
        pages = list(
            self.notion.query_pages(
                {
                    "property": self.settings.time_property,
                    "date": {"is_empty": True},
                },
                limit=limit,
            )
        )
        failures = 0
        for page in pages:
            try:
                properties = page["properties"]
                title = next(
                    (
                        read_text(prop["title"])
                        for prop in properties.values()
                        if prop["type"] == "title"
                    ),
                    "untitled",
                )
                body = self.notion.read_body(page["id"])
                result = self.llm.summarize(
                    title, body, [tag for tag, _ in candidates.most_common(MAX_CANDIDATE_TAGS)]
                )
                timestamp = datetime.now(UTC).isoformat()
                self.notion.update_summary(page["id"], result.summary, result.tags, timestamp)
                candidates.update(result.tags)
                LOGGER.info("Updated page %s", page["id"])
            except Exception:
                failures += 1
                LOGGER.exception("Processing failed for page %s", page["id"])
        LOGGER.info("Batch finished: pages=%d failures=%d", len(pages), failures)
        return failures
