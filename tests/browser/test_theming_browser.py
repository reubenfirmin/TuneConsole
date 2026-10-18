"""Runtime half of the theming contract (tests/test_theming.py is the static half).

Everything here needs a real browser: whether tokens.css actually applied, whether var() resolves
inside an SVG style attribute, and whether the canvas bridge in static/theme.js hands JS a colour
canvas can paint with. A token that resolves to nothing fails silently in production — the element
just inherits — so these assert on concrete computed values rather than on "not empty"."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def test_focus_is_quiet_and_text_fields_have_a_single_indicator(page, live_app):
    page.goto(f"{live_app}/playlists")
    heading = page.get_by_role("button", name="Playlist", exact=False).first
    page.keyboard.press("Tab")
    heading.focus()
    style = heading.evaluate("""el => {
      const s = getComputedStyle(el);
      return {color: s.outlineColor, width: s.outlineWidth, visible: el.matches(':focus-visible')};
    }""")
    assert style == {"color": "rgb(57, 135, 229)", "width": "1px", "visible": True}
    heading.click()
    # Moving focus with the pointer doesn't leave a keyboard frame behind.
    page.locator("h1").click()
    heading.click()
    assert not heading.evaluate("el => el.matches(':focus-visible')")

    field = page.locator(".omni-input")
    field.fill("funk and soul")
    expect(field).to_be_focused()
    expect(field).to_have_css("border-color", "rgb(57, 135, 229)")
    expect(field).to_have_css("outline-style", "none")
    style = field.evaluate("""el => {
      const s = getComputedStyle(el);
      return {border: s.borderColor, outline: s.outlineStyle, shadow: s.boxShadow,
              readable: s.color !== s.backgroundColor};
    }""")
    assert style == {"border": "rgb(57, 135, 229)", "outline": "none", "shadow": "none", "readable": True}


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
