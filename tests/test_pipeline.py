import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, call

import httpx
import pytest
import yaml

from webclip_summarizer.config import Settings
from webclip_summarizer.export import (
    build_prefix_index,
    format_filename_title,
    is_exported,
    read_notion_id,
    write_markdown,
)
from webclip_summarizer.llm import LlmClientImpl, Summary, parse_summary
from webclip_summarizer.notion import NotionClientImpl
from webclip_summarizer.service import SummarizerImpl


@pytest.fixture
def settings():
    return Settings("secret", "db", "gemini/test")


@pytest.fixture
def result():
    return Summary("記事の要約", ["Python", "API", "Notion", "LLM", "自動化"], "記事")


def page(identifier="p1"):
    return {
        "object": "page",
        "id": identifier,
        "created_time": "2026-10-05T16:23:00Z",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "記事 - サイト"}]},
            "tag": {"type": "multi_select", "multi_select": [{"name": "Python"}]},
            "URL": {"type": "url", "url": "https://example.com"},
        },
    }


def response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_normalizes_and_reuses_tags(result):
    payload = json.dumps(
        {
            "summary": result.summary,
            "tags": ["python", "Web API", "Notion", "LLM", "自動化"],
            "filename_title": "記事",
        }
    )
    parsed = parse_summary(payload, ["Python", "Web_API"])
    assert parsed.tags[:2] == ["Python", "Web_API"]


@pytest.mark.parametrize("tags", [["a"] * 5, ["a", "b"], ["a,b", "b", "c", "d", "e"]])
def test_invalid_tags_are_rejected(tags):
    with pytest.raises(ValueError):
        parse_summary(json.dumps({"summary": "s", "tags": tags, "filename_title": "t"}), [])


def test_long_article_reads_all_chunks(settings, result):
    settings = Settings("t", "db", "xai/test", chunk_chars=1000)
    valid = json.dumps(result.__dict__)
    completion = Mock(
        side_effect=[response("first notes"), response("last notes"), response(valid)]
    )
    parsed = LlmClientImpl(settings, completion).summarize("title", "a" * 1000 + "LAST", [])
    assert parsed == result
    assert "LAST" in completion.call_args_list[1].kwargs["messages"][1]["content"]
    assert "first notes" in completion.call_args_list[2].kwargs["messages"][1]["content"]


def test_empty_body_does_not_call_llm(settings):
    completion = Mock()
    with pytest.raises(ValueError):
        LlmClientImpl(settings, completion).summarize("title", "", [])
    completion.assert_not_called()


def test_llm_retries_invalid_json(settings, result):
    completion = Mock(side_effect=[response("bad"), response(json.dumps(result.__dict__))])
    assert LlmClientImpl(settings, completion).summarize("title", "body", []) == result
    assert completion.call_count == 2


def test_markdown_frontmatter_and_collision(tmp_path, result):
    created = datetime.fromisoformat("2026-10-06T01:23:00+09:00")
    path = write_markdown(tmp_path, page(), "a: b", "body", result, created, "2026-10-06", "url")
    assert path.name == "20261006_0123_記事.md"
    metadata = yaml.safe_load(path.read_text().split("---", 2)[1])
    assert metadata["tags"] == result.tags
    assert metadata["title"] == "a: b"
    write_markdown(tmp_path, page(), "a: b", "body2", result, created, "now", "url")
    with pytest.raises(FileExistsError):
        write_markdown(tmp_path, page("other"), "t", "body", result, created, "now", "url")
    assert "body2" in path.read_text()


def test_filename_cannot_escape_directory():
    assert "/" not in format_filename_title("../../a/b: c")
    assert format_filename_title("Hello World") == "Hello_World"
    assert format_filename_title("日本 語") == "日本語"
    assert len(format_filename_title("あ" * 100)) == 60


def test_page_failure_isolated_and_not_marked(settings, result):
    notion = Mock()
    notion.query_pages.side_effect = [[page()], [page("failed"), page("ok")]]
    notion.read_body.return_value = "body"
    llm = Mock()
    llm.summarize.side_effect = [ValueError("bad output"), result]
    assert SummarizerImpl(settings, notion, llm).run() == 1
    assert notion.update_summary.call_count == 1
    assert notion.update_summary.call_args.args[0] == "ok"


