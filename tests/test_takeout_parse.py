"""#61 Takeout watch-history parser: defensive, music-only, oldest-first, real timestamps."""
import json

import pytest

from yt_playlist.library.takeout import (TakeoutFormatError, load_watch_history,
                                         parse_watch_history, parse_watch_history_html)


def _entry(title="Watched Suave", artist="Danny Wabbit - Topic",
           url="https://music.youtube.com/watch?v=Kzjh-EzG5gI",
           time="2024-03-01T12:00:00.000Z", header="YouTube Music", **over):
    e = {"header": header, "title": title, "titleUrl": url,
         "subtitles": [{"name": artist}], "time": time}
    e.update(over)
    return e


def test_parses_music_entry_with_video_id_and_epoch_ts():
    (p,) = parse_watch_history(json.dumps([_entry()]))
    assert p["video_id"] == "Kzjh-EzG5gI"
    assert p["title"] == "Suave" and p["artist"] == "Danny Wabbit"
    assert p["ts"] == 1709294400.0                       # 2024-03-01T12:00:00Z


def test_skips_non_music_and_malformed_rows():
    rows = [_entry(),
            _entry(header="YouTube", url="https://www.youtube.com/watch?v=abc"),  # plain YT: skipped
            {"title": "Watched a video that has been removed"},                    # no url/time: skipped
            "not even a dict"]
    out = parse_watch_history(json.dumps(rows))
    assert len(out) == 1


def test_music_url_without_music_header_is_kept():
    (p,) = parse_watch_history(json.dumps([_entry(header="YouTube")]))
    assert p["video_id"] == "Kzjh-EzG5gI"


def test_missing_subtitles_and_watched_prefix_variants():
    (p,) = parse_watch_history(json.dumps([_entry(title="Suave", subtitles=[])]))
    assert p["title"] == "Suave" and p["artist"] == ""


def test_sorted_oldest_first():
    rows = [_entry(time="2024-03-02T00:00:00Z"), _entry(time="2024-03-01T00:00:00Z")]
    out = parse_watch_history(json.dumps(rows))
    assert out[0]["ts"] < out[1]["ts"]


def test_non_json_raises_format_error_for_html_hint():
    with pytest.raises(TakeoutFormatError):
        parse_watch_history("<html><body>Takeout</body></html>")
    with pytest.raises(TakeoutFormatError):
        parse_watch_history(json.dumps({"not": "a list"}))


def _zip_bytes(members):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_maybe_unzip_extracts_named_watch_history():
    from yt_playlist.library.takeout import maybe_unzip
    inner = json.dumps([_entry()]).encode()
    raw = _zip_bytes({"Takeout/YouTube and YouTube Music/history/watch-history.json": inner,
                      "Takeout/YouTube and YouTube Music/history/search-history.json": b"[]" * 4000})
    assert maybe_unzip(raw) == inner


def test_maybe_unzip_locale_fallback_skips_search_history():
    # Localized exports rename the file; the fallback picks the largest non-search .json even when
    # search-history is bigger overall would be wrong, so search is excluded from candidates.
    from yt_playlist.library.takeout import maybe_unzip
    inner = json.dumps([_entry()]).encode()
    raw = _zip_bytes({"Takeout/YouTube/verlauf/Wiedergabeverlauf.json": inner,
                      "Takeout/YouTube/verlauf/search-history.json": inner + b" " * 10000})
    assert maybe_unzip(raw) == inner


def test_maybe_unzip_html_only_zip_gets_json_hint():
    from yt_playlist.library.takeout import TakeoutFormatError, maybe_unzip
    raw = _zip_bytes({"Takeout/YouTube and YouTube Music/history/watch-history.html": b"<html/>"})
    with pytest.raises(TakeoutFormatError):
        maybe_unzip(raw)


def test_maybe_unzip_passthrough_for_plain_json():
    from yt_playlist.library.takeout import maybe_unzip
    plain = json.dumps([_entry()])
    assert maybe_unzip(plain) == plain


# --- HTML export parser (#61 option B) -------------------------------------------------------
#
# Fixtures mirror the real Takeout markup shape observed in a live export: a non-breaking space
# (\xa0) between "Watched" and the title link (not a plain space), and a trailing <br> after the
# date line inside the content-cell (so naive split-by-<br> yields a spurious empty trailing
# segment -- the parser must not treat that as the date).

def _html_row(title="Suave", video_id="Kzjh-EzG5gI", artist="Danny Wabbit - Topic",
              date="Jul 3, 2026, 12:41:03 PM PDT", product="YouTube Music",
              host="music.youtube.com", artist_href="https://www.youtube.com/channel/UCxxx"):
    watch_href = f"https://{host}/watch?v={video_id}" if video_id else ""
    title_link = f'<a href="{watch_href}">{title}</a>' if title or video_id else ""
    return (
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
        '<div class="mdl-grid">'
        f'<div class="header-cell mdl-cell mdl-cell--12-col">'
        f'<p class="mdl-typography--title">{product}<br></p></div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
        f'Watched\xa0{title_link}<br>'
        f'<a href="{artist_href}">{artist}</a><br>'
        f'{date}<br>'
        '</div>'
        '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">'
        '<b>Products:</b><br>&emsp;YouTube<br></div>'
        '</div></div>'
    )


def _html_doc(*rows):
    return ('<html><head><title>Watch History</title></head><body>'
            '<div class="mdl-grid">' + "".join(rows) + '</div></body></html>')


