#!/usr/bin/env python3
"""Regenerate the immutable v79 admission reference from empty frozen upgrades.

This produces a reviewable data artifact. Runtime preparation reads that artifact
and never imports or executes the historical upgrade registry.
"""
import json
from pathlib import Path
import sqlite3

from yt_playlist.core.migrations import current, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS


def main():
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        migrate(conn, registry=MIGRATIONS[:79])
        objects = [row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
        current._schema_contract(objects)
    path = Path(__file__).resolve().parents[1] / 'src/yt_playlist/core/migrations/installed_v079.json'
    path.write_text(json.dumps({'history':[migration.name for migration in MIGRATIONS],
                               'statements':objects}, indent=2)+'\n')
    print(f'{path}: {len(objects)} definitions, {len(MIGRATIONS)} history entries')


if __name__ == '__main__':
    main()
