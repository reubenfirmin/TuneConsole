"""Independent value/type checks for the unchanged-owner SQL verifier."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core.db_rebuild import verification


@pytest.mark.parametrize('mutation',[
    "UPDATE captures SET key='case' WHERE rowid=7",
    "UPDATE captures SET body=1.0 WHERE rowid=7",
    "UPDATE captures SET body='1' WHERE rowid=7",
    "UPDATE captures SET body='bytes' WHERE rowid=19",
    'DELETE FROM captures WHERE rowid=19',
    'UPDATE captures SET rowid=20 WHERE rowid=19',
    "INSERT INTO captures VALUES ('extra','unowned')",
])
def test_exact_sql_checks_case_types_missing_extra_and_hidden_id_rows(tmp_path,mutation):
    original = tmp_path/'source.db'
    with closing(sqlite3.connect(original)) as conn:
        conn.execute('CREATE TABLE originals(id TEXT PRIMARY KEY COLLATE NOCASE,value)')
        conn.executemany('INSERT INTO originals(rowid,id,value) VALUES (?,?,?)',[
            (7,'Case',1),(19,None,b'bytes'),(28,None,'second nullable identity')])
        conn.commit()
    with closing(sqlite3.connect(original.as_uri()+'?mode=ro',uri=True)) as reader, \
            closing(sqlite3.connect((tmp_path/'target.db').as_uri(),uri=True)) as target:
        target.execute('CREATE TABLE captures(key TEXT PRIMARY KEY COLLATE NOCASE,body)')
        target.execute('ATTACH DATABASE ? AS rebuild_source',(original.as_uri()+'?mode=ro',))
        target.execute('INSERT INTO captures(rowid,key,body) SELECT rowid,id,value FROM rebuild_source.originals')
        mapping = {'_legacy_id':'rowid','id':'key','value':'body'}
        verification._exact_sql(reader,target,'originals','captures',mapping)
        target.execute(mutation)
        with pytest.raises(ValueError,match='retained values or IDs|retained row count'):
            verification._exact_sql(reader,target,'originals','captures',mapping)
