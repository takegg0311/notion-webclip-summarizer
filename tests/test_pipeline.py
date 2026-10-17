import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml

from webclip_summarizer.config import Settings
from webclip_summarizer.export import format_filename_title, write_markdown
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
    from dataclasses import replace

    output = tmp_path / "file"
    output.write_text("not a directory")
    notion = Mock()
    notion.query_pages.side_effect = [[], [page(), page("p2")]]
    notion.read_body.return_value = "body"
    llm = Mock()
    llm.summarize.return_value = result
    assert SummarizerImpl(replace(settings, output_dir=output), notion, llm).run() == 2
    assert notion.update_summary.call_count == 2


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
    from dataclasses import replace

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
