"""Title and artist matching for music queries, independent of word order."""
import re
import unicodedata
from functools import lru_cache


@lru_cache(maxsize=4096)
def words(value):
    value = unicodedata.normalize('NFKD', value or '').casefold()
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return tuple(re.findall(r'[^\W_]+', re.sub(r"['’‘ʼ]", '', value)))


def relevance(query, title, artist):
    terms = set(words(query))
    titles, artists = words(title), words(artist)
    combined = set(titles + artists)
    matched = sum(any(word.startswith(term) for word in combined) for term in terms)
    exact = len(terms & combined)
    credited = sum(any(word.startswith(term) for word in artists) for term in terms)
    # Artist credits outrank an artist name mentioned in a cover's title.
    return matched, exact, credited, -len(combined - terms)


def match_score(query, title, artist):
    score = relevance(query, title, artist)
    if not words(query) or score[0] != len(set(words(query))):
        return 0
    return 10000 + score[1] * 1000 + score[2] * 100 + max(-99, score[3])
