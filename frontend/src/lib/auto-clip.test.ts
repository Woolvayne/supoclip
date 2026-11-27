import { describe, expect, it } from "vitest";

import { normalizeChannelInput, workflowLabel, type AutoClipWorkflow } from "./auto-clip";

describe("normalizeChannelInput", () => {
  it("trims the paste", () => {
    expect(normalizeChannelInput("  https://www.youtube.com/@MrBeast  ")).toBe(
      "https://www.youtube.com/@MrBeast",
    );
  });

  it("completes a bare handle", () => {
    expect(normalizeChannelInput("@MrBeast")).toBe("https://www.youtube.com/@MrBeast");
  });

  it("completes a host without a scheme", () => {
    expect(normalizeChannelInput("www.youtube.com/@MrBeast")).toBe(
      "https://www.youtube.com/@MrBeast",
    );
  });

  it("leaves full URLs alone", () => {
    expect(
      normalizeChannelInput("https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"),
    ).toBe("https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw");
  });

  it("returns empty for a blank paste", () => {
    expect(normalizeChannelInput("   ")).toBe("");
  });
});

describe("workflowLabel", () => {
  const workflow: AutoClipWorkflow = {
    id: "wf-1",
    channel_url: "https://www.youtube.com/@MrBeast",
    channel_id: "UCuAXFkgsw1L7xaCfnd5JJOw",
    channel_title: "MrBeast",
    enabled: true,
    last_checked_at: null,
    last_triggered_at: null,
    videos_clipped: 0,
    last_error: null,
    created_at: null,
  };

  it("prefers the channel title", () => {
    expect(workflowLabel(workflow)).toBe("MrBeast");
  });

  it("falls back to the handle", () => {
    expect(workflowLabel({ ...workflow, channel_title: null })).toBe("@MrBeast");
  });

  it("falls back to the URL when nothing else fits", () => {
    expect(
      workflowLabel({ ...workflow, channel_title: null, channel_url: "https://youtube.com/x" }),
    ).toBe("https://youtube.com/x");
  });
});
