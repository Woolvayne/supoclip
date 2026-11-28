"""
Buffer publishing via Buffer's GraphQL API (https://api.buffer.com).

Unlike direct platform integrations that require each self-hosted operator to
register and audit separate OAuth apps with Google, TikTok, and Meta, Buffer
lets users authenticate with a Personal API Key generated at
``https://publish.buffer.com/settings/api``. One Buffer API key exposes every
social channel connected in the user's Buffer organization (TikTok, Instagram,
YouTube, Facebook, Threads, X/Twitter, LinkedIn, Bluesky, Mastodon, Pinterest),
and each channel is registered as an individual destination in SupoClip.

Buffer fetches video media from a public HTTPS URL, so ``needs_local_file`` is
``False`` and ``PublishRequest.public_media_url`` is passed in ``assets``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..config import Config, get_config
from .base import (
    DEFAULT_HTTP_TIMEOUT,
    AccountProfile,
    OAuthTokens,
    PostMetrics,
    PublishRequest,
    PublishResult,
    SocialProvider,
    SocialProviderError,
    ambiguous_outcome,
    build_caption,
)

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.buffer.com"
BUFFER_DASHBOARD_URL = "https://publish.buffer.com"

SERVICE_LABELS: Dict[str, str] = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "facebook": "Facebook",
    "threads": "Threads",
    "twitter": "X (Twitter)",
    "linkedin": "LinkedIn",
    "bluesky": "Bluesky",
    "mastodon": "Mastodon",
    "pinterest": "Pinterest",
    "google": "Google Business",
    "googlebusiness": "Google Business",
    "substack": "Substack",
}

SERVICE_CAPTION_LIMITS: Dict[str, int] = {
    "twitter": 280,
    "bluesky": 300,
    "threads": 500,
    "mastodon": 500,
    "instagram": 2200,
    "tiktok": 2200,
    "facebook": 2200,
    "linkedin": 3000,
    "youtube": 5000,
}

_ORGANIZATIONS_QUERY = """
query GetOrganizations {
  account {
    organizations {
      id
      name
    }
  }
}
"""

_CHANNELS_QUERY = """
query GetChannels($input: ChannelsInput!) {
  channels(input: $input) {
    id
    name
    displayName
    descriptor
    service
    avatar
    isDisconnected
  }
}
"""

_CHANNELS_MINIMAL_QUERY = """
query GetChannelsMinimal($input: ChannelsInput!) {
  channels(input: $input) {
    id
    name
    displayName
    service
    avatar
  }
}
"""

_CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post {
        id
        status
        dueAt
      }
    }
    ... on MutationError {
      message
    }
  }
}
"""


