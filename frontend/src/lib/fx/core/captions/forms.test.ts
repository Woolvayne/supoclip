// Form caption styles (captions/forms.ts): subtitle card, one word, lower third. They keep their shape at every
// intensity, so they draw their own layout from the scene's caption style instead of the intensity looks.
import { describe, expect, it } from "vitest";

import { clockAt, frameOps } from "../frame";
import { prepareScene } from "../scene";
import { fakeMeasureHost } from "../test-utils/fake-host";
import type { DrawOp } from "../host";
import type { FxCaptionTemplate } from "../types";
import { demoScene, DEMO_STATIC_T, demoWords } from "../../demo-scene";
import { FORM_GEOMETRY } from "./forms";

const FORMS: FxCaptionTemplate[] = ["caption_card", "one_word", "lower_third"];

const prepared = (template: FxCaptionTemplate) => prepareScene(demoScene(template), fakeMeasureHost);
const opsAt = (template: FxCaptionTemplate, t = DEMO_STATIC_T): DrawOp[] => {
  const p = prepared(template);
  return frameOps(p, clockAt(p, t)).filter((op) => op.op !== "picture");
};
const texts = (ops: DrawOp[]) => ops.filter((op): op is Extract<DrawOp, { op: "text" }> => op.op === "text");
const rects = (ops: DrawOp[]) => ops.filter((op): op is Extract<DrawOp, { op: "rect" }> => op.op === "rect");

describe("form caption styles", () => {
  it.each(FORMS)("%s draws words with a colour and no look caption header", (template) => {
    const ops = opsAt(template);
    expect(texts(ops).length).toBeGreaterThan(0);
    // An outline op (fill null) is allowed; every visible word takes a hex colour.
    for (const text of texts(ops).filter((text) => text.fill !== null)) expect(text.fill).toMatch(/^#/);
  });

  it("caption card draws a paper card with the accent rule and dark ink", () => {
    const p = prepared("caption_card");
    const style = p.scene.caption!;
    const ops = opsAt("caption_card");
    const cards = rects(ops);
    // The card and its rule, both inside the frame and taller than one line.
    expect(cards.length).toBeGreaterThanOrEqual(2);
    expect(cards.every((rect) => rect.alpha > 0 && rect.w > 0 && rect.h > 0)).toBe(true);
    expect(cards[0].shadow).not.toBeNull();                       // the soft shadow under the paper
    expect(texts(ops).some((text) => text.fill === style.color)).toBe(true);   // ink
    expect(texts(ops).some((text) => text.fill === style.accent)).toBe(true);  // the key word
  });

  it("one word shows one huge upper-case word in the style's font", () => {
    const p = prepared("one_word");
    const style = p.scene.caption!;
    const chunk = p.chunkAt(DEMO_STATIC_T)!;
    const ops = opsAt("one_word");
    const shown = texts(ops);
    expect(shown).toHaveLength(2);                                // the outline and the fill of one word
    const [outline, fill] = shown;
    expect(outline.fill).toBeNull();
    expect(fill.fill).toBe(style.color);
    expect(fill.text).toBe(fill.text.toLocaleUpperCase());
    expect(fill.px).toBeGreaterThanOrEqual(chunk.px * 1.2);
    expect(fill.px).toBeLessThanOrEqual(FORM_GEOMETRY.oneWordMaxHeight * p.height);
  });

  it("lower third draws the bar and its accent block, left-aligned text", () => {
    const p = prepared("lower_third");
    const style = p.scene.caption!;
    const ops = opsAt("lower_third");
    const bars = rects(ops);
    expect(bars.length).toBeGreaterThanOrEqual(3);                // bar, block, block's square corner
    expect(bars[1].fill).toBe(style.accent);
    const first = texts(ops).sort((a, b) => a.x - b.x)[0];
    // The text starts right of the accent block, left-aligned in the bar.
    expect(first.x).toBeGreaterThan(bars[1].x + bars[1].w * 0.5);
    expect(first.text).not.toBe(first.text.toLocaleUpperCase());
  });

  it("the shape is the scene's own, not an accent cue's", () => {
    for (const template of FORMS) {
      // The demo scenes of the form styles carry no accents: the shape still draws (a look style would draw none).
      expect(demoScene(template).cues, template).toHaveLength(0);
      const p = prepared(template);
      expect(frameOps(p, clockAt(p, DEMO_STATIC_T)).some((op) => op.op === "text"), template).toBe(true);
      // Disabled captions are the only thing that removes it.
      const off = prepareScene({ ...demoScene(template), caption: { ...demoScene(template).caption!, enabled: false } },
        fakeMeasureHost);
      expect(frameOps(off, clockAt(off, DEMO_STATIC_T)).some((op) => op.op === "text"), template).toBe(false);
      // Later in the loop the next group draws too (a word at a time keeps changing).
      const later = prepared(template);
      const first = texts(frameOps(later, clockAt(later, demoWords()[1].start))).map((op) => op.text).join("");
      const second = texts(frameOps(later, clockAt(later, demoWords()[5].start))).map((op) => op.text).join("");
      expect(second, template).not.toBe(first);
    }
  });
});
