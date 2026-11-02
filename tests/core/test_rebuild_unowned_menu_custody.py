"""An appendable ledger can contain files newer than its retained SQL snapshot."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import evidence_ledger, fao, mix_choices
from yt_playlist.core.db_rebuild import mix_verification


@pytest.mark.parametrize('damage', [None, 'missing', 'changed'])
def test_unowned_compact_menu_requires_exact_file_custody(tmp_path, damage):
    # The actual schema93 donor has no descriptor dictionary but includes five
    # later compact menus. Their local descriptor IDs cannot be expanded using
    # that SQL snapshot; the captured bytes must still survive exactly.
    with closing(sqlite3.connect(tmp_path / 'source.db')) as source, \
            closing(sqlite3.connect(tmp_path / 'target.db')) as target:
        source.row_factory = target.row_factory = sqlite3.Row
        ledger = evidence_ledger.EvidenceLedger(evidence_ledger.directory(source), 2**63 - 1)
        context = ledger.artifact({'schema': 2, 'precision': mix_choices.PRECISION, 'at': 100})
        ledger.put('opportunity', 'unowned-menu', dict(schema=2, id='unowned-menu',
            format='mixes', precision=mix_choices.PRECISION, context=context, at=100,
            options=[dict(id='opaque-option', descriptor_id=987654, position=1)]))
        inventory = mix_verification.capture(source)
        root = evidence_ledger.directory(target)
        for relative, _digest in inventory:
            fao.copy(evidence_ledger.directory(source) / relative, root / relative)
        target.execute('CREATE TABLE mix_choice_preparation(id INTEGER,phase TEXT,cursor TEXT)')
        target.execute("INSERT INTO mix_choice_preparation VALUES (1,'ready','')")
        target.execute('CREATE TABLE group_retention_state(id INTEGER,roots_complete INTEGER)')
        target.execute('INSERT INTO group_retention_state VALUES (1,1)')
        copied = next(root / relative for relative, _digest in inventory
                      if relative.startswith('opportunity/'))
        if damage == 'missing':
            fao.delete(copied)
        elif damage == 'changed':
            fao.write_bytes(copied, b'{"changed":true}')
        if damage:
            with pytest.raises(ValueError, match='changed original listening evidence'):
                mix_verification.verify_captured(source, target, inventory=inventory)
        else:
            mix_verification.verify_captured(source, target, inventory=inventory)
            assert all(list(fao._hash(root / relative)) == digest for relative, digest in inventory)
