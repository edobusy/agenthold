"""
Generate assets/demo.gif by running both demo scripts and rendering their
output as an animated terminal-style GIF.

Usage:  uv run --with pillow python make_demo_gif.py
"""

import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WIDTH = 900
HEIGHT = 600
BG = (30, 30, 30)
FG = (220, 220, 220)
HEADER_FG = (97, 214, 214)   # cyan for section headers
RESULT_FG = (87, 166, 74)    # green for RESULT line
FONT_SIZE = 16
FRAME_DURATION_MS = 80       # ms per frame
FINAL_HOLD_MS = 4000         # how long to hold the final frame (ms)


def get_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to find a monospace font; fall back to default."""
    candidates = [
        "C:/Windows/Fonts/CascadiaMono.ttf",  # Cascadia Mono (Windows 11)
        "C:/Windows/Fonts/consola.ttf",        # Consolas (Windows)
        "C:/Windows/Fonts/cour.ttf",           # Courier New
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, FONT_SIZE)
    return ImageFont.load_default()


def run_script(script: str) -> list[str]:
    """Run a demo script and return its output lines."""
    result = subprocess.run(
        [sys.executable, script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (result.stdout + result.stderr).splitlines()


def line_color(line: str) -> tuple[int, int, int]:
    if line.startswith("==="):
        return HEADER_FG
    if line.startswith("RESULT:"):
        return RESULT_FG
    return FG


def render_frame(
    lines: list[str],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    line_h: int,
) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    pad = 12
    max_lines = (HEIGHT - 2 * pad) // line_h
    # Show the last max_lines lines (scrolling terminal behaviour)
    visible = lines[-max_lines:] if len(lines) > max_lines else lines
    for i, line in enumerate(visible):
        y = pad + i * line_h
        draw.text((pad, y), line, font=font, fill=line_color(line))
    return img


def build_frames(
    sections: list[tuple[str, list[str]]],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    line_h: int,
) -> list[Image.Image]:
    frames: list[Image.Image] = []
    visible: list[str] = []

    for header, lines in sections:
        visible.append(f"=== {header} ===")
        visible.append("")
        frames.append(render_frame(visible, font, line_h))

        for line in lines:
            # Wrap long lines to fit terminal width
            max_chars = (WIDTH - 24) // max(
                font.getlength("A") if hasattr(font, "getlength") else 8, 1  # type: ignore[arg-type]
            )
            wrapped = textwrap.wrap(line, width=int(max_chars)) if line.strip() else [""]
            for wline in wrapped:
                visible.append(wline)
                frames.append(render_frame(visible, font, line_h))

        visible.append("")

    return frames


def main() -> None:
    out_path = Path("assets/demo.gif")
    out_path.parent.mkdir(exist_ok=True)

    print("Running without_agenthold.py ...")
    without_lines = run_script("examples/order_processing/without_agenthold.py")

    print("Running with_agenthold.py ...")
    with_lines = run_script("examples/order_processing/with_agenthold.py")

    font = get_font()

    # Measure line height
    tmp = Image.new("RGB", (100, 100))
    draw = ImageDraw.Draw(tmp)
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    line_h = bbox[3] - bbox[1] + 4

    sections = [
        ("WITHOUT AGENTHOLD", without_lines),
        ("WITH AGENTHOLD", with_lines),
    ]

    print("Rendering frames ...")
    frames = build_frames(sections, font, line_h)

    # Quantize to palette mode so Pillow preserves all frames (avoids the
    # bug where RGB→GIF conversion silently drops near-duplicate frames).
    print(f"Quantizing {len(frames)} frames ...")
    palette_frames = [f.quantize(colors=32, dither=0) for f in frames]

    # Use a longer duration on the last frame instead of appending duplicate
    # hold-frames (Pillow deduplicates identical consecutive frames).
    durations = [FRAME_DURATION_MS] * len(palette_frames)
    durations[-1] = FINAL_HOLD_MS

    print(f"Saving {len(palette_frames)} frames to {out_path} ...")
    palette_frames[0].save(
        out_path,
        save_all=True,
        append_images=palette_frames[1:],
        duration=durations,
        loop=0,
    )
    print(f"Done — {out_path} ({out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
