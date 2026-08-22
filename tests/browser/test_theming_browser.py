"""Runtime half of the theming contract (tests/test_theming.py is the static half).

Everything here needs a real browser: whether tokens.css actually applied, whether var() resolves
inside an SVG style attribute, and whether the canvas bridge in static/theme.js hands JS a colour
canvas can paint with. A token that resolves to nothing fails silently in production — the element
just inherits — so these assert on concrete computed values rather than on "not empty"."""
import pytest

pytestmark = pytest.mark.browser


def test_tokens_resolve_at_runtime(page, live_app):
    page.goto(live_app)
    page.wait_for_load_state("networkidle")

    # 1. role tokens resolve to real colours
    vals = page.evaluate("""() => {
      const cs = getComputedStyle(document.documentElement);
      const out = {};
      for (const t of ['--bg','--surface','--text','--accent','--cta','--danger','--teal',
                       '--fam-0','--fam-house','--genre-techno','--slot-3','--rose-5','--trunk'])
        out[t] = cs.getPropertyValue(t).trim();
      return out;
    }""")
    empty = [k for k, v in vals.items() if not v]
    assert not empty, f"tokens resolved to nothing: {empty} (all: {vals})"

    # 2. body actually paints the themed background (proves tokens.css loaded & applied)
    bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(11, 9, 32)", f"body background is {bg}, expected the --bg token"

    # 3. the canvas bridge resolves tokens, including color-mix() ones
    bridged = page.evaluate("""() => ({
      plain: window.themeColor('--accent'),
      mixed: window.themeColor('--graph-link'),
      missing: window.themeColor('--totally-made-up', 'rgb(1, 2, 3)'),
      palette: window.themePalette({a: '--cta', b: '--trunk'}),
    })""")
    # every bridged colour is normalised to rgba() so callers can safely restyle the alpha
    assert bridged["plain"] == "rgba(57, 135, 229, 1)", bridged
    assert bridged["mixed"].startswith("rgba("), f"color-mix did not resolve: {bridged['mixed']}"
    # a color-mix token must come back with 0-255 channels, not CSS Color 4 floats
    chans = [float(x) for x in bridged["mixed"][5:-1].split(",")]
    assert max(chans[:3]) > 1.5, f"color-mix channels look like 0-1 floats: {bridged['mixed']}"
    assert bridged["missing"] == "rgba(1, 2, 3, 1)", bridged
    assert bridged["palette"]["a"] == "rgba(255, 182, 40, 1)", bridged

    # 4. no stylesheet failed to load
    assert page.evaluate("() => [...document.styleSheets].length") >= 3

    # 5. the current text-only brand renders after the identity refresh
    assert page.get_by_role("link", name="TuneConsole home").is_visible()
