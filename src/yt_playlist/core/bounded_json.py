"""Decode large cache collections without one uninterruptible container allocation.

The standard decoder still parses every JSON value. Large containers are joined
from bounded subtrees in Python, allowing other threads and cancellation between
them. Scalar strings remain atomic; their size does not multiply object creation.
"""
import json
import re

from yt_playlist.core.activity import check_cancellable_work

CHUNK_CHARS = 64 * 1024
_SPACE = re.compile(r'[ \t\r\n]*')


class _Reader:
    def __init__(self, encoded):
        self.text = encoded
        self.position = 0
        self.decoder = json.JSONDecoder()
        self.chunk = ''
        self.chunk_start = 0

    def space(self):
        self.position = _SPACE.match(self.text, self.position).end()

    def fail(self, message):
        raise json.JSONDecodeError(message, self.text, self.position)

    def character(self):
        return self.text[self.position:self.position + 1]

    def refill(self):
        check_cancellable_work()
        self.chunk_start = self.position
        self.chunk = self.text[self.position:self.position + CHUNK_CHARS]

    def small_container(self):
        if not self.chunk_start <= self.position < self.chunk_start + len(self.chunk):
            self.refill()
        try:
            value, end = self.decoder.raw_decode(self.chunk, self.position - self.chunk_start)
        except json.JSONDecodeError:
            if self.position == self.chunk_start:
                raise
            self.refill()
            value, end = self.decoder.raw_decode(self.chunk)
        self.position = self.chunk_start + end
        return value

    def value(self, *, keep=True, keys=None):
        self.space()
        # Flat collections also need cancellation boundaries as their scalar
        # children advance beyond the last bounded chunk.
        if not self.chunk_start <= self.position < self.chunk_start + len(self.chunk):
            self.refill()
        opening = self.character()
        if opening not in ('{', '['):
            # Scalars do not allocate a graph of dictionaries/lists. Decode on
            # the original document so numbers/escapes across a chunk stay whole.
            value, self.position = self.decoder.raw_decode(self.text, self.position)
            return value if keep else None
        if keys is None or opening != '{':
            try:
                value = self.small_container()
            except json.JSONDecodeError:
                pass  # Large or malformed: assemble/validate its children below.
            else:
                return value if keep else None
        closing = '}' if opening == '{' else ']'
        result = ({} if opening == '{' else []) if keep else None
        self.position += 1
        self.space()
        if self.character() == closing:
            self.position += 1
            return result
        while True:
            if opening == '{':
                if self.character() != '"':
                    self.fail('Expecting property name enclosed in double quotes')
                key, self.position = self.decoder.raw_decode(self.text, self.position)
                self.space()
                if self.character() != ':':
                    self.fail("Expecting ':' delimiter")
                self.position += 1
                retained = keep and (keys is None or key in keys)
                item = self.value(keep=retained)
                if retained:
                    result[key] = item
            else:
                item = self.value(keep=keep)
                if keep:
                    result.append(item)
            self.space()
            char = self.character()
            self.position += 1
            if char == closing:
                return result
            if char != ',':
                self.position -= 1
                self.fail("Expecting ',' delimiter")
            self.space()


def loads(encoded, *, keys=None):
    """Read complete JSON, optionally retaining only requested top-level fields.

    Unselected values are still parsed and validated, without retaining their
    large graphs. Small documents use the ordinary fast path.
    """
    if not isinstance(encoded, str) or len(encoded) <= CHUNK_CHARS:
        value = json.loads(encoded)
        return ({key: item for key, item in value.items() if key in keys}
                if keys is not None and isinstance(value, dict) else value)
    reader = _Reader(encoded)
    result = reader.value(keys=keys)
    reader.space()
    if reader.position != len(encoded):
        reader.fail('Extra data')
    return result
