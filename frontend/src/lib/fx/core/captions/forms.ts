// Form caption styles (WP9): the listed styles that change the SHAPE of a caption instead of drawing the intensity
// looks. They keep their shape at every intensity (`caption_look_for` returns "template" for them), so their drawers
// lay the words out themselves from the style the scene carries (font, size, colours, box colour, y).
//
//   caption_card  a warm paper card in the lower third: rounded, soft shadow, a thin accent rule on its left, and the
//                 spoken line in dark ink (the key words in the accent). Cards rise 12u and fade in over 140 ms; the
//                 next card replaces the previous one on its first frame.
//   one_word      one huge word at a time, centred, ALL CAPS, sized to fill 0.86 W (never taller than 0.3 H), hard cut
//                 on and off, 1.35 → 1 over 110 ms on arrival. The key word takes the accent.
//   lower_third   a broadcast bar in the lower third: a solid accent block on its left edge, the line left-aligned
//                 beside it in the style's colours. It slides in from the left by 24u while fading over 160 ms.
//
// Nothing animates slowly: every entrance is 2–5 frames.
import type { DrawOp, Mat2D, Shadow } from "../host";
import type { CaptionChunk, DrawerContext } from "../registry";
import { easeOutCubic, easeOutQuad, progress } from "../easing";
import { IDENTITY, mul, rectOp, textOp, translation } from "../layout";
import { captionSafe } from "../constants";
import { cssColor } from "./base";
import { registerCaptionStyle } from "../registry";

/** Geometry of the card and the bar, in em of the caption size unless stated. */
export const FORM_GEOMETRY = {
  cardPadX: 0.5, cardPadY: 0.26, cardRadius: 0.26, ruleWidth: 0.075, ruleGap: 0.22,
  cardRise: 12,             // u
  barPadX: 0.34, barPadY: 0.16, barRadius: 0.14, blockWidth: 0.34,
  barSlide: 24,             // u
  enter: 0.14, barEnter: 0.16,
  oneWordMaxWidth: 0.86, oneWordMaxHeight: 0.3, oneWordMaxScale: 3, oneWordFrom: 1.35, oneWordEnter: 0.11,
  shadowDy: 4, shadowBlur: 10,   // u
} as const;

type LaidWord = CaptionChunk["words"][number];

/** The chunk's words grouped by laid-out line, each line sorted left → right. */
function lineGroups(chunk: CaptionChunk): { line: number; y: number; h: number; words: LaidWord[] }[] {
  const byLine = new Map<number, LaidWord[]>();
  for (const w of chunk.words) byLine.set(w.line, [...(byLine.get(w.line) ?? []), w]);
  return [...byLine.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([line, words]) => {
      const sorted = [...words].sort((a, b) => a.x - b.x);
      return { line, y: sorted[0].y, h: Math.max(...sorted.map((w) => w.h)), words: sorted };
    });
}

/** Whole-chunk bounds (all lines). */
function chunkBox(chunk: CaptionChunk): { x0: number; x1: number; y0: number; y1: number } {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const w of chunk.words) {
    x0 = Math.min(x0, w.x); x1 = Math.max(x1, w.x + w.w);
    y0 = Math.min(y0, w.y); y1 = Math.max(y1, w.y + w.h);
  }
  return { x0, x1, y0, y1 };
}

/** Soft drop shadow under a card or a bar, scaled with the picture unit. */
const softShadow = (u: number): Shadow => ({
  color: "rgba(0,0,0,0.38)", blur: FORM_GEOMETRY.shadowBlur * u, dx: 0, dy: FORM_GEOMETRY.shadowDy * u,
});

/** A word's text op in the style's ink, its key words in the accent colour. */
function wordOp(w: LaidWord, family: string, px: number, ink: string, accent: string, transform: Mat2D,
  alpha: number): DrawOp {
  return textOp({
    text: w.text, font: family, px, x: w.x, y: w.y + w.h / 2, align: "left",
    fill: w.highlight ? accent : ink, transform, alpha,
  });
}

// ------------------------------------------------------------------------------------------------- caption card

/** One paper card behind the caption lines: the card, its accent rule, then the words. */
export function captionCardOps(chunk: CaptionChunk, dc: Omit<DrawerContext, "local" | "dur">): DrawOp[] {
  const p = dc.scene, style = p.scene.caption, family = p.captionFamily;
  if (!style || !family || !chunk.words.length) return [];
  const g = FORM_GEOMETRY, u = dc.u, px = chunk.px;
  const local = dc.t - chunk.start;
  const k = easeOutQuad(progress(local, 0, g.enter));
  const alpha = k;
  const outer = mul(translation(0, (1 - k) * g.cardRise * u), IDENTITY);
  const box = chunkBox(chunk);
  const padX = px * g.cardPadX, padY = px * g.cardPadY;
  const rule = px * g.ruleWidth;
  const x0 = box.x0 - padX, x1 = box.x1 + padX;
  const y0 = box.y0 - padY, y1 = box.y1 + padY;
  const safe = captionSafe(p.width, p.height);
  // Keep the card inside the frame even when a long line was laid out wide.
  const left = Math.max(safe.side * p.width, x0);
  const right = Math.min(p.width - safe.side * p.width, x1);
  const width = Math.max(1, right - left), height = Math.max(1, y1 - y0);
  const paper = cssColor(style.background ?? "#F7F4EEF2");
  const ops: DrawOp[] = [
    rectOp({ x: left, y: y0, w: width, h: height, radius: px * g.cardRadius, fill: paper, transform: outer,
      alpha, shadow: softShadow(u) }),
    // The accent rule on the card's left edge, inset by the card's padding.
    rectOp({ x: left + rule * 0.9, y: y0 + padY * 0.75, w: rule, h: Math.max(1, height - padY * 1.5),
      radius: rule / 2, fill: style.accent, transform: outer, alpha }),
  ];
  const shift = Math.max(0, left + rule + px * g.ruleGap - box.x0);
  const textShift = mul(outer, translation(shift, 0));
  for (const w of chunk.words) ops.push(wordOp(w, family, px, style.color, style.accent, textShift, alpha));
  return ops;
}

