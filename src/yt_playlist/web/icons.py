"""Shared, substantial arrow shapes for interface actions and navigation."""
from markupsafe import Markup


_PATHS = {
    "left": "M11 3 2 12l9 9 3-3-4-4h12v-4H10l4-4z",
    "right": "m13 3 9 9-9 9-3-3 4-4H2v-4h12l-4-4z",
    "up": "m3 11 9-9 9 9-3 3-4-4v12h-4V10l-4 4z",
    "down": "m3 13 9 9 9-9-3-3-4 4V2h-4v12l-4-4z",
    "external": "M5 3h16v16h-4v-9L5 22l-3-3L14 7H5z",
    "disclosure": "m4 8 8 9 8-9z",
    "refresh": "M20 7v5h-5m5 0a8 8 0 1 1-2.3-5.7",
    "reset": "M4 4v6h6M4 10a8 8 0 1 1 1.8 8",
}


def arrow_icon(kind: str) -> Markup:
    """Decorative SVG; the enclosing link or button supplies its accessible name."""
    path = _PATHS[kind]
    if kind in {"refresh", "reset"}:
        paint = ('fill="none" stroke="currentColor" stroke-width="3" '
                 'stroke-linecap="round" stroke-linejoin="round"')
    else:
        paint = 'fill="currentColor"'
    return Markup(
        '<svg class="ui-arrow ui-arrow--{}" viewBox="0 0 24 24" width="20" height="20" '
        '{} aria-hidden="true" focusable="false"><path d="{}"/></svg>'
    ).format(kind, Markup(paint), path)