def test_html_parses_music_entry_with_correct_epoch():
    # Hand-verified: "Jul 3, 2026, 12:41:03 PM PDT" (PDT = UTC-7) -> wall time 12:41:03 + 7h =
    # 19:41:03 UTC on the same day. 2024-01-01T00:00:00Z is the well-known constant 1704067200;
    # 2024 is a leap year (366 days) and 2025 is not (365 days), so 2026-01-01T00:00:00Z =
    # 1704067200 + (366+365)*86400 = 1704067200 + 63,158,400 = 1,767,225,600. Jan..Jun 2026
    # (2026 not a leap year) totals 31+28+31+30+31+30 = 181 days, so 2026-07-01T00:00:00Z is
    # +181d = 1,767,225,600 + 15,638,400 = 1,782,864,000; 2026-07-03T00:00:00Z adds 2 more days
    # (+172,800) = 1,783,036,800. Adding the time-of-day 19:41:03 (68,400 + 2,460 + 3 = 70,863)
    # gives 1,783,036,800 + 70,863 = 1,783,107,663.
    doc = _html_doc(_html_row())
    rows, unparsed = parse_watch_history_html(doc)
    assert unparsed == 0
    (p,) = rows
    assert p["video_id"] == "Kzjh-EzG5gI"
    assert p["title"] == "Suave"
    assert p["artist"] == "Danny Wabbit"
    assert p["ts"] == 1783107663.0


def test_html_narrow_nbsp_before_ampm_is_handled():
    # U+202F (narrow no-break space) is what Takeout actually emits before AM/PM; same expected
    # epoch as the plain-space case above.
    doc = _html_doc(_html_row(date="Jul 3, 2026, 12:41:03 PM PDT"))
    rows, unparsed = parse_watch_history_html(doc)
    assert unparsed == 0
    (p,) = rows
    assert p["ts"] == 1783107663.0


def test_html_non_music_row_is_skipped_not_counted():
    doc = _html_doc(_html_row(product="YouTube", host="www.youtube.com"))
    rows, unparsed = parse_watch_history_html(doc)
    assert rows == []
    assert unparsed == 0


def test_html_unparseable_timezone_is_counted_and_skipped():
    doc = _html_doc(_html_row(date="Jul 3, 2026, 12:41:03 PM IST"))   # IST not in the supported map
    rows, unparsed = parse_watch_history_html(doc)
    assert rows == []
    assert unparsed == 1


def test_html_removed_video_skipped_uncounted():
    # A removed video has no title link at all; must be skipped silently, not counted as an
    # unparsed date (it never even reaches date parsing).
    row = (
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
        '<div class="header-cell mdl-cell mdl-cell--12-col">'
        '<p class="mdl-typography--title">YouTube Music<br></p></div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
        'Watched a video that has been removed<br>'
        'Jul 3, 2026, 12:41:03 PM PDT<br></div></div></div>'
    )
    doc = _html_doc(row)
    rows, unparsed = parse_watch_history_html(doc)
    assert rows == []
    assert unparsed == 0


def test_html_mixed_music_and_non_music_rows():
    doc = _html_doc(
        _html_row(title="Suave", video_id="vid1"),
        _html_row(title="News clip", video_id="vid2", product="YouTube", host="www.youtube.com"),
        _html_row(title="Other Song", video_id="vid3", date="Jul 4, 2026, 1:00:00 AM PDT"),
    )
    rows, unparsed = parse_watch_history_html(doc)
    assert unparsed == 0
    assert [r["video_id"] for r in rows] == ["vid1", "vid3"]   # oldest first; vid2 (non-music) dropped


def test_load_watch_history_json_path_unchanged_with_zero_unparsed():
    rows, unparsed = load_watch_history(json.dumps([_entry()]))
    assert len(rows) == 1
    assert unparsed == 0


def test_load_watch_history_raw_html_routes_to_html_parser():
    doc = _html_doc(_html_row())
    rows, unparsed = load_watch_history(doc)
    assert len(rows) == 1
    assert unparsed == 0
    assert rows[0]["video_id"] == "Kzjh-EzG5gI"


def test_load_watch_history_zip_with_html_only_extracts_and_parses():
    doc = _html_doc(_html_row())
    raw = _zip_bytes({
        "Takeout/YouTube and YouTube Music/history/watch-history.html": doc.encode(),
        "Takeout/YouTube and YouTube Music/history/search-history.html": b"<html></html>" * 1000,
    })
    rows, unparsed = load_watch_history(raw)
    assert len(rows) == 1
    assert unparsed == 0
    assert rows[0]["video_id"] == "Kzjh-EzG5gI"


def test_load_watch_history_zip_prefers_json_member():
    inner = json.dumps([_entry()]).encode()
    doc = _html_doc(_html_row(video_id="wontbeused"))
    raw = _zip_bytes({
        "Takeout/YouTube and YouTube Music/history/watch-history.json": inner,
        "Takeout/YouTube and YouTube Music/history/watch-history.html": doc.encode(),
    })
    rows, unparsed = load_watch_history(raw)
    assert unparsed == 0
    assert rows[0]["video_id"] == "Kzjh-EzG5gI"


def test_load_watch_history_zip_with_neither_format_raises():
    # No .json, no .html member at all: neither format is recoverable.
    raw = _zip_bytes({"Takeout/YouTube and YouTube Music/history/other.txt": b"nothing useful"})
    with pytest.raises(TakeoutFormatError):
        load_watch_history(raw)


def test_load_watch_history_garbage_raises_format_error():
    with pytest.raises(TakeoutFormatError):
        load_watch_history("not json and not html, just garbage")
