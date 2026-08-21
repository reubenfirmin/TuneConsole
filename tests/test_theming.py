"""The theming contract: exactly one file in the codebase may name a colour.

Colour used to be scattered across stylesheets, three inline <style> blocks, two canvas
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


@pytest.mark.parametrize("template", ["base.html", "story_reel.html", "generating.html"])
def test_application_stylesheets_keep_cascade_order(template):
    expected = ["tokens.css", "app.css", "home.css", "clusters.css", "onboarding.css",
                "analytics.css", "page-overrides.css"]
    text = (WEB / "templates" / template).read_text()
    sheets = re.findall(r'<link[^>]+href="/static/([a-z_-]+\.css)', text)
    assert sheets[:len(expected)] == expected, f"{template} stylesheet order changed: {sheets}"


def test_no_inline_style_blocks_in_templates():
    """Page CSS belongs in a versioned stylesheet, not in a <style> tag, or it
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


def test_generating_takeover_title_is_scoped():
    """The takeover heading must not override dashboard playlist-row song titles."""
    css = (WEB / "static" / "page-overrides.css").read_text()
    assert ".gen-stage .gen-title" in css
    assert not re.search(r"(?m)^\.gen-title\s*\{", css)


def test_flat_surface_foundation_and_protected_experiences():
    """Ordinary cards use flat roles; Clusters and Recap keep their palette bindings."""
    tokens = TOKENS.read_text()
    app = (WEB / "static" / "app.css").read_text()
    for role in ("--canvas", "--surface-card", "--surface-interactive", "--surface-raised",
                 "--surface-inset", "--border-subtle", "--border-strong"):
        assert role in tokens
    card = re.search(r"(?s)\.card\s*\{(.*?)\}", app).group(1)
    assert "background: var(--surface-card)" in card
    assert "gradient(" not in card
    assert "--graph-node:   var(--p-indigo-850)" in tokens
    assert "--reel-card-bg:      linear-gradient(160deg, var(--p-indigo-850), var(--p-night))" in tokens


def test_ordinary_controls_do_not_use_legacy_gradient_tokens():
    """Flat controls have semantic solid fills; gradient roles belong to protected experiences."""
    tokens = TOKENS.read_text()
    for legacy in ("--grad:", "--grad-soft:", "--grad-cta:", "--grad-danger:"):
        assert legacy not in tokens
    consumers = "\n".join(
        p.read_text() for p in (WEB / "static").glob("*.css") if p.name != "tokens.css"
    )
    assert not re.search(r"var\(--grad(?:-soft|-cta|-danger)?\)", consumers)
    assert "background: var(--control-primary)" in (WEB / "static" / "app.css").read_text()
    assert "background: var(--cluster-seed-bg)" in (WEB / "static" / "clusters.css").read_text()


def test_application_shell_is_flat():
    app = (WEB / "static" / "app.css").read_text()
    body = re.search(r"(?s)^body\s*\{(.*?)\}", app, re.M).group(1)
    topbar = re.search(r"(?s)\.topbar\s*\{(.*?)\}", app).group(1)
    assert "background: var(--canvas)" in body
    assert "gradient(" not in body
    assert "body::before" not in app
    assert "background: var(--surface-card)" in topbar
    assert "gradient(" not in topbar and "backdrop-filter" not in topbar


def test_flat_card_variants_exist_and_are_used():
    app = (WEB / "static" / "app.css").read_text()
    templates = "\n".join(p.read_text() for p in (WEB / "templates").rglob("*.html"))
    for variant in ("interactive", "featured", "status", "data", "inset", "toolbar"):
        selector = f".card--{variant}"
        assert selector in app, f"missing {selector} rules"
        assert f"card--{variant}" in templates, f"{selector} is not used"
    base = re.search(r"(?s)\.card\s*\{(.*?)\}", app).group(1)
    hover = re.search(r"(?s)\.card--interactive:hover\s*\{(.*?)\}", app).group(1)
    assert "box-shadow: none" in base and "gradient(" not in base
    assert "transform" not in hover and "gradient(" not in hover


