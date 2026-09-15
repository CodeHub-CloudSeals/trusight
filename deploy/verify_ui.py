"""Re-runnable UI verification: contrast, overflow, console errors.

This existed as a scratch script and was lost between sessions, which meant a
claim about the interface could not be re-checked. It lives in the repo now so
the go/no-go in the runbook can be executed rather than remembered.

Two things it gets right that a naive checker does not:

* Chromium serialises ``color-mix()`` as ``color(srgb 1 1 1 / 0.77)`` — 0-1
  floats, not 0-255 bytes. Read as bytes, white becomes near-black and every
  glass surface reports a false failure.
* A gradient background is invisible to a computed-style walk. When an
  ancestor paints one, the effective backdrop is unknowable from CSS alone, so
  those nodes are reported as unchecked rather than silently passed.

Usage:  python deploy/verify_ui.py http://localhost:8601
"""
from __future__ import annotations

import asyncio
import re
import sys

AA_NORMAL = 4.5
AA_LARGE = 3.0
PHONE_WIDTH = 430

# (label, javascript to reach it). The first two are pre-run product shell;
# the rest are workspace tabs reached through the app's own router, so this
# checks the screens the client actually sees rather than a synthetic DOM.
SCREENS = [
    ("sign-in", None),
    ("projects", "chooseUser('priya.raman@demo-client.com')"),
    # Enter through openProject, the path a person takes. Calling start()
    # directly leaves the sign-in stage overlaying the workspace, so every tab
    # measures the app chrome and reports clean — which is exactly how a
    # contrast run can pass without ever seeing a screen.
    ("overview", "openProject('atlantic-demo')"),
    ("assessment", "go('assess')"),
    ("flow", "go('flow')"),
    ("drawings", "go('drawings')"),
    ("schedule", "go('results')"),
    ("review", "go('review')"),
    ("value", "go('value')"),
    ("evidence", "go('evidence')"),
    ("capabilities", "go('capabilities')"),
    # Overlays are what a presenter opens in front of the client and what a
    # tab-walking checker never sees. Both are opened here on purpose.
    ("alerts", "document.querySelector('#alerts-open').click()"),
    ("ask-action", "document.querySelector('#alerts-open').click();"
                   "openAsk();setTimeout(()=>askNow('approve the rulebook'),150)"),
]

_SRGB = re.compile(r"color\(srgb([^)]*)\)")
_RGB = re.compile(r"rgba?\(([^)]*)\)")


