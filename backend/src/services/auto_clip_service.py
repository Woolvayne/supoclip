"""Auto-clip workflows: watch a YouTube channel and clip every new upload.

A workflow stores the channel to watch and the newest video seen so far. The
cron worker polls each enabled workflow's RSS feed (no API key, no quota); every
upload newer than the marker becomes a regular highlights task with the user's
defaults, minutes charged exactly like a task the user created by hand.

The UI calls this feature "Workflows"; internally ``task.workflow`` is the clip
kind (highlights, series, …), so everything here keeps the ``auto_clip_`` name.
"""

from __future__ import annotations

import logging
import math
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Config, get_config
from ..models import AutoClipWorkflow
from ..repositories.task_repository import TaskRepository
from ..source_identity import source_key_for_url
from ..utils.async_helpers import run_in_thread
from ..workers.job_queue import JobQueue
from ..youtube_channels import (
    ChannelFeedEntry,
    ChannelFeedError,
    ChannelResolutionError,
    fetch_channel_feed,
    resolve_channel,
    select_new_entries,
)
from .billing_service import BillingLimitExceeded, BillingService
from .task_service import TaskService

logger = logging.getLogger(__name__)

# One poll clips at most this many uploads; a burst keeps working on the rest
# on the following polls without flooding the queue (and the user's minutes).
MAX_CLIPS_PER_CHECK = 3
LAST_ERROR_MAX_CHARS = 500