def test_neon_green_is_reserved_for_explicit_primary_actions():
    app = (WEB / "static" / "app.css").read_text()
    base = re.search(r"(?s)button, \.btn\s*\{(.*?)\}", app).group(1)
    base_hover = re.search(r"(?s)button:hover, \.btn:hover\s*\{(.*?)\}", app).group(1)
    primary = re.search(r"(?s)\.btn-primary\s*\{(.*?)\}", app).group(1)
    assert "var(--cta)" not in base and "var(--cta)" not in base_hover
    assert "background: var(--surface-interactive)" in base
    assert "background: var(--cta)" in primary

    templates = "\n".join(p.read_text() for p in (WEB / "templates").rglob("*.html"))
    for label in ("Import history", "Create playlist", "Save choices", "Save recipe"):
        line = next(line for line in templates.splitlines() if label in line)
        assert "btn-primary" in line, f"primary action is not classified: {line.strip()}"


def test_serif_is_limited_to_the_generating_takeover():
    offenders = {}
    for path in (WEB / "static").glob("*.css"):
        if path.name in {"tokens.css", "page-overrides.css"}:
            continue
        if "var(--font-display)" in path.read_text():
            offenders[path.name] = True
    assert not offenders, f"routine application CSS still uses the serif face: {offenders}"

    overrides = (WEB / "static" / "page-overrides.css").read_text()
    uses = re.findall(r"font-family:\s*var\(--font-display\)", overrides)
    assert len(uses) == 1
    assert ".gen-stage .gen-title" in overrides


def test_eyebrows_are_limited_to_protected_experiences():
    ordinary_templates = [p for p in (WEB / "templates").rglob("*.html")
                          if p.name not in {"story_reel.html", "clusters.html"}]
    offenders = [p.name for p in ordinary_templates if re.search(r"\beyebrow(?:-|\b)", p.read_text())]
    assert not offenders, f"ordinary templates still use eyebrows: {offenders}"
    assert ".eyebrow" not in (WEB / "static" / "app.css").read_text()

    assert "slide-eyebrow" in (WEB / "templates" / "story_reel.html").read_text()
    assert "cj-eyebrow" in (WEB / "templates" / "clusters.html").read_text()
    assert "gen-status" in (WEB / "templates" / "generating.html").read_text()


def test_shared_typography_uses_the_application_scale():
    tokens = TOKENS.read_text()
    for role in ("--type-xs", "--type-sm", "--type-ui", "--type-body", "--type-lg",
                 "--type-xl", "--type-2xl", "--type-page", "--leading-tight",
                 "--leading-compact", "--leading-body"):
        assert role in tokens

    app = (WEB / "static" / "app.css").read_text()
    for selector, role in ((".page-head h1", "--type-page"), ("h2.section", "--type-xl"),
                           ("h3.section", "--type-lg"), ("button, .btn", "--type-ui"),
                           (".section-note", "--type-ui")):
        rules = re.findall(rf"(?s){re.escape(selector)}\s*\{{(.*?)\}}", app)
        assert rules and any(f"var({role})" in rule for rule in rules), selector

    ordinary = "\n".join(
        (WEB / "static" / name).read_text()
        for name in ("app.css", "home.css", "onboarding.css", "analytics.css", "setup.css")
    )
    assert not re.search(
        r"font-size:\s*(?:\.7[24568]|\.8(?:0|2|4|5|6|8)?|\.9(?:0|2|5|8)?|1\.(?:05|1|12|15))rem",
        ordinary,
    )


