"""The FAO-managed playback queue and its cross-process writer fence."""
from contextlib import contextmanager
import json
import math
import time

from yt_playlist.core import fao
from yt_playlist.core.file_paths import root_for_database


MAX_CAPTURE_BYTES = 1024 * 1024
KINDS = {'rating', 'player', 'live'}
STATUSES = {'LIKE', 'DISLIKE', 'INDIFFERENT'}


def inbox_path(database, memory_key=None):
    return root_for_database(database, memory_key, 'playback-feedback') / 'inbox.json'


def empty():
    return {'format': 1, 'sequence': 0, 'captures': []}


def read(path):
    try:
        return decode(fao.read(path).bytes)
    except FileNotFoundError:
        return empty()


def decode(data):
    queue = json.loads(data)
    if (not isinstance(queue, dict) or queue.get('format') != 1
            or type(queue.get('sequence')) is not int or queue['sequence'] < 0
            or not isinstance(queue.get('captures'), list)):
        raise ValueError('Invalid playback feedback queue')
    return queue


def write(path, queue):
    data = json.dumps(queue, allow_nan=False, separators=(',', ':')).encode()
    fao.write(path, fao.Content(data, durable=True))


def validate_capture(row):
    if (not isinstance(row['capture_id'], str) or not row['capture_id'] or row['kind'] not in KINDS
            or type(row['account_id']) is not int or type(row['observed_at']) not in (int, float)
            or not math.isfinite(row['observed_at']) or row['rating_status'] not in STATUSES | {None}
            or not isinstance(row['body'], dict)):
        raise ValueError('Invalid playback capture')
    envelope = {key: row[key] for key in ('capture_id', 'kind', 'body')}
    envelope['format'] = 1
    if len(json.dumps(envelope, allow_nan=False, separators=(',', ':')).encode()) > MAX_CAPTURE_BYTES:
        raise ValueError('Playback observation exceeds its storage budget')


@contextmanager
def fence(path, *, read_only=False):
    with path.open('rb' if read_only else 'a+b') as stream:
        deadline = time.monotonic() + 5
        while (unlock := fao._try_file_lock(stream, exclusive=True)) is None:
            if time.monotonic() >= deadline:
                raise TimeoutError('Playback feedback admission is busy')
            time.sleep(.025)
        try:
            yield
        finally:
            unlock()


class InboxFile:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def admission(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with fence(self.path.with_suffix('.lock')):
            yield

    def read(self):
        return read(self.path)

    def write(self, queue):
        write(self.path, queue)