// ------------------------------------------------------------------------------------------------- one word

/** The chunk's single word, huge and centred, cut in hard with a 110 ms settle. */
export function oneWordOps(chunk: CaptionChunk, dc: Omit<DrawerContext, "local" | "dur">): DrawOp[] {
  const p = dc.scene, style = p.scene.caption, family = p.captionFamily;
  if (!style || !family || !chunk.words.length) return [];
  const g = FORM_GEOMETRY, u = dc.u;
  const text = chunk.words.map((w) => w.text).join(" ");
  if (!text.trim()) return [];
  const label = style.uppercase ? text.toLocaleUpperCase() : text;
  // Fill 0.86 W, never taller than 0.3 H; a short word stops at 3× the caption size.
  const W = p.width, H = p.height;
  const base = chunk.px;
  const widthAt = (size: number) => p.measure(label, family, size);
  let px = Math.min(base * g.oneWordMaxScale, (g.oneWordMaxWidth * W * base) / Math.max(1e-6, widthAt(base)));
  px = Math.min(px, g.oneWordMaxHeight * H);
  px = Math.max(base * 1.2, px);
  const w = widthAt(px);
  const cx = chunk.centerX;
  const cy = Math.min(H - px, Math.max(px, chunk.centerY));
  const local = dc.t - chunk.start;
  const settle = progress(local, 0, g.oneWordEnter);
  const scale = g.oneWordFrom + (1 - g.oneWordFrom) * easeOutCubic(settle);
  const transform: Mat2D = [scale, 0, 0, scale, cx - scale * cx, cy - scale * cy];
  const accent = chunk.words.some((word) => word.highlight);
  const fill = accent ? style.accent : style.color;
  const strokePx = style.stroke_color ? (style.stroke_px > 0 ? style.stroke_px : 0.1 * px) : 0;
  const shadow: Shadow | null = style.shadow
    ? { color: "rgba(0,0,0,0.55)", blur: 0, dx: 0, dy: 5 * u * scale } : null;
  const at = { text: label, font: family, px, x: cx - w / 2, y: cy, align: "left" as const, transform };
  const ops: DrawOp[] = [];
  if (strokePx > 0) {
    ops.push(textOp({ ...at, fill: null, stroke: style.stroke_color, strokePx, shadow }));
  }
  ops.push(textOp({ ...at, fill, shadow: strokePx > 0 ? null : shadow }));
  return ops;
}

// ------------------------------------------------------------------------------------------------- lower third

/** The broadcast bar: accent block on the left, the line left-aligned beside it, sliding in from the left. */
export function lowerThirdOps(chunk: CaptionChunk, dc: Omit<DrawerContext, "local" | "dur">): DrawOp[] {
  const p = dc.scene, style = p.scene.caption, family = p.captionFamily;
  if (!style || !family || !chunk.words.length) return [];
  const g = FORM_GEOMETRY, u = dc.u, px = chunk.px;
  const local = dc.t - chunk.start;
  const k = easeOutQuad(progress(local, 0, g.barEnter));
  const alpha = k;
  const outer = mul(translation(-(1 - k) * g.barSlide * u, 0), IDENTITY);
  const padX = px * g.barPadX, padY = px * g.barPadY;
  const block = px * g.blockWidth;
  const lines = lineGroups(chunk);
  const widest = Math.max(...lines.map((l) => l.words[l.words.length - 1].x + l.words[l.words.length - 1].w - l.words[0].x));
  const safe = captionSafe(p.width, p.height);
  const left = Math.max(safe.side * p.width, Math.min(...lines.map((l) => l.words[0].x)) - padX - block);
  const width = Math.min(p.width - safe.side * p.width - left, widest + 2 * padX + block);
  const height = (lines.length - 1) * px * 1.2 + px + 2 * padY;
  const top = chunk.centerY - height / 2;
  const radius = px * g.barRadius;
  const fill = cssColor(style.background ?? "#0B0B0FE6");
  const ops: DrawOp[] = [
    rectOp({ x: left, y: top, w: Math.max(1, width), h: height, radius, fill, transform: outer, alpha,
      shadow: softShadow(u) }),
    // The accent block: rounded on the bar's left corners, square where it meets the text.
    rectOp({ x: left, y: top, w: block, h: height, radius, fill: style.accent, transform: outer, alpha }),
    rectOp({ x: left + radius, y: top, w: Math.max(1, block - radius), h: height, radius: 0, fill: style.accent,
      transform: outer, alpha }),
  ];
  const shift = left + block + padX - Math.min(...lines.map((l) => l.words[0].x));
  const textShift = mul(outer, translation(shift, 0));
  for (const w of chunk.words) ops.push(wordOp(w, family, px, style.color, style.accent, textShift, alpha));
  return ops;
}

// Form captions never draw the intensity looks: their shape is the whole point of the style.

registerCaptionStyle({ template: "caption_card", line: captionCardOps });
registerCaptionStyle({ template: "one_word", line: oneWordOps });
registerCaptionStyle({ template: "lower_third", line: lowerThirdOps });
