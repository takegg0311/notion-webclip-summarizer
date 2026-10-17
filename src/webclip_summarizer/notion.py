"""Notion transport and recursive block rendering."""

import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

from .config import Settings

LOGGER = logging.getLogger(__name__)

NOTION_VERSION = "2025-09-03"
MAX_ATTEMPTS = 5
RICH_TEXT_LIMIT = 2000


def read_text(items: list[dict[str, Any]]) -> str:
    return "".join(
        item.get("plain_text", item.get("text", {}).get("content", "")) for item in items
    )


class NotionClientImpl:
    def __init__(self, settings: Settings, client: httpx.Client) -> None:
        self.settings = settings
        self.client = client
        self.source_id = settings.data_source_id
        self.last_request = 0.0

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(MAX_ATTEMPTS):
            time.sleep(max(0, 0.35 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.client.request(
                    method,
                    "https://api.notion.com/v1/" + path,
                    headers={
                        "Authorization": f"Bearer {self.settings.notion_token}",
                        "Notion-Version": NOTION_VERSION,
                    },
                    **kwargs,
                )
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(min(60, float(response.headers.get("Retry-After", 2**attempt))))
                    continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Notion retries exhausted")

    def validate_schema(self) -> None:
        database = self.request("GET", f"databases/{self.settings.database_id}")
        sources = [source["id"] for source in database.get("data_sources", [])]
        if not self.source_id:
            if len(sources) != 1:
                raise ValueError("Set NOTION_DATA_SOURCE_ID for a database with multiple sources")
            self.source_id = sources[0]
        elif self.source_id not in sources:
            raise ValueError("NOTION_DATA_SOURCE_ID does not belong to the database")
        schema = self.request("GET", f"data_sources/{self.source_id}")["properties"]
        for name, kind in (
            (self.settings.summary_property, "rich_text"),
            (self.settings.tags_property, "multi_select"),
            (self.settings.time_property, "date"),
        ):
            if schema.get(name, {}).get("type") != kind:
                raise ValueError(f"Notion property {name} must be {kind}")

    def query_pages(
        self, filter_value: dict[str, Any], direction: str = "ascending", limit: int | None = None
    ) -> Iterator[dict[str, Any]]:
        cursor = None
        count = 0
        warned_incomplete = False
        while True:
            payload: dict[str, Any] = {
                "filter": filter_value,
                "sorts": [{"timestamp": "created_time", "direction": direction}],
                "page_size": min(100, limit - count) if limit else 100,
            }
            if cursor:
                payload["start_cursor"] = cursor
            result = self.request("POST", f"data_sources/{self.source_id}/query", json=payload)
            status = result.get("request_status", {})
            if status.get("type") == "incomplete" and not warned_incomplete:
                LOGGER.warning("Notion query incomplete: %s", status.get("incomplete_reason"))
                warned_incomplete = True
            for page in result["results"]:
                if page.get("object") != "page":
                    continue
                yield page
                count += 1
                if limit and count >= limit:
                    return
            if not result.get("has_more"):
                return
            cursor = result["next_cursor"]

    def read_body(self, block_id: str, depth: int = 0) -> str:
        lines: list[str] = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            result = self.request("GET", f"blocks/{block_id}/children", params=params)
            for block in result["results"]:
                kind = block["type"]
                value = block.get(kind, {})
                text = read_text(value.get("rich_text", []))
                prefix = {
                    "heading_1": "# ",
                    "heading_2": "## ",
                    "heading_3": "### ",
                    "bulleted_list_item": "- ",
                    "numbered_list_item": "1. ",
                    "quote": "> ",
                    "to_do": "- [x] " if value.get("checked") else "- [ ] ",
                }
                if kind == "code":
                    text = f"```{value.get('language', '')}\n{text}\n```"
                elif kind == "table_row":
                    text = "| " + " | ".join(read_text(cell) for cell in value["cells"]) + " |"
                elif kind in ("image", "file", "pdf", "video", "audio"):
                    url = value.get(value.get("type", ""), {}).get("url", "")
                    text = f"{'!' if kind == 'image' else ''}[{read_text(value.get('caption', [])) or kind}]({url})"
                elif kind in ("bookmark", "link_preview", "embed"):
                    text = value.get("url", "")
                elif kind == "divider":
                    text = "---"
                if text:
                    lines.append("  " * depth + prefix.get(kind, "") + text)
                if block.get("has_children"):
                    lines.append(self.read_body(block["id"], depth + 1))
            if not result.get("has_more"):
                return "\n\n".join(lines)
            cursor = result["next_cursor"]

    def update_summary(self, page_id: str, summary: str, tags: list[str], timestamp: str) -> None:
        chunks = [
            summary[index : index + RICH_TEXT_LIMIT]
            for index in range(0, len(summary), RICH_TEXT_LIMIT)
        ]
        if len(chunks) > 100:
            raise ValueError("Summary exceeds Notion rich_text array limit")
        self.request(
            "PATCH",
            f"pages/{page_id}",
            json={
                "properties": {
                    self.settings.summary_property: {
                        "rich_text": [
                            {"type": "text", "text": {"content": chunk}} for chunk in chunks
                        ]
                    },
                    self.settings.tags_property: {"multi_select": [{"name": tag} for tag in tags]},
                    self.settings.time_property: {"date": {"start": timestamp}},
                }
            },
        )
