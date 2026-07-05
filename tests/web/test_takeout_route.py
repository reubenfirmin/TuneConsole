"""#61 upload route: happy path report, idempotent re-post, HTML-export hint."""
import json

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.testclient import TestClient

from yt_playlist.core.store import Store
from yt_playlist.web.routes.setup import build as build_setup_route

_TEMPLATES_DIR = "src/yt_playlist/web/templates"


class _FakeRecWorker:
    def __init__(self):
        self.calls = 0

    def trigger(self):
        self.calls += 1


def _entry(title, artist, vid, iso):
    return {"header": "YouTube Music", "title": f"Watched {title}",
            "titleUrl": f"https://music.youtube.com/watch?v={vid}" if vid else None,
            "subtitles": [{"name": f"{artist} - Topic"}], "time": iso}


def _client():
    store = Store(":memory:")
    store.init_schema()
    store.upsert_identity("main", "bridge", None, True)
    store.upsert_track("vidA", "Suave", "Danny Wabbit", "", 200)
    rec_worker = _FakeRecWorker()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    ctx = type("C", (), {
        "store": store, "templates": templates, "setup": None,
        "rec_worker": rec_worker, "now_fn": staticmethod(lambda: 1_700_000_000.0),
    })()
    app = FastAPI()
    app.include_router(build_setup_route(ctx))
    return TestClient(app), store, rec_worker


def test_upload_reports_and_triggers_rebuild():
    client, store, rec_worker = _client()
    entries = [_entry("Suave", "Danny Wabbit", "vidA", "2024-01-01T10:00:00Z"),
               _entry("Ghost Song", "Nobody", "NOVID9", "2024-01-02T10:00:00Z")]
    payload = json.dumps(entries).encode()

    # No seed_discovery field at all: a real browser omits unchecked checkboxes entirely, so this
    # is the true checkbox-off shape (an empty-string value would read as checked).
    resp = client.post("/import/takeout",
                       files={"file": ("watch-history.json", payload, "application/json")})

    assert resp.status_code == 200
    body = resp.text
    assert "Import complete" in body
    assert "plays matched your library" in body           # labeled stat, not a run-on sentence
    assert "not in your library" in body                  # unmatched gets a plain-language label
    # Success replaces the whole import block (form included), not just the note under the button.
    assert resp.headers.get("HX-Retarget") == "#takeout-import-block"
    assert rec_worker.calls == 1
    assert store.get_setting("takeout_imported_at") is not None

    # Re-posting the same file is idempotent: nothing new added, worker not re-triggered.
    resp2 = client.post("/import/takeout",
                        files={"file": ("watch-history.json", payload, "application/json")})
    assert resp2.status_code == 200
    assert rec_worker.calls == 1


def test_genuinely_unreadable_upload_gets_json_hint():
    # Neither JSON nor HTML (doesn't start with "<" and isn't valid JSON): the dispatcher can't
    # place it at all, so this is the one case that still raises TakeoutFormatError.
    client, store, rec_worker = _client()
    resp = client.post("/import/takeout",
                       files={"file": ("watch-history.txt", b"not json and not html",
                                       "text/plain")})
    assert resp.status_code == 200
    assert "JSON" in resp.text
    # Errors must NOT retarget: the note lands under the form, which survives for a retry.
    assert "HX-Retarget" not in resp.headers
    assert rec_worker.calls == 0
    assert store.get_setting("takeout_imported_at") is None


def _html_row(title, video_id, artist, iso_date_text):
    return (
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
        '<div class="header-cell mdl-cell mdl-cell--12-col">'
        '<p class="mdl-typography--title">YouTube Music<br></p></div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
        f'Watched\xa0<a href="https://music.youtube.com/watch?v={video_id}">{title}</a><br>'
        f'<a href="https://www.youtube.com/channel/UCxxx">{artist} - Topic</a><br>'
        f'{iso_date_text}<br></div></div></div>'
    )


def test_html_export_now_parses_and_reports_unreadable_dates():
    # Owner decision (#61 option B): the default HTML export is parsed too, honestly reporting
    # any entries whose date text didn't parse rather than silently dropping them.
    client, store, rec_worker = _client()
    doc = ('<html><body><div class="mdl-grid">' +
           _html_row("Suave", "vidA", "Danny Wabbit", "Jan 1, 2024, 10:00:00 AM UTC") +
           _html_row("Ghost Song", "NOVID9", "Nobody", "not a real date at all") +
           '</div></body></html>')
    resp = client.post("/import/takeout",
                       files={"file": ("watch-history.html", doc.encode(), "text/html")})
    assert resp.status_code == 200
    body = resp.text
    assert "Import complete" in body       # matched == 1 (Suave, in the library)
    assert "Skipped 1 entry with an unreadable date" in body
    assert rec_worker.calls == 1
    assert store.get_setting("takeout_imported_at") is not None


def _unmatched_payload():
    # Three unmatched plays of the same artist so seeding clears the default min_plays=3 bar.
    entries = [_entry("Ghost Song", "Nobody", f"NOVID{i}", f"2024-01-0{i}T10:00:00Z")
              for i in (1, 2, 3)]
    return json.dumps(entries).encode()


def test_seeding_is_automatic_above_threshold():
    # Owner decision (2026-07-03): no opt-in checkbox; unmatched artists clearing the min-plays
    # gate always seed discovery.
    client, store, rec_worker = _client()
    resp = client.post("/import/takeout",
                       files={"file": ("watch-history.json", _unmatched_payload(), "application/json")})
    assert resp.status_code == 200
    assert "Seeded" in resp.text
    pool = store.get_discovered_artists()
    assert len(pool) == 1
    assert pool[0]["artist"] == "Nobody"


def test_below_threshold_does_not_seed():
    client, store, rec_worker = _client()
    entries = [_entry("Ghost Song", "Nobody", f"NOVID{i}", f"2024-01-0{i}T10:00:00Z")
               for i in (1, 2)]                          # only 2 plays: under min_plays=3
    resp = client.post("/import/takeout",
                       files={"file": ("watch-history.json", json.dumps(entries).encode(),
                                       "application/json")})
    assert resp.status_code == 200
    assert "Seeded" not in resp.text
    assert store.get_discovered_artists() == []


def test_zero_match_import_snoozes_the_nag():
    # A zero-match import (typically pre-sync) must not re-nag on the next Home render: the route
    # stamps the 90-day snooze instead of the terminal takeout_imported_at.
    client, store, rec_worker = _client()
    entries = [_entry("Ghost Song", "Nobody", "NOVID1", "2024-01-01T10:00:00Z")]
    resp = client.post("/import/takeout",
                       files={"file": ("watch-history.json", json.dumps(entries).encode(),
                                       "application/json")})
    assert resp.status_code == 200
    assert store.get_setting("takeout_imported_at") is None
    assert store.get_setting("takeout_nag_dismissed_at") == "1700000000.0"
