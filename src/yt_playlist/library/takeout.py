"""#61 Google Takeout watch-history import: parse the full exported YouTube Music history and
backfill it into the play stores, curing the thin forward-only history the live API allows.

Everything is processed locally; nothing leaves the machine. The parser is deliberately defensive:
Takeout format drifts, entries are frequently partial (removed videos, missing subtitles), and the
same file mixes YouTube and YouTube Music rows. Bad rows are skipped, never fatal.

Takeout's DEFAULT export format is HTML, not JSON (most users' first upload is
watch-history.html), so both are parsed (`load_watch_history` dispatches to whichever the input
actually is). JSON stays the recommended path -- it carries real ISO timestamps for every row --
but the HTML export is no longer a dead end: it is parsed English-locale-first, with honest
reporting (never silently lossy) of any entries whose date text could not be parsed."""
import datetime
import io
import json
import re
import zipfile
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from yt_playlist.library.live_plays import resolve_identity
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util.matching import identity_key


class TakeoutFormatError(ValueError):
    """Neither Takeout watch-history format (JSON list or HTML export) could be recognized in the
    input, e.g. a zip with no history file inside it at all, or plain garbage. The UI maps this to
    a "we recommend JSON" hint (the HTML export itself is now parsed by load_watch_history, so
    this is reserved for genuinely unusable input)."""


_WATCHED_PREFIX = "Watched "
_TOPIC_SUFFIX = " - Topic"
_ZIP_MAGIC = b"PK\x03\x04"
_ZIP_MEMBER_CAP = 256 * 1024 * 1024   # a heavy multi-year history is ~100MB of JSON; 256MB is generous


def maybe_unzip(raw) -> bytes | str:
    """#61 Accept the Takeout .zip directly: extract the watch-history JSON from it, else pass the
    input through untouched. Member choice is locale-defensive: prefer a member literally named
    watch-history.json anywhere in the archive; otherwise, among .json members, take the LARGEST
    (the watch history dwarfs every other JSON Takeout ships, and directory names are localized
    while sizes are not). Raises TakeoutFormatError for a zip with no .json member or one whose
    candidate exceeds the size cap (zip-bomb guard)."""
    data = raw.encode() if isinstance(raw, str) else raw
    if not data.startswith(_ZIP_MAGIC):
        return raw
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        members = [m for m in zf.infolist() if not m.is_dir() and m.filename.lower().endswith(".json")]
    except zipfile.BadZipFile as e:
        raise TakeoutFormatError("that zip could not be read; re-download the export") from e
    if not members:
        raise TakeoutFormatError("no JSON found inside the zip (was history exported as JSON, not HTML?)")
    named = [m for m in members if m.filename.rsplit("/", 1)[-1] == "watch-history.json"]
    # Locale fallback: prefer the exact name; else the largest .json that is not the search history
    # (a real export ships search-history.json alongside, and a search-heavy account could make it
    # the biggest member).
    candidates = [m for m in members if "search" not in m.filename.rsplit("/", 1)[-1].lower()] or members
    pick = named[0] if named else max(candidates, key=lambda m: m.file_size)
    if pick.file_size > _ZIP_MEMBER_CAP:
        raise TakeoutFormatError("the history file inside the zip is unreasonably large")
    with zf.open(pick) as f:
        return f.read()


