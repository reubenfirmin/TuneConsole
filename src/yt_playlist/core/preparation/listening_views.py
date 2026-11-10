"""Explicit listening projections shared by retained stages and selected reads."""


DERIVED = ('listening_evidence', 'eligible_listening_evidence', 'listening_days',
    'listening_daily_items', 'listening_daily_snapshots', 'listening_activity', 'listening_day_counts')


def materialized():
    """Capture native subject projections once for whole-history calculations.

    Canonical keys are resolved with COALESCE in the subject views. Repeated
    correlated probes cannot use the physical subject indexes through that
    expression. Materialized inputs let SQLite index the resolved keys while
    preserving the same evidence eligibility and calendar rules.
    """
    names = {name: 'recent_' + name for name in
             ('play_event_subjects', 'listening_observation_subjects', *DERIVED)}
    names.update(sources='play_event_sources', calendar='play_event_calendar')
    inputs = [f'{names[name]} AS MATERIALIZED (SELECT * FROM {name})' for name in
              ('play_event_subjects', 'listening_observation_subjects')]
    inputs.extend(f'{names[name]} AS MATERIALIZED ({query})'
                  for name, query in definitions(names.__getitem__))
    return ','.join(inputs), names


def definitions(relation, *, staged=False):
    """Generate our finite projection family; never rewrite installed SQL."""
    events, observations, sources, calendar = (relation(name) for name in
        ('play_event_subjects', 'listening_observation_subjects', 'sources', 'calendar'))
    evidence, eligible, days = (relation(name) for name in DERIVED[:3])
    field = lambda alias: alias+'.stage,' if staged else ''
    same = lambda left, right: f'{left}.stage={right}.stage AND ' if staged else ''
    group = 'stage,' if staged else ''
    coarse = (f"SELECT {field('o')}CASE WHEN o.capture_item_id IS NOT NULL THEN 'capture:'||o.capture_item_id "
        "ELSE 'day:'||o.id END evidence_id,o.account_id,o.identity_key,o.recording_id,o.day,o.source,o.timezone,"
        f'o.precision,o.last_observed_at observed_at,NULL event_id,0 is_generated FROM {observations} o ')
    exact = (f"SELECT {field('pe')}'event:'||pe.id||':'||s.source,pe.account_id,pe.identity_key,pe.recording_id,c.day,"
        "s.source,c.timezone,'exact_timestamp',pe.played_at,pe.id,coalesce(pe.is_generated,0) "
        f'FROM {events} pe JOIN {calendar} c ON {same("pe", "c")}c.event_id=pe.id '
        f'JOIN {sources} s ON {same("pe", "s")}s.event_id=pe.id')
    yield 'listening_evidence', coarse+'WHERE o.day IS NOT NULL UNION ALL '+exact
    yield 'eligible_listening_evidence', coarse+f'''WHERE o.day IS NOT NULL AND (
        o.source<>'ytm_history' OR NOT EXISTS (
            SELECT 1 FROM {events} pe JOIN {calendar} c ON {same('pe', 'c')}c.event_id=pe.id
            JOIN {sources} s ON {same('pe', 's')}s.event_id=pe.id
            WHERE {same('pe', 'o')}pe.account_id=o.account_id AND pe.identity_key=o.identity_key
              AND c.day=o.day AND pe.is_generated=1)
        OR EXISTS (
            SELECT 1 FROM {events} pe JOIN {calendar} c ON {same('pe', 'c')}c.event_id=pe.id
            JOIN {sources} s ON {same('pe', 's')}s.event_id=pe.id
            WHERE {same('pe', 'o')}pe.account_id=o.account_id AND pe.identity_key=o.identity_key
              AND c.day=o.day AND coalesce(pe.is_generated,0)=0)
        OR EXISTS (
            SELECT 1 FROM {observations} independent WHERE {same('independent', 'o')}
            independent.account_id=o.account_id AND independent.identity_key=o.identity_key
            AND independent.day=o.day AND independent.source<>'ytm_history'))
        UNION ALL '''+exact+' WHERE coalesce(pe.is_generated,0)=0'
    yield 'listening_days', f'''SELECT {group}account_id,identity_key,min(recording_id) recording_id,day,
        CAST(strftime('%s',day||' 12:00:00') AS REAL) day_ts,
        max(CASE WHEN event_id IS NOT NULL THEN observed_at END) last_event_at,
        max(observed_at) last_observed_at,
        CASE WHEN min(source)=max(source) THEN min(source) ELSE 'multiple' END source
        FROM {eligible} GROUP BY {group}account_id,identity_key,day'''
    yield 'listening_daily_items', f'''SELECT {group}account_id||':'||day||':'||identity_key rowid,
        account_id||':'||day snapshot_id,identity_key,recording_id,source,day,account_id,last_event_at,last_observed_at
        FROM {days}'''
    yield 'listening_daily_snapshots', f'''SELECT {group}account_id||':'||day id,account_id,day_ts taken_at,day
        FROM {days} GROUP BY {group}account_id,day'''
    yield 'listening_activity', f'''SELECT {field('pe')}pe.account_id,pe.identity_key,pe.recording_id,c.day,pe.played_at at,
        'exact_timestamp' precision,pe.id event_id FROM {events} pe JOIN {calendar} c
        ON {same('pe', 'c')}c.event_id=pe.id WHERE coalesce(pe.is_generated,0)=0
        UNION ALL SELECT {field('d')}d.account_id,d.identity_key,d.recording_id,d.day,d.day_ts,
        CASE WHEN EXISTS (SELECT 1 FROM {eligible} e WHERE {same('e', 'd')}e.account_id=d.account_id
            AND e.identity_key=d.identity_key AND e.day=d.day AND e.precision='calendar_day')
        THEN 'calendar_day' ELSE 'legacy_unknown' END,NULL FROM {days} d
        WHERE d.last_event_at IS NULL'''
    yield 'listening_day_counts', f'''SELECT {group}identity_key,count(*) plays FROM (
        SELECT DISTINCT {group}account_id,identity_key,day FROM {eligible}) GROUP BY {group}identity_key'''


