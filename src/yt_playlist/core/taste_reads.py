"""Taste identity and serving projections, independent of preparation jobs."""
import hashlib
import json

from . import genre_identity


KEYS = {'rec_weights': ('axis',), 'rec_lean': ('axis',), 'home_hidden_facet': ('axis',),
        'rec_theme': ('facet',), 'rec_play_grad': ('axis',), 'rec_grad_log': ('id',)}


def axis(conn, token):
    if not isinstance(token, str) or not token or len(token.encode()) > 4096:
        raise ValueError('A taste axis requires 1..4096 bytes of text')
    kind, genre, family, value, reason = 'other', None, None, token, None
    if token.startswith(('genre:', 'family:')):
        label = token.split(':', 1)[1]
        if conn.execute('SELECT 1 FROM genre_family WHERE key=?', (label,)).fetchone():
            kind, family, value = 'family', label, None
        else:
            genre, reason = genre_identity.resolve(conn, label)
            if genre is not None:
                kind, value = 'genre', None
            else:
                kind, value, reason = 'unresolved_genre', label, reason or 'unknown_genre'
    fields = kind, genre, family, value, reason
    identity = hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(',', ':'),
                                        allow_nan=False).encode()).hexdigest()
    return identity, fields


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='taste_axes'").fetchone() is not None


def selected(conn):
    if available(conn) or conn.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE name='published_taste_head'").fetchone() is None:
        return None
    row = conn.execute('SELECT revision FROM published_taste_head WHERE active=1').fetchone()
    return row[0] if row else None


def projection(conn, owner, columns, *, token_name='axis'):
    identity = selected(conn)
    if identity is None or owner not in KEYS:
        return None
    token = ("CASE kind WHEN 'genre' THEN 'genre:'||genre_key WHEN 'family' THEN 'genre:'||family_key "
             "WHEN 'unresolved_genre' THEN 'genre:'||axis_value ELSE axis_value END AS "+token_name)
    fields = {'rec_weights':'weight,updated_at','rec_lean':'value,updated_at,last_graduated_day',
        'home_hidden_facet':'','rec_theme':'sum(score) AS score,max(updated_at) AS updated_at',
        'rec_play_grad':'max(last_graduated_day) AS last_graduated_day',
        'rec_grad_log':"json_extract(source_key,'$[0]') AS id,source,score,factor,new_weight,created_at"}[owner]
    folded = 'SELECT '+token+(','+fields if fields else '')+' FROM main.published_taste_rows'
    folded += ' WHERE stage='+str(identity)+' AND owner='+repr(owner)
    if owner in ('rec_theme','rec_play_grad','home_hidden_facet'):
        folded += ' GROUP BY axis_key'
    return 'SELECT '+columns+' FROM ('+folded+') t'


def legacy_parts(column):
    prefixed = f"(substr({column},1,6)='genre:' OR substr({column},1,7)='family:')"
    label = f"CASE WHEN substr({column},1,7)='family:' THEN substr({column},8) ELSE substr({column},7) END"
    candidates = (f'SELECT key AS identity_key FROM genre_catalog WHERE key=({label}) OR name=({label}) COLLATE NOCASE '
        f'UNION SELECT genre_key FROM genre_alias WHERE label_key=tastenorm({label}) OR label=({label}) COLLATE NOCASE')
    return prefixed,label,candidates


def legacy_token(column):
    prefixed,label,candidates = legacy_parts(column)
    return ('CASE WHEN '+prefixed+' THEN CASE WHEN EXISTS(SELECT 1 FROM genre_family WHERE key=('+label+')) '
        "THEN 'genre:'||("+label+") ELSE coalesce((SELECT CASE WHEN count(*)=1 THEN 'genre:'||min(identity_key) END FROM ("+
        candidates+")),'genre:'||("+label+')) END ELSE '+column+' END')


def legacy_kind(column):
    prefixed,label,candidates = legacy_parts(column)
    return ('CASE WHEN '+prefixed+' THEN CASE WHEN EXISTS(SELECT 1 FROM genre_family WHERE key=('+label+')) '
        "THEN 'family' WHEN (SELECT count(*) FROM ("+candidates+"))=1 THEN 'genre' ELSE 'unresolved_genre' END ELSE 'other' END")


def live_projection(owner, columns, *, token_name='axis'):
    """Fold current aliases after a continuing write withdraws publication."""
    legacy = 'facet' if owner=='rec_theme' else 'axis'
    fields = {'rec_weights':'weight,updated_at','rec_lean':'value,updated_at,last_graduated_day',
        'home_hidden_facet':'','rec_theme':'score,updated_at','rec_play_grad':'last_graduated_day',
        'rec_grad_log':'id,source,score,factor,new_weight,created_at'}[owner]
    source = ('SELECT '+legacy+' AS raw_token,'+legacy_token(legacy)+' AS canonical_token,'
        +legacy_kind(legacy)+' AS axis_kind'+(','+fields if fields else '')+' FROM main.'+owner)
    folded = 'SELECT canonical_token,axis_kind,'
    if owner in ('rec_theme','rec_play_grad','home_hidden_facet'):
        folded += 'min(raw_token) AS raw_token'
    else:
        folded += 'raw_token'
    if owner=='rec_theme':
        folded += ',sum(score) AS score,max(updated_at) AS updated_at'
    elif owner=='rec_play_grad':
        folded += ',max(last_graduated_day) AS last_graduated_day'
    elif fields:
        folded += ','+fields
    folded += ' FROM ('+source+')'
    if owner in ('rec_theme','rec_play_grad','home_hidden_facet'):
        folded += ' GROUP BY axis_kind,canonical_token'
    unique = 'min(axis_kind) OVER (PARTITION BY canonical_token)=max(axis_kind) OVER (PARTITION BY canonical_token)'
    if owner in ('rec_weights','rec_lean'):
        unique += ' AND count(*) OVER (PARTITION BY canonical_token)=1'
    token = 'CASE WHEN '+unique+' THEN canonical_token ELSE raw_token END'
    visible = 'SELECT '+token+' AS '+token_name+(','+fields if fields else '')+' FROM ('+folded+')'
    return 'SELECT '+columns+' FROM ('+visible+') t'
