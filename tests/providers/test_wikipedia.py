from yt_playlist.providers import wikipedia
from yt_playlist import egress


def _stub_json(monkeypatch, by_url):
    def fake(url):
        for frag, payload in by_url.items():
            if frag in url:
                return payload
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(wikipedia, "_get_json", fake)


def test_fetch_summary_standard_page(monkeypatch):
    _stub_json(monkeypatch, {
        "list=search": {"query": {"search": [{"title": "Khruangbin"}]}},
        "page/summary": {
            "type": "standard", "title": "Khruangbin",
            "extract": "Khruangbin is an American musical trio.",
            "thumbnail": {"source": "http://img/k.jpg"},
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Khruangbin"}},
        },
    })
    out = wikipedia.fetch_summary("artist", "Khruangbin")
    assert out == {
        "display": "Khruangbin", "title": "Khruangbin",
        "extract": "Khruangbin is an American musical trio.",
        "thumbnail": "http://img/k.jpg",
        "url": "https://en.wikipedia.org/wiki/Khruangbin",
    }


def test_fetch_summary_disambiguation_is_miss(monkeypatch):
    _stub_json(monkeypatch, {
        "list=search": {"query": {"search": [{"title": "Mercury"}]}},
        "page/summary": {"type": "disambiguation", "title": "Mercury",
                         "extract": "Mercury may refer to..."},
    })
    assert wikipedia.fetch_summary("artist", "Mercury") is None


def test_fetch_summary_empty_extract_is_miss(monkeypatch):
    _stub_json(monkeypatch, {
        "list=search": {"query": {"search": [{"title": "Whatever"}]}},
        "page/summary": {"type": "standard", "title": "Whatever", "extract": "  "},
    })
    assert wikipedia.fetch_summary("genre", "whatever") is None


def test_fetch_summary_rejects_unrelated_page(monkeypatch):
    # The real bug: searching an obscure artist returned a confidently-wrong album page whose
    # extract never mentions the artist. The relevance guard must reject it (-> miss).
    _stub_json(monkeypatch, {
        "list=search": {"query": {"search": [{"title": "The Courtauld Talks"}]}},
        "page/summary": {"type": "standard", "title": "The Courtauld Talks",
                         "extract": "The Courtauld Talks is a live album by Killing Joke."},
    })
    assert wikipedia.fetch_summary("artist", "Martin Schulte") is None


def test_fetch_summary_scans_past_a_bad_first_hit(monkeypatch):
    # First candidate is unrelated; the second is the right page and is accepted.
    def fake(url):
        if "list=search" in url:
            return {"query": {"search": [{"title": "Some Album"}, {"title": "Khruangbin"}]}}
        if "Some_Album" in url:
            return {"type": "standard", "title": "Some Album", "extract": "An album by someone."}
        return {"type": "standard", "title": "Khruangbin",
                "extract": "Khruangbin is an American musical trio.",
                "content_urls": {"desktop": {"page": "http://w/K"}}}
    monkeypatch.setattr(wikipedia, "_get_json", fake)
    out = wikipedia.fetch_summary("artist", "Khruangbin")
    assert out and out["title"] == "Khruangbin"


def test_fetch_summary_no_search_results_is_miss(monkeypatch):
    _stub_json(monkeypatch, {"list=search": {"query": {"search": []}}})
    assert wikipedia.fetch_summary("artist", "Nobody At All") is None


def test_fetch_summary_network_error_is_miss(monkeypatch):
    def boom(url):
        raise OSError("network down")
    monkeypatch.setattr(wikipedia, "_get_json", boom)
    assert wikipedia.fetch_summary("artist", "Khruangbin") is None


def test_wikipedia_host_is_allowed():
    assert egress.host_allowed("en.wikipedia.org")
