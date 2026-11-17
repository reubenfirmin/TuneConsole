"""Observation codecs and files selected by an inbox capture's database ID."""
import hashlib
import json
from pathlib import Path
import re
from contextlib import contextmanager
import threading
import time

from yt_playlist.core import fao, file_collection

MAX_BYTES = 1024 * 1024


def encode(capture, kind, body):
    if kind not in ('rating', 'player', 'live') or not isinstance(body, dict):
        raise ValueError('Invalid playback observation')
    encoded = json.dumps({'format': 1, 'capture_id': capture, 'kind': kind, 'body': body},
                         allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
    if not 0 < len(encoded) <= MAX_BYTES:
        raise ValueError('Playback observation exceeds its storage budget')
    return encoded


def relative(capture):
    if not isinstance(capture, str) or re.fullmatch('[0-9a-f]{32}', capture) is None:
        raise ValueError('Invalid playback capture ID')
    return Path(capture[-2:], capture + '.json')


class ObservationFiles:
    def __init__(self, root, admission_path):
        self.root = Path(root)
        self.admission_path = Path(admission_path)
        self.lock = threading.Lock()

    @contextmanager
    def admission(self):
        with self.lock, self.admission_path.open('a+b') as fence:
            deadline = time.monotonic() + 5
            while True:
                unlock = fao._try_file_lock(fence, exclusive=True)
                if unlock is not None:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Playback feedback admission is busy')
                time.sleep(.025)
            try:
                yield
            finally:
                unlock()

    def retained(self):
        return file_collection.retained_reader(self.root)

    def recovery(self):
        return file_collection.collector(self.root)

    def reference(self, capture, encoded):
        return str(self.root / relative(capture)), hashlib.sha256(encoded).hexdigest(), len(encoded)

    def publish(self, capture, kind, body):
        encoded = encode(capture, kind, body)
        reference = self.reference(capture, encoded)
        fao.write(reference[0], encoded)
        return reference

    def delete(self, filename):
        return fao.delete(filename, missing_ok=True)

    @staticmethod
    def body(row):
        if 'body' in row.keys():
            body = json.loads(row['body'])
            if not isinstance(body, dict):
                raise ValueError('Invalid retained playback observation')
            return body
        encoded = fao.read(row['filename'], max_bytes=row['byte_size']).bytes
        if (len(encoded) != row['byte_size']
                or hashlib.sha256(encoded).hexdigest() != row['content_digest']):
            raise ValueError('Playback observation does not match its retained content')
        document = json.loads(encoded)
        if (not isinstance(document, dict) or set(document) != {'format', 'capture_id', 'kind', 'body'}
                or document['format'] != 1 or document['capture_id'] != row['capture_id']
                or document['kind'] != row['kind'] or not isinstance(document['body'], dict)):
            raise ValueError('Invalid retained playback observation')
        return document['body']