def test_export_failure_does_not_stop_notion(settings, result, tmp_path):
    output = tmp_path / "file"
    output.write_text("not a directory")
    notion = Mock()
    notion.query_pages.side_effect = [[], [page(), page("p2")]]
    notion.read_body.return_value = "body"
    llm = Mock()
    llm.summarize.return_value = result
    assert SummarizerImpl(replace(settings, output_dir=output), notion, llm).run() == 3
    assert notion.update_summary.call_count == 2
    assert notion.query_pages.call_count == 2


def test_paginated_query_and_recursive_blocks(settings):
    def handler(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            if "start_cursor" not in payload:
                return httpx.Response(
                    200, json={"results": [page()], "has_more": True, "next_cursor": "next"}
                )
            return httpx.Response(200, json={"results": [page("p2")], "has_more": False})
        if "/child/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "leaf",
                            "type": "paragraph",
                            "paragraph": {"rich_text": [{"plain_text": "nested"}]},
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "child",
                        "type": "heading_2",
                        "heading_2": {"rich_text": [{"plain_text": "heading"}]},
                        "has_children": True,
                    }
                ],
                "has_more": False,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notion = NotionClientImpl(settings, client)
        notion.source_id = "source"
        assert len(list(notion.query_pages({}))) == 2
        assert "nested" in notion.read_body("p1")


def test_atomic_update_chunks_summary(settings):
    notion = NotionClientImpl(settings, Mock())
    notion.request = Mock()
    notion.update_summary("p", "あ" * 4500, ["a", "b", "c", "d", "e"], "now")
    properties = notion.request.call_args.kwargs["json"]["properties"]
    assert [len(x["text"]["content"]) for x in properties["summary"]["rich_text"]] == [
        2000,
        2000,
        500,
    ]
    assert properties["summary_time"]["date"]["start"] == "now"


def test_ambiguous_data_source_rejected(settings):
    notion = NotionClientImpl(settings, Mock())
    notion.request = Mock(return_value={"data_sources": [{"id": "a"}, {"id": "b"}]})
    with pytest.raises(ValueError, match="multiple sources"):
        notion.validate_schema()


def test_retry_rate_limit_then_success(settings, monkeypatch):
    sleeps = []
    monkeypatch.setattr("webclip_summarizer.notion.time.sleep", sleeps.append)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert NotionClientImpl(settings, client).request("GET", "databases/db") == {"ok": True}
    assert len(calls) == 2
    assert 2 in sleeps
    assert calls[0].headers["Notion-Version"] == "2025-09-03"


def test_export_only_uses_stored_summary(settings, result, tmp_path):
    completed = page()
    completed["properties"]["summary"] = {
        "type": "rich_text",
        "rich_text": [{"plain_text": "stored"}],
    }
    completed["properties"]["summary_time"] = {"type": "date", "date": {"start": "2026-10-06"}}
    notion = Mock()
    notion.query_pages.return_value = [completed]
    notion.read_body.return_value = "original body"
    llm = Mock()
    llm.format_title.return_value = "記事"
    assert SummarizerImpl(replace(settings, output_dir=tmp_path), notion, llm).run(True) == 0
    llm.summarize.assert_not_called()
    notion.update_summary.assert_not_called()
    assert "stored" in (tmp_path / "20261006_0123_記事.md").read_text()


def test_litellm_openrouter_429_retries_real_transport(monkeypatch, result):
    """Exercise LiteLLM's actual error mapping and tenacity import without network."""
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm
    import tenacity
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    monkeypatch.setattr(tenacity, "wait_exponential", lambda **kwargs: tenacity.wait_none())
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429, json={"error": {"code": 429, "message": "upstream rate limit"}}
            )
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(result.__dict__)},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = HTTPHandler(client=client)

        def completion(**kwargs):
            return litellm.completion(**kwargs, api_key="test-key", client=transport)

        parsed = LlmClientImpl(
            Settings("t", "db", "openrouter/openai/gpt-5-nano"), completion
        ).summarize("title", "body", [])
    assert parsed == result
    assert len(calls) == 2
    payload = json.loads(calls[-1].content)
    assert payload["reasoning"]["effort"] == "minimal"
    assert payload["max_tokens"] == 8192


def test_batch_model_rejected_before_api_call():
    with pytest.raises(ValueError, match="cannot use chat/completions"):
        Settings("t", "db", "openrouter/openai/gpt-5-nano:batch")


def test_unprefixed_model_rejected():
    with pytest.raises(ValueError, match="supported provider prefix"):
        Settings("t", "db", "local-model")


