"""Training and target conditioning from detached original-evidence facts."""
import json

from .activity import WorkBudgetExceeded, check_stopping
from .evidence_ledger import canonical


def albums(facts):
    return {recording: str(album) for recording, (album, count) in facts.items() if count == 1}


def training(rows, album_facts, *, limit=32768, max_bytes=8 * 1024 * 1024):
    if len(rows) > limit:
        raise WorkBudgetExceeded('Joint vibe original manifest exceeds bounded admission')
    selected, result, used = albums(album_facts), [], 0
    for row in rows:
        check_stopping()
        item = {'recording': row['recording_id'], 'measurement': row['measurement_id'],
            'artists': json.loads(row['artist_key'] or '[]'), 'genre': row['subgenre_key'],
            'album': selected.get(row['recording_id']),
            'source': row['source'] + '/' + row['exact_version'],
            'score': row['score'], 'support': row['support'],
            'input_digest': row['input_digest'], 'version': row['exact_version']}
        used += len(canonical(item))
        if used > max_bytes:
            raise WorkBudgetExceeded('Joint vibe original manifest exceeds bounded admission')
        result.append(item)
    return result
