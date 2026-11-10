"""The saved-output portion of the numbered v95 owner cutover."""
from pathlib import Path
import sqlite3

from .runner import MigrationError


def statements():
    yield 'DROP TRIGGER radio_seed_policy_insert'
    yield 'DROP TRIGGER radio_seed_policy_update'
    yield 'DROP TABLE radio_seed_policies'
    yield from definitions()


def definitions():
    pending = ''
    for line in Path(__file__).with_name('schema_recommendation_setting_outputs.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete saved recommendation output schema')
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER saved_recommendation_settings_{event.lower()} BEFORE {event} ON settings '
               "WHEN NEW.key IN ('radio_seed_offer','rec_autotune_result') BEGIN "
               "SELECT RAISE(ABORT,'Saved recommendation output requires its domain owner'); END")
        yield (f'CREATE TRIGGER radio_seed_subject_{event.lower()} BEFORE {event} ON radio_seed_subjects '
               'WHEN NEW.entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM catalog_entries '
               'WHERE id=NEW.entry_id AND video_id=NEW.video_id) BEGIN '
               "SELECT RAISE(ABORT,'Radio seed does not match its catalog entry'); END")
    for table, masks in (('model_comparison_plan', {'policy_mask': 2047, 'previous_mask': 3, 'challenger_mask': 3}),
                         ('model_listening_monitor', {'policy_mask': 2047, 'setup_mask': 3}),
                         ('model_album_assignment', {'comparison_mask': 1023})):
        invalid = ' OR '.join(f'NEW.{field}>{maximum}' for field, maximum in masks.items())
        if table == 'model_album_assignment':
            invalid += " OR (typeof(NEW.epoch)='text' AND length(NEW.epoch) NOT BETWEEN 1 AND 4096)"
        for event in ('INSERT', 'UPDATE'):
            yield (f'CREATE TRIGGER {table}_fields_{event.lower()} BEFORE {event} ON {table} WHEN {invalid} '
                   "BEGIN SELECT RAISE(ABORT,'Invalid model comparison fields'); END")