def test_openai_compatible_requires_base_url():
    with pytest.raises(ValueError, match="OPENAI_COMPATIBLE_BASE_URL"):
        Settings("t", "db", "openai_compatible/test-model")


def test_openai_compatible_accepts_with_base_url():
    settings = Settings(
        "t",
        "db",
        "openai_compatible/test-model",
        openai_compatible_base_url="http://example.invalid/v1",
    )
    assert settings.model == "openai_compatible/test-model"


def test_from_env_reads_openai_compatible_settings(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "token")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db")
    monkeypatch.setenv("LLM_MODEL", "openai_compatible/test-model")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "  http://host/v1  ")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "  sk-test  ")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    loaded = Settings.from_env()
    assert loaded.openai_compatible_base_url == "http://host/v1"
    assert loaded.openai_compatible_api_key == "sk-test"


def test_openai_compatible_routes_to_litellm(result):
    settings = Settings(
        "t",
        "db",
        "openai_compatible/test-model",
        openai_compatible_base_url="http://example.invalid/v1",
    )
    completion = Mock(return_value=response(json.dumps(result.__dict__)))
    LlmClientImpl(settings, completion).summarize("title", "body", [])
    assert completion.call_args.kwargs["model"] == "openai/test-model"
    assert completion.call_args.kwargs["api_base"] == "http://example.invalid/v1"
    assert completion.call_args.kwargs["api_key"] == "openai-compatible-unused"


def test_openai_compatible_passes_configured_api_key(result):
    settings = Settings(
        "t",
        "db",
        "openai_compatible/test-model",
        openai_compatible_base_url="http://example.invalid/v1",
        openai_compatible_api_key="sk-test",
    )
    completion = Mock(return_value=response(json.dumps(result.__dict__)))
    LlmClientImpl(settings, completion).summarize("title", "body", [])
    assert completion.call_args.kwargs["api_key"] == "sk-test"


def test_openai_compatible_dummy_key_not_openai_env(result, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    settings = Settings(
        "t",
        "db",
        "openai_compatible/test-model",
        openai_compatible_base_url="http://example.invalid/v1",
    )
    completion = Mock(return_value=response(json.dumps(result.__dict__)))
    LlmClientImpl(settings, completion).summarize("title", "body", [])
    api_key = completion.call_args.kwargs["api_key"]
    assert api_key
    assert api_key != "sk-from-env"


@pytest.mark.parametrize(
    "model",
    [
        "gemini/gemini-2.5-flash",
        "openai/gpt-4o",
        "openrouter/openai/gpt-5-nano",
    ],
)
def test_non_compatible_models_do_not_pass_api_base(model, result):
    settings = Settings(
        "t",
        "db",
        model,
        openai_compatible_base_url="http://example.invalid/v1",
        openai_compatible_api_key="sk-should-not-appear",
    )
    completion = Mock(return_value=response(json.dumps(result.__dict__)))
    LlmClientImpl(settings, completion).summarize("title", "body", [])
    assert "api_base" not in completion.call_args.kwargs
    assert "api_key" not in completion.call_args.kwargs


def test_standard_openrouter_model_accepted():
    assert (
        Settings("t", "db", "openrouter/openai/gpt-5-nano").model == "openrouter/openai/gpt-5-nano"
    )


def token_response(content, finish_reason="length", reasoning_tokens=2000):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))
        ],
        usage=SimpleNamespace(
            completion_tokens=2000,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
    )


def test_reasoning_exhaustion_retries_with_more_tokens(result):
    completion = Mock(side_effect=[token_response(None), response(json.dumps(result.__dict__))])
    llm = LlmClientImpl(Settings("t", "db", "openrouter/openai/gpt-5-nano"), completion)
    assert llm.summarize("title", "body", []) == result
    assert [call.kwargs["max_tokens"] for call in completion.call_args_list] == [8192, 16384]
    assert completion.call_args.kwargs["extra_body"]["reasoning"]["effort"] == "minimal"


def test_empty_response_has_bounded_retry_and_diagnostics(settings):
    completion = Mock(return_value=token_response(None))
    with pytest.raises(ValueError, match="reasoning_tokens=2000"):
        LlmClientImpl(settings, completion).summarize("title", "body", [])
    assert completion.call_count == 2


def test_truncated_nonempty_response_is_not_used(settings):
    completion = Mock(side_effect=[token_response("partial"), response("complete")])
    assert LlmClientImpl(settings, completion).format_title("title") == "complete"


