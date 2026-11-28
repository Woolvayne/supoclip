"""Unit tests for automatic source-channel credit watermarks and social attribution."""

from pathlib import Path

from src.media.captions import (
     build_assemblyai_ass_subtitles,
    build_source_watermark_ass,
    format_source_watermark_text,
    resolve_task_watermark_text,
)
from src.media.common import HOOK_TITLE_TOP_MARGIN_FRAC
from src.services.social_caption_service import (
    append_source_credit,
    fallback_social_post_metadata,
)
from src.stream_source import stream_channel_from_url


def test_format_source_watermark_text_handles_names_prefixes_and_truncation():
    assert format_source_watermark_text("Finanzfluss") == "Credit: Finanzfluss"
    assert format_source_watermark_text("  @MontanaBlack \n ") == "Credit: @MontanaBlack"
    assert format_source_watermark_text("Credit: Tagesschau") == "Credit: Tagesschau"
    assert format_source_watermark_text("Quelle: MaiLab") == "Quelle: MaiLab"
    assert format_source_watermark_text("via Lex Fridman") == "via Lex Fridman"
    assert format_source_watermark_text("") is None
    assert format_source_watermark_text("   ") is None
    assert format_source_watermark_text(None) is None

    long_name = "A" * 60
    formatted = format_source_watermark_text(long_name)
    assert formatted is not None
    assert formatted.startswith("Credit: ")
    assert formatted.endswith("…")
    assert len(formatted) <= len("Credit: ") + 40


def test_resolve_task_watermark_text_defaults_on_and_respects_opt_out():
    assert resolve_task_watermark_text({"source_channel": "MrBeast"}) == "Credit: MrBeast"
    assert resolve_task_watermark_text({}, fallback_channel="Papaplatte") == "Credit: Papaplatte"
    assert (
        resolve_task_watermark_text(
            {"source_channel": "AutoName", "watermark_text": "CustomHandle"}
        )
        == "Credit: CustomHandle"
    )
    assert (
        resolve_task_watermark_text(
            {"source_channel": "MrBeast", "source_watermark": False},
            fallback_channel="MrBeast",
        )
        is None
    )


def test_build_source_watermark_ass_safe_zone_and_time_map():
    style_line, events = build_source_watermark_ass(
        "Finanzfluss",
        video_width=1080,
        video_height=1920,
        output_duration=24.5,
        time_map=lambda t: t * 0.8,
    )
    assert style_line.startswith("Style: Watermark,")
    fields = style_line.split(",")
    alignment = int(fields[18])
    margin_v = int(fields[21])
    hook_top_px = int(1920 * HOOK_TITLE_TOP_MARGIN_FRAC)

    assert alignment == 7  # top-left safe zone
    assert 36 <= margin_v < hook_top_px
    assert len(events) == 1
    assert "Dialogue: 2,0:00:00.00,0:00:19.60,Watermark," in events[0]
    assert "Credit: " in events[0]
    assert "Finanzfluss" in events[0]


def test_build_assemblyai_ass_subtitles_renders_watermark_even_without_subtitles(tmp_path: Path):
    dummy_video = tmp_path / "dummy.mp4"
    dummy_video.write_bytes(b"")
    ass_path = tmp_path / "watermark_only.ass"

    ok = build_assemblyai_ass_subtitles(
        dummy_video,
        clip_start=0.0,
        clip_end=15.0,
        video_width=1080,
        video_height=1920,
        output_ass_path=ass_path,
        include_captions=False,
        hook_title=None,
        watermark_text="Tagesschau",
    )
    assert ok is True
    content = ass_path.read_text(encoding="utf-8")
    assert "Style: Watermark," in content
    assert "Dialogue: 2,0:00:00.00,0:00:15.00,Watermark," in content
    assert "Tagesschau" in content


def test_stream_channel_from_url_extracts_twitch_and_kick_channels():
    assert (
        stream_channel_from_url("https://www.twitch.tv/papaplatte/clip/CleverClip-abc123")
        == "papaplatte"
    )
    assert (
        stream_channel_from_url(
            "https://kick.com/xqc/videos/11111111-2222-3333-4444-555555555555"
        )
        == "xqc"
    )
    assert (
        stream_channel_from_url("https://kick.com/trainwreckstv/clips/clip_01HXYZ123")
        == "trainwreckstv"
    )
    assert stream_channel_from_url("https://www.twitch.tv/videos/987654321") is None


def test_social_caption_credit_and_hashtag_attribution():
    assert (
        append_source_credit("Starkes Argument zum ETF-Sparplan!", "Finanzfluss")
        == "Starkes Argument zum ETF-Sparplan!\n\nCredit: Finanzfluss"
    )
    # Already credited -> no duplicate line
    already = "Starkes Video von Finanzfluss über ETFs."
    assert append_source_credit(already, "Finanzfluss") == already
    assert append_source_credit("Nur Text", None) == "Nur Text"

    meta = fallback_social_post_metadata(
        {"hook_title": "Warum Zinseszins alles verändert", "text": "Langfristig investieren lohnt sich extrem."},
        source_channel="Finanzfluss",
    )
    assert meta["caption"].endswith("Credit: Finanzfluss")
    assert "finanzfluss" in meta["hashtags"]
