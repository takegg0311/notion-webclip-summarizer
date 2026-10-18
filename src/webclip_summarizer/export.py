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


def read_notion_id(path: Path) -> str | None:
    """Read line-delimited frontmatter; malformed candidates do not match."""
    try:
        with path.open(encoding="utf-8") as stream:
            if stream.readline() != "---\n":
                return None
            lines: list[str] = []
            for line in stream:
                if line.rstrip("\r\n") == "---":
                    metadata = yaml.safe_load("".join(lines))
                    return metadata.get("notion_id") if isinstance(metadata, dict) else None
                lines.append(line)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return None


def build_prefix_index(directory: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if (
                    not entry.name.startswith(".webclip-")
                    and entry.name.endswith(".md")
                    and entry.is_file()
                ):
                    index.setdefault(entry.name[:14], []).append(Path(entry.path))
    except FileNotFoundError:
        if directory.exists():
            raise
    return index


def is_exported(index: dict[str, list[Path]], created: datetime, page_id: str) -> bool:
    return any(
        read_notion_id(path) == page_id for path in index.get(created.strftime("%Y%m%d_%H%M_"), [])
    )


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
        if read_notion_id(path) != page["id"]:
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