def test_filtered_response_is_not_retried(settings):
    completion = Mock(return_value=token_response(None, "content_filter"))
    with pytest.raises(ValueError, match="refused"):
        LlmClientImpl(settings, completion).format_title("title")
    assert completion.call_count == 1


def test_explicit_token_and_effort_settings():
    completion = Mock(return_value=response("title"))
    settings = Settings(
        "t", "db", "openrouter/openai/gpt-5-nano", max_tokens=4000, reasoning_effort="low"
    )
    LlmClientImpl(settings, completion).format_title("title")
    assert completion.call_args.kwargs["max_tokens"] == 4000
    assert completion.call_args.kwargs["extra_body"]["reasoning"]["effort"] == "low"


def completed_page(identifier="saved"):
    completed = page(identifier)
    completed["properties"]["summary"] = {
        "type": "rich_text",
        "rich_text": [{"plain_text": "stored summary"}],
    }
    completed["properties"]["summary_time"] = {
        "type": "date",
        "date": {"start": "2026-10-06T02:00:00+09:00"},
    }
    return completed


def catchup_service(settings, output, completed, pending=()):
    notion = Mock()
    notion.query_pages.side_effect = [[], list(pending), completed]
    notion.read_body.return_value = "original body"
    llm = Mock()
    llm.format_title.return_value = "整形タイトル"
    return SummarizerImpl(replace(settings, output_dir=output), notion, llm)


def test_catchup_exports_saved_properties_to_missing_directory(settings, tmp_path, caplog):
    output = tmp_path / "missing"
    service = catchup_service(settings, output, [completed_page()])
    with caplog.at_level("INFO"):
        assert service.run() == 0
    path = output / "20261006_0123_整形タイトル.md"
    metadata = yaml.safe_load(path.read_text().split("---\n", 2)[1])
    assert metadata["summary"] == "stored summary"
    assert metadata["tags"] == ["Python"]
    assert metadata["summary_time"] == "2026-10-06T02:00:00+09:00"
    assert metadata["source"] == "https://example.com"
    assert metadata["created"] == "2026-10-06T01:23:00+09:00"
    assert "original body" in path.read_text()
    service.llm.summarize.assert_not_called()
    service.notion.update_summary.assert_not_called()
    service.llm.format_title.assert_called_once_with("記事 - サイト")
    service.notion.query_pages.assert_called_with(
        {"property": "summary_time", "date": {"is_not_empty": True}}, "descending"
    )
    assert "catchup_checked=1 catchup_exported=1 catchup_skipped=0" in caplog.text


def test_catchup_skips_matching_id_with_different_title(settings, tmp_path, monkeypatch):
    existing = tmp_path / "20261006_0123_別タイトル.md"
    existing.write_text("---\nnotion_id: saved\n---\nunchanged")
    writer = Mock()
    monkeypatch.setattr("webclip_summarizer.service.write_markdown", writer)
    service = catchup_service(settings, tmp_path, [completed_page()])
    assert service.run() == 0
    service.notion.read_body.assert_not_called()
    service.llm.format_title.assert_not_called()
    writer.assert_not_called()
    assert existing.read_text().endswith("unchanged")


def test_catchup_does_not_skip_other_id_in_same_minute(settings, tmp_path):
    existing = tmp_path / "20261006_0123_別タイトル.md"
    existing.write_text("---\nnotion_id: other\n---\n")
    service = catchup_service(settings, tmp_path, [completed_page()])
    assert service.run() == 0
    assert (tmp_path / "20261006_0123_整形タイトル.md").exists()
    assert read_notion_id(existing) == "other"


def test_no_catchup_query_without_output(settings):
    service = catchup_service(settings, None, [completed_page()])
    assert service.run() == 0
    assert service.notion.query_pages.call_count == 2
    service.llm.format_title.assert_not_called()


def test_export_only_forces_overwrite_without_catchup(settings, tmp_path):
    existing = tmp_path / "20261006_0123_整形タイトル.md"
    existing.write_text("---\nnotion_id: saved\n---\nold")
    notion = Mock()
    notion.query_pages.return_value = [completed_page()]
    notion.read_body.return_value = "new body"
    llm = Mock()
    llm.format_title.return_value = "整形タイトル"
    assert SummarizerImpl(replace(settings, output_dir=tmp_path), notion, llm).run(True) == 0
    notion.query_pages.assert_called_once_with(
        {"property": "summary_time", "date": {"is_not_empty": True}}, limit=None
    )
    notion.update_summary.assert_not_called()
    llm.summarize.assert_not_called()
    llm.format_title.assert_called_once()
    assert "new body" in existing.read_text()


