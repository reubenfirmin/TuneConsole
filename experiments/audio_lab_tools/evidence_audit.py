"""Read-only account evidence inventory and focused #586 acceptance entrypoints."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from threading import RLock
from types import SimpleNamespace

from experiments.audio_lab_tools.provider_pilot import connect, digest, save

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'experiments/audio_lab/local/evidence-586'
CHECKS = [
    'tests/test_track_web.py', 'tests/test_home_vibe.py', 'tests/test_radio_musical_evidence.py',
    'tests/rec/test_choice_outcomes.py', 'tests/repos/test_generated_provenance.py',
    'tests/rec/test_home_cards.py',
    'tests/rec/test_choice_evidence.py::test_prepared_context_reference_survives_maintenance_and_issues_exact_capture',
    'tests/rec/test_choice_evidence.py::test_mix_playback_resolves_recipe_from_exact_external_queue',
    'tests/rec/test_vibe_prediction.py::test_replays_are_one_recording_partial_runs_do_not_replace_complete_and_annotations_never_seed',
    'tests/rec/test_vibe_prediction.py::test_changed_measurement_and_subgenre_retract_old_cohort_and_notify_without_content_refit',
    'tests/rec/test_vibe_prediction.py::test_model_version_change_recomputes_all_evidence_in_pages',
    'tests/rec/test_vibe_prediction.py::test_artist_tags_never_become_recording_measurements_or_propagation_seeds',
]
FOLLOWUP_CHECKS = [
    'tests/test_home_vibe.py::test_vibe_changes_rendered_tracks_persists_and_expires_without_learning',
    'tests/test_home_vibe.py::test_mix_journey_keeps_exact_profiles_and_outcomes_after_preparation_changes',
]


def snapshot(db, output):
    from yt_playlist.repos.vibe_predictions import VibePredictionsRepo

    counts, versions, cases = Counter(), Counter(), {}
    with closing(connect(db)) as conn:
        schema = conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        # Use the same relational overlay as the track route, on the already
        # pinned, query-only connection. This facade cannot initialize a Store
        # or run migrations against the account.
        overlay = VibePredictionsRepo(SimpleNamespace(conn=conn, _lock=RLock()))
        for row in conn.execute('''SELECT p.identity_key,p.payload,r.title,r.artist
                FROM rec_musical_profiles p JOIN recordings r ON r.identity_key=p.identity_key ORDER BY r.id'''):
            profile = json.loads(row['payload'])
            overlay.overlay({row['identity_key']: profile})
            counts['profiles'] += 1
            versions[profile['version']] += 1
            flags = {f"vibe:{fit['source']}" for fit in profile.get('vibes', {}).get('fits', {}).values()}
            dimensions = profile.get('dimensions', {}).values()
            if any(d.get('override') for d in dimensions):
                flags.add('explicit_correction')
            if any(d.get('conflict') for d in dimensions):
                flags.add('conflicting_sources')
            if any(d.get('value') is None for d in dimensions):
                flags.add('sparse_attributes')
            counts.update(flags)
            for flag in flags - cases.keys():
                sources, missing = [], False
                expected = set(profile.get('source_versions', []))
                found = set()
                for source in conn.execute('''SELECT p.payload,p.digest FROM rec_musical_source_profiles p
                        JOIN rec_musical_links l ON l.source_key=p.source_key
                        JOIN recordings r ON r.id=l.recording_id WHERE r.identity_key=? ORDER BY p.source_key''',
                        (row['identity_key'],)):
                    if source['digest'] in expected:
                        sources.append(json.loads(source['payload']))
                        found.add(source['digest'])
                missing = expected != found
                cases[flag] = {'key': row['identity_key'], 'title': row['title'], 'artist': row['artist'],
                               'profile': profile, 'sources': sources, 'truncated': missing}
        for case in cases.values():
            case['current_vibes'] = [dict(row) for row in conn.execute('''
                SELECT v.*,m.model_version,m.input_digest,p.policy_version,p.lineage_complete
                FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id
                LEFT JOIN measurements m ON m.id=v.measurement_id
                LEFT JOIN predictions p ON p.id=v.prediction_id WHERE r.identity_key=? ORDER BY v.vibe''', (case['key'],))]
            for fit in case['current_vibes']:
                fit['sample_ids'] = [row[0] for row in conn.execute(
                    'SELECT sample_id FROM measurement_samples WHERE measurement_id=? ORDER BY sample_id',
                    (fit['measurement_id'],))]
                fit['contributors'] = [dict(row) for row in conn.execute('''
                    SELECT m.id,m.recording_id,m.source,m.model_version,m.input_digest
                    FROM prediction_inputs i JOIN measurements m ON m.id=i.measurement_id
                    WHERE i.prediction_id=? ORDER BY m.recording_id LIMIT 32''', (fit['prediction_id'],))]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        lineage = {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                   for name in ('recording_vibes', 'measurements', 'measurement_samples', 'predictions', 'prediction_inputs',
                                'play_events', 'play_event_sources', 'listening_observations') if name in tables}
        lineage_examples = [dict(row) for row in conn.execute('''
            SELECT p.id AS prediction_id,p.recording_id,p.vibe,p.kind,p.policy_version,p.lineage_complete,
                   m.id AS measurement_id,m.recording_id AS contributor_recording_id,
                   m.source,m.model_version,m.input_digest,
                   (SELECT COUNT(*) FROM measurement_samples s WHERE s.measurement_id=m.id) AS sample_count
            FROM predictions p JOIN prediction_inputs i ON i.prediction_id=p.id
            JOIN measurements m ON m.id=i.measurement_id
            ORDER BY p.id DESC,m.id LIMIT 24''')]
    # Immutable account files are observed directly; absent historical coverage
    # stays absent. Do not issue synthetic actions into the account ledger.
    directory = db.parent / (db.name + '.evidence')
    ledger_counts = {kind: len(list((directory / kind).glob('*/*.json'))) for kind in ('opportunity', 'response')}
    records = []
    for path in sorted((directory / 'opportunity').glob('*/*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:24]:
        value = json.loads(path.read_bytes())
        context_id = value.get('context')
        token = hashlib.sha256(str(context_id).encode()).hexdigest()
        context_path = directory / 'artifact' / token[:2] / (token + '.json')
        context = json.loads(context_path.read_bytes()) if context_path.exists() else None
        records.append({'record': value, 'context_manifest': context, 'sha256': digest(path)})
    responses = []
    for path in sorted((directory / 'response').glob('*/*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:24]:
        response = json.loads(path.read_bytes())
        token = hashlib.sha256(response['opportunity_id'].encode()).hexdigest()
        opportunity_path = directory / 'opportunity' / token[:2] / (token + '.json')
        opportunity = json.loads(opportunity_path.read_bytes()) if opportunity_path.exists() else None
        selected = next((option for option in (opportunity or {}).get('options', [])
                         if str(option['id']) == response.get('option_id')), None)
        responses.append({'response': response, 'sha256': digest(path),
                          'opportunity_present': opportunity is not None,
                          'format': (opportunity or {}).get('format'),
                          'context_id': (opportunity or {}).get('context'),
                          'selected_recipe': (selected or {}).get('recipe')})
    report = {'schema_version': schema, 'counts': dict(counts), 'profile_versions': dict(versions),
              'lineage_table_rows': lineage, 'lineage_examples': lineage_examples,
              'ledger_counts': ledger_counts, 'recent_opportunities': records,
              'recent_responses': responses,
              'cases': cases, 'source_hashes': {name: digest(ROOT / name) for name in (
                  'src/yt_playlist/web/musical_evidence.py',
                  'src/yt_playlist/web/templates/_partials/track_musical_evidence.html',
                  'src/yt_playlist/rec/home_cards.py', 'src/yt_playlist/rec/home_prepared.py')}}
    save(output / 'snapshot.json', report)
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ('cases', 'recent_opportunities', 'recent_responses', 'lineage_examples')}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['snapshot', 'check', 'browser'])
    parser.add_argument('--db', type=Path, default=Path.home() / '.local/share/yt-playlist/state.db')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--test', choices=CHECKS + FOLLOWUP_CHECKS, action='append',
                        help='Select a focused check; repeat to combine affected paths')
    parser.add_argument('--components-only', action='store_true',
                        help='Render saved account cases without initializing a full-app fixture')
    args = parser.parse_args()
    if args.action == 'snapshot':
        snapshot(args.db, args.output)
        return
    command = ['uv', 'run', 'pytest', '--test-lock-wait=60']
    if args.action == 'check':
        command += args.test or CHECKS
    else:
        command += ['-m', 'browser', 'experiments/audio_lab_tools/test_evidence_browser.py']
        if not args.components_only:
            command += ['tests/browser/test_track_browser.py::test_musical_evidence_opens_with_keyboard_and_fits_narrow_screens']
    command += ['--junitxml=' + str(args.output / (args.action + '.xml')), '--tb=short']
    args.output.mkdir(parents=True, exist_ok=True)
    sys.exit(subprocess.run(command, cwd=ROOT).returncode)


if __name__ == '__main__':
    main()