def _unzip_html_member(data: bytes) -> bytes:
    """#61 Sibling of maybe_unzip's json member-picking logic, for the HTML export: prefer a
    member literally named watch-history.html, else the largest non-search .html member. Raises
    TakeoutFormatError when the zip has no .html member either (caller already tried .json)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        members = [m for m in zf.infolist() if not m.is_dir() and m.filename.lower().endswith(".html")]
    except zipfile.BadZipFile as e:
        raise TakeoutFormatError("that zip could not be read; re-download the export") from e
    if not members:
        raise TakeoutFormatError("no JSON or HTML watch history found inside the zip")
    named = [m for m in members if m.filename.rsplit("/", 1)[-1] == "watch-history.html"]
    candidates = [m for m in members if "search" not in m.filename.rsplit("/", 1)[-1].lower()] or members
    pick = named[0] if named else max(candidates, key=lambda m: m.file_size)
    if pick.file_size > _ZIP_MEMBER_CAP:
        raise TakeoutFormatError("the history file inside the zip is unreasonably large")
    with zf.open(pick) as f:
        return f.read()


def load_watch_history(raw) -> tuple[list, int]:
    """#61 Option B dispatcher: JSON stays the recommended, fully-timestamped path; the default
    Takeout export (HTML) is now parsed too, honestly. Zip -> prefer the .json member (existing
    maybe_unzip logic); if that isn't there, fall back to a .html member (see
    _unzip_html_member). Raw HTML text (starts with '<') -> the HTML parser. Anything else -> the
    JSON parser (unparsed_dates is always 0 there; every JSON entry carries a real ISO
    timestamp). Returns (rows, unparsed_dates)."""
    data = raw.encode() if isinstance(raw, str) else raw
    if data.startswith(_ZIP_MAGIC):
        try:
            return parse_watch_history(maybe_unzip(data)), 0
        except TakeoutFormatError:
            return parse_watch_history_html(_unzip_html_member(data))
    if data.lstrip()[:1] == b"<":
        return parse_watch_history_html(raw)
    return parse_watch_history(raw), 0


def _parse_time(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _video_id(url):
    try:
        q = parse_qs(urlsplit(url).query)
        return (q.get("v") or [None])[0]
    except (ValueError, TypeError, AttributeError):
        return None


def parse_watch_history(raw) -> list:
    """Takeout watch-history JSON -> [{'video_id','title','artist','ts'}], oldest first. Music rows
    only (YouTube Music header, or a music.youtube.com URL); rows without a parseable time are
    skipped. Raises TakeoutFormatError when the input is not the JSON export at all."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise TakeoutFormatError("not JSON (did you export the HTML variant?)") from e
    if not isinstance(data, list):
        raise TakeoutFormatError("unexpected JSON shape (expected the watch-history list)")
    out = []
    for e in data:
        if not isinstance(e, dict):
            continue
        url = e.get("titleUrl") or ""
        if e.get("header") != "YouTube Music" and "music.youtube.com" not in url:
            continue
        ts = _parse_time(e.get("time"))
        if ts is None:
            continue
        title = e.get("title") or ""
        if title.startswith(_WATCHED_PREFIX):
            title = title[len(_WATCHED_PREFIX):]
        title = title.strip()
        if not title:
            continue
        subs = e.get("subtitles") or []
        artist = (subs[0].get("name") or "") if subs and isinstance(subs[0], dict) else ""
        if artist.endswith(_TOPIC_SUFFIX):
            artist = artist[: -len(_TOPIC_SUFFIX)]
        out.append({"video_id": _video_id(url), "title": title,
                    "artist": artist.strip(), "ts": ts})
    out.sort(key=lambda p: p["ts"])
    return out


# --- HTML export parser (#61 option B) -----------------------------------------------------
#
# Takeout's DEFAULT export format is HTML, not JSON, so most users' first upload is this. Parsed
# with the stdlib html.parser only (no bs4/lxml dependency). Structure (observed live, and
# defended against drift): each history entry is a `div.outer-cell`; inside it a sibling
# `div.header-cell` names the product ("YouTube Music" vs plain "YouTube"), and the first
# `div.content-cell` holds `Watched <a href=".../watch?v=ID">Title</a><br><a>Artist - Topic</a>
# <br>DATE`. A real export was observed to emit a trailing <br> after DATE (a naive split-by-<br>
# would then treat an empty string as "the date") and a non-breaking space (not a plain space)
# between "Watched" and the title link -- both handled below by ignoring whitespace-only text
# tokens entirely rather than depending on exact spacing.

_VOID_TAGS = frozenset({"br", "img", "hr", "input", "meta", "link", "area", "base",
                         "col", "embed", "source", "track", "wbr"})

_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

# US-locale abbreviations only (owner decision: English-locale-first). Anything else (including
# other countries' PST-alikes, or a bare UTC offset spelled out differently) is honestly reported
# as unparseable rather than guessed at.
_TZ_OFFSETS = {"PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST": -6, "CDT": -5,
               "EST": -5, "EDT": -4, "UTC": 0, "GMT": 0}

_HTML_DATE_RE = re.compile(
    r"^(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4}),\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})\s*(?P<ampm>AM|PM)\s+(?P<tz>[A-Za-z]{2,5})$")


