"""Neutral Wikipedia subject identity checks, shared by preparation and acquisition."""
import re
import unicodedata

_STOPWORDS = {'band', 'musician', 'music', 'genre', 'the', 'and', 'of', 'a'}


def matches_subject(kind, display, title, *, canonical_name=lambda name: (name or '').strip(),
                    allow_qualified_artist=True):
    plain = lambda text: ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', text).casefold()))
    if kind == 'artist':
        base = re.sub(r'\s*\([^)]*\)\s*$', '', title).strip() if allow_qualified_artist else title
        wanted, actual = plain(display), plain(base)
        if not wanted:
            return display.casefold() == base.casefold()
        return wanted.removeprefix('the ') == actual.removeprefix('the ')
    name = 'minimal techno' if display.casefold() == 'minimal' else display
    if canonical_name(name).casefold() == canonical_name(title).casefold():
        return True
    wanted = [w for w in re.findall(r'[a-z0-9]+', (name or '').lower())
              if len(w) >= 3 and w not in _STOPWORDS]
    actual = plain(title)
    return bool(wanted) and all(word in actual.split() for word in wanted)
