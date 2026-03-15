"""
Generate two terminal-style GIFs for the budget allocation examples.

  assets/budget_without.gif  -- the silent overcommit problem
  assets/budget_with.gif     -- conflict-safe allocation with Agenthold

Usage:  uv run --with pillow python make_budget_gifs.py
"""

import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WIDTH     = 860
HEIGHT    = 520
BG        = (18, 18, 18)
FONT_SIZE = 15
LINE_MS   = 150     # ms per frame (one new line revealed)
PAUSE_MS  = 700     # extra pause after section headers and key events
HOLD_MS   = 7000    # hold the final frame

# Colour palette
C_DEFAULT  = (200, 200, 200)   # light grey  -- regular text
C_SEP      = ( 65,  65,  65)   # dim grey    -- "------" separators
C_HEADER   = ( 97, 214, 214)   # cyan        -- section titles
C_PROBLEM  = (230,  80,  80)   # red         -- PROBLEM section
C_RESULT   = ( 87, 166,  74)   # green       -- RESULT section
C_CONFLICT = (220, 160,  50)   # amber       -- CONFLICT lines
C_VERSION  = (152, 118, 200)   # purple      -- audit trail (v1, v2 ...)
C_MONEY    = (255, 255, 255)   # bright white-- allocation "=>" lines
C_DIM      = ( 95,  95,  95)   # dim         -- "=====" sub-separators


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in [
        "C:/Windows/Fonts/CascadiaMono.ttf",
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    ]:
        if Path(path).exists():
            return ImageFont.truetype(path, FONT_SIZE)
    return ImageFont.load_default()


def measure_line_height(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    tmp  = Image.new("RGB", (200, 50))
    draw = ImageDraw.Draw(tmp)
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return bbox[3] - bbox[1] + 4


def run_script(script: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (result.stdout + result.stderr).splitlines()


# ---------------------------------------------------------------------------
# Per-line colour + timing
# ---------------------------------------------------------------------------

def classify_lines(
    lines: list[str],
) -> list[tuple[str, tuple[int, int, int], int]]:
    """Return (text, colour, extra_pause_ms) for every output line.

    Section structure in the scripts:
        ------        <-- separator
          TITLE       <-- section title (line right after first separator)
        ------        <-- closing separator (must NOT re-trigger title logic)
          content...
    """
    out: list[tuple[str, tuple[int, int, int], int]] = []
    current_section = ""
    prev_was_sep    = False
    just_saw_title  = False   # True while we're at the closing separator of a header block

    for line in lines:
        stripped = line.strip()

        # Pure dash separator line  e.g. "------..."
        if stripped and all(c == "-" for c in stripped):
            if just_saw_title:
                # This is the closing separator of a header block; reset flags
                # but do NOT set prev_was_sep so the next content line is not
                # mistaken for a section title.
                just_saw_title = False
                prev_was_sep   = False
            else:
                prev_was_sep = True
            out.append((line, C_SEP, 0))
            continue

        # Section title: first non-empty line immediately after an opening separator
        if prev_was_sep and stripped:
            prev_was_sep   = False
            just_saw_title = True
            if "PROBLEM" in stripped:
                current_section = "PROBLEM"
                out.append((line, C_PROBLEM, PAUSE_MS))
            elif "RESULT" in stripped:
                current_section = "RESULT"
                out.append((line, C_RESULT, PAUSE_MS))
            else:
                current_section = stripped
                out.append((line, C_HEADER, PAUSE_MS))
            continue

        just_saw_title = False
        prev_was_sep   = False

        # Empty line
        if not stripped:
            out.append((line, C_DEFAULT, 0))
            continue

        # "=====" sub-separator (inside BUDGET REPORT)
        if all(c == "=" for c in stripped):
            out.append((line, C_DIM, 0))
            continue

        # Section-coloured content
        if current_section == "PROBLEM":
            colour = C_CONFLICT if "<--" in line else C_PROBLEM
            out.append((line, colour, 0))
            continue

        if current_section == "RESULT":
            out.append((line, C_RESULT, 0))
            continue

        # Pattern-based colouring for non-section-header content
        if "CONFLICT" in line:
            out.append((line, C_CONFLICT, PAUSE_MS // 2))
        elif stripped.startswith("v") and "$" in line and "written by" in line:
            out.append((line, C_VERSION, 0))
        elif "=>" in line and "$" in line:
            out.append((line, C_MONEY, 0))
        else:
            out.append((line, C_DEFAULT, 0))

    return out


# ---------------------------------------------------------------------------
# Frame rendering
# ---------------------------------------------------------------------------

def render_frame(
    visible: list[tuple[str, tuple[int, int, int]]],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    line_h: int,
) -> Image.Image:
    img  = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    pad  = 14
    max_lines = (HEIGHT - 2 * pad) // line_h
    shown = visible[-max_lines:] if len(visible) > max_lines else visible
    for i, (text, colour) in enumerate(shown):
        draw.text((pad, pad + i * line_h), text, font=font, fill=colour)
    return img


# ---------------------------------------------------------------------------
# GIF assembly
# ---------------------------------------------------------------------------

def build_gif(
    script_path: str,
    out_path: Path,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    line_h: int,
) -> None:
    print(f"\nRunning {script_path} ...")
    raw_lines  = run_script(script_path)
    classified = classify_lines(raw_lines)

    # Wrap long lines; carry the colour and pause from the first wrapped segment
    char_w    = font.getlength("A") if hasattr(font, "getlength") else 8  # type: ignore[arg-type]
    max_chars = max(1, int((WIDTH - 28) / char_w))

    expanded: list[tuple[str, tuple[int, int, int], int]] = []
    for text, colour, pause in classified:
        if text.strip():
            segments = textwrap.wrap(text, width=max_chars, subsequent_indent="    ")
        else:
            segments = [""]
        for i, seg in enumerate(segments):
            expanded.append((seg, colour, pause if i == 0 else 0))

    print(f"  Rendering {len(expanded)} frames ...")
    frames:    list[Image.Image] = []
    durations: list[int]         = []
    visible:   list[tuple[str, tuple[int, int, int]]] = []

    for text, colour, extra_pause in expanded:
        visible.append((text, colour))
        frames.append(render_frame(visible, font, line_h))
        durations.append(LINE_MS + extra_pause)

    durations[-1] = HOLD_MS

    print(f"  Quantizing {len(frames)} frames ...")
    palette_frames = [f.quantize(colors=64, dither=0) for f in frames]

    print(f"  Saving to {out_path} ...")
    palette_frames[0].save(
        out_path,
        save_all=True,
        append_images=palette_frames[1:],
        duration=durations,
        loop=0,
    )
    print(f"  Done -- {out_path.stat().st_size // 1024} KB")


def main() -> None:
    Path("assets").mkdir(exist_ok=True)
    font   = get_font()
    line_h = measure_line_height(font)

    build_gif(
        "examples/budget_allocation/without_agenthold.py",
        Path("assets/budget_without.gif"),
        font, line_h,
    )
    build_gif(
        "examples/budget_allocation/with_agenthold.py",
        Path("assets/budget_with.gif"),
        font, line_h,
    )

    print("\nAll done.")


if __name__ == "__main__":
    main()
