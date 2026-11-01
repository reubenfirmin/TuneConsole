"""Verify and stage the complete audio runtime for a production extension build."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil


MODEL = Path(__file__).resolve().parents[2] / 'extension/audio_model'
SCRIPTS = ('model_worker.js', 'runtime_loader.js', 'features.js', 'embedding_graph.js', 'audio_attributes.js')
HEADS = ('relaxed', 'aggressive', 'happy', 'sad', 'deam')
VENDOR_FILES = frozenset({
    'tf.min.js', 'meyda.min.js', 'meyda-LICENSE', 'essentia-LICENSE', 'model-LICENSE',
} | {f'{head}/{name}' for head in ('musicnn', *HEADS)
     for name in ('model.json', 'group1-shard1of1.bin', 'metadata.json')})


def verified_files(source):
    source = Path(source)
    vendor = source / 'vendor'
    bundle = json.loads((vendor / 'bundle.json').read_text())
    runtime = json.loads((vendor / 'runtime.json').read_text())
    if (bundle.get('schema') != 1 or runtime.get('schema') != 1
            or {head['id'] for head in bundle.get('mood_heads', [])} != set(HEADS)
            or bundle.get('meyda_version') != '5.6.3'
            or runtime.get('runtime') != 'essentia-csp-audio-2'
            or '-sDYNAMIC_EXECUTION=0' not in runtime.get('flags', [])):
        raise ValueError('Incomplete or incompatible audio model manifests')
    required = [(bundle, VENDOR_FILES), (runtime, {'essentia-csp.js'})]
    files = {'vendor/bundle.json', 'vendor/runtime.json'}
    for manifest, names in required:
        rows = {row['path']: row for row in manifest['files']}
        missing = names - rows.keys()
        if missing:
            raise ValueError('Missing required audio artifacts: ' + ', '.join(sorted(missing)))
        for name, row in rows.items():
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or not path.parts:
                raise ValueError('Invalid audio artifact path: ' + name)
            artifact = vendor / name
            if artifact.is_symlink() or not artifact.is_file():
                raise ValueError('Missing audio artifact: ' + name)
            data = artifact.read_bytes()
            if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                raise ValueError('Audio artifact changed: ' + name)
            files.add('vendor/' + name)
    workers = []
    for name in SCRIPTS:
        data = (source / name).read_bytes()
        if not data:
            raise ValueError('Empty audio worker source: ' + name)
        files.add(name)
        workers.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return files, {**bundle, 'purpose': 'tuneconsole_audio', 'worker_sources': workers}


def prepare(source=MODEL, destination=None):
    """Validate every artifact before replacing a staged runtime; no downloads."""
    source = Path(source)
    files, bundle = verified_files(source)
    if destination is not None:
        destination = Path(destination)
        if destination.resolve() == source.resolve():
            raise ValueError('Stage the audio runtime in a separate destination')
        if destination.exists():
            shutil.rmtree(destination)
        for name in sorted(files):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        (destination / 'vendor/bundle.json').write_text(json.dumps(bundle, indent=2) + '\n')
        runtime_path = destination / 'vendor/runtime.json'
        runtime = json.loads(runtime_path.read_text())
        runtime['purpose'] = 'tuneconsole_audio'
        runtime_path.write_text(json.dumps(runtime, indent=2) + '\n')
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, help='Stage verified runtime files here')
    args = parser.parse_args()
    try:
        files = prepare(destination=args.destination)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Cannot package audio models: {error}\n'
                    'Prepare the required assets using scripts/audio_model/README.md.\n')
    print(f'Verified {len(files)} audio runtime and model files.')


if __name__ == '__main__':
    main()
