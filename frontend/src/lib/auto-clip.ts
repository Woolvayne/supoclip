/** Auto-clip workflows: watched YouTube channels whose new uploads are clipped. */

export type AutoClipWorkflow = {
  id: string;
  channel_url: string;
  channel_id: string;
  channel_title: string | null;
  enabled: boolean;
  last_checked_at: string | null;
  last_triggered_at: string | null;
  videos_clipped: number;
  last_error: string | null;
  created_at: string | null;
};

export type AutoClipListResponse = { workflows: AutoClipWorkflow[] };
export type AutoClipWorkflowResponse = { workflow: AutoClipWorkflow };

/**
 * What the create form sends: trim the paste, and a bare @handle or channel
 * path gets the full YouTube URL the API expects. Mirrors
 * `normalize_channel_url` in backend/src/youtube_channels.py.
 */
export function normalizeChannelInput(value: string): string {
  const cleaned = value.trim();
  if (!cleaned) return "";
  if (cleaned.startsWith("@")) return `https://www.youtube.com/${cleaned}`;
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(cleaned)) return `https://${cleaned}`;
  return cleaned;
}

/** A display name for a workflow: the channel title, or its handle/URL. */
export function workflowLabel(workflow: AutoClipWorkflow): string {
  const title = workflow.channel_title?.trim();
  if (title) return title;
  const handle = workflow.channel_url.match(/@[\w.\-]+/)?.[0];
  return handle || workflow.channel_url;
}
