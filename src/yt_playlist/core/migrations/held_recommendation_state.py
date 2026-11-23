"""Keep held listening choices and Home progress with the code that owns them."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'own_held_recommendation_state'
EVIDENCE = 'evidence_file_writes'
HOME = 'home_genre_requests'
OWNERS = (EVIDENCE, HOME)
WINDOWS = """active_windows TEXT CHECK(active_windows IS NULL OR
    (id=0 AND kind='maintenance' AND json_type(active_windows) IS 'object'))"""
OFFER = "offer_window TEXT CHECK(offer_window IS NULL OR json_type(offer_window) IS 'object')"
REVISIT = """revisit_remaining INTEGER CHECK(revisit_remaining IS NULL OR
    (typeof(revisit_remaining)='integer' AND revisit_remaining BETWEEN 0 AND 7))"""
REVISIT_PROGRESS = """CREATE TABLE home_revisit_progress (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    remaining INTEGER NOT NULL CHECK(typeof(remaining)='integer' AND remaining BETWEEN 0 AND 7))"""


def install_revisit_progress(conn, _context):
    conn.execute(REVISIT_PROGRESS)


def validate_revisit_progress(conn):
    if tokens(definitions(conn, ('home_revisit_progress',)).get('home_revisit_progress')) != tokens(REVISIT_PROGRESS):
        raise MigrationError('Incompatible Home Revisit progress')


def predecessor_objects(conn):
    schema = definitions(conn, OWNERS)
    return [schema[owner] for owner in OWNERS]


def install(conn, _context):
    conn.execute(f'ALTER TABLE {EVIDENCE} ADD COLUMN {WINDOWS}')
    conn.execute("""UPDATE evidence_file_writes SET active_windows=(
        SELECT json_group_object(substr(key,15),json(value)) FROM settings
        WHERE key IN ('choice_window:albums','choice_window:mixes','choice_window:radio')
    ) WHERE id=0""")
    conn.execute(f'ALTER TABLE {HOME} ADD COLUMN {OFFER}')
    conn.execute(f'ALTER TABLE {HOME} ADD COLUMN {REVISIT}')
    conn.execute("""INSERT INTO home_genre_requests(id,offer_window,revisit_remaining)
        SELECT 1,max(value) FILTER(WHERE key='home_offer_window'),
            max(value) FILTER(WHERE key='home_revisit_remaining')
        FROM settings WHERE key IN ('home_offer_window','home_revisit_remaining') HAVING count(*)>0
        ON CONFLICT(id) DO UPDATE SET offer_window=excluded.offer_window,revisit_remaining=excluded.revisit_remaining
    """)
    conn.execute("""DELETE FROM settings WHERE key IN (
        'choice_window:albums','choice_window:mixes','choice_window:radio',
        'home_offer_window','home_revisit_remaining'
    )""")


def validate(conn):
    schema = definitions(conn, OWNERS)
    if ('active_windows' not in tokens(schema.get(EVIDENCE))
            or not {'offer_window', 'revisit_remaining'} <= set(tokens(schema.get(HOME)))):
        raise MigrationError('Incomplete held recommendation state storage')
