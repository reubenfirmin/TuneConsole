"""#100 Every /static/nag/*.png a template references must exist on disk. The <img onerror> in the
alert cards silently degrades to .no-art, so a missing file looks like a styling bug, not a 404.
This walks the templates instead of listing names so a new nag can't ship art-less."""
import re
from pathlib import Path

import pytest

import yt_playlist.web as web

WEB = Path(web.__file__).parent
REF = re.compile(r"/static/(nag/[a-z0-9_-]+\.png)")


def _referenced() -> set[str]:
    return {
        m
        for tpl in (WEB / "templates").rglob("*.html")
        for m in REF.findall(tpl.read_text())
    }


def test_templates_reference_nag_art():
    # Guard the guard: if the templates stop matching, the test below passes vacuously.
    assert "nag/takeout.png" in _referenced()


@pytest.mark.parametrize("rel", sorted(_referenced()))
def test_referenced_nag_art_exists(rel):
    assert (WEB / "static" / rel).is_file(), f"template references /static/{rel}, which is missing"