@pytest.mark.parametrize("failure_stage", ["summarize", "write"])
def test_catchup_excludes_all_pending_ids(settings, tmp_path, result, monkeypatch, failure_stage):
    service = catchup_service(settings, tmp_path, [completed_page("p1")], [page()])
    service.llm.summarize.return_value = result
    writer = Mock()
    monkeypatch.setattr("webclip_summarizer.service.write_markdown", writer)
    if failure_stage == "write":
        writer.side_effect = OSError("write failed")
    else:
        service.llm.summarize.side_effect = ValueError("summarize failed")
    assert service.run() == 1
    assert writer.call_count == (1 if failure_stage == "write" else 0)
    service.notion.read_body.assert_called_once_with("p1")
    service.llm.format_title.assert_not_called()


def test_catchup_limit_is_separate_and_skips_do_not_consume_it(settings, tmp_path, result):
    (tmp_path / "20261006_0123_既存.md").write_text("---\nnotion_id: existing\n---\n")
    service = catchup_service(
        settings,
        tmp_path,
        [
            completed_page("p1"),
            completed_page("existing"),
            completed_page("first"),
            completed_page("second"),
        ],
        [page()],
    )
    service.llm.summarize.return_value = result
    assert service.run(limit=1) == 0
    service.notion.query_pages.assert_any_call(
        {"property": "summary_time", "date": {"is_empty": True}}, limit=1
    )
    service.notion.query_pages.assert_called_with(
        {"property": "summary_time", "date": {"is_not_empty": True}}, "descending"
    )
    service.notion.update_summary.assert_called_once()
    assert service.notion.read_body.call_args_list == [call("p1"), call("first")]
    service.llm.format_title.assert_called_once()
    assert read_notion_id(tmp_path / "20261006_0123_整形タイトル.md") == "first"


@pytest.mark.parametrize("stage", ["body", "title", "write", "collision"])
def test_catchup_page_failure_isolated(settings, tmp_path, monkeypatch, stage):
    service = catchup_service(settings, tmp_path, [completed_page("bad"), completed_page("ok")])
    if stage == "body":
        service.notion.read_body.side_effect = [OSError("body failed"), "body"]
    elif stage == "title":
        service.llm.format_title.side_effect = [ValueError("title failed"), "整形タイトル"]
    elif stage == "write":
        writer = Mock(side_effect=[OSError("disk full"), None])
        monkeypatch.setattr("webclip_summarizer.service.write_markdown", writer)
    else:
        (tmp_path / "20261006_0123_衝突.md").write_text("---\nnotion_id: other\n---\n")
        service.llm.format_title.side_effect = ["衝突", "整形タイトル"]
    assert service.run() == 1
    assert service.notion.read_body.call_args_list == [call("bad"), call("ok")]
    if stage == "write":
        assert writer.call_count == 2
    else:
        assert read_notion_id(tmp_path / "20261006_0123_整形タイトル.md") == "ok"


def test_catchup_failed_attempt_consumes_limit(settings, tmp_path):
    service = catchup_service(settings, tmp_path, [completed_page("bad"), completed_page("later")])
    service.llm.format_title.side_effect = ValueError("title failed")
    assert service.run(limit=1) == 1
    service.notion.read_body.assert_called_once_with("bad")
    service.llm.format_title.assert_called_once()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("yield_first", [False, True])
def test_catchup_query_failure_has_no_partial_exports(
    settings, tmp_path, result, monkeypatch, yield_first, caplog
):
    def broken_pages():
        if yield_first:
            yield completed_page()
        raise OSError("listing failed")

    service = catchup_service(settings, tmp_path, broken_pages(), [page()])
    service.llm.summarize.return_value = result
    writer = Mock()
    monkeypatch.setattr("webclip_summarizer.service.write_markdown", writer)
    with caplog.at_level("INFO"):
        assert service.run() == 1
    service.notion.update_summary.assert_called_once()
    service.notion.read_body.assert_called_once_with("p1")
    service.llm.format_title.assert_not_called()
    writer.assert_called_once()
    assert "Batch finished: pages=1 failures=1" in caplog.text


def test_catchup_unreadable_index_stops_before_query(settings, tmp_path, monkeypatch):
    def denied(path):
        raise PermissionError("directory unreadable")

    monkeypatch.setattr("webclip_summarizer.export.os.scandir", denied)
    service = catchup_service(settings, tmp_path, [completed_page()])
    assert service.run() == 1
    assert service.notion.query_pages.call_count == 2
    service.notion.read_body.assert_not_called()
    service.llm.format_title.assert_not_called()


