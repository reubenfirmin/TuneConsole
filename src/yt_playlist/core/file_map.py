"""Immutable bounded hash-trie pages behind domain-owned FAO heads.

This file format has no SQL registry or knowledge of runtime owners. A domain
publishes its root with its own transaction, after all new pages are durable.
"""
import hashlib
import json
from pathlib import Path

from . import fao
from .evidence_ledger import canonical

MAX_ROWS = 128
MAX_BYTES = 8 * 1024 * 1024
FORMAT = 'fao-map-1'
MAX_WORK = 32 * 1024 * 1024


class Budget:
    def __init__(self, remaining=MAX_WORK):
        self.remaining = remaining


def relative(digest):
    if not isinstance(digest, str) or len(digest) != 64 or set(digest)-set('0123456789abcdef'):
        raise fao.CorruptFile('Invalid map page identity')
    return Path(digest[:2], digest + '.json')


def _read(root, digest, budget=None):
    path = Path(root) / relative(digest)
    if budget is not None:
        budget.remaining -= fao.read(path).info.logical_bytes
        if budget.remaining < 0:
            raise fao.FileBudgetExceeded('Map operation exceeds its decompression work budget')
    raw = fao.read(path, max_bytes=MAX_BYTES).bytes
    if hashlib.sha256(raw).hexdigest() != digest:
        raise fao.CorruptFile('Map page differs from its publication')
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get('format') != FORMAT
            or set(value) not in ({'format','entries'}, {'format','children'})):
        raise fao.CorruptFile('Invalid map page')
    if 'entries' in value:
        if not isinstance(value['entries'],dict) or len(value['entries']) > MAX_ROWS:
            raise fao.CorruptFile('Invalid map leaf')
    else:
        if (not isinstance(value['children'],dict) or not value['children'] or len(value['children']) > 16
                or any(key not in '0123456789abcdef' or len(key) != 1 for key in value['children'])):
            raise fao.CorruptFile('Invalid map branch')
        for child in value['children'].values():
            relative(child)
    return value


def _write(root, value):
    raw = canonical({'format': FORMAT, **value})
    digest = hashlib.sha256(raw).hexdigest()
    fao.write(Path(root) / relative(digest), raw)
    return digest


def _key(key):
    if not isinstance(key,str) or len(key.encode()) > 65536:
        raise ValueError('Invalid map key')
    return hashlib.sha256(key.encode()).hexdigest()


def get(root, publication, key):
    route = _key(key)
    budget = Budget()
    for depth in range(65):
        if publication is None:
            return None
        page = _read(root,publication,budget)
        if 'entries' in page:
            return page['entries'].get(key)
        if depth == 64:
            break
        publication = page['children'].get(route[depth])
    raise fao.CorruptFile('Map depth exceeds its hash budget')


def update(root, publication, changes):
    """Copy changed paths, never overwrite pages visible to older snapshots."""
    changes = dict(changes)
    if len(changes) > MAX_ROWS:
        raise ValueError('Map update exceeds 128 keys')
    routes = {key:_key(key) for key in changes}
    budget = Budget()
    def rewrite(digest, updates, depth):
        page = _read(root,digest,budget) if digest else {'entries':{}}
        if 'entries' in page:
            entries = {**page['entries'], **updates}
            entries = {key:value for key,value in entries.items() if value is not None}
            if len(entries) <= MAX_ROWS and len(canonical({'format':FORMAT,'entries':entries})) <= MAX_BYTES:
                return _write(root, {'entries':entries})
            if depth == 64:
                raise fao.FileBudgetExceeded('Map leaf exceeds its hash/byte budget')
            children, groups = {}, {}
            for key,value in entries.items():
                groups.setdefault(_key(key)[depth],{})[key] = value
            for prefix,group in groups.items():
                children[prefix] = rewrite(None,group,depth+1)
        else:
            if depth == 64:
                raise fao.CorruptFile('Map branch exceeds its hash budget')
            children, groups = dict(page['children']), {}
            for key,value in updates.items():
                groups.setdefault(routes[key][depth],{})[key] = value
            for prefix,group in groups.items():
                children[prefix] = rewrite(children.get(prefix),group,depth+1)
        return _write(root, {'children':children})
    return rewrite(publication,changes,0)


