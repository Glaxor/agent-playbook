"""Render agent-playbook log lines into the README's terminal-style demo GIF.

Usage:  python docs/render_demo_gif.py <runner.log> <out.gif>

Re-recording docs/demo.gif from scratch (the committed one is a REAL run —
keep it that way):
  1. In an empty dir, seed a test the agent will trip over, e.g. test_greet.py
     asserting greet("World") == "Hello, World!" while the playbook prompt asks
     for the greeting 'Hello, <name>' (no exclamation mark), with
     verify: python -m pytest test_greet.py -q  and  fix_attempts: 2.
  2. Run:  agent-playbook playbook.yaml   (haiku keeps it around $0.04)
  3. Feed the resulting playbook.logs/runner.log to this script unedited
     (trim only lines after PLAYBOOK COMPLETE).

Needs Pillow and a monospace TTF (Consolas on Windows; pass another path via
the FONT constant on other platforms).
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LOG_IN = Path(sys.argv[1])
GIF_OUT = Path(sys.argv[2])

FONT = ImageFont.truetype(r"C:\Windows\Fonts\consola.ttf", 17)
CW, CH = FONT.getbbox("M")[2], 24          # cell width/height
COLS, ROWS = 88, 17
PAD = 14
W, H = COLS * CW + 2 * PAD, ROWS * CH + 2 * PAD + 30

BG = (13, 17, 23)                          # GitHub-dark palette
FG = (201, 209, 217)
DIM = (110, 118, 129)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)
GREEN = (63, 185, 80)
CYAN = (83, 155, 245)


def color_for(line: str):
    if "verify FAILED" in line or ("FAILED" in line and "test" not in line):
        return RED
    if line.strip().startswith(("2 failed", "1 failed")) or "failed," in line:
        return RED
    if "asking [claude] to fix it" in line or "verify failed" in line:
        return YELLOW
    if "DONE" in line or "PLAYBOOK COMPLETE" in line or "passed" in line:
        return GREEN
    if line.startswith("$"):
        return CYAN
    if line.strip().startswith("verify:") or "running prompt" in line:
        return DIM
    return FG


def frame(lines):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # window chrome
    d.rounded_rectangle([4, 4, W - 5, H - 5], radius=10, outline=(48, 54, 61), width=1)
    for i, c in enumerate([RED, YELLOW, GREEN]):
        d.ellipse([16 + i * 22, 14, 28 + i * 22, 26], fill=c)
    d.text((W // 2 - 60, 12), "agent-playbook", font=FONT, fill=DIM)
    for row, line in enumerate(lines[-ROWS:]):
        d.text((PAD, 34 + row * CH), line[:COLS], font=FONT, fill=color_for(line))
    return img


raw = LOG_IN.read_text(encoding="utf-8").splitlines()
lines = ["$ agent-playbook playbook.yaml", ""]
frames, durations = [frame(lines)], [1200]
for ln in raw:
    lines.append(ln)
    frames.append(frame(lines))
    if "FAILED" in ln or "fix it" in ln:
        durations.append(2200)             # dramatic pause on the interesting beats
    elif "COMPLETE" in ln:
        durations.append(500)
    else:
        durations.append(650)
frames.append(frame(lines))
durations.append(6000)                     # hold the final state

frames[0].save(GIF_OUT, save_all=True, append_images=frames[1:],
               duration=durations, loop=0, optimize=True)
print(f"wrote {GIF_OUT} ({GIF_OUT.stat().st_size / 1024:.0f} KB, {len(frames)} frames)")