def parse_color(value: str) -> tuple[float, float, float, float] | None:
    """Return non-premultiplied RGBA in 0-255, or None if not a flat colour."""
    value = (value or "").strip()
    m = _SRGB.match(value)
    if m:
        parts = [p for p in re.split(r"[\s/]+", m.group(1)) if p]
        if len(parts) < 3:
            return None
        r, g, b = (float(p) * 255 for p in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
        return r, g, b, a
    m = _RGB.match(value)
    if m:
        parts = [p.strip() for p in m.group(1).replace("/", " ").split(",")]
        if len(parts) == 1:
            parts = m.group(1).split()
        if len(parts) < 3:
            return None
        nums = []
        for p in parts[:4]:
            p = p.strip()
            nums.append(float(p[:-1]) / 100 * 255 if p.endswith("%") else float(p))
        r, g, b = nums[:3]
        a = nums[3] if len(nums) > 3 else 1.0
        if len(nums) > 3 and a > 1:      # rgba(…, 100%) already scaled above
            a = a / 255
        return r, g, b, a
    return None


def composite(fg: tuple[float, float, float, float],
              bg: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    r1, g1, b1, a1 = fg
    r2, g2, b2, a2 = bg
    a = a1 + a2 * (1 - a1)
    if a == 0:
        return 0.0, 0.0, 0.0, 0.0
    f = lambda c1, c2: (c1 * a1 + c2 * a2 * (1 - a1)) / a
    return f(r1, r2), f(g1, g2), f(b1, b2), a


def luminance(c: tuple[float, float, float, float]) -> float:
    def ch(v: float) -> float:
        v = v / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def ratio(fg, bg) -> float:
    l1, l2 = luminance(fg), luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


PROBE = """() => {
  const out = [];
  const walk = (el) => {
    for (const node of el.childNodes) {
      if (node.nodeType === 3 && node.textContent.trim().length > 1) {
        const p = node.parentElement;
        if (!p) continue;
        const cs = getComputedStyle(p);
        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
        const box = p.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) continue;
        const stack = [];
        let a = p, gradient = false;
        while (a) {
          const acs = getComputedStyle(a);
          if (acs.backgroundImage && acs.backgroundImage !== 'none') gradient = true;
          stack.push(acs.backgroundColor);
          a = a.parentElement;
        }
        out.push({
          text: node.textContent.trim().slice(0, 48),
          color: cs.color,
          size: parseFloat(cs.fontSize),
          weight: parseInt(cs.fontWeight) || 400,
          backdrops: stack,
          gradient,
          tag: p.tagName.toLowerCase() + (p.className ? '.' + String(p.className).split(' ')[0] : ''),
        });
      }
      if (node.nodeType === 1) walk(node);
    }
  };
  walk(document.body);
  return out;
}"""


def evaluate(nodes: list[dict]) -> tuple[list[dict], int, int]:
    failures, checked, unchecked = [], 0, 0
    for n in nodes:
        fg = parse_color(n["color"])
        if fg is None:
            unchecked += 1
            continue
        bg = None
        for raw in n["backdrops"]:
            c = parse_color(raw)
            if c is None or c[3] == 0:
                continue
            bg = c if bg is None else composite(bg, c)
            if bg[3] >= 0.999:
                break
        if bg is None or bg[3] < 0.999:
            # no opaque backdrop resolved from CSS alone
            unchecked += 1
            continue
        if n["gradient"]:
            unchecked += 1
            continue
        fg_on_bg = composite(fg, bg) if fg[3] < 1 else fg
        large = n["size"] >= 24 or (n["size"] >= 18.66 and n["weight"] >= 700)
        need = AA_LARGE if large else AA_NORMAL
        r = ratio(fg_on_bg, bg)
        checked += 1
        if r < need - 0.005:
            failures.append({**n, "ratio": round(r, 2), "need": need})
    return failures, checked, unchecked


async def main(base: str) -> int:
    from playwright.async_api import async_playwright

    bad = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for theme in ("light", "dark"):
            page = await browser.new_page(
                viewport={"width": 1440, "height": 900},
                color_scheme=theme)
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text)
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(base, wait_until="networkidle")
            await page.wait_for_timeout(600)

            seen: dict[str, int] = {}
            for name, action in SCREENS:
                if action:
                    try:
                        await page.evaluate(f"() => {{ {action}; }}")
                        if "openProject(" in action:
                            # wait for the run to settle, not for a guessed
                            # number of milliseconds — a fixed sleep once made
                            # every tab report identical counts because the
                            # router was still refusing to render.
                            await page.wait_for_function(
                                "() => { const m = document.querySelector('#main');"
                                " if (!m || !document.querySelector('#nav button.active')"
                                "     || document.querySelector('.loading-brand')) return false;"
                                " const b = m.getBoundingClientRect();"
                                " return b.width > 200 && b.height > 200"
                                "   && m.innerHTML.length > 1500; }",
                                timeout=60000)
                        await page.wait_for_timeout(1400 if name.startswith(
                            ("alerts", "ask")) else 700)
                    except Exception as exc:
                        print(f"  FAIL  {theme:<5} {name:<12} could not be reached: "
                              f"{str(exc).splitlines()[0]}")
                        bad += 1
                        continue
                nodes = await page.evaluate(PROBE)
                seen[name] = len(nodes)
                fails, checked, unchecked = evaluate(nodes)
                mark = "FAIL" if fails else "PASS"
                if fails:
                    bad += len(fails)
                print(f"  {mark}  {theme:<5} {name:<12} "
                      f"{checked} checked, {unchecked} unresolved, {len(fails)} below AA")
                for f in fails[:6]:
                    print(f"        {f['ratio']}:1 (need {f['need']}) "
                          f"{f['tag']} — {f['text']!r}")

            # A checker that silently never navigated would report every screen
            # clean. If the tabs all measure the same, it measured one screen
            # eleven times.
            tabs = [v for k, v in seen.items()
                    if k not in ("sign-in", "projects", "alerts", "ask-action")]
            if len(set(tabs)) < 3:
                print(f"  FAIL  {theme:<5} {'navigation':<12} "
                      f"tabs did not render distinctly: {seen}")
                bad += 1
            else:
                print(f"  PASS  {theme:<5} {'navigation':<12} "
                      f"{len(set(tabs))} distinct screens measured")

            # phone width: nothing may scroll sideways
            await page.set_viewport_size({"width": PHONE_WIDTH, "height": 900})
            await page.wait_for_timeout(400)
            overflow = await page.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            mark = "PASS" if overflow <= 0 else "FAIL"
            if overflow > 0:
                bad += 1
            print(f"  {mark}  {theme:<5} {'overflow':<12} {overflow}px at {PHONE_WIDTH}px")

            real = [e for e in errors if "favicon" not in e.lower()]
            mark = "PASS" if not real else "FAIL"
            if real:
                bad += len(real)
            print(f"  {mark}  {theme:<5} {'console':<12} {len(real)} errors")
            for e in real[:5]:
                print(f"        {e[:120]}")
            await page.close()
        await browser.close()

    print()
    print("ALL CHECKS PASSED" if bad == 0 else f"{bad} PROBLEM(S) — do not demo this build")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    raise SystemExit(asyncio.run(main(url.rstrip("/"))))
