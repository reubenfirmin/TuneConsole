"""Evidence utilities for the bounded November #136 research round."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

from .external_experiment import DATA, LAB, digest, save, stamp
from .focus_experiment import inside, load, runtime

EXPERIMENTS = LAB.parent
RESERVED = LAB / 'local/quality-20261101/memo-reservation.json'


def reserve_memo():
    """Reserve filenames without opening their emotion annotations."""
    if RESERVED.exists():
        value = load(RESERVED)
        for name, checksum in value['references'].items():
            if digest(name) != checksum:
                raise ValueError('Memo reservation input changed: ' + name)
        return value
    root = DATA / 'discovery/memo2496'
    paths = [root / 'archive-members.json', root / 'pilot-selection.json']
    old = load(paths[1])['members']
    names = {r['name'] for r in old}
    signatures = {(r['bytes'], r['crc32']) for r in old}
    available = [r for r in load(paths[0])['members']
                 if Path(r['name']).name == r['name'] and r['name'].endswith('.mp3')
                 and r['name'] not in names and (r['bytes'], r['crc32']) not in signatures]
    available.sort(key=lambda r: hashlib.sha256(('memo-confirmation-710-v1/' + r['name']).encode()).hexdigest())
    chosen, total = [], 0
    for row in available:
        if len(chosen) == 128 or total + row['bytes'] > 1024 ** 3:
            break
        chosen.append(row)
        total += row['bytes']
    value = {'created_at': stamp(), 'ticket': 710,
        'rule': 'Ascending SHA256(memo-confirmation-710-v1/filename); no target outcomes inspected.',
        'references': {str(p): digest(p) for p in paths}, 'old_pilot_names': sorted(names),
        'selected': chosen, 'uncompressed_audio_bytes': total,
        'reserved_from_other_experiments': 'Every nonpilot Memo recording, including rejected/backfill candidates. Only the original pilot may enter training, selection or calibration.',
        'limits': 'Filename/member CRC-size screening is provisional. Verify audio hashes and known identity components after acquisition; artist independence remains unknown.'}
    save(RESERVED, value)
    return value


def freeze(directory, ticket, specification, inputs, references=(), documents=()):
    directory = inside(directory)
    if directory.exists():
        raise ValueError('Preserve prior rounds; choose a new directory')
    reserve_memo()
    directory.mkdir(parents=True)
    copied = {}
    for name, path in inputs.items():
        path = Path(path)
        target = directory / 'inputs' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied[str(target.relative_to(directory))] = digest(target)
    paths = {Path(m.__file__).resolve() for name, m in list(sys.modules.items())
             if name.startswith('experiments.audio_lab_tools.') and getattr(m, '__file__', None)}
    entrypoint = getattr(sys.modules.get('__main__'), '__file__', None)
    if entrypoint and EXPERIMENTS in Path(entrypoint).resolve().parents:
        paths.add(Path(entrypoint).resolve())
    paths.update([EXPERIMENTS / 'run.py', *documents])
    code = {}
    for path in sorted(paths):
        name = str(path.relative_to(EXPERIMENTS))
        target = directory / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        code[name] = digest(path)
    plan = {'created_at': stamp(), 'ticket': ticket, 'runtime': runtime(),
            'protocol': specification, 'inputs': copied, 'code': code,
            'references': {str(p): digest(p) for p in [*inputs.values(), *references, RESERVED]},
            'exposure': 'Previously inspected populations are exploratory diagnostics; #710 alone reserves additional Memo recordings for evaluation.'}
    save(directory / 'plan.json', plan)
    validate(directory, execution=True)
    return plan


def validate(directory, execution=False):
    plan = load(directory / 'plan.json')
    if execution and runtime() != plan['runtime']:
        raise ValueError('Runtime changed since freeze')
    for name, checksum in plan['inputs'].items():
        if digest(directory / name) != checksum:
            raise ValueError('Frozen input changed: ' + name)
    for name, checksum in plan['code'].items():
        if digest(directory / 'source' / name) != checksum:
            raise ValueError('Archived code changed: ' + name)
        if execution and digest(EXPERIMENTS / name) != checksum:
            raise ValueError('Executing code changed; freeze a new round: ' + name)
    for name, checksum in plan['references'].items():
        if digest(name) != checksum:
            raise ValueError('Referenced evidence changed: ' + name)
    return plan


def seal(directory, stage, paths):
    save(directory / (stage + '.receipt.json'), {'created_at': stamp(),
         'plan_sha256': digest(directory / 'plan.json'),
         'outputs': {str(p.relative_to(directory)): digest(p) for p in paths}})


def verify(directory, *, required_stages=None, optional_stages=()):
    validate(directory)
    receipts = list(directory.glob('*.receipt.json'))
    if not receipts:
        raise ValueError('No completed stages')
    if required_stages is not None:
        actual = {p.name.removesuffix('.receipt.json') for p in receipts}
        required = set(required_stages)
        unexpected = actual - required - set(optional_stages)
        if required - actual or unexpected:
            raise ValueError(f'Receipt stages differ: missing={sorted(required - actual)}, '
                             f'unexpected={sorted(unexpected)}')
    from .focus_experiment import check_receipt
    for path in receipts:
        check_receipt(directory, path.name)
    return {'plan_sha256': digest(directory / 'plan.json'), 'receipts': len(receipts)}


def archive_current_source(directory, sources):
    """Preserve reproduction code without claiming it was frozen prospectively."""
    directory = inside(directory)
    plan = validate(directory)
    if (directory / 'postrun-source.receipt.json').exists():
        raise ValueError('Preserve the existing post-run source record')
    records, outputs = [], []
    for source in sources:
        source = inside(source)
        name = str(source.relative_to(EXPERIMENTS))
        target = directory / 'postrun-source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        checksum = digest(target)
        records.append({'name': name, 'sha256': checksum,
            'prospective_archive_sha256': plan['code'].get(name),
            'matches_prospective_archive': plan['code'].get(name) == checksum})
        outputs.append(target)
    manifest = directory / 'postrun-source.json'
    save(manifest, {'captured_at': stamp(), 'original_plan_sha256': digest(directory / 'plan.json'),
        'notice': 'Post-run reproduction sources. These copies do not establish missing prospective entrypoint hashes or retroactively complete the original source freeze. Original plans, source archives and measured outputs remain unchanged.',
        'sources': records})
    seal(directory, 'postrun-source', [manifest, *outputs])
    print(json.dumps({'directory': str(directory), 'sources': records}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['reserve-memo', 'archive-current-source'])
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--source', type=Path, action='append')
    args = parser.parse_args()
    if args.action == 'archive-current-source':
        if args.directory is None or not args.source:
            parser.error('archive-current-source requires --directory and --source')
        archive_current_source(args.directory, args.source)
        return
    value = reserve_memo()
    print(json.dumps({'reservation': str(RESERVED), 'sha256': digest(RESERVED),
                      'recordings': len(value['selected']), 'audio_bytes': value['uncompressed_audio_bytes']}))


if __name__ == '__main__':
    main()
