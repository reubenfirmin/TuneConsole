"""Frozen correction of repeated sync-like graduations; no repository writes/commits."""
import math

from .v001_baseline import setting, stamp


def _param(conn, name, default, lo, hi, integer=False):
    try:
        value = float(setting(conn, 'rec_param:' + name))
        if not math.isfinite(value):
            return default
        value = max(lo, min(hi, value))
        return int(round(value)) if integer else value
    except (TypeError, ValueError):
        return default


def apply(conn, context):
    if setting(conn, 'like_ratchet_repaired'):
        return
    # Family/subgenre classification uses the existing genre vocabulary; repair mathematics,
    # source weight, bounds and defaults are frozen here instead of importing runtime knobs.
    from yt_playlist.util import genre_map
    actual, nonlike, entitled, ledger = {}, {}, {}, {}
    for axis, source, factor in conn.execute('SELECT axis,source,factor FROM rec_grad_log ORDER BY id'):
        if source == 'like':
            actual[axis] = actual.get(axis, 0) + 1
        else:
            nonlike[axis] = nonlike.get(axis, 1.0) * factor
    threshold = _param(conn, 'theme_threshold', 1.2, 0.2, 5.0)
    up = _param(conn, 'graduate_up', 1.05, 1.0, 1.5)
    halflife = _param(conn, 'weight_revert_halflife_d', 60, 7, 365, True)
    likes = [r[0] for r in conn.execute("SELECT item_key FROM rec_feedback WHERE kind='like' ORDER BY created_at")]
    for key in likes:
        rows = list(conn.execute('SELECT genre,mb_year,artist FROM tracks WHERE identity_key=?', (key,)))
        axes = set()
        genres = [r[0] for r in rows if r[0]]
        if genres:
            family, sub = genre_map.family(genres[-1]), genre_map.subgenre(genres[-1])
            if family:
                axes.add('genre:' + family)
            if sub and sub != family:
                axes.add('genre:' + sub)
        years = [r[1] for r in rows if r[1]]
        year = str(min(years))[:4] if years else ''
        if len(year) == 4 and year.isdigit():
            axes.add('era:' + str(int(year) // 10 * 10))
        artists = [r[2] for r in rows if r[2]]
        if artists:
            axes.add('artist:' + min(artists))
        for axis in axes:
            score = ledger.get(axis, 0) + 1.0
            if score >= threshold:
                entitled[axis] = entitled.get(axis, 0) + 1
                score -= threshold
            ledger[axis] = score
    changed = 0
    for axis in sorted(actual):
        row = conn.execute('SELECT weight,updated_at FROM rec_weights WHERE axis=?', (axis,)).fetchone()
        before = row[0] if row else 1.0
        if row and row[1] is not None and not (axis.startswith('genre:') and before == 0):
            before = 1 + (before - 1) * 0.5 ** (max(0, (context.now - row[1]) / 86400) / halflife)
        surplus = actual[axis] - entitled.get(axis, 0)
        target = before / up ** surplus if surplus > 0 else before
        after = min(before, max(1.0, nonlike.get(axis, 1.0), target))
        if after != before:
            conn.execute('INSERT INTO rec_weights VALUES (?,?,?) ON CONFLICT(axis) DO UPDATE SET weight=excluded.weight,updated_at=excluded.updated_at', (axis, max(0.2, min(3.0, after)), context.now))
            changed += 1
        conn.execute('UPDATE rec_theme SET score=0 WHERE facet=?', (axis,))
    provenance = conn.execute("UPDATE rec_feedback SET reason='sync' WHERE kind='like' AND (reason IS NULL OR reason='')").rowcount
    graduated = 0
    for key, kind in list(conn.execute("SELECT item_key,kind FROM rec_feedback WHERE kind IN ('like','dislike')")):
        guard = kind + '_grad'
        graduated += conn.execute("INSERT OR IGNORE INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) VALUES (?,?,?,NULL,'',NULL,?)", (guard, key, guard, context.now)).rowcount
    stamp(conn, 'like_ratchet_repaired', f'adjusted {changed} of {len(actual)} like-touched axis weight(s); {provenance} like row(s) stamped sync-provenance; {graduated} once-ever graduation stamp(s) backfilled')
