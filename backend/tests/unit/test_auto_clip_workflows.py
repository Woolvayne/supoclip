"""Unit tests for the auto-clip workflow building blocks.

Pure parsing/selection logic plus the marker semantics of the clipping step;
network access and the database stay out of these tests.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.auto_clip_service import AutoClipService
from src.youtube_channels import (
    ChannelFeedEntry,
    ChannelFeedError,
    ChannelResolutionError,
    channel_id_from_page_html,
    channel_id_from_url,
    normalize_channel_url,
    parse_channel_feed,
    select_new_entries,
)

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Example Channel</title>
  <entry>
    <id>yt:video:NEWEST00000000000000000</id>
    <yt:videoId>NEWEST00000000000000000</yt:videoId>
    <title>Newest video</title>
    <published>2026-11-27T12:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:MIDDLE00000000000000000</id>
    <yt:videoId>MIDDLE00000000000000000</yt:videoId>
    <title>Middle video</title>
    <published>2026-11-20T12:00:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:OLDEST00000000000000000</id>
    <title>Oldest video (id only in the entry id)</title>
    <published>2026-11-13T12:00:00+00:00</published>
  </entry>
</feed>
"""


def entry(video_id: str, published: str, title: str = "") -> ChannelFeedEntry:
    return ChannelFeedEntry(
        video_id=video_id,
        title=title or video_id,
        published_at=datetime.fromisoformat(published).replace(tzinfo=timezone.utc),
    )


class TestChannelUrls:
    def test_channel_id_from_channel_url(self):
        url = "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"
        assert channel_id_from_url(url) == "UCuAXFkgsw1L7xaCfnd5JJOw"

    def test_handle_urls_have_no_direct_id(self):
        assert channel_id_from_url("https://www.youtube.com/@MrBeast") is None
        assert channel_id_from_url("https://www.youtube.com/c/Vsauce") is None

    def test_normalize_channel_url(self):
        assert normalize_channel_url("@MrBeast") == "https://www.youtube.com/@MrBeast"
        assert (
            normalize_channel_url("www.youtube.com/@MrBeast")
            == "https://www.youtube.com/@MrBeast"
        )
        assert (
            normalize_channel_url("https://www.youtube.com/@MrBeast")
            == "https://www.youtube.com/@MrBeast"
        )

    def test_normalize_rejects_empty(self):
        with pytest.raises(ChannelResolutionError):
            normalize_channel_url("   ")

    def test_channel_id_from_page_html(self):
        html = '<link rel="canonical" href="https://www.youtube.com/@x">"channelId":"UCuAXFkgsw1L7xaCfnd5JJOw"'
        assert channel_id_from_page_html(html) == "UCuAXFkgsw1L7xaCfnd5JJOw"

    def test_channel_id_from_page_html_external_id(self):
        html = 'window.yt = {"externalId":"UC1234567890123456789012"};'
        assert channel_id_from_page_html(html) == "UC1234567890123456789012"

    def test_channel_id_missing_from_page(self):
        assert channel_id_from_page_html("<html>nothing here</html>") is None


class TestFeedParsing:
    def test_parses_title_and_entries_newest_first(self):
        title, entries = parse_channel_feed(FEED_XML)
        assert title == "Example Channel"
        assert [item.video_id for item in entries] == [
            "NEWEST00000000000000000",
            "MIDDLE00000000000000000",
            "OLDEST00000000000000000",
        ]
        assert entries[0].title == "Newest video"

    def test_entry_id_fallback(self):
        _, entries = parse_channel_feed(FEED_XML)
        oldest = entries[-1]
        assert oldest.video_id == "OLDEST00000000000000000"

    def test_broken_xml_raises(self):
        with pytest.raises(ChannelFeedError):
            parse_channel_feed("<feed><entry>")


class TestSelectNewEntries:
    def test_unseeded_workflow_sees_nothing(self):
        entries = [entry("A", "2026-11-27T12:00:00")]
        assert select_new_entries(entries, None, None) == []

    def test_no_entries(self):
        assert (
            select_new_entries([], "A", datetime(2026, 11, 1, tzinfo=timezone.utc)) == []
        )

    def test_marker_video_excludes_the_backlog(self):
        entries = [
            entry("NEW", "2026-11-27T12:00:00"),
            entry("MID", "2026-11-20T12:00:00"),
            entry("OLD", "2026-11-13T12:00:00"),
        ]
        fresh = select_new_entries(
            entries, "MID", datetime(2026, 11, 20, tzinfo=timezone.utc)
        )
        assert [item.video_id for item in fresh] == ["NEW"]

    def test_two_uploads_come_back_oldest_first(self):
        entries = [
            entry("NEW2", "2026-11-27T12:00:00"),
            entry("NEW1", "2026-11-26T12:00:00"),
            entry("OLD", "2026-11-13T12:00:00"),
        ]
        fresh = select_new_entries(
            entries, "OLD", datetime(2026, 11, 13, tzinfo=timezone.utc)
        )
        assert [item.video_id for item in fresh] == ["NEW1", "NEW2"]

    def test_missing_marker_falls_back_to_the_published_cutoff(self):
        entries = [
            entry("NEW", "2026-11-27T12:00:00"),
            entry("OLD", "2026-11-13T12:00:00"),
        ]
        fresh = select_new_entries(
            entries, "DELETED", datetime(2026, 11, 20, tzinfo=timezone.utc)
        )
        assert [item.video_id for item in fresh] == ["NEW"]

    def test_missing_marker_without_cutoff_seeds_only(self):
        entries = [entry("NEW", "2026-11-27T12:00:00")]
        assert select_new_entries(entries, "DELETED", None) == []

    def test_first_upload_after_empty_channel_creation_counts_as_new(self):
        entries = [entry("FIRST", "2026-11-27T12:00:00")]
        fresh = select_new_entries(
            entries, None, datetime(2026, 11, 20, tzinfo=timezone.utc)
        )
        assert [item.video_id for item in fresh] == ["FIRST"]