def get_many(root, publication, keys):
    """Group batch lookups by page rather than decompressing per original."""
    keys = list(dict.fromkeys(keys))
    if len(keys) > 128:
        raise ValueError('Map lookup exceeds 128 keys')
    routes = {key:_key(key) for key in keys}
    result = {}
    budget = Budget()
    def visit(digest, selected, depth):
        if digest is None:
            return
        if depth > 64:
            raise fao.CorruptFile('Map depth exceeds its hash budget')
        page = _read(root,digest,budget)
        if 'entries' in page:
            result.update((key,page['entries'][key]) for key in selected if key in page['entries'])
            return
        groups = {}
        if depth == 64:
            raise fao.CorruptFile('Map depth exceeds its hash budget')
        for key in selected:
            groups.setdefault(routes[key][depth],[]).append(key)
        for prefix,group in groups.items():
            visit(page['children'].get(prefix),group,depth+1)
    visit(publication,keys,0)
    return result


def bounded_changes(root, publication, changes):
    """Admit a prefix whose old paths and new bodies fit a bounded update.

    Different keys can touch large unrelated leaves. A row count alone cannot
    bound the bytes read by update and subsequent verification. Reserve half
    the operation budget for those existing paths and a quarter for new values.
    Shared pages count once, exactly as in get_many/update.
    """
    selected, pages, old_bytes, new_bytes = {}, {}, 0, 0
    for key,value in changes.items():
        added = len(canonical({key:value}))
        if new_bytes+added>MAX_WORK//4:
            if not selected:
                raise fao.FileBudgetExceeded('Map value exceeds its update budget')
            break
        route, digest, pending = _key(key), publication, {}
        for depth in range(65):
            if digest is None:
                break
            page = pages.get(digest) or pending.get(digest)
            if page is None:
                size = fao.read(Path(root)/relative(digest)).info.logical_bytes
                if old_bytes+sum(size for size,_page in pending.values())+size>MAX_WORK//2:
                    break
                page = _read(root,digest)
                pending[digest] = (size,page)
            elif isinstance(page,tuple):
                page = page[1]
            if 'entries' in page:
                digest = None
                break
            if depth == 64:
                raise fao.CorruptFile('Map depth exceeds its hash budget')
            digest = page['children'].get(route[depth])
        if digest is not None:
            if not selected:
                raise fao.FileBudgetExceeded('Map path exceeds its update budget')
            break
        for digest,(size,page) in pending.items():
            pages[digest] = page
            old_bytes += size
        selected[key] = value
        new_bytes += added
    return selected


def next_leaf(root, publication, after=None):
    """Seek by hash route without rereading earlier leaves on every tick."""
    budget = Budget()
    def visit(digest, route):
        if digest is None or after is not None and route < after and not after.startswith(route):
            return None
        if len(route) > 64:
            raise fao.CorruptFile('Map depth exceeds its hash budget')
        page = _read(root,digest,budget)
        if 'entries' in page:
            return (route,page) if after is None or route > after else None
        for prefix,child in sorted(page['children'].items()):
            result = visit(child,route+prefix)
            if result is not None:
                return result
        return None
    return visit(publication,'')


def contains_page(root, publication, candidate, budget):
    """Find a candidate's route, then check that route in the current tree."""
    digest, depth = candidate, 0
    while True:
        page = _read(root,digest,budget)
        if 'entries' in page:
            if not page['entries']:
                return candidate == publication
            route = _key(next(iter(page['entries'])))
            break
        if depth == 64:
            raise fao.CorruptFile('Map depth exceeds its hash budget')
        digest = next(iter(page['children'].values()))
        depth += 1
    for depth in range(65):
        if publication == candidate:
            return True
        if publication is None:
            return False
        page = _read(root,publication,budget)
        if 'entries' in page:
            return False
        if depth == 64:
            raise fao.CorruptFile('Map depth exceeds its hash budget')
        publication = page['children'].get(route[depth])
    return False


def pages(root, publication):
    """Depth-first enumeration keeps at most one small branch per hash level."""
    if publication is None:
        return
    def walk(digest, depth):
        if depth > 64:
            raise fao.CorruptFile('Map depth exceeds its hash budget')
        yield Path(root) / relative(digest)
        page = _read(root,digest)
        for child in page.get('children',{}).values():
            yield from walk(child,depth+1)
    yield from walk(publication,0)


def items(root, publication):
    if publication is None:
        return
    for path in pages(root,publication):
        page = _read(root,path.stem)
        yield from page.get('entries',{}).items()