class _TreeBuilder(HTMLParser):
    """Minimal generic HTML -> tree builder. Void elements (br, img, ...) never get pushed onto
    the open-element stack (Takeout's HTML omits their closing tags, which would otherwise nest
    everything that follows inside them). handle_endtag searches upward for a matching open tag
    and truncates the stack there, so stray or mismatched close tags cannot corrupt the tree."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": None, "attrs": {}, "children": []}
        self._stack = [self.root]

    def _open(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self._stack[-1]["children"].append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._open(tag, attrs)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1]["children"].append(data)


def _has_class(node, cls):
    return cls in (node.get("attrs", {}).get("class") or "").split()


def _find_first(node, pred):
    for child in node.get("children", []):
        if isinstance(child, dict):
            if pred(child):
                return child
            found = _find_first(child, pred)
            if found is not None:
                return found
    return None


def _find_all_top(node, pred, out=None):
    """Like _find_first but collects every match, without descending into a matched node's own
    children (history entries -- outer-cells -- do not nest, so this also guards against a
    pathological doubled-count if they ever did)."""
    if out is None:
        out = []
    for child in node.get("children", []):
        if isinstance(child, dict):
            if pred(child):
                out.append(child)
                continue
            _find_all_top(child, pred, out)
    return out


def _flatten_text(node):
    parts = []

    def walk(n):
        for c in n.get("children", []):
            if isinstance(c, str):
                parts.append(c)
            else:
                walk(c)

    walk(node)
    return "".join(parts)


def _content_segments(node):
    """The first content-cell's children, split on <br> into segments, dropping whitespace-only
    text tokens and any resulting empty segments (the real export's trailing <br> after DATE, and
    HTML-source indentation, would otherwise produce spurious empty segments). Each segment is a
    list of ("text", str) / ("link", {"href","text"}) tokens, in document order."""
    segments, cur = [], []
    for c in node.get("children", []):
        if isinstance(c, str):
            if c.strip():
                cur.append(("text", c))
            continue
        tag = c.get("tag")
        if tag == "br":
            if cur:
                segments.append(cur)
            cur = []
        elif tag == "a":
            text = _flatten_text(c).strip()
            if text:
                cur.append(("link", {"href": c.get("attrs", {}).get("href") or "", "text": text}))
        else:
            text = _flatten_text(c).strip()
            if text:
                cur.append(("text", text))
    if cur:
        segments.append(cur)
    return segments


def _first_link(segment):
    for kind, val in segment:
        if kind == "link":
            return val
    return None


def _segment_text(segment):
    return "".join(val["text"] if kind == "link" else val for kind, val in segment)


def _parse_html_date(s):
    """English-locale Takeout date text, e.g. 'Jul 3, 2026, 12:41:03 PM PDT'. BEWARE: Takeout
    puts a narrow no-break space (U+202F) before AM/PM, not a plain space; normalized away by the
    caller before this ever sees the string. Returns None (caller counts it as unparseable) for
    an unrecognized month, a timezone abbreviation outside the supported US set (see
    _TZ_OFFSETS), or a string that doesn't match the expected shape at all."""
    m = _HTML_DATE_RE.match(" ".join(s.split()))
    if not m:
        return None
    month = _MONTHS.get(m.group("mon").title())
    offset = _TZ_OFFSETS.get(m.group("tz").upper())
    if month is None or offset is None:
        return None
    hour = int(m.group("hour")) % 12
    if m.group("ampm").upper() == "PM":
        hour += 12
    try:
        dt = datetime.datetime(int(m.group("year")), month, int(m.group("day")), hour,
                                int(m.group("minute")), int(m.group("second")),
                                tzinfo=datetime.timezone(datetime.timedelta(hours=offset)))
    except ValueError:
        return None
    return dt.timestamp()


