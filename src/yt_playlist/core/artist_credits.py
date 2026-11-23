"""Immutable ordered artist credits and idempotent source captures.

Provider deliveries identify evidence. Without a delivery ID, a capture changes
only when that entry/source's ordered credit changes. Historical captures keep
their original IDs and times; identical text alone never proves redundancy.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import sqlite3

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.artist_keys import canonical_id

MAX_MEMBERS = 128
MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Member:
    artist_id: int | None
    credit_text: str
    provider: str | None = None
    raw_id: str | None = None
    raw_browse_id: str | None = None
    role: str | None = None
    presence: int = 0


@dataclass(frozen=True)
class Credit:
    display_credit: str
    members: tuple[Member, ...]

    def document(self):
        return ['artist-credit-1', self.display_credit,
                [[m.artist_id, m.credit_text, m.provider, m.raw_id, m.raw_browse_id, m.role, m.presence]
                 for m in self.members]]

    def digest(self):
        if (not isinstance(self.display_credit, str) or not 1 <= len(self.members) <= MAX_MEMBERS
                or any(not isinstance(m, Member) or (m.artist_id is not None and type(m.artist_id) is not int)
                       or not isinstance(m.credit_text, str) or not m.credit_text
                       or any(value is not None and not isinstance(value, str)
                              for value in (m.provider, m.raw_id, m.raw_browse_id, m.role))
                       or m.provider == ''
                       or type(m.presence) is not int or not 0 <= m.presence <= 7
                       or any(not m.presence & bit and value is not None
                              for bit, value in ((1, m.raw_id), (2, m.raw_browse_id), (4, m.role)))
                       or bool(m.provider) != bool(m.raw_id or m.raw_browse_id) for m in self.members)):
            raise ValueError('Invalid normalized ordered artist credit')
        encoded = json.dumps(self.document(), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        if len(encoded) > MAX_BYTES:
            raise ValueError('Artist credit exceeds its content budget')
        return hashlib.sha256(encoded).hexdigest()


def normalize_credit(display_credit, structured, source):
    """Validate provider credit text without consulting artist identities."""
    if (not isinstance(display_credit, str) or not isinstance(source, str) or not source
            or not isinstance(structured, list) or not 1 <= len(structured) <= MAX_MEMBERS):
        raise ValueError('Invalid ordered artist credit')
    members = []
    for value in structured:
        if (not isinstance(value, dict) or value.keys() - {'name', 'id', 'browseId', 'role', 'provider'}
                or not isinstance(value.get('name'), str) or not value['name']
                or any(value.get(key) is not None and not isinstance(value[key], str)
                       for key in ('id', 'browseId', 'role', 'provider'))
                or 'provider' in value and not value['provider']):
            raise ValueError('Unsupported structured artist credit; original evidence retained')
        provider_id = value.get('id') or value.get('browseId')
        # v21 captured YouTube browse IDs under the literal-credit source label.
        provider = (value.get('provider') or ('youtube' if source == 'legacy_literal' else source)) if provider_id else None
        members.append(Member(None, value['name'], provider, value.get('id'), value.get('browseId'),
                              value.get('role'), sum(1 << i for i, k in enumerate(('id', 'browseId', 'role')) if k in value)))
    result = Credit(display_credit, tuple(members))
    result.digest()
    return result


def bind_credit(credit, artist_ids):
    if len(artist_ids) != len(credit.members) or any(type(identity) is not int for identity in artist_ids):
        raise ValueError('A selected artist credit needs exact artist identities')
    result = replace(credit, members=tuple(replace(member, artist_id=identity)
                                          for member, identity in zip(credit.members, artist_ids)))
    result.digest()
    return result


def prepare(conn, display_credit, structured, source, *, artist_ids=None):
    credit = normalize_credit(display_credit, structured, source)
    if artist_ids is not None:
        return bind_credit(credit, artist_ids)
    members = []
    for member in credit.members:
        provider_id = member.raw_id or member.raw_browse_id
        row = conn.execute('SELECT artist_id FROM artist_provider_ids WHERE provider=? AND provider_id=?',
                           (member.provider, provider_id)).fetchone() if provider_id else None
        members.append(replace(member, artist_id=row[0]) if row else member)
    return replace(credit, members=tuple(members))


def read(conn, credit_id):
    row = conn.execute('SELECT display_credit,members_json FROM artist_credits WHERE id=?', (credit_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown artist credit')
    return Credit(row[0], tuple(Member(*value) for value in json.loads(row[1])))


def intern(conn, credit):
    if not conn.in_transaction:
        raise RuntimeError('Artist credit admission requires an outer transaction')
    digest = credit.digest()
    row = conn.execute('SELECT id FROM artist_credits WHERE content_digest=?', (digest,)).fetchone()
    if row:
        if read(conn, row[0]) != credit:
            raise sqlite3.IntegrityError('Artist credit digest collision or incomplete content')
        return row[0]
    members = json.dumps(credit.document()[2], ensure_ascii=False, separators=(',', ':'))
    return conn.execute('INSERT INTO artist_credits(display_credit,content_digest,members_json) VALUES (?,?,?)',
                        (credit.display_credit, digest, members)).lastrowid


def _clock(now):
    if type(now) not in (int, float) or not -1.7976931348623157e308 <= now <= 1.7976931348623157e308:
        raise ValueError('Artist credit capture time must be finite')


def _same_delivered_credit(conn, previous, current):
    """An identity redirect cannot turn a replay into changed source evidence."""
    old, new = read(conn, previous), read(conn, current)
    def canonical(credit):
        return replace(credit, members=tuple(replace(member, artist_id=canonical_id(conn, member.artist_id))
                                              for member in credit.members))
    return canonical(old) == canonical(new)


def capture(conn, entry_id, credit_id, source, now, *, delivery_key=None, kind='capture', metadata_delivery_id=None):
    """Return (observation ID, may select) without replaying an old delivery."""
    if not conn.in_transaction:
        raise RuntimeError('Artist credit capture requires an outer transaction')
    _clock(now)
    if metadata_delivery_id is not None:
        if type(metadata_delivery_id) is not int or kind != 'capture':
            raise ValueError('Invalid artist credit metadata delivery')
        key = f'metadata-delivery:{metadata_delivery_id}'
        if delivery_key not in (None, key):
            raise ValueError('Artist credit metadata delivery has a different input or provider')
        delivery_key = key
    if not isinstance(source, str) or not source or (delivery_key is not None and
            (not isinstance(delivery_key, str) or not delivery_key or len(delivery_key.encode()) > 4096)):
        raise ValueError('Invalid artist credit source delivery')
    old = None
    if delivery_key is not None:
        old = conn.execute('SELECT id,credit_id,metadata_bound,metadata_delivery_id,retired FROM artist_credit_observations '
            'WHERE entry_id=? AND source=? AND delivery_key=?', (entry_id, source, delivery_key)).fetchone()
        if old:
            if metadata_delivery_id is not None and not old['metadata_bound']:
                raise sqlite3.IntegrityError('Artist replay has no verified metadata delivery binding')
            if old['credit_id'] != credit_id and not _same_delivered_credit(conn, old['credit_id'], credit_id):
                raise sqlite3.IntegrityError('A provider delivery cannot change its artist credit')
            if old['retired']:
                return old['id'], False
    if metadata_delivery_id is not None:
        delivery = conn.execute('SELECT input_entry_id,provider FROM metadata_deliveries WHERE id=?',
                                (metadata_delivery_id,)).fetchone()
        if delivery is None and old:
            return old['id'], False
        if delivery is None or tuple(delivery) != (entry_id, source):
            raise ValueError('Artist credit metadata delivery has a different input or provider')
    if delivery_key is not None:
        if old and metadata_delivery_id is not None and old['metadata_delivery_id'] != metadata_delivery_id:
            raise sqlite3.IntegrityError('A provider delivery cannot change its artist credit')
    else:
        head = conn.execute('SELECT id,credit_id FROM artist_credit_observations '
                            'WHERE entry_id=? AND source=? AND is_capture_head=1', (entry_id, source)).fetchone()
        old = head if head and head['credit_id'] == credit_id else None
    if old:
        selected = conn.execute('UPDATE artist_credit_observations SET '
            'first_seen=CASE WHEN first_seen IS NULL THEN ? ELSE min(first_seen,?) END,'
            'last_seen=CASE WHEN last_seen IS NULL THEN ? ELSE max(last_seen,?) END,'
            'processing_count=processing_count+1 WHERE id=? RETURNING is_selected',
            (now, now, now, now, old['id'])).fetchone()
        # Another provider or an identity reconciliation may have supplied a
        # newer selection while this source's capture head stayed unchanged.
        return old['id'], bool(selected[0])
    conn.execute('UPDATE artist_credit_observations SET is_capture_head=0 '
                 'WHERE entry_id=? AND source=? AND is_capture_head=1', (entry_id, source))
    observation = conn.execute('INSERT INTO artist_credit_observations('
        'entry_id,credit_id,source,delivery_key,first_seen,last_seen,processing_count,capture_kind,is_capture_head,'
        'metadata_delivery_id,metadata_bound) VALUES (?,?,?,?,?,?,1,?,1,?,?)',
        (entry_id, credit_id, source, delivery_key, now, now,
         'provider_delivery' if delivery_key is not None and kind == 'capture' else kind,
         metadata_delivery_id, int(metadata_delivery_id is not None))).lastrowid
    return observation, True


def select(conn, entry_id, observation_id):
    row = conn.execute('SELECT credit_id,source,is_selected,retired FROM artist_credit_observations WHERE id=? AND entry_id=?',
                       (observation_id, entry_id)).fetchone()
    if row is None:
        raise ValueError('Selected artist evidence belongs to another entry')
    if row['retired']:
        raise ValueError('Artist credit capture expired; refresh the selection')
    credit = read(conn, row['credit_id'])
    if any(m.artist_id is None for m in credit.members):
        raise ValueError('Selected artist credit contains unresolved historical identities')
    desired = [(i, m.artist_id, m.credit_text, row['source']) for i, m in enumerate(credit.members)]
    update_projection(conn, entry_id, desired)
    if not row['is_selected']:
        _select_observation(conn, entry_id, observation_id)


def _select_observation(conn, entry_id, observation_id):
    conn.execute('UPDATE artist_credit_observations SET is_selected=0 WHERE entry_id=? AND is_selected=1 AND id<>?',
                 (entry_id, observation_id))
    changed = conn.execute('UPDATE artist_credit_observations SET is_selected=1 WHERE id=? AND entry_id=?',
                           (observation_id, entry_id)).rowcount
    if changed != 1:
        raise ValueError('Selected artist evidence belongs to another entry')


def update_projection(conn, entry_id, desired):
    """Write only changed ordered membership; unchanged ingest fires no artist triggers."""
    existing = {row['position']: tuple(row) for row in conn.execute(
        'SELECT position,artist_id,credit,source FROM entry_artists WHERE entry_id=? ORDER BY position', (entry_id,))}
    for value in desired:
        if existing.get(value[0]) != value:
            conn.execute('INSERT INTO entry_artists(entry_id,position,artist_id,credit,source) VALUES (?,?,?,?,?) '
                'ON CONFLICT(entry_id,position) DO UPDATE SET artist_id=excluded.artist_id,'
                'credit=excluded.credit,source=excluded.source', (entry_id, *value))
    if any(position >= len(desired) for position in existing):
        conn.execute('DELETE FROM entry_artists WHERE entry_id=? AND position>=?', (entry_id, len(desired)))


def reconcile_current(conn, previous, target, source, now):
    """Rebind current projections while retaining immutable retired identities."""
    after = None
    while True:
        rows = conn.execute('SELECT DISTINCT entry_id FROM entry_artists '
            'WHERE artist_id=?' + (' AND entry_id>?' if after is not None else '') +
            ' ORDER BY entry_id LIMIT 128', (previous, *((after,) if after is not None else ()))).fetchall()
        if not rows:
            break
        for entry, in rows:
            check_cancellable_work()
            selected = conn.execute('SELECT credit_id FROM artist_credit_observations WHERE entry_id=? AND is_selected=1',
                                    (entry,)).fetchone()
            if selected is None:
                raise ValueError('Current artist projection has no selected evidence')
            credit = read(conn, selected[0])
            replacement = Credit(credit.display_credit, tuple(
                replace(m, artist_id=target) if m.artist_id == previous else m for m in credit.members))
            version = intern(conn, replacement)
            observation, _ = capture(conn, entry, version, 'identity_reconciliation:' + source, now,
                delivery_key=f'redirect:{previous}:{target}:{selected[0]}', kind='identity_reconciliation')
            # Account reconciliation does not turn the provider's original
            # current-source label into the reconciliation mechanism's label.
            current = conn.execute('SELECT position,artist_id,credit,source FROM entry_artists '
                                   'WHERE entry_id=? ORDER BY position', (entry,)).fetchall()
            update_projection(conn, entry, [(p, target if a == previous else a, c, s) for p, a, c, s in current])
            _select_observation(conn, entry, observation)
        after = rows[-1][0]