def test_shared_spacing_scale_and_layout_primitives_are_used():
    tokens = TOKENS.read_text()
    app = (WEB / "static" / "app.css").read_text()
    templates = "\n".join(
        (WEB / "templates" / name).read_text()
        for name in ("albums.html", "charts.html", "cleanup.html", "move.html",
                     "network.html", "playlists.html")
    )
    for role in ("--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6", "--sp-7"):
        assert role in tokens
    for role in ("--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6"):
        assert f"var({role})" in app
    for selector in (".cluster", ".toolbar", ".section-block"):
        assert selector in app
    for utility in ("toolbar", "section-block", "flush", "push-end", "inline-control"):
        assert re.search(rf'class="[^"]*\b{utility}\b', templates), utility


def test_static_presentation_is_not_repeated_inline():
    """Common static layout belongs to CSS; inline styles are reserved for runtime data."""
    templates = "\n".join(
        p.read_text() for p in (WEB / "templates").rglob("*.html")
        if p.name not in {"story_reel.html", "clusters.html"}
    )
    forbidden = (
        'style="margin-left:auto"', 'style="margin-top:0"',
        'style="display:inline"', 'style="width:auto"',
        'style="text-align:right; white-space:nowrap"',
        'style="white-space:nowrap; color:var(--dim)"',
    )
    assert not [rule for rule in forbidden if rule in templates]

    app = (WEB / "static" / "app.css").read_text()
    for selector in (".push-end", ".section-flush", ".inline", ".control-auto",
                     ".cell-actions", ".col-thumb", ".col-length"):
        assert selector in app


def test_dense_pages_have_scoped_compact_screen_behavior():
    app = (WEB / "static" / "app.css").read_text()
    assert "@media (max-width: 720px)" in app
    for selector in (".playlist-index", ".track-table", ".album-index", ".table-scroll"):
        assert selector in app

    playlists = (WEB / "templates" / "playlists.html").read_text()
    albums = (WEB / "templates" / "albums.html").read_text()
    assert playlists.count('class="playlist-index"') == 2
    assert albums.count('class="album-index"') == 2
    for name in ("network.html", "actions.html"):
        assert 'class="table-scroll"' in (WEB / "templates" / name).read_text()

    # Interactive track tables have popovers and must not be placed in an
    # overflow container that would clip them.
    for name in ("playlist.html", "album.html"):
        text = (WEB / "templates" / name).read_text()
        assert 'table-scroll"><table class="track-table' not in text


def test_ordinary_interactions_have_keyboard_and_motion_support():
    app = (WEB / "static" / "app.css").read_text()
    assert "main:not(.page-clusters)" in app
    assert ":focus-visible" in app
    assert "@media (pointer: coarse)" in app
    assert "@media (prefers-reduced-motion: reduce)" in app
    assert "animation-duration: .01ms !important" in app

    sort_headers = re.findall(
        r'<th class="sorth[^>]+>',
        "\n".join(p.read_text() for p in (WEB / "templates").rglob("*.html")),
    )
    assert sort_headers
    for header in sort_headers:
        assert 'tabindex="0"' in header
        assert 'role="button"' in header
        assert '@keydown.enter.space.prevent=' in header

    clusters = (WEB / "templates" / "clusters.html").read_text()
    assert "{% block page_class %}page-clusters{% endblock %}" in clusters


def test_final_style_foundation_has_no_obsolete_layout_aliases():
    app = (WEB / "static" / "app.css").read_text()
    tokens = TOKENS.read_text()
    for obsolete in (".pl-toolbar", ".layout-grid", ".stack >", ".flow >"):
        assert obsolete not in app
    for obsolete in ("--flow-space", "--grid-space", "--grid-min"):
        assert obsolete not in tokens

    home = (WEB / "static" / "home.css").read_text()
    onboard = re.search(r"(?s)\.onboard-card\s*\{(.*?)\}", home).group(1)
    assert "background: var(--surface-card)" in onboard
    assert "gradient(" not in onboard

    # Static SVG presentation belongs with the shared brand component.
    for name in ("base.html", "generating.html", "home.html"):
        text = (WEB / "templates" / name).read_text()
        assert 'style="stop-color:' not in text
    assert ".eq-stop-start" in app and ".eq-delay-1" in app


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
