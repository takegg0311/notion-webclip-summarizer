"""Batch orchestration. Failed pages remain pending and do not stop other pages."""

import logging
from collections import Counter
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .export import write_markdown
from .llm import LlmClientImpl, Summary
from .notion import NotionClientImpl, read_text

LOGGER = logging.getLogger(__name__)
MAX_CANDIDATE_TAGS = 300


class SummarizerImpl:
    def __init__(self, settings: Settings, notion: NotionClientImpl, llm: LlmClientImpl) -> None:
        self.settings = settings
        self.notion = notion
        self.llm = llm

    def run(self, export_only: bool = False, limit: int | None = None) -> int:
        self.notion.validate_schema()
        if export_only and self.settings.output_dir is None:
            raise ValueError("--export-only requires MARKDOWN_OUTPUT_DIR")
        candidates: Counter[str] = Counter()
        if not export_only:
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
                    "date": {"is_not_empty" if export_only else "is_empty": True},
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
                source = next(
                    (
                        prop.get("url") or ""
                        for prop in properties.values()
                        if prop["type"] == "url"
                    ),
                    "",
                )
                body = self.notion.read_body(page["id"])
                if export_only:
                    result = Summary(
                        read_text(properties[self.settings.summary_property]["rich_text"]),
                        [
                            tag["name"]
                            for tag in properties[self.settings.tags_property]["multi_select"]
                        ],
                        title,
                    )
                    # Reuse the same title formatting rules without re-summarizing the article.
                    result = Summary(
                        result.summary,
                        result.tags,
                        self.llm.format_title(title),
                    )
                    timestamp = properties[self.settings.time_property]["date"]["start"]
                else:
                    result = self.llm.summarize(
                        title, body, [tag for tag, _ in candidates.most_common(MAX_CANDIDATE_TAGS)]
                    )
                    timestamp = datetime.now(UTC).isoformat()
                    self.notion.update_summary(page["id"], result.summary, result.tags, timestamp)
                    candidates.update(result.tags)
                    LOGGER.info("Updated page %s", page["id"])
                if self.settings.output_dir is not None:
                    try:
                        created = datetime.fromisoformat(page["created_time"])
                        write_markdown(
                            self.settings.output_dir,
                            page,
                            title,
                            body,
                            result,
                            created.astimezone(ZoneInfo(self.settings.timezone)),
                            timestamp,
                            source,
                        )
                    except Exception:
                        failures += 1
                        LOGGER.exception(
                            "Markdown export failed for %s; retry with --export-only", page["id"]
                        )
            except Exception:
                failures += 1
                LOGGER.exception("Processing failed for page %s", page["id"])
        LOGGER.info("Batch finished: pages=%d failures=%d", len(pages), failures)
        return failures
