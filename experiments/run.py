#!/usr/bin/env python3
"""Run the isolated #530 research tools from one stable command prefix."""
import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import sys


EXPERIMENTS = Path(__file__).resolve().parent
ROOT = EXPERIMENTS.parent
LOCAL = EXPERIMENTS / 'audio_lab/local'
CHECKPOINT = LOCAL / 'external-20261028/checkpoint-1'
MODULES = {
    'vibe-pooling': 'vibe_pooling_evaluation',
    'vibe-pooling-labels': 'vibe_pooling_labels',
    'vibe-pooling-metadata': 'vibe_pooling_metadata',
    'vibe-pooling-offset': 'vibe_pooling_offset',
    'vibe-pooling-source-genre': 'vibe_pooling_source_genre',
    'vibe-pooling-descriptors': 'vibe_pooling_descriptors',
    'vibe-pooling-transfer': 'vibe_pooling_transfer',
    'vibe-pooling-pairs': 'vibe_pooling_pairs',
    'vibe-pooling-ranking': 'vibe_pooling_ranking',
    'vibe-pooling-selection': 'vibe_pooling_selection',
    'quality-round': 'quality_round',
    'focus-counts': 'focus_count_experiment',
    'focus-confirmation': 'focus_confirmation',
    'many-windows': 'many_window_experiment',
    'uplift-gate': 'uplift_gate_experiment',
    'release-extension': 'release_extension',
    'memo-confirmation': 'memo_confirmation',
    'encoder-assets': 'encoder_assets',
    'encoder-comparison': 'encoder_experiment',
    'representation-study': 'representation_study',
    'chroma-study': 'chroma_study',
    'midlevel-study': 'midlevel_study',
    'midlevel-pilot': 'midlevel_pilot',
    'wcmed-study': 'wcmed_study',
    'situnes-study': 'situnes_study',
    'checkpoint': 'external_checkpoint',
    'acquire': 'dataset_range',
    'audit': 'dataset_audit',
    'reference': 'reference_pipeline',
    'windows': 'dataset_windows',
    'pilot': 'new_corpus_campaign',
    'evidence': 'checkpoint_evidence',
    'complete': 'completion_campaign',
    'reserved': 'heldout_cohort',
    'library': 'library_replay',
    'output-evaluation': 'output_evaluation',
    'output-evaluation-report': 'output_evaluation_report',
    'capture': 'browser_capture',
    'comparisons': 'paired_evidence',
    'local-export': 'local_reserved',
    'preprocessing': 'browser_preprocess',
    'leads': 'dataset_leads',
    'replay-stop': 'replay_control',
    'specialists': 'vibe_specialist_round',
    'all-specialists': 'vibe_specialist_all',
    'mapped-vibes': 'mapped_vibe_round',
    'transfer-repair': 'transfer_component_repair',
    'candidates': 'candidate_followup',
    'product-baseline': 'product_baseline_audit',
    'cruise-package': 'cruise_export',
    'cruise-baselines': 'cruise_baselines',
    'cruise-audit': 'cruise_exposure',
    'cruise-production': 'cruise_production_replay',
    'cruise-wheel': 'cruise_package_check',
    'score-contract': 'suitability_experiment',
    'audio-evaluation': 'audio_evaluation',
    'audio-evaluation-report': 'audio_evaluation_report',
    'preprocessing-quality': 'preprocessing_quality',
    'preprocessing-quality-report': 'preprocessing_quality_report',
    'provider-pilot': 'provider_pilot',
    'evidence-audit': 'evidence_audit',
    'focus-references': 'focus_references',
    'focus': 'focus_experiment',
    'focus-mapping': 'focus_mapping_experiment',
    'focus-controls': 'focus_controls_experiment',
    'focus-package': 'focus_export',
    'focus-production': 'focus_production_replay',
    'focus-wheel': 'focus_package_check',
    'individual-local': 'individual_local',
    'individual': 'individual_experiment',
    'release': 'release_experiment',
    'relax': 'relax_experiment',
    'uplift': 'uplift_experiment',
    'energize': 'energize_experiment',
    'energize-package': 'energize_export',
    'energize-production': 'energize_production_replay',
    'energize-wheel': 'energize_package_check',
    'uplift-package': 'uplift_export',
    'uplift-production': 'uplift_production_replay',
    'uplift-wheel': 'uplift_package_check',
    'relax-package': 'relax_export',
    'relax-production': 'relax_production_replay',
    'relax-wheel': 'relax_package_check',
    'release-package': 'release_export',
    'release-production': 'release_production_replay',
    'release-wheel': 'release_package_check',
}
TESTS = (
    'test_vibe_pooling_metadata.py',
    'retained_copy/test_playback_feedback_backup.py',
    'test_representation_study.py',
    'test_focus_counts.py',
    'test_many_windows.py',
    'test_uplift_gate.py',
    'test_release_extension.py',
    'test_memo_confirmation.py',
    'test_encoder_models.py',
    'test_quality_round.py',
    'test_research_coordination.py',
    'test_output_evaluation.py',
    'test_external_checkpoint.py',
    'test_external_annotations.py',
    'test_external_propagation.py',
    'test_external_local_pool.py',
    'test_dataset_sources.py',
    'test_dataset_windows.py',
    'test_external_consistency.py',
    'test_external_calibration.py',
    'test_heldout_cohort.py',
    'test_library_replay.py',
    'test_vibe_specialists.py',
    'test_vibe_specialist_all.py',
    'test_mapped_vibes.py',
    'test_transfer_identity.py',
    'test_candidate_followup.py',
    'test_cruise_specialist.py',
    'test_cruise_baselines.py',
    'test_suitability_contract.py',
    'test_audio_evaluation.py',
    'test_preprocessing_quality.py',
    'test_provider_pilot.py',
    'test_evidence_versions.py',
    'test_focus_experiment.py',
    'test_focus_mapping.py',
    'test_focus_controls.py',
    'test_individual_experiment.py',
    'test_individual_results.py',
    'test_release_mapping.py',
    'test_release_results.py',
    'test_relax_consensus.py',
    'test_uplift_consensus.py',
    'test_uplift_replay.py',
    'test_energize_consensus.py',
)


