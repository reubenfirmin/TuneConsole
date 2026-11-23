"""Keep the state of a taste axis on its definition row."""
from .. import group_preview_work
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'fold_taste_axis_state'
RETIRED = ('rec_weights', 'rec_lean', 'rec_theme', 'play_gradient_checkpoints')


def installed(conn):
    return any(row[1] == 'weight_updated_at' for row in conn.execute('PRAGMA main.table_info(taste_axes)'))


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?,?,?,?) '
                                        'AND sql IS NOT NULL ORDER BY type,name', ('taste_axes', *RETIRED))]


def install(conn, _context):
    for column in (
        'weight REAL', 'weight_updated_at REAL CHECK(weight IS NOT NULL OR weight_updated_at IS NULL)',
        'lean REAL', 'lean_updated_at REAL CHECK(lean IS NOT NULL OR lean_updated_at IS NULL)',
        'lean_last_graduated_day TEXT CHECK(lean IS NOT NULL OR lean_last_graduated_day IS NULL)',
        'theme_score REAL', 'theme_updated_at REAL CHECK((theme_score IS NULL)=(theme_updated_at IS NULL))',
        'play_last_graduated_day TEXT',
    ):
        conn.execute(f'ALTER TABLE taste_axes ADD COLUMN {column}')
    conn.execute('UPDATE taste_axes AS a SET weight=w.weight,weight_updated_at=w.updated_at '
                 'FROM rec_weights w WHERE w.axis_id=a.id')
    conn.execute('UPDATE taste_axes AS a SET lean=l.value,lean_updated_at=l.updated_at, '
                 'lean_last_graduated_day=l.last_graduated_day FROM rec_lean l WHERE l.axis_id=a.id')
    conn.execute('UPDATE taste_axes AS a SET theme_score=t.score,theme_updated_at=t.updated_at '
                 'FROM rec_theme t WHERE t.axis_id=a.id')
    conn.execute('UPDATE taste_axes AS a SET play_last_graduated_day=p.last_graduated_day '
                 'FROM play_gradient_checkpoints p WHERE p.axis_id=a.id AND p.last_graduated_day IS NOT NULL')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE EXISTS (SELECT 1 FROM group_preview_dependencies '
                 'WHERE table_name IN (?,?,?,?))', RETIRED)
    group_preview_work.install_guards(conn, sources={'taste_axes'})


def validate(conn):
    if not installed(conn) or definitions(conn, RETIRED):
        raise MigrationError('Taste-axis state was not consolidated')
