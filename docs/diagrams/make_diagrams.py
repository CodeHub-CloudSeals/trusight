#!/usr/bin/env python3
"""Generate the two architecture diagrams, light and dark.

Two diagrams, not a deck. The product demonstrates itself for fifteen minutes;
a picture only earns its place where the product cannot show the thing:

1. ``trust-boundary`` — where the AI sits, and the two things it may never do.
   The workspace shows 23 steps executing and labels each one's kind, but it
   never draws the fence. "AI-assisted" is the phrase a room mishears, and the
   correction has to be a picture or it will not survive the drive home.

2. ``scale-path`` — what deploys today versus what production needs. This one
   exists because the honest answer to "what happens with fifty estimators"
   is uncomfortable: run state lives in process, which is the single reason
   App Runner is pinned to one instance. Drawn as a comparison so the
   dependency is visible rather than asserted.

Both are generated from one source so the light and dark versions cannot
drift. Colours are explicit rather than currentColor: these are standalone
files headed for a slide, where nothing inherits.

    python docs/diagrams/make_diagrams.py
"""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent

# CloudSeals palette, carried twice: a fill value and an ink value dark enough
# to read as text on it — the same rule the product's own stylesheet follows.
THEMES = {
    "light": {
        "bg": "#F3F8FC",
        "panel": "#FFFFFF",
        "line": "#C3D3E4",
        "ink": "#17233B",
        "ink2": "#33415C",
        "muted": "#55647F",
        # 4.97:1 on --accent-soft, which is the tighter of the two grounds
        # it lands on. #1F6FD0 read 4.36 there — under AA on the one
        # label in this diagram that carries the claim.
        "accent": "#1B66C2",
        "accent_soft": "#E6F2FF",
        "stop": "#B5453A",
        "stop_soft": "#FDECEC",
        "ok": "#107C4D",
        "ok_soft": "#E3F5EB",
        "human": "#8A5A00",
        "human_soft": "#FDF3E2",
    },
    "dark": {
        "bg": "#08111F",
        "panel": "#142338",
        "line": "#31486B",
        "ink": "#E9F1FB",
        "ink2": "#C0D0E6",
        "muted": "#93A6C2",
        "accent": "#6FB4FF",
        "accent_soft": "#12294A",
        "stop": "#E8897F",
        "stop_soft": "#331F1D",
        "ok": "#7CCBB1",
        "ok_soft": "#123026",
        "human": "#E8C17C",
        "human_soft": "#33290F",
    },
}

FONT = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, "
        "Roboto, Helvetica, Arial, sans-serif")


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, *, fill, size=13, weight=400, anchor="start"):
    return (f'<text x="{x}" y="{y}" fill="{fill}" font-family="{FONT}" '
            f'font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}">{esc(s)}</text>')