class AutoClipError(Exception):
    """User-visible failure with the HTTP status the route should answer."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _public_workflow(row: Any) -> Dict[str, Any]:
    def iso(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    return {
        "id": row.id,
        "channel_url": row.channel_url,
        "channel_id": row.channel_id,
        "channel_title": row.channel_title,
        "enabled": bool(row.enabled),
        "last_checked_at": iso(row.last_checked_at),
        "last_triggered_at": iso(row.last_triggered_at),
        "videos_clipped": int(row.videos_clipped or 0),
        "last_error": row.last_error,
        "created_at": iso(row.created_at),
    }


def _trim_error(message: str) -> str:
    return message[:LAST_ERROR_MAX_CHARS]


class AutoClipService:
    def __init__(
        self,
        db: AsyncSession,
        config: Optional[Config] = None,
        queue_adapter=None,
    ):
        self.db = db
        self.config = config or get_config()
        self.queue_adapter = queue_adapter or JobQueue

    # ------------------------------------------------------------------
    # Workflows CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _workspace_filter(user_id: str, team_id: Optional[str]):
        """One workspace's workflows: the team's, or the user's own."""
        if team_id:
            return AutoClipWorkflow.team_id == team_id
        return (AutoClipWorkflow.user_id == user_id) & (
            AutoClipWorkflow.team_id.is_(None)
        )

    async def list_workflows(
        self, user_id: str, team_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        rows = (
            await self.db.execute(
                select(AutoClipWorkflow)
                .where(self._workspace_filter(user_id, team_id))
                .order_by(AutoClipWorkflow.created_at.desc())
            )
        ).scalars().all()
        return [_public_workflow(row) for row in rows]

    async def create_workflow(
        self, user_id: str, team_id: Optional[str], channel_url: str
    ) -> Dict[str, Any]:
        raw = (channel_url or "").strip()
        if not raw:
            raise AutoClipError(422, "Enter a YouTube channel link.")
        if len(raw) > 500:
            raise AutoClipError(422, "That channel link is too long.")
        try:
            resolved = await run_in_thread(resolve_channel, raw)
        except ChannelResolutionError as exc:
            raise AutoClipError(422, str(exc)) from exc
        except ChannelFeedError as exc:
            raise AutoClipError(502, str(exc)) from exc

        now = datetime.now(timezone.utc)
        newest = resolved.entries[0] if resolved.entries else None
        row = AutoClipWorkflow(
            user_id=user_id,
            team_id=team_id,
            channel_url=raw,
            channel_id=resolved.channel_id,
            channel_title=resolved.channel_title,
            enabled=True,
            # Seed the marker so existing uploads are never clipped as backlog.
            # A channel without videos yet seeds with the creation time, so its
            # first upload does count as new.
            last_video_id=newest.video_id if newest else None,
            last_video_published_at=(newest.published_at if newest else now),
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        logger.info(
            "Auto-clip workflow %s watches channel %s", row.id, resolved.channel_id
        )
        return _public_workflow(row)

    async def _get_owned(
        self, workflow_id: str, user_id: str, team_id: Optional[str]
    ) -> Any:
        row = (
            await self.db.execute(
                select(AutoClipWorkflow).where(
                    AutoClipWorkflow.id == workflow_id,
                    self._workspace_filter(user_id, team_id),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise AutoClipError(404, "Workflow not found.")
        return row

    async def set_enabled(
        self, workflow_id: str, user_id: str, team_id: Optional[str], enabled: bool
    ) -> Dict[str, Any]:
        row = await self._get_owned(workflow_id, user_id, team_id)
        row.enabled = bool(enabled)
        await self.db.commit()
        await self.db.refresh(row)
        return _public_workflow(row)

    async def delete_workflow(
        self, workflow_id: str, user_id: str, team_id: Optional[str]
    ) -> None:
        row = await self._get_owned(workflow_id, user_id, team_id)
        await self.db.delete(row)
        await self.db.commit()

    # ------------------------------------------------------------------
    # The cron pass
    # ------------------------------------------------------------------

    async def check_new_uploads(self, limit: int = 25) -> Dict[str, int]:
        """Poll every enabled workflow once. Safe to run on a schedule."""
        rows = (
            await self.db.execute(
                select(AutoClipWorkflow)
                .where(AutoClipWorkflow.enabled.is_(True))
                .order_by(AutoClipWorkflow.last_checked_at.asc().nullsfirst())
                .limit(limit)
            )
        ).scalars().all()

        summary = {"checked": 0, "clipped": 0, "skipped": 0, "failed": 0, "limited": 0}
        for row in rows:
            try:
                outcome = await self._check_workflow(row)
            except Exception:  # noqa: BLE001 - one broken channel must not stop the rest
                logger.exception("Auto-clip check failed for workflow %s", row.id)
                outcome = {"failed": 1}
            summary["checked"] += 1
            for key in ("clipped", "skipped", "failed", "limited"):
                summary[key] += outcome.get(key, 0)
        return summary

    async def _check_workflow(self, row: Any) -> Dict[str, int]:
        row.last_checked_at = datetime.now(timezone.utc)
        outcome = {"clipped": 0, "skipped": 0, "failed": 0, "limited": 0}
        try:
            _title, entries = await run_in_thread(fetch_channel_feed, row.channel_id)
        except ChannelFeedError as exc:
            row.last_error = _trim_error(str(exc))
            await self.db.commit()
            return outcome

        if row.last_video_id is None and row.last_video_published_at is None:
            # Never seeded: record what the channel has now and clip nothing,
            # so the backlog stays untouched.
            if entries:
                row.last_video_id = entries[0].video_id
                row.last_video_published_at = entries[0].published_at
            await self.db.commit()
            return outcome

        new_entries = select_new_entries(
            entries, row.last_video_id, row.last_video_published_at
        )[:MAX_CLIPS_PER_CHECK]

        for entry in new_entries:
            result = await self._clip_entry(row, entry)
            outcome[result] += 1
            if result == "limited":
                # Out of minutes: keep the marker so the upload is clipped
                # when the allowance resets instead of being dropped.
                break
        await self.db.commit()
        return outcome

    async def _clip_entry(self, row: Any, entry: ChannelFeedEntry) -> str:
        """Clip one upload. Returns clipped | skipped | failed | limited.

        The marker advances unless the video should be retried later (limited).
        """
        url = f"https://www.youtube.com/watch?v={entry.video_id}"
        try:
            source_key = await source_key_for_url(self.db, row.user_id, url)
            if source_key:
                active = await TaskRepository.find_active_source_task(
                    self.db, row.user_id, source_key, team_id=row.team_id
                )
                if active is not None:
                    # The workspace already processes this video (e.g. the user
                    # queued it by hand). Count it as handled.
                    self._advance(row, entry)
                    return "skipped"

            task_service = TaskService(self.db)
            try:
                duration = await task_service.video_service.measure_source_duration(
                    url, row.user_id
                )
            except Exception as exc:  # noqa: BLE001 - an unmeasurable video is dead
                logger.warning(
                    "Auto-clip could not measure %s (%s)", entry.video_id, exc
                )
                row.last_error = _trim_error(
                    f"Could not read the duration of “{entry.title or entry.video_id}”."
                )
                self._advance(row, entry)
                return "failed"

            try:
                await BillingService(self.db).assert_can_create_task(
                    row.user_id,
                    math.ceil(duration),
                    team_id=row.team_id,
                )
            except BillingLimitExceeded as exc:
                row.last_error = _trim_error(
                    exc.summary.get("reason")
                    or "Your plan's video limit is reached."
                )
                return "limited"

            task_id = await task_service.create_task_with_source(
                user_id=row.user_id,
                url=url,
                title=entry.title or None,
                source_duration_seconds=math.ceil(duration),
                processing_mode=self.config.default_processing_mode,
                team_id=row.team_id,
            )
            await self._enqueue_task(task_id, url, row.user_id, source_key)
            self._advance(row, entry)
            row.videos_clipped = int(row.videos_clipped or 0) + 1
            row.last_error = None
            logger.info(
                "Auto-clip workflow %s queued task %s for video %s",
                row.id,
                task_id,
                entry.video_id,
            )
            return "clipped"
        except Exception as exc:  # noqa: BLE001 - one bad upload must not block the next
            logger.exception(
                "Auto-clip workflow %s failed on video %s", row.id, entry.video_id
            )
            row.last_error = _trim_error(
                f"Could not clip “{entry.title or entry.video_id}”: {exc}"
            )
            self._advance(row, entry)
            return "failed"

    async def _enqueue_task(
        self, task_id: str, url: str, user_id: str, source_key: Optional[str]
    ) -> None:
        """Queue the task the same way the create-task route does."""
        processing_mode = self.config.default_processing_mode
        processing_job_id = f"{task_id}:{secrets.token_hex(16)}"
        await TaskRepository.save_source_metadata(
            self.db,
            task_id,
            {
                "processing_job_id": processing_job_id,
                "clip_length": "auto",
                "enqueued_at": datetime.now(timezone.utc).isoformat(),
                "url": url,
                "source_type": "youtube",
                "output_format": "vertical",
                "add_subtitles": True,
                **({"source_key": source_key} if source_key else {}),
            },
        )
        await self.queue_adapter.enqueue_processing_job(
            "process_video_task",
            processing_mode,
            task_id,
            url,
            "youtube",
            user_id,
            None,  # font_family
            None,  # font_size
            None,  # font_color
            "default",  # caption_template
            processing_mode,
            "vertical",  # output_format
            True,  # add_subtitles
            None,  # cleanup_settings
            "auto",  # clip_length
            _job_id=processing_job_id,
        )

    def _advance(self, row: Any, entry: ChannelFeedEntry) -> None:
        """Mark the upload as handled so it is never clipped twice."""
        row.last_video_id = entry.video_id
        row.last_video_published_at = entry.published_at
        row.last_triggered_at = datetime.now(timezone.utc)
