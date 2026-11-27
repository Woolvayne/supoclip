-- Auto-clip workflows: watched YouTube channels whose new uploads are clipped
-- automatically. The marker columns record the newest video seen so far.
CREATE TABLE IF NOT EXISTS auto_clip_workflows (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    team_id VARCHAR(36) REFERENCES teams(id) ON DELETE CASCADE,
    channel_url VARCHAR(500) NOT NULL,
    channel_id VARCHAR(64) NOT NULL,
    channel_title VARCHAR(255),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_video_id VARCHAR(32),
    last_video_published_at TIMESTAMP WITH TIME ZONE,
    last_checked_at TIMESTAMP WITH TIME ZONE,
    last_triggered_at TIMESTAMP WITH TIME ZONE,
    videos_clipped INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_auto_clip_workflows_user_id ON auto_clip_workflows (user_id);
CREATE INDEX IF NOT EXISTS idx_auto_clip_workflows_enabled ON auto_clip_workflows (enabled);
