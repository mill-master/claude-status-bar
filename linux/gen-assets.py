#!/usr/bin/env python3
"""Derives the Linux app's image and word assets from the Swift sources at build time.

The Swift files under Sources/ are the single home of the animation frames and the
thinking-word list; this script extracts them so the two apps can never drift apart.
Build-machine-only requirements: Pillow and the DejaVu Sans font (both present on a
stock Ubuntu runner) for rasterizing the "Claude Code" spinner glyphs.

Usage: gen-assets.py --repo <repo-root> --out <dir>
Writes: spark-<n>.png, logo.png, crab-<n>.png, glyph-<n>.png, words.json, completion.mp3
"""

import argparse
import base64
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Same glyph set and geometry as StatusController in Sources/main.swift: each glyph is
# rasterized into a 60x60 alpha mask whose tight bounding box fills ~92% of the square.
GLYPHS = ["✻", "✽", "✶", "✳", "✢"]
MASK_SIDE = 60
MASK_FILL = 0.92


def swift_strings(text, varname):
    """The string literals of `let <varname> ... = [ ... ]` (or a single-string let)."""
    m = re.search(rf"let {re.escape(varname)}\b[^=]*=\s*", text)
    if not m:
        sys.exit(f"gen-assets: `{varname}` not found")
    tail = text[m.end():]
    if tail.startswith("["):
        tail = tail[: tail.index("]")]
    else:
        tail = tail[: tail.index("\n")]
    strings = re.findall(r'"((?:[^"\\]|\\.)*)"', tail)
    if not strings:
        sys.exit(f"gen-assets: `{varname}` yielded no strings")
    return strings


def write_pngs(strings, out, stem):
    for i, b64 in enumerate(strings):
        data = base64.b64decode(b64)
        Image.open(io.BytesIO(data)).verify()  # corrupt frames fail the build, not the app
        name = f"{stem}-{i}.png" if len(strings) > 1 else f"{stem}.png"
        (out / name).write_bytes(data)
    return len(strings)


def dejavu_font(size):
    common = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if common.exists():
        return ImageFont.truetype(str(common), size)
    path = subprocess.run(
        ["fc-match", "-f", "%{file}", "DejaVu Sans"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return ImageFont.truetype(path, size)


def glyph_mask(glyph, font):
    """Rasterize one spinner glyph to a centered 60x60 alpha mask (see glyphMask in main.swift)."""
    canvas = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(canvas).text((200, 200), glyph, font=font, fill=255, anchor="mm")
    bbox = canvas.getbbox()
    if bbox is None:
        sys.exit(f"gen-assets: glyph {glyph!r} rendered empty — font coverage missing")
    tight = canvas.crop(bbox)
    fill = MASK_SIDE * MASK_FILL
    scale = fill / max(tight.size)
    w, h = (max(1, round(d * scale)) for d in tight.size)
    tight = tight.resize((w, h), Image.LANCZOS)
    mask = Image.new("L", (MASK_SIDE, MASK_SIDE), 0)
    mask.paste(tight, ((MASK_SIDE - w) // 2, (MASK_SIDE - h) // 2))
    out = Image.new("RGBA", (MASK_SIDE, MASK_SIDE), (0, 0, 0, 0))
    out.putalpha(mask)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    sources = args.repo / "Sources"
    args.out.mkdir(parents=True, exist_ok=True)

    spark = write_pngs(swift_strings((sources / "SparkFrames.swift").read_text(),
                                     "claudeSparkFramePNGs"), args.out, "spark")
    logo = write_pngs(swift_strings((sources / "LogoFrame.swift").read_text(),
                                    "claudeLogoPNG"), args.out, "logo")
    crab = write_pngs(swift_strings((sources / "CrabFrames.swift").read_text(),
                                    "clawdCrabFramePNGs"), args.out, "crab")

    words = swift_strings((sources / "main.swift").read_text(), "thinkingWords")
    (args.out / "words.json").write_text(json.dumps(words, ensure_ascii=False, indent=0) + "\n")

    font = dejavu_font(180)
    for i, g in enumerate(GLYPHS):
        glyph_mask(g, font).save(args.out / f"glyph-{i}.png")

    shutil.copyfile(args.repo / "assets" / "completion.mp3", args.out / "completion.mp3")

    print(f"gen-assets: {spark} spark, {logo} logo, {crab} crab, "
          f"{len(GLYPHS)} glyph frames, {len(words)} thinking words -> {args.out}")


if __name__ == "__main__":
    main()