def make_row(**overrides):
    row = SimpleNamespace(
        id="wf-1",
        user_id="user-1",
        team_id=None,
        channel_id="UC1234567890123456789012",
        last_video_id="OLD",
        last_video_published_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
        last_error=None,
        videos_clipped=0,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class TestClipEntryMarker:
    """The marker advances past handled videos, but not past a billing stop."""

    def service(self):
        db = MagicMock()
        db.commit = AsyncMock()
        return AutoClipService(db)

    async def test_clipped_advances_the_marker(self, monkeypatch):
        service = self.service()
        row = make_row()
        item = entry("NEW", "2026-11-27T12:00:00", "Newest video")

        monkeypatch.setattr(
            "src.services.auto_clip_service.source_key_for_url",
            AsyncMock(return_value="key"),
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.TaskRepository.find_active_source_task",
            AsyncMock(return_value=None),
        )
        fake_task_service = MagicMock()
        fake_task_service.video_service.measure_source_duration = AsyncMock(
            return_value=95.0
        )
        fake_task_service.create_task_with_source = AsyncMock(return_value="task-1")
        monkeypatch.setattr(
            "src.services.auto_clip_service.TaskService",
            lambda _db: fake_task_service,
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.BillingService",
            lambda _db: MagicMock(
                assert_can_create_task=AsyncMock(return_value={}),
            ),
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.AutoClipService._enqueue_task",
            AsyncMock(),
        )

        result = await service._clip_entry(row, item)

        assert result == "clipped"
        assert row.last_video_id == "NEW"
        assert row.videos_clipped == 1
        assert row.last_error is None

    async def test_billing_limit_keeps_the_marker_for_later(self, monkeypatch):
        from src.services.billing_service import BillingLimitExceeded

        service = self.service()
        row = make_row()
        item = entry("NEW", "2026-11-27T12:00:00", "Newest video")

        monkeypatch.setattr(
            "src.services.auto_clip_service.source_key_for_url",
            AsyncMock(return_value="key"),
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.TaskRepository.find_active_source_task",
            AsyncMock(return_value=None),
        )
        fake_task_service = MagicMock()
        fake_task_service.video_service.measure_source_duration = AsyncMock(
            return_value=95.0
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.TaskService",
            lambda _db: fake_task_service,
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.BillingService",
            lambda _db: MagicMock(
                assert_can_create_task=AsyncMock(
                    side_effect=BillingLimitExceeded({"reason": "No minutes left."})
                ),
            ),
        )

        result = await service._clip_entry(row, item)

        assert result == "limited"
        assert row.last_video_id == "OLD"
        assert row.last_error == "No minutes left."

    async def test_dead_video_advances_and_reports(self, monkeypatch):
        service = self.service()
        row = make_row()
        item = entry("NEW", "2026-11-27T12:00:00", "Newest video")

        monkeypatch.setattr(
            "src.services.auto_clip_service.source_key_for_url",
            AsyncMock(return_value="key"),
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.TaskRepository.find_active_source_task",
            AsyncMock(return_value=None),
        )
        fake_task_service = MagicMock()
        fake_task_service.video_service.measure_source_duration = AsyncMock(
            side_effect=RuntimeError("video unavailable")
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.TaskService",
            lambda _db: fake_task_service,
        )

        result = await service._clip_entry(row, item)

        assert result == "failed"
        assert row.last_video_id == "NEW"
        assert "Newest video" in row.last_error

    async def test_already_processing_video_is_skipped_once(self, monkeypatch):
        service = self.service()
        row = make_row()
        item = entry("NEW", "2026-11-27T12:00:00")

        monkeypatch.setattr(
            "src.services.auto_clip_service.source_key_for_url",
            AsyncMock(return_value="key"),
        )
        monkeypatch.setattr(
            "src.services.auto_clip_service.TaskRepository.find_active_source_task",
            AsyncMock(return_value=SimpleNamespace(id="task-9")),
        )

        result = await service._clip_entry(row, item)

        assert result == "skipped"
        assert row.last_video_id == "NEW"
        assert row.videos_clipped == 0
