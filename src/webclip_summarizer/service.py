"""Batch orchestration. Failed pages remain pending and do not stop other pages."""

import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .export import build_prefix_index, is_exported, write_markdown
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
                if export_only:
                    self._export_saved_page(page)
                    continue
                title, source = self._read_title_source(page)
                body = self.notion.read_body(page["id"])
                result = self.llm.summarize(
                    title, body, [tag for tag, _ in candidates.most_common(MAX_CANDIDATE_TAGS)]
                )
                timestamp = datetime.now(UTC).isoformat()
                self.notion.update_summary(page["id"], result.summary, result.tags, timestamp)
                candidates.update(result.tags)
                LOGGER.info("Updated page %s", page["id"])
                if self.settings.output_dir is not None:
                    try:
                        self._write_page(page, title, source, body, result, timestamp)
                    except Exception:
                        failures += 1
                        LOGGER.exception(
                            "Markdown export failed for %s; retry on next normal run", page["id"]
                        )
            except Exception:
                failures += 1
                LOGGER.exception("Processing failed for page %s", page["id"])
        checked = exported = skipped = 0
        if not export_only and self.settings.output_dir is not None:
            extra_failures, checked, exported, skipped = self._run_catchup(
                {page["id"] for page in pages}, limit
            )
            failures += extra_failures
        LOGGER.info(
            "Batch finished: pages=%d failures=%d catchup_checked=%d "
            "catchup_exported=%d catchup_skipped=%d",
            len(pages),
            failures,
            checked,
            exported,
            skipped,
        )
        return failures

    def _read_title_source(self, page: dict[str, Any]) -> tuple[str, str]:
        properties = page["properties"]
        title = next(
            (read_text(prop["title"]) for prop in properties.values() if prop["type"] == "title"),
            "untitled",
        )
        source = next(
            (prop.get("url") or "" for prop in properties.values() if prop["type"] == "url"), ""
        )
        return title, source

    def _created_time(self, page: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(page["created_time"]).astimezone(
            ZoneInfo(self.settings.timezone)
        )

    def _write_page(
        self,
        page: dict[str, Any],
        title: str,
        source: str,
        body: str,
        result: Summary,
        timestamp: str,
    ) -> None:
        assert self.settings.output_dir is not None
        write_markdown(
            self.settings.output_dir,
            page,
            title,
            body,
            result,
            self._created_time(page),
            timestamp,
            source,
        )

    def _export_saved_page(self, page: dict[str, Any]) -> None:
        title, source = self._read_title_source(page)
        properties = page["properties"]
        body = self.notion.read_body(page["id"])
        result = Summary(
            read_text(properties[self.settings.summary_property]["rich_text"]),
            [tag["name"] for tag in properties[self.settings.tags_property]["multi_select"]],
            self.llm.format_title(title),
        )
        timestamp = properties[self.settings.time_property]["date"]["start"]
        self._write_page(page, title, source, body, result, timestamp)

    def _run_catchup(self, excluded: set[str], limit: int | None) -> tuple[int, int, int, int]:
        assert self.settings.output_dir is not None
        try:
            index = build_prefix_index(self.settings.output_dir)
        except OSError:
            LOGGER.exception("Markdown catchup index failed")
            return 1, 0, 0, 0
        try:
            # Finish pagination before any exports so listing failures have no partial output.
            pages = list(
                self.notion.query_pages(
                    {"property": self.settings.time_property, "date": {"is_not_empty": True}},
                    "descending",
                )
            )
        except Exception:
            LOGGER.exception("Markdown catchup query failed")
            return 1, 0, 0, 0
        failures = checked = exported = skipped = attempted = 0
        for page in pages:
            if limit is not None and attempted >= limit:
                break
            checked += 1
            try:
                if page["id"] in excluded or is_exported(
                    index, self._created_time(page), page["id"]
                ):
                    skipped += 1
                    continue
                # Failed attempts consume the limit too, bounding body and LLM calls.
                attempted += 1
                self._export_saved_page(page)
                exported += 1
            except Exception:
                failures += 1
                LOGGER.exception("Markdown catchup failed for page %s", page["id"])
        return failures, checked, exported, skipped
