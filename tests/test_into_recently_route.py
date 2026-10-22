from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.rec import into_recently
from yt_playlist.providers import wikipedia
from tests.conftest import FakeClient


def _client(store):
    return TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")


def test_card_renders_from_fetched_summary(store, monkeypatch):
    monkeypatch.setattr(into_recently, "subjects_for_epoch",
                        lambda s, now, epoch=0: [{"kind": "genre", "subject": "genre:shoegaze",
                                                  "display": "shoegaze"}])
    monkeypatch.setattr(wikipedia, "fetch_summary",
                        lambda kind, display: {"display": "shoegaze", "title": "Shoegaze",
                                               "extract": "Shoegaze is a subgenre of indie rock.",
                                               "thumbnail": "http://img", "url": "http://wiki/Shoegaze"})
    html = _client(store).get("/home/into-recently").text
    assert "You're into" in html
    assert "shoegaze" in html
    assert "Shoegaze is a subgenre" in html
    assert "http://wiki/Shoegaze" in html


def test_card_caches_and_does_not_refetch(store, monkeypatch):
    monkeypatch.setattr(into_recently, "subjects_for_epoch",
                        lambda s, now, epoch=0: [{"kind": "artist", "subject": "artist:khruangbin",
                                                  "display": "Khruangbin"}])
    calls = {"n": 0}

    def fetch(kind, display):
        calls["n"] += 1
        return {"display": display, "title": "Khruangbin", "extract": "A trio.",
                "thumbnail": None, "url": "http://wiki/K"}
    monkeypatch.setattr(wikipedia, "fetch_summary", fetch)
    c = _client(store)
    c.get("/home/into-recently")
    c.get("/home/into-recently")
    assert calls["n"] == 1


def test_empty_when_no_subject(store, monkeypatch):
    monkeypatch.setattr(into_recently, "subjects_for_epoch", lambda s, now, epoch=0: [])
    r = _client(store).get("/home/into-recently")
    assert r.status_code == 200
    assert "You're into" not in r.text


def test_empty_when_wikipedia_misses(store, monkeypatch):
    monkeypatch.setattr(into_recently, "subjects_for_epoch",
                        lambda s, now, epoch=0: [{"kind": "artist", "subject": "artist:nobody",
                                                  "display": "Nobody"}])
    monkeypatch.setattr(wikipedia, "fetch_summary", lambda kind, display: None)
    r = _client(store).get("/home/into-recently")
    assert "You're into" not in r.text


def test_falls_back_to_next_subject_when_first_fails(store, monkeypatch):
    # The reported bug: a rotation landed on a subject with no Wikipedia page and the card blanked.
    # Now the route walks past the dead subject to the next one that resolves.
    monkeypatch.setattr(into_recently, "subjects_for_epoch",
                        lambda s, now, epoch=0: [
                            {"kind": "artist", "subject": "artist:nopage", "display": "NoPage"},
                            {"kind": "genre", "subject": "genre:shoegaze", "display": "shoegaze"}])

    def fetch(kind, display):
        if display == "shoegaze":
            return {"display": "shoegaze", "title": "Shoegaze", "extract": "A genre.",
                    "thumbnail": "http://img", "url": "http://wiki/Shoegaze"}
        return None
    monkeypatch.setattr(wikipedia, "fetch_summary", fetch)
    html = _client(store).get("/home/into-recently").text
    assert "shoegaze" in html and "A genre." in html    # fell through the failed first subject


def test_artist_card_uses_local_thumbnail_color_and_cta(store, monkeypatch):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store,'k|khruangbin','T','Khruangbin',track_id=1,thumbnail='http://local/k.jpg')
    store.conn.commit()
    monkeypatch.setattr(into_recently, "subjects_for_epoch",
                        lambda s, now, epoch=0: [{"kind": "artist", "subject": "artist:Khruangbin",
                                                  "display": "Khruangbin"}])
    monkeypatch.setattr(wikipedia, "fetch_summary",
                        lambda kind, display: {"display": "Khruangbin", "title": "Khruangbin",
                                               "extract": "A trio.", "thumbnail": None, "url": "http://wiki/K"})
    html = _client(store).get("/home/into-recently").text
    assert "http://local/k.jpg" in html                 # fell back to the local thumbnail
    assert "wiki-subject" in html and "--subj" in html  # coloured/glowing heading
    assert "/clusters?seed=Khruangbin" in html and "depth=2" in html  # explore CTA deep-link
    assert "label=Khruangbin" in html                   # names the new canvas (switcher badge)


def test_card_hidden_when_no_thumbnail_anywhere(store, monkeypatch):
    monkeypatch.setattr(into_recently, "subjects_for_epoch",
                        lambda s, now, epoch=0: [{"kind": "artist", "subject": "artist:Ghost",
                                                  "display": "Ghost"}])
    monkeypatch.setattr(wikipedia, "fetch_summary",
                        lambda kind, display: {"display": "Ghost", "title": "Ghost",
                                               "extract": "Spooky.", "thumbnail": None, "url": "u"})
    assert "You're into" not in _client(store).get("/home/into-recently").text


def test_refresh_failure_preserves_stale_card_and_retries(store, monkeypatch):
    subj = {"kind": "artist", "subject": "artist:Khruangbin", "display": "Khruangbin"}
    monkeypatch.setattr(into_recently, "subjects_for_epoch", lambda *a, **kw: [subj])
    store.wiki.put(subj["subject"], "artist", "Khruangbin",
                   {"extract": "A trio.", "thumbnail": "http://img", "url": "http://wiki"}, 0)
    calls = []

    def failed(*args):
        calls.append(args)
        raise wikipedia.LookupUnavailable("offline")

    monkeypatch.setattr(wikipedia, "fetch_summary", failed)
    client = _client(store)
    assert "A trio." in client.get("/home/into-recently").text
    assert "A trio." in client.get("/home/into-recently").text
    assert len(calls) == 1
    assert store.wiki.get(subj["subject"])["fetched_at"] == 0
    deadline = store.wiki._retry_after[subj["subject"]]
    assert not store.wiki.should_refresh(subj["subject"], store.wiki.get(subj["subject"]), deadline - 1)
    assert store.wiki.should_refresh(subj["subject"], store.wiki.get(subj["subject"]), deadline)
    into_recently.refresh_summary(store, subj, deadline,
                                  lambda *a: {"extract": "Recovered", "thumbnail": "http://new"})
    assert store.wiki.get(subj["subject"])["extract"] == "Recovered"
    assert subj["subject"] not in store.wiki._retry_after
