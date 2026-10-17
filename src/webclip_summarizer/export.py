"""Atomic Obsidian Markdown export with collision detection."""

import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .llm import Summary


def format_filename_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title)
    title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|#,.]', "", title)
    title = re.sub(
        r"(?<=[\u3040-\u30ff\u3400-\u9fff])\s+(?=[\u3040-\u30ff\u3400-\u9fff])", "", title
    )
    title = re.sub(r"\s+", "_", title).strip(" ._")[:60]
    return title or "untitled"


def write_markdown(
    directory: Path,
    page: dict[str, Any],
    title: str,
    body: str,
    result: Summary,
    created: datetime,
    summarized: str,
    source: str,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = created.strftime("%Y%m%d_%H%M_") + format_filename_title(result.filename_title)
    path = directory / f"{stem}.md"
    if path.exists():
        content = path.read_text(encoding="utf-8")
        metadata = yaml.safe_load(content.split("---", 2)[1]) if content.startswith("---\n") else {}
        if not isinstance(metadata, dict) or metadata.get("notion_id") != page["id"]:
            raise FileExistsError(f"Filename collision: {path.name}")
    metadata = {
        "title": title,
        "created": created.isoformat(),
        "summary_time": summarized,
        "tags": result.tags,
        "source": source,
        "notion_id": page["id"],
        "notion_url": page.get("url", ""),
        "summary": result.summary,
    }
    content = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    content += f"---\n\n# {title}\n\n## 要約\n\n{result.summary}\n\n## 本文\n\n{body}\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".webclip-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path