@pytest.mark.parametrize("valid_first", [False, True])
def test_frontmatter_candidates_ignore_bad_yaml_in_any_order(tmp_path, valid_first):
    good = tmp_path / "20261006_0123_good.md"
    bad = tmp_path / "20261006_0123_bad.md"
    good.write_text("---\nnotion_id: p1\n---\n")
    bad.write_text("---\ninvalid: [\n---\n")
    paths = [good, bad] if valid_first else [bad, good]
    created = datetime.fromisoformat("2026-10-06T01:23:00+09:00")
    assert is_exported({"20261006_0123_": paths}, created, "p1")
    assert not is_exported({"20261006_0123_": [bad]}, created, "p1")


@pytest.mark.parametrize(
    "content",
    [
        b"no frontmatter",
        b"---\ninvalid: [\n---\n",
        b"---\n- list\n---\n",
        b"---\ntitle: missing id\n---\n",
        b"---\nnotion_id: p1\n",
        b"---\n\xff\n---\n",
    ],
)
def test_unusable_frontmatter_does_not_match_or_allow_overwrite(tmp_path, result, content):
    path = tmp_path / "20261006_0123_記事.md"
    path.write_bytes(content)
    assert read_notion_id(path) is None
    with pytest.raises(FileExistsError):
        write_markdown(
            tmp_path,
            page(),
            "title",
            "body",
            result,
            datetime.fromisoformat("2026-10-06T01:23:00+09:00"),
            "now",
            "url",
        )
    assert path.read_bytes() == content


def test_missing_or_unreadable_frontmatter_is_ignored(tmp_path, monkeypatch):
    assert read_notion_id(tmp_path / "missing") is None
    path = tmp_path / "unreadable.md"
    path.write_text("---\nnotion_id: p1\n---\n")

    def denied(*args, **kwargs):
        raise PermissionError("unreadable")

    monkeypatch.setattr(type(path), "open", denied)
    assert read_notion_id(path) is None


def test_dash_in_title_supports_catchup_detection_and_overwrite(settings, tmp_path, result):
    created = datetime.fromisoformat("2026-10-06T01:23:00+09:00")
    path = write_markdown(
        tmp_path, completed_page(), "記事---サイト", "body", result, created, "now", "url"
    )
    service = catchup_service(settings, tmp_path, [completed_page()])
    assert service.run() == 0
    service.notion.read_body.assert_not_called()
    service.llm.format_title.assert_not_called()
    write_markdown(
        tmp_path, completed_page(), "記事---サイト", "new body", result, created, "now", "url"
    )
    assert "new body" in path.read_text()


def test_prefix_index_scans_once_and_ignores_temporary_files(tmp_path, monkeypatch):
    (tmp_path / "20261006_0123_article.md").write_text("---\nnotion_id: p1\n---\n")
    (tmp_path / ".webclip-temp.md").write_text("temporary")
    (tmp_path / "20261006_0123_other.txt").write_text("text")
    (tmp_path / "20261006_0123_directory.md").mkdir()
    import os

    scanner = Mock(wraps=os.scandir)
    monkeypatch.setattr("webclip_summarizer.export.os.scandir", scanner)
    index = build_prefix_index(tmp_path)
    created = datetime.fromisoformat("2026-10-06T01:23:00+09:00")
    assert is_exported(index, created, "p1")
    assert not is_exported(index, created, "other")
    assert list(index) == ["20261006_0123_"]
    assert len(index["20261006_0123_"]) == 1
    scanner.assert_called_once_with(tmp_path)


def test_incomplete_query_warns_once_and_returns_pages(settings, caplog, monkeypatch):
    monkeypatch.setattr("webclip_summarizer.notion.time.sleep", lambda _: None)

    def handler(request):
        second = "start_cursor" in json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [page("p2" if second else "p1")],
                "has_more": not second,
                "next_cursor": "next",
                "request_status": {
                    "type": "incomplete",
                    "incomplete_reason": "query_limit_reached",
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notion = NotionClientImpl(settings, client)
        notion.source_id = "source"
        assert [p["id"] for p in notion.query_pages({})] == ["p1", "p2"]
    warnings = [record for record in caplog.records if "Notion query incomplete" in record.message]
    assert len(warnings) == 1
    assert "query_limit_reached" in warnings[0].message
