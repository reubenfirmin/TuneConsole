"""The theming contract: exactly one file in the codebase may name a colour.

Colour used to be scattered across app.css, story.css, three inline <style> blocks, two canvas
renderers with their own hard-coded palettes, and two Python dicts that disagreed with each other.
Retheming meant a grep-and-hope across four languages. These tests keep it collapsed to
static/tokens.css so a new theme is a single-file edit.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "yt_playlist" / "web"
SRC = Path(__file__).resolve().parents[1] / "src"
TOKENS = WEB / "static" / "tokens.css"

HEX = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b")
RGB = re.compile(r"\brgba?\(\s*\d+\s*,")
HSL = re.compile(r"\bhsla?\(\s*\d")
NAMED = re.compile(
    r"(?:^|[;{\s])(?:color|background|background-color|fill|stroke|border-color|stop-color)"
    r"\s*:\s*(red|blue|green|white|black|yellow|orange|purple|pink|gray|grey|cyan|magenta)\b",
    re.I,
)


def _source_files():
    for p in sorted(SRC.rglob("*")):
        if p.is_dir() or p.suffix not in {".css", ".js", ".html", ".py"}:
            continue
        if "vendor" in p.parts or "node_modules" in p.parts or p == TOKENS:
            continue
        yield p


def _colour_literals(path: Path) -> list[str]:
    """Colour literals in `path`, ignoring things that only look like one."""
    found = []
    for lineno, line in enumerate(path.read_text(errors="ignore").split("\n"), 1):
        # `(#106)` / `see #79` are GitHub issue references, not colours
        if re.search(r"\(#\d+\)|see #\d+|issue #\d+", line):
            continue
        for m in HEX.finditer(line):
            if line[: m.start()].endswith("&"):      # HTML entity, e.g. &#9662;
                continue
            tok = m.group(0)
            if len(tok) == 4 and tok[1:].isdigit():  # bare issue number
                continue
            # in Python a colour can only appear inside a string literal
            if path.suffix == ".py" and not re.search(r"[\"'][^\"']*" + re.escape(tok), line):
                continue
            found.append(f"{path.name}:{lineno}: {tok}")
        for rx in (RGB, HSL, NAMED):
            for m in rx.finditer(line):
                found.append(f"{path.name}:{lineno}: {m.group(0).strip()}")
    return found


def test_no_colour_literals_outside_the_token_layer():
    offenders = [hit for p in _source_files() for hit in _colour_literals(p)]
    assert not offenders, (
        "Colour literals belong in static/tokens.css, not here:\n  "
        + "\n  ".join(offenders)
        + "\n\nAdd a palette entry + a role token in tokens.css and reference the role instead."
    )


def test_tokens_css_is_the_only_stylesheet_with_literals():
    assert HEX.search(TOKENS.read_text()), "tokens.css should hold the palette"


def test_every_referenced_token_is_defined():
    """A var(--x) with no definition renders as nothing — catch typos before a blank UI does."""
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", TOKENS.read_text()))
    # set from JS or inline style at runtime, so they have no static definition
    runtime = {
        "--topbar-h", "--p", "--card", "--dot", "--heat", "--subj", "--petal-c",
        "--node-hue", "--node-sat", "--node-light", "--bg-grad", "--v",
    }
    missing = {}
    for p in _source_files():
        if p.suffix not in {".css", ".html", ".js"}:
            continue
        for tok in re.findall(r"var\((--[a-z0-9-]+)", p.read_text(errors="ignore")):
            if tok not in defined and tok not in runtime:
                missing.setdefault(tok, set()).add(p.name)
    assert not missing, f"undefined tokens referenced: { {k: sorted(v) for k, v in missing.items()} }"


def test_palette_layer_is_not_referenced_by_rules():
    """Consumers name roles, never raw hues, so themes only redefine the role layer."""
    leaks = {}
    for p in _source_files():
        if p.suffix not in {".css", ".html"}:
            continue
        for tok in set(re.findall(r"var\((--p-[a-z0-9-]+)", p.read_text(errors="ignore"))):
            leaks.setdefault(p.name, set()).add(tok)
    assert not leaks, f"raw palette hues used directly in rules: {leaks}"


@pytest.mark.parametrize("template", ["base.html", "story_reel.html", "generating.html"])
def test_tokens_css_loads_before_the_stylesheets_that_use_it(template):
    t = (WEB / "templates" / template).read_text()
    # order the actual <link> tags, not any prose that happens to mention a filename
    sheets = re.findall(r'<link[^>]+href="/static/([a-z_]+\.css)', t)
    assert "tokens.css" in sheets, f"{template} must load the token layer"
    assert sheets[0] == "tokens.css", f"{template} loads stylesheets in the wrong order: {sheets}"


def test_no_inline_style_blocks_in_templates():
    """Page CSS belongs in app.css (scoped via the page_class block), not in a <style> tag, or it
    escapes the token contract and the asset-version cache bust."""
    offenders = [p.name for p in (WEB / "templates").rglob("*.html") if "<style" in p.read_text()]
    assert not offenders, f"inline <style> blocks found in: {offenders}"


def test_elements_do_not_repeat_style_attributes():
    """Duplicate style attributes are invalid HTML and browsers may discard either declaration."""
    offenders = []
    tag = re.compile(r"<[^>]+>", re.S)
    for path in (WEB / "templates").rglob("*.html"):
        for match in tag.finditer(path.read_text()):
            if len(re.findall(r"(?:^|\s):?style\s*=", match.group(0))) > 1:
                lineno = path.read_text()[:match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"duplicate style attributes found: {offenders}"


def test_python_returns_token_references_not_colours():
    from yt_playlist.web import theme

    for value in (theme.family_hue("house"), theme.family_hue("nope"),
                  theme.genre_tint("techno"), theme.genre_tint(None)):
        assert value.startswith("var(--") and value.endswith(")"), value


def test_python_genre_tokens_all_exist_in_the_token_layer():
    """theme.py builds token names by string interpolation, so a family with no matching token
    would silently render as an empty colour."""
    from yt_playlist.web import theme

    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", TOKENS.read_text()))
    for fam in theme._BAND_FAMILIES:
        assert f"--fam-{fam}" in defined, f"--fam-{fam} missing from tokens.css"
    for fam in theme._TINT_FAMILIES:
        assert f"--genre-{fam}" in defined, f"--genre-{fam} missing from tokens.css"
    assert "--fam-other" in defined and "--genre-default" in defined