def status(argv):
    parser = argparse.ArgumentParser(prog='./experiments/run.py status')
    parser.add_argument('--json', action='store_true', help='Print the saved checkpoint state in full')
    args = parser.parse_args(argv)
    path = CHECKPOINT / 'status.json'
    if not path.exists():
        print('No checkpoint state saved yet.')
        return
    state = json.loads(path.read_text())
    if args.json:
        print(json.dumps(state, indent=2))
        return
    print(f"Saved checkpoint: {state.get('state', 'unknown')}; active: {state.get('active') or 'none'}")
    print(f"Updated: {state.get('updated_at', 'unknown')}; PID: {state.get('pid', 'unknown')}")
    latest = {row['name']: row for row in state.get('steps', [])}
    print('Latest recorded stages (including earlier invocations):')
    for name, row in latest.items():
        print(f"  {name}: {row['state']}")
    active_dataset = {'deam': 'deam', 'emma': 'emma-preview-matched', 'merge': 'merge-quadrants'}.get(state.get('active'))
    progress_path = CHECKPOINT.parent / f'{active_dataset}.status.json'
    if active_dataset and progress_path.exists():
        progress = json.loads(progress_path.read_text())
        print(f"Saved extraction progress: {progress['completed']}/{progress['total']}; "
              f"errors: {progress['errors']}; updated: {progress.get('updated_at', 'unknown')}")
    print('Saved state only; this does not establish process liveness or validate cached outputs.')
    discovery = LOCAL / 'datasets/discovery'
    for name, directory, report in (
        ('ManyMusic', discovery / 'manymusic/rated-audio', discovery / 'manymusic/rated-audio-acquisition.json'),
        ('Memo2496', discovery / 'memo2496/pilot-audio', discovery / 'memo2496/pilot-acquisition.json'),
    ):
        files = list(directory.glob('*.mp3'))
        sidecars = sum(path.with_suffix('.json').exists() for path in files)
        print(f'{name}: {len(files)} MP3 files, {sidecars} acquisition records; '
              f"completion report {'present' if report.exists() else 'absent'}")
    pilot_path = CHECKPOINT / 'new-corpora/campaign-status.json'
    if pilot_path.exists():
        pilot = json.loads(pilot_path.read_text())
        print(f"Original pilot coordinator: {pilot['state']}; active: {pilot.get('active') or 'none'}; "
              f"updated: {pilot.get('updated_at', 'unknown')}")
    features_path = CHECKPOINT / 'new-corpora/features.json'
    if features_path.exists():
        features = json.loads(features_path.read_text())
        print(f"Saved aligned audio: {len(features['rows'])}/{features['total']}; "
              f"errors: {len(features['errors'])}; state: {features['state']}")
    transfer_path = CHECKPOINT / 'new-corpora/transfer-report.json'
    if transfer_path.exists():
        transfer = json.loads(transfer_path.read_text())
        directions = transfer.get('directions', {})
        print(f"Saved transfer artifacts: {transfer.get('state', 'unknown')}; "
              f"complete directions: {sum(row.get('state') == 'complete' for row in directions.values())}/{len(directions)}")
    completion_path = CHECKPOINT / 'completion/status.json'
    if completion_path.exists():
        completion = json.loads(completion_path.read_text())
        latest = {row['name']: row for row in completion['steps']}
        print(f"Remaining-comparison campaign: {completion['state']}; active: {completion.get('active') or 'none'}; "
              f"finished stages: {sum(row['state'] == 'complete' for row in latest.values())}; "
              f"updated: {completion.get('updated_at', 'unknown')}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Commands accept --help. Outputs stay under experiments/. '
        'Experiments and focused checks do not use the global-suite test lock.'))
    parser.add_argument('--log', type=Path, help='Append stdout/stderr to a file inside experiments/ (path relative to experiments/)')
    parser.add_argument('--runtime-snapshot', type=Path,
                        help='Use a checksummed experiments-only source snapshot for library replay')
    parser.add_argument('command', choices=[*MODULES, 'check', 'status', 'investigate'], help=(
        'checkpoint: resume selected campaign stages; acquire: inspect/download datasets; '
        'audit: join human labels to audio; reference: native/Node/browser comparison; '
        'windows: plan/extract aligned new-corpus pilots; '
        'pilot: check and evaluate the frozen pilot sequentially; '
        'evidence: verify completed outputs and consolidate results; '
        'complete: run the remaining pinned experiment comparisons; '
        'reserved: freeze models and evaluate the reserved local cohort; '
        'library: private cold/fresh preparation and paired consumer replay; '
        'capture: supplied-music browser capture parity; '
        'comparisons: paired uncertainty for historical predictions; '
        'local-export: freeze local-data ablations; preprocessing: compare captured audio decodes; '
        'leads: audit bounded publisher metadata and existing-audio intersections; '
        'replay-stop: interrupt only this checkout\'s active frozen paired replay; '
        'specialists: independently select native targets and each vibe; '
        'all-specialists: extend the fresh native comparison to all thirteen sources; '
        'mapped-vibes: external attributes, fixed vibe mappings and external evaluation; '
        'transfer-repair: preserve the frozen round and repair connected-identity exclusions; '
        'candidates: common-target Focus, activation, aggression and Cruise follow-up; '
        'product-baseline: exact shipped-artifact external comparison and read-only label inventory; '
        'cruise-package: compact Cruise artifact and mixed shared/individual replay; '
        'cruise-baselines: enlarged-corpus freeze and refreshed shared-model controls; '
        'cruise-audit: supplementary shared-native identity exposure audit; '
        'cruise-production: production inference and isolated real-capture preparation; '
        'cruise-wheel: verify bundled inference from the built wheel; '
        'score-contract: independent Cruise calibration and coverage evidence; '
        'audio-evaluation: pinned browser/reference preprocessing and resource checks for #424; '
        'audio-evaluation-report: verify completed browser/capture receipts; '
        'preprocessing-quality: fixed-model external-label comparison of browser and historical preparation; '
        'preprocessing-quality-report: lightweight input audit and completed quality results; '
        'check: focused research tests; status: saved progress'))
    parser.add_argument('args', nargs=argparse.REMAINDER, help='Arguments for the selected command')
    args = parser.parse_args(argv)
    if args.runtime_snapshot and args.command not in ('library', 'output-evaluation'):
        parser.error('--runtime-snapshot is supported only for library/output evaluation')
    os.chdir(ROOT)
    if args.log:
        path = (EXPERIMENTS / args.log).resolve()
        if not path.is_relative_to(EXPERIMENTS):
            parser.error('--log must resolve to a file inside experiments/')
        path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout.flush()
        sys.stderr.flush()
        with path.open('a') as stream:
            os.dup2(stream.fileno(), sys.stdout.fileno())
            os.dup2(stream.fileno(), sys.stderr.fileno())
    if args.command == 'status':
        status(args.args)
        return
    if args.command == 'investigate':
        if not args.args:
            parser.error('investigate requires one Python file from experiments/investigations/')
        path = (EXPERIMENTS / 'investigations' / args.args[0]).resolve()
        if (not path.is_relative_to(EXPERIMENTS / 'investigations')
                or path.suffix != '.py' or not path.is_file()):
            parser.error('investigate requires an existing Python file inside experiments/investigations/')
        executable = shutil.which('uv')
        if not executable:
            parser.error('uv is required for investigations')
        # An explicit path is a focused run. Keep root CLI options and isolated
        # test fixtures without selecting the ordinary suite or its lock.
        command = [executable, 'run', 'pytest', '-p', 'tests.conftest', str(path), *args.args[1:]]
    elif args.command == 'check':
        checks = argparse.ArgumentParser(prog='./experiments/run.py check', description=(
            'Run focused research checks independently of the global test-suite lock.'))
        checks.add_argument('--syntax-only', action='store_true', help='Parse research Python files without running tests or acquiring the machine lock')
        checks.add_argument('--test', choices=TESTS, action='append', help='Run only the named research test file; repeat for several files')
        options = checks.parse_args(args.args)
        if options.syntax_only:
            paths = [Path(__file__), *EXPERIMENTS.joinpath('audio_lab_tools').glob('*.py'),
                     *EXPERIMENTS.joinpath('retained_copy').glob('*.py'),
                     *EXPERIMENTS.joinpath('investigations').glob('*.py')]
            for path in paths:
                ast.parse(path.read_text(), filename=str(path))
            print(f'Parsed {len(paths)} research Python files; no tests executed.')
            return
        executable = shutil.which('uv')
        if not executable:
            parser.error('uv is required for focused checks')
        # Research checks keep the isolation fixtures, without loading the root
        # conftest's global-suite lock and worker orchestration.
        command = [executable, 'run', 'pytest', '--confcutdir=' + str(EXPERIMENTS), '-p', 'tests.conftest',
                   '-o', 'cache_dir=experiments/audio_lab/local/pytest-cache',
                   '--basetemp=experiments/audio_lab/local/pytest-checkpoint',
                   *(str(EXPERIMENTS / (name if name.startswith('retained_copy/') else
                                       'audio_lab_tools/' + name)) for name in (options.test or TESTS))]
    else:
        if args.command == 'reference':
            reference = argparse.ArgumentParser(prog='./experiments/run.py reference', description=(
                'Compare the frozen native, Node and browser inference pipelines.'))
            reference.add_argument('--fetch-only', action='store_true', help='Acquire pinned reference graph assets only')
            reference.parse_args(args.args)
        executable = str(ROOT / '.venv/bin/python')
        if not Path(executable).exists():
            parser.error('The checkout .venv/bin/python is required; use the existing project environment')
        command = [executable, '-B', '-m', 'experiments.audio_lab_tools.' + MODULES[args.command], *args.args]
        if args.runtime_snapshot:
            command = [executable, '-B', str(EXPERIMENTS / 'audio_lab_tools/replay_runtime.py'),
                       str(args.runtime_snapshot), 'experiments.audio_lab_tools.' + MODULES[args.command], *args.args]
    print('Running: ' + ' '.join(command), flush=True)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.execv(executable, command)


if __name__ == '__main__':
    main()
