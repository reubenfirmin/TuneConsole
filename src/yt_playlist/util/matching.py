import re
import unicodedata
from rapidfuzz import fuzz

_FEAT_RE = re.compile(r"\((?:feat|ft|with)\.?[^)]*\)|\[(?:feat|ft|with)\.?[^\]]*\]", re.I)
_PAREN_NOISE_RE = re.compile(
    r"[\(\[][^)\]]*\b(remaster(?:ed)?|remix|radio edit|explicit|clean|deluxe|"
    r"bonus|live|mono|stereo|version|edit|anniversary)\b[^)\]]*[\)\]]", re.I)
_PUNCT_RE = re.compile(r"[^\w\s]|_", re.UNICODE)
_WS_RE = re.compile(r"\s+")

def normalize(s: str) -> str:
    if not s:
        return ""
    # Fold accented Latin to ASCII (café -> cafe). For an all-non-Latin string (Cyrillic, CJK,
    # Greek, emoji) the ASCII fold is empty — keep the original there so distinct songs keep
    # distinct identity_keys instead of every one collapsing to "" (and the key to "|").
    ascii_s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = ascii_s if ascii_s.strip() else s
    s = s.lower()
    s = _FEAT_RE.sub(" ", s)
    s = _PAREN_NOISE_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s

def identity_key(title: str, artist: str) -> str:
    return f"{normalize(title)}|{normalize(artist)}"

def fuzzy_ratio(a: str, b: str) -> float:
    return fuzz.token_sort_ratio(a, b) / 100.0

def track_artist(track: dict) -> str:
    artists = track.get("artists") or []
    return artists[0].get("name") or "" if artists else ""

def track_album(track: dict):
    alb = track.get("album")
    return alb.get("name") if isinstance(alb, dict) else None
