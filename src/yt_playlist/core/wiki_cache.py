"""Bounded, replaceable Wikipedia cards owned by story preparation."""
import json
import logging

from . import fao
from .file_paths import root_for_database


log = logging.getLogger(__name__)


def path_for(database):
    return root_for_database(database, None, 'wiki') / 'cards.json'


def read(path):
    try:
        cards = json.loads(fao.read_cache(path, max_bytes=fao.MAX_BYTES))
        if not isinstance(cards, dict):
            raise ValueError('Invalid Wikipedia card cache')
        return cards
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning('Discarding unreadable Wikipedia cards: %s', exc)
        return {}


def write(path, cards, *, durable=False):
    payload = json.dumps(cards, ensure_ascii=False, separators=(',', ':')).encode()
    fao.write(path, fao.Content(payload, durable=durable))
