import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.services import social_caption_service as captions


async def test_caption_uses_haiku_and_grounded_clip_data(monkeypatch):
    run = AsyncMock(return_value=SimpleNamespace(output="  Watch this idea.  "))
    settings = {}

    def agent(model, **kwargs):
        settings.update(kwargs)
        settings["model"] = model.model_name
        return SimpleNamespace(run=run)

    monkeypatch.setattr(captions, "Agent", agent)
    result = await captions.generate_social_caption(
        "test-key", {"hook_title": "An idea", "text": "A real transcript"}, "instagram"
    )
    assert result == "Watch this idea."
    assert settings["model"] == "claude-haiku-5-5"
    assert "temperature" not in settings["model_settings"]
    assert json.loads(run.await_args.args[0]) == {
        "platform": "instagram", "headline": "An idea", "transcript": "A real transcript"
    }


@pytest.mark.parametrize("output", [" ", "x" * 2201])
async def test_rejects_invalid_caption(monkeypatch, output):
    monkeypatch.setattr(captions, "Agent", lambda *args, **kwargs: SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(output=output))
    ))
    with pytest.raises(ValueError):
        await captions.generate_social_caption("test-key", {"text": "Transcript"}, "youtube")


async def test_generate_social_post_metadata_parses_json_and_hashtags(monkeypatch):
    payload = json.dumps({
        "title": "Warum 90% beim Investieren scheitern",
        "caption": "Der häufigste Fehler kostet dich Jahre an Rendite. Hier ist die einfache Lösung.",
        "hashtags": ["#Finanzen", "aktien", " Börse ", "shorts"],
    })
    monkeypatch.setattr(captions, "Agent", lambda *args, **kwargs: SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(output=f"```json\n{payload}\n```"))
    ))
    meta = await captions.generate_social_post_metadata(
        "test-key",
        {"hook_title": "Warum 90% scheitern", "text": "Der häufigste Fehler beim Investieren..."},
        "buffer",
    )
    assert meta["title"] == "Warum 90% scheitern"
    assert "Rendite" in meta["caption"]
    assert meta["hashtags"] == ["finanzen", "aktien", "börse", "shorts"]


async def test_generate_social_post_metadata_falls_back_cleanly_on_error(monkeypatch):
    monkeypatch.setattr(captions, "Agent", lambda *args, **kwargs: SimpleNamespace(
        run=AsyncMock(side_effect=RuntimeError("rate limited"))
    ))
    meta = await captions.generate_social_post_metadata(
        "test-key",
        {"hook_title": "Drei Produktivitäts-Tricks", "text": "Starte jeden Morgen mit Fokus und Klarheit."},
        "buffer",
        allow_fallback=True,
    )
    assert meta["title"] == "Drei Produktivitäts-Tricks"
    assert "Starte jeden Morgen" in meta["caption"]
    assert len(meta["hashtags"]) >= 3

