"""Map MusicBrainz evidence to enrichment fields; vendor access belongs to its IAO."""
import logging
import re
from yt_playlist.core.activity import WorkClosing, WorkBudgetExceeded
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.integrations.musicbrainz import MusicBrainzIAO, TIMEOUT_S
from yt_playlist.util.matching import is_remix, normalize, recording_title
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers import descriptive_tags, genres

logger = logging.getLogger(__name__)

name = "musicbrainz"
manages_availability = True
_iao = MusicBrainzIAO()


def access(store=None):
    return _iao.context(store.enrichment_state if store is not None else None)


def available(store=None) -> bool:
    # Configuration availability is distinct from a temporary access cooldown.
    return _iao.configured


def tripped() -> bool:
    return not _iao.availability().ready(_iao.now())


def reset() -> None:
    """Batch boundaries do not reset vendor availability or retained responses."""


def probe(track, store=None) -> EnrichmentResult:
    """Look up metadata without applying it; the IAO can persist access cooldowns."""
    observations = []
    with access(store):
        genre, year, mbid, release_ids, isrcs = enrich_full(track["title"], track["artist"],
                                                        observations=observations)
    fields = {}
    if genre:
        fields["genre"] = genre
    if year:
        fields["year"] = year
    if mbid:
        fields["mb_recording_id"] = mbid
    if release_ids:
        fields["mb_release_ids"] = release_ids
    if isrcs:
        fields["isrc"] = isrcs
    return EnrichmentResult("musicbrainz", fields, tuple(observations))

def _get(path, params, *, timeout=TIMEOUT_S):
    return _iao.get(path, params, timeout=timeout)


def _lucene_escape(text):
    # neutralize the Lucene query syntax in user-supplied title/artist
    return "".join("\\" + c if c in '+-&|!(){}[]^"~*?:\\/' else c for c in (text or ""))


def tag_artists(tag, decade=None, limit=25, *, store=None):
    """Artists MusicBrainz files under a genre tag, optionally narrowed to those who RELEASED in a
    given decade. [] on error.

    Queries release-groups rather than artists when a decade is given: an artist's own date range is
    when they existed, not when they put anything out, and "80s new wave" means records from the 80s.
    MB has no popularity ranking, so this is a source of NAMES - the ordering that matters comes from
    whoever consumes them (Last.fm's listener counts, then YouTube's own per-artist track ranking)."""
    tag = (tag or "").strip()
    if not tag:
        return []
    query = f'tag:"{_lucene_escape(tag)}"'
    try:
        if decade:
            query += f" AND firstreleasedate:[{int(decade)} TO {int(decade) + 9}]"
            with access(store):
                res = _get("release-group", {"query": query, "fmt": "json", "limit": str(limit)})
            rows = res.get("release-groups") or []
            names = []
            for row in rows:
                credit = next((c for c in row.get('artist-credit') or [] if isinstance(c, dict)), {})
                names.append((credit.get('name') or (credit.get('artist') or {}).get('name') or '').strip())
        else:
            with access(store):
                res = _get("artist", {"query": query, "fmt": "json", "limit": str(limit)})
            names = [(a.get("name") or "").strip() for a in (res.get("artists") or [])]
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as e:  # noqa: BLE001 - optional candidate acquisition can defer
        logger.warning("MusicBrainz tag artists failed for %r: %s", tag, e)
        return []
    out = []
    for nm in names:
        if nm and nm not in out:
            out.append(nm)
    return out


def _years(dates):
    return [d[:4] for d in dates if d and len(d) >= 4 and d[:4].isdigit()]


def _earliest_year(recordings):
    """A song's original release year: the earliest recording-level first-release-date across ALL
    candidate recordings. The top search match is usually a remaster/compilation with a much later
    date. Fall back to release dates, then release-group dates."""
    frd = _years(r.get("first-release-date") for r in recordings)
    if frd:
        return min(frd)
    rel = _years(rel.get("date") for r in recordings for rel in (r.get("releases") or []))
    if rel:
        return min(rel)
    rg = _years((rel.get("release-group") or {}).get("first-release-date")
                for r in recordings for rel in (r.get("releases") or []))
    return min(rg) if rg else None


def _top_label(obj):
    """Vote order chooses the style; a documented child tag can refine its broad parent."""
    tags = []
    for field in ("genres", "tags"):
        items = sorted(obj.get(field) or [], key=lambda x: int(x.get("count") or 0), reverse=True)
        tags.extend(item.get('name') for item in items)
    return genres.pick_genre(tags)


def _artist_genre(mbid, *, observations=None, match_evidence=None):
    """Artist-level genre (MusicBrainz keeps most genre data on artists, not recordings).

    Re-enrichment shares fresh raw evidence: tagged responses live for a day, empty responses
    for an hour. It does not force another artist lookup for every recording. Worker
    batches share the IAO's bounded cache and do not reset operational availability.
    """
    if not mbid:
        return None
    try:
        art = _get(f"artist/{mbid}", {"inc": "genres+tags", "fmt": "json"})
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as e:  # noqa: BLE001
        logger.debug("MB artist lookup failed for %s: %s", mbid, e)
        return None
    _retain_tags(observations, art, 'artist', mbid, match_evidence)
    return _top_label(art)


def _retain_tags(observations, data, scope, mbid, match):
    tags = [{**tag, 'kind': field} for field in ('genres', 'tags') for tag in data.get(field) or []]
    descriptive_tags.retain(observations, scope=scope, entity_id=mbid, tags=tags,
        source_path=f'{scope}/{mbid}:genres+tags', match_evidence=match or {})