def first_play(relation):
    recordings, artists, activity = (relation(name) for name in
        ('recording_listening_summary', 'artist_listening_summary', 'listening_activity'))
    return f'''SELECT 'track' kind,r.identity_key id_key,
        CAST(julianday(min(s.day))-julianday('1970-01-01') AS INTEGER) first_day,
        min(s.exact_at) first_ts,'canonical_listening' source
        FROM (SELECT recording_id,first_exact_day day,first_exact_play_at exact_at FROM {recordings}
            UNION ALL SELECT recording_id,first_observed_day,NULL FROM {recordings}
            UNION ALL SELECT recording_id,first_legacy_unknown_day,NULL FROM {recordings}) s
        JOIN main.recordings r ON r.id=s.recording_id WHERE s.day IS NOT NULL GROUP BY s.recording_id
        UNION ALL SELECT 'artist','@artist:'||s.artist_id,
        CAST(julianday(min(s.day))-julianday('1970-01-01') AS INTEGER),min(s.exact_at),'canonical_listening'
        FROM (SELECT artist_id,first_exact_day day,first_exact_play_at exact_at FROM {artists}
            UNION ALL SELECT artist_id,first_observed_day,NULL FROM {artists}
            UNION ALL SELECT artist_id,first_legacy_unknown_day,NULL FROM {artists}) s
        WHERE s.day IS NOT NULL GROUP BY s.artist_id
        UNION ALL SELECT 'track',identity_key,CAST(julianday(min(day))-julianday('1970-01-01') AS INTEGER),
        min(CASE WHEN precision='exact_timestamp' THEN at END),'unresolved_listening'
        FROM {activity} WHERE recording_id IS NULL AND day IS NOT NULL GROUP BY identity_key'''
