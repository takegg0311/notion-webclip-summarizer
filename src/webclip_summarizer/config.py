"""Environment configuration, isolated from application logic."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    notion_token: str
    database_id: str
    model: str
    data_source_id: str = ""
    timezone: str = "Asia/Tokyo"
    summary_property: str = "summary"
    tags_property: str = "tag"
    time_property: str = "summary_time"
    recent_count: int = 100
    chunk_chars: int = 12000
    max_tokens: int = 8192
    reasoning_effort: str = ""

    def __post_init__(self) -> None:
        if not self.notion_token or not self.database_id or not self.model:
            raise ValueError("NOTION_TOKEN, NOTION_DATABASE_ID, LLM_MODEL are required")
        if self.recent_count < 1 or self.chunk_chars < 1000:
            raise ValueError("RECENT_ARTICLE_COUNT >= 1 and LLM_CHUNK_CHARS >= 1000 required")
        if self.max_tokens < 1:
            raise ValueError("LLM_MAX_TOKENS must be positive")
        if self.reasoning_effort not in (
            "",
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ):
            raise ValueError("Invalid LLM_REASONING_EFFORT")
        ZoneInfo(self.timezone)
        if not self.model.startswith(("openai/", "anthropic/", "gemini/", "xai/", "openrouter/")):
            raise ValueError("LLM_MODEL must include a supported provider prefix")

        if self.model.startswith("openrouter/") and self.model.endswith(":batch"):
            raise ValueError(
                "OpenRouter :batch models cannot use chat/completions. "
                "This application supports synchronous models only; "
                "remove :batch from LLM_MODEL to use the standard model. "
                "Standard model pricing differs from batch pricing."
            )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            notion_token=os.getenv("NOTION_TOKEN", ""),
            database_id=os.getenv("NOTION_DATABASE_ID", ""),
            model=os.getenv("LLM_MODEL", ""),
            data_source_id=os.getenv("NOTION_DATA_SOURCE_ID", ""),
            timezone=os.getenv("TIMEZONE", "Asia/Tokyo"),
            summary_property=os.getenv("NOTION_SUMMARY_PROPERTY", "summary"),
            tags_property=os.getenv("NOTION_TAG_PROPERTY", "tag"),
            time_property=os.getenv("NOTION_SUMMARY_TIME_PROPERTY", "summary_time"),
            recent_count=int(os.getenv("RECENT_ARTICLE_COUNT", "100")),
            chunk_chars=int(os.getenv("LLM_CHUNK_CHARS", "12000")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
            reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip(),
        )
