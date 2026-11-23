"""Unit tests for OpenRouter :free smart-routing, CPU thread caps, yt-dlp PO-token, and Buffer service."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from src.ai import TranscriptAnalysis, _build_transcript_model, _uses_native_output
from src.config import Config
from src.media.ffmpeg import _apply_ffmpeg_thread_limit
from src.openrouter_free import (
    FreeOpenRouterSmartModel,
    discover_free_models,
    extract_json_object,
    is_free_openrouter_model,
    sanitize_for_schema,
)
from src.youtube_utils import YouTubeDownloader, _build_info_options


def test_is_free_openrouter_model_distinguishes_free_from_paid():
    assert is_free_openrouter_model("openrouter/free") is True
    assert is_free_openrouter_model("nvidia/nemotron-3-super-120b-a12b:free") is True
    assert is_free_openrouter_model("qwen/qwen3-coder:free") is True
    assert is_free_openrouter_model("anthropic/claude-sonnet-5.5") is False
    assert _uses_native_output("openrouter:anthropic/claude-sonnet-5.5") is True
    assert _uses_native_output("openrouter:openrouter/free") is False


@pytest.mark.asyncio
async def test_discover_free_models_orders_nvidia_qwen_mistral():
    import src.openrouter_free as mod

    mod._catalog_cache = (0.0, [])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "mistralai/mistral-small-3.1-24b-instruct:free", "context_length": 96000},
                    {"id": "qwen/qwen3-coder:free", "context_length": 128000},
                    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "context_length": 128000},
                    {"id": "tiny/short-ctx:free", "context_length": 4096},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ordered = await discover_free_models("openrouter/free", client=client)

    assert ordered[0].startswith("nvidia/")
    assert ordered[1].startswith("qwen/")
    assert ordered[2].startswith("mistralai/")
    assert "tiny/short-ctx:free" not in ordered


def test_extract_and_sanitize_transcript_analysis_schema():
    raw_text = """<think>Analyzing lines...</think>
```json
{
  "most_relevant_segments": [
    {
      "start_time": "00:10",
      "end_time": "00:42",
      "segment": "Verbal text from transcript",
      "relevance_score": 88,
      "headline": "Why 2 cores keep servers responsive",
      "virality": {
        "hook_score": 29.4,
        "engagement_score": "19.8",
        "value_score": 21,
        "shareability_score": -3,
        "total_score": 105,
        "hook_type": "STATEMENT"
      }
    }
  ],
  "summary": "Overview"
}
```"""
    parsed = extract_json_object(raw_text)
    assert isinstance(parsed, dict)
    schema = TranscriptAnalysis.model_json_schema()
    cleaned = sanitize_for_schema(parsed, schema)
    validated = TranscriptAnalysis.model_validate(cleaned)
    seg = validated.most_relevant_segments[0]
    assert seg.text == "Verbal text from transcript"
    assert seg.hook_title == "Why 2 cores keep servers responsive"
    assert seg.relevance_score == pytest.approx(0.88)
    assert seg.virality.hook_score == 25
    assert seg.virality.engagement_score == 20
    assert seg.virality.shareability_score == 0
    assert seg.virality.total_score == 100
    assert seg.virality.hook_type == "statement"
    assert validated.key_topics == []


@pytest.mark.asyncio
async def test_free_openrouter_smart_model_fails_over_and_allows_data_collection():
    import src.openrouter_free as mod

    mod._catalog_cache = (999999999.0, ["nvidia/test:free", "qwen/test:free"])
    seen_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(body)
        if body["model"] == "nvidia/test:free":
            return httpx.Response(429, text="rate limited")
        return httpx.Response(
            200,
            json={
                "model": "qwen/test:free",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "most_relevant_segments": [],
                                    "summary": "Clean summary",
                                    "key_topics": ["testing"],
                                }
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 25},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = FreeOpenRouterSmartModel(
            "openrouter/free", api_key="test-or-key", http_client=client
        )
        params = ModelRequestParameters(
            output_mode="tool",
            output_tools=[
                ToolDefinition(
                    name="final_result",
                    description="Return analysis",
                    parameters_json_schema=TranscriptAnalysis.model_json_schema(),
                )
            ],
        )
        resp = await model.request(
            [ModelRequest(parts=[UserPromptPart(content="Analyze transcript")])],
            {"max_tokens": 2048},
            params,
        )

    assert len(seen_payloads) == 2
    assert seen_payloads[0]["provider"]["data_collection"] == "allow"
    assert seen_payloads[0]["tool_choice"] == "auto"
    assert resp.model_name == "qwen/test:free"
    assert isinstance(resp.parts[0], ToolCallPart)
    assert resp.parts[0].tool_name == "final_result"


def test_build_transcript_model_uses_smart_router_only_for_free_models(monkeypatch):
    monkeypatch.setenv("LLM", "openrouter:openrouter/free")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    cfg = Config()
    built = _build_transcript_model(cfg)
    assert isinstance(built, FreeOpenRouterSmartModel)


def test_ffmpeg_thread_limit_targets_active_video_encoder_only():
    encode_cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=black",
        "-c:v", "libx264", "-preset", "veryfast", "out.mp4",
    ]
    assert _apply_ffmpeg_thread_limit(encode_cmd, 2) == [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=black",
        "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "out.mp4",
    ]

    copy_cmd = ["ffmpeg", "-y", "-i", "in.mp4", "-c:v", "copy", "out.mp4"]
    assert _apply_ffmpeg_thread_limit(copy_cmd, 2) == copy_cmd

    probe_cmd = ["ffprobe", "-v", "error", "in.mp4"]
    assert _apply_ffmpeg_thread_limit(probe_cmd, 2) == probe_cmd


def test_ytdlp_direct_options_attach_pot_provider_without_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_POT_PROVIDER_URL", "http://127.0.0.1:4416")
    monkeypatch.delenv("YOUTUBE_PROXY_URL", raising=False)
    monkeypatch.setenv("TEMP_DIR", str(tmp_path))
    from src.config import set_config_override

    set_config_override(None)
    dl_opts = YouTubeDownloader().get_optimal_download_options("abc123XYZ00")
    info_opts = _build_info_options()
    assert dl_opts["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": ["http://127.0.0.1:4416"]
    }
    assert info_opts["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": ["http://127.0.0.1:4416"]
    }