def normalize_buffer_token(raw_token: str) -> str:
    token = (raw_token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


class BufferProvider(SocialProvider):
    name = "buffer"
    display_name = "Buffer"
    auth_type = "api_key"
    supported_privacy_levels = ("public", "unlisted", "private")
    default_privacy_level = "public"
    needs_local_file = False

    def __init__(self, config: Optional[Config] = None, session=None):
        super().__init__(session)
        self.config = config or get_config()

    @property
    def is_configured(self) -> bool:
        return bool(self.config.buffer_enabled)

    # -- OAuth stubs (Buffer uses personal API keys via connect_buffer) --
    def build_authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: Optional[str] = None
    ) -> str:
        raise SocialProviderError(
            "Buffer connects with a Personal API Key from publish.buffer.com/settings/api."
        )

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: Optional[str] = None
    ) -> OAuthTokens:
        token = normalize_buffer_token(code)
        if not token:
            raise SocialProviderError("Buffer API key is required.")
        return OAuthTokens(access_token=token)

    def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        token = normalize_buffer_token(refresh_token)
        if not token:
            raise SocialProviderError(
                "Reconnect your Buffer API key in Social Settings.", reauth=True
            )
        return OAuthTokens(access_token=token, refresh_token=token)

    def fetch_profile(self, access_token: str) -> AccountProfile:
        channels = self.list_channels(access_token)
        if not channels:
            raise SocialProviderError(
                "No social channels found in your Buffer account. Connect TikTok, "
                "Instagram, or YouTube inside Buffer first, then reconnect."
            )
        return channels[0]

    # -- Buffer GraphQL helpers -----------------------------------------
    def _graphql(
        self,
        access_token: str,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        *,
        action: str,
    ) -> Dict[str, Any]:
        token = normalize_buffer_token(access_token)
        if not token:
            raise SocialProviderError("Missing Buffer API key.", reauth=True)

        body: Dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = variables

        response = self.http.post(
            GRAPHQL_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )

        if response.status_code in {401, 403}:
            raise SocialProviderError(
                "Buffer rejected the API key. Generate a valid key at "
                "publish.buffer.com/settings/api and reconnect.",
                reauth=True,
            )
        if response.status_code == 429:
            raise SocialProviderError(
                "Buffer API rate limit reached. Retrying shortly.",
                retryable=True,
            )
        if response.status_code >= 500:
            raise SocialProviderError(
                f"Buffer API is temporarily unavailable (HTTP {response.status_code}).",
                retryable=True,
            )
        if response.status_code >= 400:
            raise SocialProviderError(
                f"Could not {action}: {self._extract_error(response)}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SocialProviderError(
                f"Buffer returned an invalid JSON response while trying to {action}.",
                retryable=True,
            ) from exc

        if not isinstance(payload, dict):
            raise SocialProviderError(f"Unexpected Buffer response while trying to {action}.")

        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
            msg = str(first.get("message") or "GraphQL request failed")
            code = str((first.get("extensions") or {}).get("code") or "").upper()
            lowered = msg.lower()
            if (
                code in {"UNAUTHENTICATED", "FORBIDDEN", "UNAUTHORIZED"}
                or "unauthorized" in lowered
                or "unauthenticated" in lowered
                or "invalid token" in lowered
                or "invalid api key" in lowered
            ):
                raise SocialProviderError(
                    "Buffer rejected the API key. Check your key at publish.buffer.com/settings/api.",
                    reauth=True,
                )
            raise SocialProviderError(f"Buffer API error while trying to {action}: {msg}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise SocialProviderError(f"Buffer returned no data while trying to {action}.")
        return data

    def list_channels(self, access_token: str) -> List[AccountProfile]:
        """Return every active social channel across all Buffer organizations."""
        org_data = self._graphql(
            access_token,
            _ORGANIZATIONS_QUERY,
            action="load Buffer organizations",
        )
        organizations = ((org_data.get("account") or {}).get("organizations")) or []
        if not isinstance(organizations, list) or not organizations:
            raise SocialProviderError(
                "No Buffer organization was found for this API key."
            )

        profiles: List[AccountProfile] = []
        seen_ids: set[str] = set()

        for org in organizations:
            if not isinstance(org, dict) or not org.get("id"):
                continue
            org_id = str(org["id"])
            org_name = str(org.get("name") or "").strip() or None
            variables = {"input": {"organizationId": org_id}}
            try:
                ch_data = self._graphql(
                    access_token,
                    _CHANNELS_QUERY,
                    variables,
                    action="load Buffer channels",
                )
            except SocialProviderError as exc:
                if exc.reauth or exc.retryable:
                    raise
                ch_data = self._graphql(
                    access_token,
                    _CHANNELS_MINIMAL_QUERY,
                    variables,
                    action="load Buffer channels",
                )

            channels = ch_data.get("channels") or []
            if not isinstance(channels, list):
                continue

            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                if channel.get("isDisconnected") is True:
                    continue
                channel_id = str(channel.get("id") or "").strip()
                if not channel_id or channel_id in seen_ids:
                    continue
                seen_ids.add(channel_id)

                service = str(channel.get("service") or "social").strip().lower()
                service_label = SERVICE_LABELS.get(service, service.capitalize())
                raw_display = str(channel.get("displayName") or "").strip()
                raw_name = str(channel.get("name") or "").strip()
                label_target = raw_display or raw_name or channel_id
                display_name = f"{service_label} · {label_target}"
                username = raw_name if raw_name and raw_name != raw_display else None
                avatar_url = str(channel.get("avatar") or "").strip() or None

                profiles.append(
                    AccountProfile(
                        external_account_id=channel_id,
                        display_name=display_name,
                        username=username,
                        avatar_url=avatar_url,
                        metadata={
                            "channel_id": channel_id,
                            "service": service,
                            "service_label": service_label,
                            "organization_id": org_id,
                            "organization_name": org_name,
                            "descriptor": channel.get("descriptor"),
                        },
                    )
                )

        return profiles

    # -- Publishing -----------------------------------------------------
    @staticmethod
    def _build_service_metadata(
        service: str, title: str, privacy_level: str
    ) -> Optional[Dict[str, Any]]:
        if service == "instagram":
            return {
                "instagram": {
                    "type": "reel",
                    "shouldShareToFeed": True,
                }
            }
        if service == "youtube":
            yt_privacy = (
                privacy_level
                if privacy_level in {"public", "unlisted", "private"}
                else "public"
            )
            return {
                "youtube": {
                    "title": (title or "Short").strip()[:100] or "Short",
                    "categoryId": "22",
                    "privacy": yt_privacy,
                    "madeForKids": False,
                    "notifySubscribers": True,
                }
            }
        if service == "facebook":
            return {"facebook": {"type": "reel"}}
        return None

    def publish(self, access_token: str, request: PublishRequest) -> PublishResult:
        media_url = (request.public_media_url or "").strip()
        if not media_url or not media_url.lower().startswith("https://"):
            raise SocialProviderError(
                "Buffer requires a public HTTPS URL to fetch rendered clips. "
                "Set APP_BASE_URL or SOCIAL_PUBLIC_MEDIA_BASE_URL to an https:// address.",
                rejected=True,
            )

        meta = request.account_metadata or {}
        channel_id = str(meta.get("channel_id") or "").strip()
        if not channel_id:
            raise SocialProviderError(
                "Missing Buffer channel ID on this connected account. Reconnect Buffer in Social Settings.",
                rejected=True,
            )

        service = str(meta.get("service") or "").strip().lower()
        caption_limit = SERVICE_CAPTION_LIMITS.get(service, 2200)
        base_text = (request.caption or request.title or "").strip()
        text = build_caption(base_text, request.hashtags, caption_limit)

        video_asset: Dict[str, Any] = {"url": media_url}
        if service in {"instagram", "tiktok", "pinterest"}:
            video_asset["metadata"] = {"thumbnailOffset": 2000}

        mode = self.config.buffer_default_mode or "shareNow"
        post_input: Dict[str, Any] = {
            "channelId": channel_id,
            "text": text,
            "schedulingType": "automatic",
            "mode": mode,
            "assets": [{"video": video_asset}],
        }
        service_metadata = self._build_service_metadata(
            service, request.title, request.privacy_level
        )
        if service_metadata:
            post_input["metadata"] = service_metadata

        result_union = self._send_create_post(access_token, post_input)
        typename = str(result_union.get("__typename") or "")
        if typename == "MutationError" or "message" in result_union:
            err_msg = str(result_union.get("message") or "Buffer rejected the post.")
            lowered = err_msg.lower()
            # Instagram personal profiles and certain TikTok/Facebook channels
            # require reminder/notification scheduling instead of automatic direct post.
            if any(
                hint in lowered
                for hint in ("notification", "reminder", "personal profile")
            ):
                fallback_input = {**post_input, "schedulingType": "notification"}
                result_union = self._send_create_post(access_token, fallback_input)
                typename = str(result_union.get("__typename") or "")

        if typename == "MutationError" or "message" in result_union:
            err_msg = str(result_union.get("message") or "Buffer rejected the post.")
            raise SocialProviderError(
                f"Buffer rejected the post: {err_msg}",
                rejected=True,
            )

        post = result_union.get("post") or {}
        post_id = str(post.get("id") or "").strip() or None
        return PublishResult(
            external_post_id=post_id,
            external_url=BUFFER_DASHBOARD_URL,
            raw=post if isinstance(post, dict) else {},
        )

    def _send_create_post(
        self, access_token: str, post_input: Dict[str, Any]
    ) -> Dict[str, Any]:
        with ambiguous_outcome("create the Buffer post"):
            data = self._graphql(
                access_token,
                _CREATE_POST_MUTATION,
                {"input": post_input},
                action="create the Buffer post",
            )
        create_result = data.get("createPost")
        if not isinstance(create_result, dict):
            raise SocialProviderError("Buffer did not return a post result.")
        return create_result

    def fetch_metrics(
        self, access_token: str, external_post_id: str, account_metadata: Dict[str, Any]
    ) -> PostMetrics:
        # Buffer's public GraphQL API exposes post status via post(input:{id});
        # return an empty metrics snapshot when per-network insights are absent.
        return PostMetrics(raw={"buffer_post_id": external_post_id})