_PARENS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def _strip_parens(title):
    """Drop parenthetical/bracketed qualifiers, e.g. 'No Quarter (Remaster)' -> 'No Quarter'."""
    return _PARENS.sub("", title or "").strip()


def recording_mbid(title, artist, *, store=None):
    """Best-match MusicBrainz recording MBID for a track, or None. Used to key AcousticBrainz.
    Retries once with parenthetical qualifiers stripped, same as enrich_full()."""
    with access(store):
        recs = _recording_search(title, artist)
    return recs[0].get("id") if recs else None


def _search_recordings(title, artist):
    """Wide-net MB recording search (limit 25 so the original is in the pool, not just remasters
    near the top). Returns the recordings list, best match first, or [] on no match / error."""
    query = f'recording:"{_lucene_escape(title)}"'
    if artist:
        query += f' AND artist:"{_lucene_escape(artist)}"'
    try:
        res = _get("recording", {"query": query, "fmt": "json", "limit": "25"})
    except (IntegrationUnavailable, WorkClosing, WorkBudgetExceeded):
        raise  # A transient failure must not trigger the alternate search.
    except Exception as e:  # noqa: BLE001
        logger.debug("MB search failed for %r / %r: %s", title, artist, e)
        return []
    return res.get("recordings") or []


def _recording_search(title, artist):
    """One recording search with the parens-stripped retry (a remaster / live / remix suffix often
    blocks the match). The IAO shares successful responses across callers."""
    recs = [r for r in _search_recordings(title, artist) if _matches_recording(r, title, artist)]
    if not recs:
        stripped = _strip_parens(title)
        if stripped and stripped != title:
            recs = [r for r in _search_recordings(stripped, artist)
                    if _matches_recording(r, title, artist)]
    return recs


def _matches_recording(recording, title, artist):
    """Search ranking is not identity: verify the title and the credited artist(s).

    A collaboration can credit the requested artist second (e.g. MAARVN & Deco (BE)). Accept
    individual credits, credited names/aliases, or the combined credit without accepting another
    artist's recording of the same title. Keep meaningful title qualifiers even on the retry.
    """
    wanted_title, wanted_artist = recording_title(title), normalize(artist)
    if not wanted_title or not wanted_artist or recording_title(recording.get("title")) != wanted_title:
        return False
    names, joined = set(), []
    for credit in recording.get("artist-credit") or []:
        if isinstance(credit, str):
            joined.append(credit)
            continue
        credited_artist = credit.get("artist") or {}
        credited_name = credit.get("name") or credited_artist.get("name") or ""
        joined.append(credited_name + (credit.get("joinphrase") or ""))
        names.update(normalize(n) for n in (credited_name, credited_artist.get("name")))
        names.update(normalize(a.get("name")) for a in credited_artist.get("aliases") or [])
    names.add(normalize("".join(joined)))
    return wanted_artist in names


def _release_ids(recordings):
    """Candidate release MBIDs, best match first. Cover Art Archive is keyed by RELEASE (not
    recording), and these ride along in the search response we already made, so the fallback costs
    no extra MusicBrainz traffic. De-duplicated: the same release recurs across candidates."""
    ids = []
    for rec in recordings:
        for rel in (rec.get("releases") or []):
            rid = rel.get("id")
            if rid and rid not in ids:
                ids.append(rid)
    return ids


def enrich_full(title, artist, *, observations=None):
    """(genre, year, mb_recording_id, release_ids, isrcs) from one recording search.

    release_ids keys Cover Art Archive (which is keyed by release, not recording). It rides along
    here rather than in its own function so the cover fallback costs no extra search, and so this
    stays the single seam where MusicBrainz touches the network."""
    try:
        recs = _recording_search(title, artist)
    except IntegrationUnavailable:
        return None, None, None, [], []
    genre, year = _genre_year(recs, title=title, observations=observations)
    return (genre, year, (recs[0].get("id") if recs else None), _release_ids(recs),
            (recs[0].get("isrcs") or []) if recs else [])


def _genre_year(recordings, *, title="", observations=None):
    """Derive (genre, year) from a recordings list (best match first), or (None, None) if empty."""
    if not recordings:
        return (None, None)
    rec = recordings[0]                          # best match drives genre/artist
    year = _earliest_year(recordings)            # but year comes from the earliest candidate
    credit = rec.get("artist-credit") or []
    first = credit[0] if credit else None        # MB sometimes returns bare join-phrase strings here
    artist_mbid = (first.get("artist") or {}).get("id") if isinstance(first, dict) else None
    genre = None
    mbid = rec.get("id")
    match = {'method': 'title_artist_match', 'recording_mbid': mbid,
             'search_score': rec.get('score')}
    if mbid:
        try:
            full = _get(f"recording/{mbid}", {"inc": "genres+tags+releases+isrcs", "fmt": "json"})
            rec["isrcs"] = full.get("isrcs") or rec.get("isrcs") or []
            _retain_tags(observations, full, 'recording', mbid, match)
            genre = _top_label(full)
            year = year or _earliest_year([full])
        except IntegrationUnavailable:
            return genre, year  # Preserve search evidence without another vendor request.
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug("MB lookup failed for %s: %s", mbid, e)
    if not genre and not is_remix(title or rec.get("title")):
        genre = _artist_genre(artist_mbid, observations=observations, match_evidence=match)
    return (genre, year)