def box(x, y, w, h, *, fill, stroke, rx=10, width=1.2, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"{d}/>')


def arrow(x1, y1, x2, y2, *, stroke, width=1.4, marker="arrow", dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
            f'stroke-width="{width}" marker-end="url(#{marker})"{d}/>')


def defs(c: dict) -> str:
    heads = (("arrow", c["muted"]), ("arrow-accent", c["accent"]),
             ("arrow-human", c["human"]), ("arrow-stop", c["stop"]),
             ("arrow-ok", c["ok"]), ("arrow-line", c["line"]))
    parts = ['<defs>']
    for name, fill in heads:
        parts.append(
            f'<marker id="{name}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0 0L10 5L0 10z" fill="{fill}"/></marker>')
    parts.append('</defs>')
    return "".join(parts)


def svg_open(w: int, h: int, label: str, bg: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" role="img" aria-label="{esc(label)}">'
            f'<rect width="{w}" height="{h}" fill="{bg}"/>')


# ── Diagram 1 — the trust boundary ────────────────────────────────────────────
#
# Columns are wide enough for their longest line: an emphasis label that
# touches its own border reads as a mistake, and this one carries the claim.

def trust_boundary(c: dict) -> str:
    W, H = 1010, 516
    s = [svg_open(W, H,
                  "Where the AI sits in TrustSight. It interprets a drawing and "
                  "proposes facts with citations, but never computes a quantity "
                  "and never approves a release. Deterministic rules do the "
                  "arithmetic and stop to ask an engineer for anything the "
                  "drawing does not state; every step is appended to a "
                  "hash-chained evidence record.", c["bg"]),
         defs(c)]

    s.append(text(40, 44, "Where the AI sits", fill=c["ink"], size=20, weight=700))
    s.append(text(40, 68, "and the two things it is never allowed to do",
                  fill=c["muted"], size=13))

    top, bh = 100, 108
    cols = [(40, 140), (242, 220), (524, 220), (806, 160)]
    mid = top + bh / 2

    # 1 — the input
    x, w = cols[0]
    s.append(box(x, top, w, bh, fill=c["panel"], stroke=c["line"]))
    s.append(text(x + w / 2, top + 44, "Drawing set", fill=c["ink"], size=14,
                  weight=600, anchor="middle"))
    s.append(text(x + w / 2, top + 66, "native-text PDF", fill=c["muted"],
                  size=11.5, anchor="middle"))
    s.append(text(x + w / 2, top + 84, "the client's own issue", fill=c["muted"],
                  size=11.5, anchor="middle"))

    # 2 — interpretation, the fenced stage
    x, w = cols[1]
    s.append(box(x, top, w, bh, fill=c["accent_soft"], stroke=c["accent"],
                 width=2))
    s.append(text(x + w / 2, top + 34, "Interpretation", fill=c["ink"], size=14,
                  weight=600, anchor="middle"))
    s.append(text(x + w / 2, top + 56, "reads the sheet and proposes",
                  fill=c["ink2"], size=11.5, anchor="middle"))
    s.append(text(x + w / 2, top + 72, "what a callout means", fill=c["ink2"],
                  size=11.5, anchor="middle"))
    s.append(text(x + w / 2, top + 94, "AI runs here — nowhere else",
                  fill=c["accent"], size=11.5, weight=700, anchor="middle"))

    # 3 — calculation
    x, w = cols[2]
    s.append(box(x, top, w, bh, fill=c["panel"], stroke=c["line"]))
    s.append(text(x + w / 2, top + 34, "Calculation", fill=c["ink"], size=14,
                  weight=600, anchor="middle"))
    s.append(text(x + w / 2, top + 56, "shape catalogue, rulebook,",
                  fill=c["ink2"], size=11.5, anchor="middle"))
    s.append(text(x + w / 2, top + 72, "deterministic arithmetic",
                  fill=c["ink2"], size=11.5, anchor="middle"))
    s.append(text(x + w / 2, top + 94, "no model runs here", fill=c["muted"],
                  size=11.5, weight=700, anchor="middle"))

    # 4 — release
    x, w = cols[3]
    s.append(box(x, top, w, bh, fill=c["ok_soft"], stroke=c["ok"]))
    s.append(text(x + w / 2, top + 40, "Release gate", fill=c["ink"], size=14,
                  weight=600, anchor="middle"))
    s.append(text(x + w / 2, top + 62, "six conditions,", fill=c["ink2"],
                  size=11.5, anchor="middle"))
    s.append(text(x + w / 2, top + 78, "checked per claim", fill=c["ink2"],
                  size=11.5, anchor="middle"))

    # flow, every arrow named
    for (ax, aw), (bx, _), label, hue, marker in (
            (cols[0], cols[1], ["pages"], c["muted"], "arrow"),
            (cols[1], cols[2], ["proposed", "+ citation"], c["accent"],
             "arrow-accent"),
            (cols[2], cols[3], ["quantity"], c["muted"], "arrow")):
        s.append(arrow(ax + aw + 6, mid, bx - 6, mid, stroke=hue, marker=marker))
        cx = (ax + aw + bx) / 2
        for i, line in enumerate(label):
            s.append(text(cx, mid - 16 - (len(label) - 1 - i) * 14, line,
                          fill=hue, size=11, anchor="middle"))

    # the fence
    fy, (fx, fw) = 248, cols[1]
    s.append(f'<line x1="{fx + fw / 2}" y1="{top + bh}" x2="{fx + fw / 2}" '
             f'y2="{fy}" stroke="{c["stop"]}" stroke-width="1.2" '
             f'stroke-dasharray="4 4"/>')
    s.append(box(fx, fy, fw, 76, fill=c["stop_soft"], stroke=c["stop"],
                 dash="5 4"))
    s.append(text(fx + fw / 2, fy + 30, "never computes a quantity",
                  fill=c["stop"], size=12, weight=700, anchor="middle"))
    s.append(text(fx + fw / 2, fy + 52, "never approves a release",
                  fill=c["stop"], size=12, weight=700, anchor="middle"))

    # the human, with both directions named
    hy, (hx, hw) = 248, cols[2]
    s.append(box(hx, hy, hw, 76, fill=c["human_soft"], stroke=c["human"]))
    s.append(text(hx + hw / 2, hy + 30, "Engineer", fill=c["ink"], size=14,
                  weight=600, anchor="middle"))
    s.append(text(hx + hw / 2, hy + 52, "answers what the drawing",
                  fill=c["ink2"], size=11.5, anchor="middle"))
    s.append(text(hx + hw / 2, hy + 68, "never states", fill=c["ink2"],
                  size=11.5, anchor="middle"))
    s.append(arrow(hx + 52, top + bh + 4, hx + 52, hy - 4, stroke=c["human"],
                   marker="arrow-human"))
    s.append(text(hx + 44, (top + bh + hy) / 2 + 4, "asks", fill=c["human"],
                  size=11, anchor="end"))
    s.append(arrow(hx + hw - 52, hy - 4, hx + hw - 52, top + bh + 4,
                   stroke=c["human"], marker="arrow-human"))
    s.append(text(hx + hw - 44, (top + bh + hy) / 2 + 4, "answers,",
                  fill=c["human"], size=11))
    s.append(text(hx + hw - 44, (top + bh + hy) / 2 + 18, "with a rationale",
                  fill=c["human"], size=11))

    # evidence chain — one arrow down the clear channel between the two boxes
    ey = 384
    channel = (fx + fw + hx) / 2
    s.append(arrow(channel, fy + 80, channel, ey - 6, stroke=c["line"],
                   marker="arrow-line", dash="4 4"))
    s.append(text(channel - 8, fy + 104, "every step appends", fill=c["muted"],
                  size=11, anchor="end"))
    s.append(box(40, ey, W - 80, 62, fill=c["panel"], stroke=c["line"]))
    s.append(text(64, ey + 26, "Evidence chain", fill=c["ink"], size=14,
                  weight=600))
    s.append(text(64, ey + 47,
                  "every proposal, every answer, every calculation — one hashed "
                  "record naming the record before it",
                  fill=c["ink2"], size=12))

    s.append(text(40, H - 18,
                  "A model that proposes is useful. A model that computes the "
                  "quantity, or signs off the release, is the thing being "
                  "replaced.",
                  fill=c["muted"], size=12))
    s.append("</svg>")
    return "".join(s)


# ── Diagram 2 — what deploys today vs what production needs ───────────────────

ROWS = [
    ("Sign-in", "demo user picker · no password checked",
     "Cognito, then OIDC / SAML", False),
    ("Intake", "web upload, hashed and revisioned",
     "+ S3 prefix watcher, SFTP transfer", False),
    ("App tier", "App Runner · pinned to 1 instance",
     "App Runner · N instances", True),
    ("Run state", "held in the process, lost on restart",
     "external store (DynamoDB or Postgres)", True),
    ("Projects", "one JSON file on the instance",
     "the same external store", False),
]

NOTE = [
    "One instance is not a capacity choice. A second instance would serve runs "
    "it holds no state for,",
    "so the deploy pins MaxSize to 1 — and that pin lifts the moment run state "
    "moves out of the process.",
]


def scale_path(c: dict) -> str:
    W, H = 1100, 566
    s = [svg_open(W, H,
                  "What TrustSight deploys today versus what production needs, "
                  "row by row. Run state is held in the application process, "
                  "which forces the hosted service to run on a single instance. "
                  "Moving run state to an external store is what unlocks "
                  "multiple instances; sign-in, intake and the project store "
                  "change independently.", c["bg"]),
         defs(c)]

    s.append(text(40, 44, "What deploys today, and what production needs",
                  fill=c["ink"], size=20, weight=700))
    s.append(text(40, 68, "one change unlocks the rest — the others are additive",
                  fill=c["muted"], size=13))

    lx, lw = 150, 340
    rx, rw = 560, 380
    s.append(text(lx + lw / 2, 104, "TODAY · DEMONSTRATOR", fill=c["muted"],
                  size=11.5, weight=700, anchor="middle"))
    s.append(text(rx + rw / 2, 104, "PRODUCTION · WHAT CHANGES", fill=c["muted"],
                  size=11.5, weight=700, anchor="middle"))

    y0, rh, gap = 122, 62, 12
    for i, (label, today, prod, key) in enumerate(ROWS):
        y = y0 + i * (rh + gap)
        s.append(text(40, y + rh / 2 + 5, label, fill=c["ink"], size=13,
                      weight=600))
        s.append(box(lx, y, lw, rh, fill=c["stop_soft"] if key else c["panel"],
                     stroke=c["stop"] if key else c["line"],
                     width=1.6 if key else 1.2))
        s.append(text(lx + 18, y + rh / 2 + 5, today,
                      fill=c["stop"] if key else c["ink2"], size=12.5,
                      weight=600 if key else 400))
        s.append(box(rx, y, rw, rh, fill=c["ok_soft"] if key else c["panel"],
                     stroke=c["ok"] if key else c["line"],
                     width=1.6 if key else 1.2))
        s.append(text(rx + 18, y + rh / 2 + 5, prod,
                      fill=c["ok"] if key else c["ink2"], size=12.5,
                      weight=600 if key else 400))
        s.append(arrow(lx + lw + 10, y + rh / 2, rx - 10, y + rh / 2,
                       stroke=c["accent"] if key else c["line"],
                       marker="arrow-accent" if key else "arrow-line",
                       width=1.6 if key else 1.1))

    # The dependency the diagram exists to show: the pin is caused, not chosen.
    app_y = y0 + 2 * (rh + gap) + rh / 2
    state_y = y0 + 3 * (rh + gap) + rh / 2
    s.append(f'<path d="M{lx - 14} {state_y} C{lx - 52} {state_y}, '
             f'{lx - 52} {app_y}, {lx - 14} {app_y}" fill="none" '
             f'stroke="{c["stop"]}" stroke-width="1.6" '
             f'marker-end="url(#arrow-stop)"/>')
    s.append(text(lx - 60, (app_y + state_y) / 2 + 4, "forces", fill=c["stop"],
                  size=11.5, weight=700, anchor="end"))
    s.append(f'<path d="M{rx + rw + 14} {state_y} C{rx + rw + 52} {state_y}, '
             f'{rx + rw + 52} {app_y}, {rx + rw + 14} {app_y}" fill="none" '
             f'stroke="{c["ok"]}" stroke-width="1.6" '
             f'marker-end="url(#arrow-ok)"/>')
    s.append(text(rx + rw + 62, (app_y + state_y) / 2 + 4, "unlocks",
                  fill=c["ok"], size=11.5, weight=700))

    note = 486
    s.append(box(40, note, W - 80, 62, fill=c["panel"], stroke=c["line"]))
    for i, line in enumerate(NOTE):
        s.append(text(64, note + 26 + i * 20, line, fill=c["ink2"], size=12))
    s.append("</svg>")
    return "".join(s)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in (("trust-boundary", trust_boundary),
                     ("scale-path", scale_path)):
        for theme, colours in THEMES.items():
            path = OUT / f"{name}-{theme}.svg"
            path.write_text(fn(colours), encoding="utf-8")
            print(f"wrote {path.name}")


if __name__ == "__main__":
    main()
