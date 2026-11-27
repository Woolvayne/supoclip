"""Watch YouTube channels for new uploads.

The auto-clip feature polls a channel's public RSS feed — it needs no API key
and no YouTube quota — and compares it against the newest video a workflow has
already seen. Channel resolution (handle or custom URL to a channel id) scrapes
the channel page once when a workflow is created.

Everything here is synchronous network I/O or pure parsing; async callers wrap
it with ``run_in_thread``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests

ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"

_CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
_CHANNEL_URL_ID_RE = re.compile(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})")
# Present in the initial HTML of @handle, /c/ and /user/ channel pages.
_PAGE_CHANNEL_ID_PATTERNS = (
    re.compile(r'"channelId":"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"'),
    _CHANNEL_URL_ID_RE,
)

_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SupoClip/1.0; +https://www.supoclip.com)",
    "Accept-Language": "en",
}
_FETCH_TIMEOUT = (10, 20)


class ChannelResolutionError(Exception):
    """The URL does not point at a findable YouTube channel."""


class ChannelFeedError(Exception):
    """The channel's upload feed could not be fetched or parsed."""


@dataclass(frozen=True)
class ChannelFeedEntry:
    video_id: str
    title: str
    published_at: datetime


@dataclass(frozen=True)
class ResolvedChannel:
    channel_id: str
    channel_title: Optional[str]
    entries: List[ChannelFeedEntry]  # newest first


def normalize_channel_url(url: str) -> str:
    """A bare ``@handle`` or host-less path becomes a full YouTube URL."""
    cleaned = (url or "").strip()
    if not cleaned:
        raise ChannelResolutionError("Enter a YouTube channel link.")
    if cleaned.startswith("@"):
        return f"https://www.youtube.com/{cleaned}"
    if "://" not in cleaned:
        return f"https://{cleaned}"
    return cleaned


def channel_id_from_url(url: str) -> Optional[str]:
    """The channel id in a /channel/UC… URL, or None for handles and custom URLs."""
    match = _CHANNEL_URL_ID_RE.search(url or "")
    return match.group(1) if match else None


def channel_id_from_page_html(html: str) -> Optional[str]:
    """The channel id embedded in a channel page's initial HTML."""
    for pattern in _PAGE_CHANNEL_ID_PATTERNS:
        match = pattern.search(html or "")
        if match:
            candidate = match.group(1)
            if _CHANNEL_ID_RE.fullmatch(candidate):
                return candidate
    return None


def feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _parse_published(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_channel_feed(xml_text: str) -> Tuple[Optional[str], List[ChannelFeedEntry]]:
    """Channel title and upload entries (newest first) from a feed's XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ChannelFeedError(f"Could not parse the channel feed: {exc}") from exc

    title = (root.findtext(f"{ATOM_NS}title") or "").strip() or None
    entries: List[ChannelFeedEntry] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        video_id = (entry.findtext(f"{YT_NS}videoId") or "").strip()
        if not video_id:
            raw_id = (entry.findtext(f"{ATOM_NS}id") or "").strip()
            # <id>yt:video:VIDEOID</id>
            if raw_id.startswith("yt:video:"):
                video_id = raw_id[len("yt:video:"):]
        published = _parse_published(entry.findtext(f"{ATOM_NS}published") or "")
        if not video_id or published is None:
            continue
        entry_title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
        entries.append(
            ChannelFeedEntry(
                video_id=video_id[:32], title=entry_title, published_at=published
            )
        )

    entries.sort(key=lambda item: item.published_at, reverse=True)
    return title, entries


def select_new_entries(
    entries: List[ChannelFeedEntry],
    last_video_id: Optional[str],
    last_video_published_at: Optional[datetime],
) -> List[ChannelFeedEntry]:
    """Entries to clip, oldest first.

    ``entries`` is newest first. A workflow that never saw the channel
    (no video id and no published cutoff) gets nothing: the caller records the
    current newest video instead of clipping the channel's backlog. When the
    marker video vanished from the feed (deleted, or older than the feed
    keeps), the published cutoff decides instead.
    """
    if not entries:
        return []

    newest_first = list(entries)
    if last_video_id:
        for index, entry in enumerate(newest_first):
            if entry.video_id == last_video_id:
                return list(reversed(newest_first[:index]))
        if last_video_published_at is None:
            return []
    elif last_video_published_at is None:
        return []

    cutoff = last_video_published_at
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    fresh = [entry for entry in newest_first if entry.published_at > cutoff]
    return list(reversed(fresh))


def _http_get(url: str) -> requests.Response:
    try:
        response = requests.get(
            url, headers=_FETCH_HEADERS, timeout=_FETCH_TIMEOUT, allow_redirects=True
        )
    except requests.RequestException as exc:
        raise ChannelFeedError(f"Could not reach YouTube: {exc}") from exc
    return response


def fetch_channel_feed(channel_id: str) -> Tuple[Optional[str], List[ChannelFeedEntry]]:
    """Fetch and parse the channel's upload feed (title + entries, newest first)."""
    response = _http_get(feed_url(channel_id))
    if response.status_code == 404:
        raise ChannelFeedError("This channel has no public upload feed.")
    if response.status_code >= 400:
        raise ChannelFeedError(f"YouTube returned {response.status_code} for the channel feed.")
    return parse_channel_feed(response.text)


def resolve_channel(url: str) -> ResolvedChannel:
    """Resolve a channel URL to its id, name, and current uploads.

    Raises ``ChannelResolutionError`` when no channel is behind the URL and
    ``ChannelFeedError`` when the channel exists but its feed cannot be read.
    """
    cleaned = normalize_channel_url(url)
    channel_id = channel_id_from_url(cleaned)
    if channel_id is None:
        response = _http_get(cleaned)
        if response.status_code >= 400:
            raise ChannelResolutionError("Couldn't find a YouTube channel at that link.")
        channel_id = channel_id_from_page_html(response.text)
        if channel_id is None:
            raise ChannelResolutionError("Couldn't find a YouTube channel at that link.")
    title, entries = fetch_channel_feed(channel_id)
    return ResolvedChannel(channel_id=channel_id, channel_title=title, entries=entries)


def watch_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"