def parse_watch_history_html(raw) -> tuple[list, int]:
    """Takeout watch-history.html -> ([{'video_id','title','artist','ts'}], unparsed_dates),
    oldest first. Music rows only (header-cell says "YouTube Music", or the watch link is
    music.youtube.com); rows with no watch link or no title (a removed video) are skipped
    silently. Rows that are otherwise complete music rows but whose date text doesn't parse are
    skipped AND counted in unparsed_dates -- honest reporting, never silently lossy."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    text = text.replace(" ", " ")   # narrow no-break space Takeout puts before AM/PM
    builder = _TreeBuilder()
    builder.feed(text)
    outer_cells = _find_all_top(builder.root,
                                 lambda n: n.get("tag") == "div" and _has_class(n, "outer-cell"))
    rows = []
    unparsed_dates = 0
    for cell in outer_cells:
        header = _find_first(cell, lambda n: n.get("tag") == "div" and _has_class(n, "header-cell"))
        content = _find_first(cell, lambda n: n.get("tag") == "div" and _has_class(n, "content-cell"))
        if content is None:
            continue
        segments = _content_segments(content)
        if not segments:
            continue
        title_link = _first_link(segments[0])
        if title_link is None or not title_link["text"]:
            continue   # no watch link or no title (removed video): skip silently, not a date failure
        href = title_link["href"]
        product_text = _flatten_text(header) if header is not None else ""
        is_music = "YouTube Music" in product_text or "music.youtube.com" in urlsplit(href).netloc.lower()
        if not is_music:
            continue
        artist = ""
        for seg in segments[1:-1]:
            link = _first_link(seg)
            if link is not None:
                artist = link["text"]
                break
        if artist.endswith(_TOPIC_SUFFIX):
            artist = artist[: -len(_TOPIC_SUFFIX)]
        date_text = _segment_text(segments[-1]) if len(segments) > 1 else ""
        ts = _parse_html_date(date_text)
        if ts is None:
            unparsed_dates += 1
            continue
        rows.append({"video_id": _video_id(href), "title": title_link["text"],
                    "artist": artist.strip(), "ts": ts})
    rows.sort(key=lambda p: p["ts"])
    return rows, unparsed_dates


def import_takeout(store, raw) -> dict:
    """#61 Parse + match + backfill. videoId matches are exact (library lookup); the fallback is the
    normalized title/artist identity_key, accepted only when that key is actually in the library
    (a play of a song you do not own belongs to discovery, not history). Both stores are written:
    the (track, day) model (charts, comfort, baskets) and play_events (real timestamps: wall-clock
    decay, layers, temporal eval). Idempotent end to end.

    Returns {"error": "no identity configured"} when no identity exists yet (mirrors live_plays'
    None-means-nothing-to-attribute-to semantics; there is nothing sensible to import into)."""
    ident = resolve_identity(store, "")
    if ident is None:
        return {"error": "no identity configured"}
    parsed, unparsed_dates = load_watch_history(raw)   # the .zip Takeout serves works as-is; JSON
                                                        # or the default HTML export, either way
    owned = set(RecDao(store).library_keys())
    matched_rows, unmatched = [], Counter()
    for p in parsed:
        key = store.identity_key_for_video(p["video_id"]) if p["video_id"] else None
        if key is None:
            cand = identity_key(p["title"], p["artist"])
            key = cand if cand in owned else None
        if key is None:
            unmatched[p["artist"] or "?"] += 1
            continue
        matched_rows.append((key, p["video_id"], p["ts"]))
    plays_added = store.import_plays(ident, [(k, ts) for k, _v, ts in matched_rows])
    events_added = store.import_play_events(ident, matched_rows)
    span_days = 0
    if parsed:
        span_days = int((parsed[-1]["ts"] - parsed[0]["ts"]) // 86400)
    return {"matched": len(matched_rows), "unmatched": sum(unmatched.values()),
            "plays_added": plays_added, "events_added": events_added,
            "span_days": span_days, "unmatched_artists": dict(unmatched),
            "unparsed_dates": unparsed_dates}


def seed_discovery_from_unmatched(store, unmatched_artists, now, min_plays=3) -> int:
    """#61 Task 5: opt-in seeding of the discovered-artists pool from Takeout plays that didn't
    match anything already in the library (`import_takeout`'s `unmatched_artists`). An artist with
    at least `min_plays` unmatched plays is upserted via the same `upsert_discovered_artist` API
    `run_discovery` uses for the taste-bridged pool (rec/discover.py), scored by play count.

    That reuse is deliberate, not a shortcut: `pick_discovered_artists` (rec/discover.py) only ever
    uses `score` as a relative sort key (`score * facet_weight`, descending, ties broken by
    least-recently-shown) -- never against an absolute threshold or in a formula mixed with other
    pools' scores -- so a play-count-derived score is a legitimate opaque rank here, not a
    fabricated taste score.

    `because`/`fits` are left empty (None -> stored as `[]`): unlike the taste-bridge pool, we have
    no bridge anchors or per-playlist fit to honestly report for a Takeout-derived artist, so the
    "because you play ..." / "fits your ..." lines in the New Artists tile simply don't render for
    these, rather than fabricating an explanation.

    Artists ALREADY in the discovered pool are skipped: upsert_discovered_artist overwrites every
    field on conflict, so re-seeding would clobber a taste-bridge entry's richer because/fits/
    thumbnail with our empty ones (final-review finding). Seeding only ever ADDS new names.

    Returns how many artists were seeded."""
    existing = {a.get("artist", "") for a in (store.get_discovered_artists() or [])}
    seeded = 0
    for artist, plays in unmatched_artists.items():
        if plays >= min_plays and artist not in existing:
            store.upsert_discovered_artist(artist, float(plays), None, None, None, now)
            seeded += 1
    return seeded
