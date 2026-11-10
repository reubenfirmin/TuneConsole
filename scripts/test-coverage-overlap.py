#!/usr/bin/env python3
"""Summarize observed per-test line overlap without an all-pairs comparison."""
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory


# These tests execute application workflows in explicitly owned processes.
# An outer end receipt alone cannot establish their Python coverage.
CHILD_WORKFLOWS = {
    ('test_road_trip_browser.py', 'test_build_curate_then_save_road_trip_recipe'),
    ('test_road_trip_browser.py', 'test_journey_deadline_keeps_blocked_http_and_worker_diagnostics'),
    ('test_recap_browser.py', 'test_recap_keeps_packaged_server_alive_through_download_and_exit'),
    ('test_recap_browser.py', 'test_closing_one_ui_tab_preserves_the_other'),
}


def load_coverage(path, *, scratch=None):
    with path.open("rb") as stream:
        is_database = stream.read(16) == b"SQLite format 3\0"
    if not is_database:
        return json.loads(path.read_text())
    from coverage import CoverageData

    # Inspect a consistent read-only snapshot. CoverageData may initialize its
    # schema while reading; recovery must never change the original evidence.
    with TemporaryDirectory(prefix='tuneconsole-coverage-read-', dir=scratch) as directory:
        snapshot = Path(directory) / 'coverage.sqlite3'
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            integrity = source.execute('PRAGMA integrity_check(10)').fetchall()
            if integrity != [('ok',)]:
                raise ValueError(f'SQLite integrity check failed: {integrity}')
            if (len(source.execute('SELECT version FROM coverage_schema').fetchall()) != 1
                    or source.execute('PRAGMA foreign_key_check').fetchall()):
                raise ValueError('Invalid coverage schema or references')
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
        data = CoverageData(basename=str(snapshot))
        try:
            data.read()
            return {"files": {name: {"contexts": data.contexts_by_lineno(name)}
                              for name in sorted(data.measured_files())}}
        finally:
            data.close()


def load_inputs(paths, *, scratch=None):
    from coverage.exceptions import CoverageException

    paths = list(dict.fromkeys(path.resolve() for path in paths))
    files, accepted, excluded = {}, [], []
    for path in paths:
        try:
            data = load_coverage(path, scratch=scratch)
            # Validate each complete input before admitting any of its lines.
            if not isinstance(data, dict) or not isinstance(data.get('files'), dict):
                raise ValueError('Invalid coverage files')
            for name, values in data['files'].items():
                if (not isinstance(name, str) or not isinstance(values, dict)
                        or not isinstance(values.get('contexts', {}), dict)):
                    raise ValueError('Invalid file contexts')
                for line, contexts in values.get('contexts', {}).items():
                    if (int(line) < 0 or not isinstance(contexts, list)
                            or not all(isinstance(context, str) for context in contexts)):
                        raise ValueError('Invalid line contexts')
        except (OSError, sqlite3.DatabaseError, CoverageException, ValueError, KeyError, TypeError) as error:
            excluded.append({'path': str(path), 'error': f'{type(error).__name__}: {error}'})
            continue
        accepted.append(str(path))
        if len(paths) == 1:
            return data, {'accepted': accepted, 'excluded': [], 'all_inputs_valid': True}
        for name, values in data['files'].items():
            for line, contexts in values.get('contexts', {}).items():
                files.setdefault(name, {'contexts': {}})['contexts'].setdefault(str(int(line)), set()).update(contexts)
    for values in files.values():
        values['contexts'] = {line: sorted(contexts) for line, contexts in values['contexts'].items()}
    return {'files': files}, {'accepted': accepted, 'excluded': excluded,
                             'all_inputs_valid': not excluded}


def summarize(coverage, progress=(), *, phase="run", neighbors=0):
    tests = {}
    fingerprints = {}
    bitmaps = {}
    bitmap_bytes = (sum(len(data.get("contexts", {})) for data in coverage["files"].values()) + 7) // 8
    def test(node):
        if node not in tests:
            tests[node] = {"nodeid": node, "lines": 0, "exclusive_lines": 0,
                           "seconds": {}, "outcomes": {}, "started": False, "ended": False}
            fingerprints[node] = hashlib.sha256()
        return tests[node]

    terminal = None
    for event in progress:
        node = event.get("nodeid")
        if node:
            row = test(node)
            if event["event"] == "start":
                row["started"] = True
            elif event["event"] == "end":
                row["ended"] = True
            elif event["event"] == "phase":
                row["seconds"][event["phase"]] = event["seconds"]
                row["outcomes"][event["phase"]] = event["outcome"]
        elif event["event"] == "finish":
            terminal = event["exitstatus"]

    observed_lines = shared_lines = 0
    workflow_run_lines = defaultdict(int)
    for filename, data in sorted(coverage["files"].items()):
        for line, contexts in sorted(data.get("contexts", {}).items(), key=lambda pair: int(pair[0])):
            owners = set()
            run_owners = set()
            for context in contexts:
                node, separator, when = context.rpartition("|")
                if separator and node and when == 'run':
                    run_owners.add(node)
                if separator and node and when in ("setup", "run", "teardown") and (phase == "all" or when == phase):
                    owners.add(node)
            if 'yt_playlist' in Path(filename).parts:
                for node in run_owners:
                    workflow_run_lines[node] += 1
            if not owners:
                continue
            observed_lines += 1
            shared_lines += len(owners) > 1
            unit = json.dumps([filename, int(line)], separators=(",", ":")).encode() + b"\n"
            for node in owners:
                row = test(node)
                row["lines"] += 1
                row["exclusive_lines"] += len(owners) == 1
                fingerprints[node].update(unit)
                if neighbors:
                    if node not in bitmaps:
                        bitmaps[node] = bytearray(bitmap_bytes)
                    byte, bit = divmod(observed_lines - 1, 8)
                    bitmaps[node][byte] |= 1 << bit

    groups = defaultdict(list)
    for node, row in tests.items():
        row["shared_lines"] = row["lines"] - row["exclusive_lines"]
        row["total_seconds"] = sum(row["seconds"].values())
        row["selected_seconds"] = (row["total_seconds"] if phase == "all" else
                                   row["seconds"].get("call" if phase == "run" else phase, 0))
        if row["lines"]:
            groups[fingerprints[node].hexdigest()].append(node)
    required = sorted(node for node in tests if (
        Path(node.split('::', 1)[0]).name, node.rsplit('::', 1)[-1].split('[', 1)[0]) in CHILD_WORKFLOWS)
    gaps = [node for node in required if not workflow_run_lines[node]]
    incomplete = [node for node in required if not tests[node]['ended']]
    identical = [sorted(nodes) for nodes in groups.values() if len(nodes) > 1]
    identical_costs = []
    for nodes in identical:
        rows = [tests[node] for node in nodes]
        seconds = sum(row["selected_seconds"] for row in rows)
        identical_costs.append({
            "tests": sorted(nodes), "lines": rows[0]["lines"],
            "selected_seconds": seconds,
            "seconds_above_fastest": seconds - min(row["selected_seconds"] for row in rows),
        })
    files = {}
    for row in tests.values():
        filename = row["nodeid"].split("::", 1)[0]
        aggregate = files.setdefault(filename, {
            "file": filename, "tests": 0, "total_seconds": 0,
            "setup_seconds": 0, "call_seconds": 0, "teardown_seconds": 0,
            "tests_with_exclusive_lines": 0,
        })
        aggregate["tests"] += 1
        aggregate["total_seconds"] += row["total_seconds"]
        aggregate["tests_with_exclusive_lines"] += row["exclusive_lines"] > 0
        for when in ("setup", "call", "teardown"):
            aggregate[when + "_seconds"] += row["seconds"].get(when, 0)
    nearest = []
    if neighbors:
        masks = {node: int.from_bytes(bitmap, "little") for node, bitmap in bitmaps.items()}
        bitmaps.clear()
        expensive = sorted((row for row in tests.values() if row["lines"]),
                           key=lambda row: -row["selected_seconds"])[:neighbors]
        for row in expensive:
            matches = []
            for node, mask in masks.items():
                if node == row["nodeid"]:
                    continue
                intersection = (masks[row["nodeid"]] & mask).bit_count()
                if intersection:
                    other = tests[node]
                    matches.append({
                        "nodeid": node, "intersection_lines": intersection,
                        "target_fraction": intersection / row["lines"],
                        "jaccard": intersection / (row["lines"] + other["lines"] - intersection),
                        "selected_seconds": other["selected_seconds"],
                    })
            matches.sort(key=lambda match: (-match["target_fraction"], -match["jaccard"],
                                            match["selected_seconds"], match["nodeid"]))
            nearest.append({"nodeid": row["nodeid"], "lines": row["lines"],
                            "selected_seconds": row["selected_seconds"], "nearest": matches[:5]})
    return {
        "scope": "Observed Python source lines only; equal coverage does not establish equal assertions or behavior.",
        "phase": phase, "exitstatus": terminal,
        "child_workflow_coverage": {
            'required': required, 'without_run_phase_lines': gaps,
            'without_end_receipt': incomplete,
            'run_lines': {node: workflow_run_lines[node] for node in required},
            'complete': not gaps and not incomplete,
            'scope': 'Observed child-workflow lines; outer outcomes and coverage completeness are separate.'},
        "observed_tests": len(tests), "ended_tests": sum(row["ended"] for row in tests.values()),
        "observed_lines": observed_lines, "shared_lines": shared_lines,
        "identical_line_groups": sorted(identical, key=lambda nodes: (-len(nodes), nodes)),
        "identical_line_group_costs": sorted(identical_costs,
                                            key=lambda row: -row["seconds_above_fastest"]),
        "files": sorted(files.values(), key=lambda row: -row["total_seconds"]),
        "expensive_test_neighbors": nearest,
        "tests": sorted(tests.values(), key=lambda row: (-row["selected_seconds"], -row["total_seconds"], row["nodeid"])),
    }


def audit_collection(report, nodeids):
    expected = set(nodeids)
    rows = {row["nodeid"]: row for row in report["tests"]}
    ended = {node for node, row in rows.items() if row["ended"]}
    started = {node for node, row in rows.items() if row["started"]}
    return {
        "expected_tests": len(expected), "ended_tests": len(expected & ended),
        "complete": expected <= ended and not (set(rows) - expected),
        "not_started": sorted(expected - started),
        "incomplete": sorted((expected & started) - ended),
        "unexpected": sorted(set(rows) - expected),
        "without_selected_phase_lines": sorted(node for node in expected & ended if not rows[node]["lines"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage", type=Path, help=".coverage database or coverage json --show-contexts output")
    parser.add_argument('--coverage-input', action='append', type=Path, default=[], metavar='PATH',
                        help='Recover additional stopped-run inputs; identify and exclude invalid inputs, returning exit 1 for partial recovery')
    parser.add_argument("--progress", type=Path, help="JSONL from pytest --test-progress")
    parser.add_argument("--collection", type=Path,
                        help="Collection JSONL receipt containing the exact nodeids to audit")
    parser.add_argument("--phase", choices=("run", "setup", "teardown", "all"), default="run")
    parser.add_argument("--neighbors", type=int, default=0, metavar="N",
                        help="Compare the N most expensive tests with the other tests' measured line sets")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evidence = [args.coverage, *args.coverage_input, args.progress, args.collection]
    if args.output.resolve() in {path.resolve() for path in evidence if path is not None}:
        parser.error('Output must differ from coverage, progress and collection inputs')
    progress = []
    if args.progress:
        lines = args.progress.read_text().splitlines(keepends=True)
        # A killed append may leave its last receipt incomplete. Earlier
        # malformed records remain errors rather than silently disappearing.
        progress = [json.loads(line) for i, line in enumerate(lines)
                    if line.endswith("\n") or i < len(lines) - 1]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    coverage, inputs = load_inputs([args.coverage, *args.coverage_input], scratch=args.output.parent)
    if inputs['excluded'] and not args.coverage_input:
        parser.error(f"Invalid coverage input: {inputs['excluded']}")
    if not any(context.endswith(("|setup", "|run", "|teardown")) and context.rsplit("|", 1)[0]
               for data in coverage["files"].values()
               for contexts in data.get("contexts", {}).values() for context in contexts) and not inputs['excluded']:
        parser.error("Coverage has no contexts; use --cov-context=test and coverage json --show-contexts")
    if args.neighbors < 0:
        parser.error("--neighbors must be nonnegative")
    report = summarize(coverage, progress, phase=args.phase, neighbors=args.neighbors)
    report['coverage_inputs'] = inputs
    if inputs['excluded']:
        report['scope'] += ' Invalid inputs were excluded; missing coverage remains unknown and cannot be certified.'
    if args.collection:
        receipts = [json.loads(line) for line in args.collection.read_text().splitlines()]
        collections = [row for row in receipts if row["event"] == "collection"]
        if len(collections) != 1 or "nodeids" not in collections[0]:
            parser.error("Collection must contain exactly one collection receipt with nodeids")
        report["collection_audit"] = audit_collection(report, collections[0]["nodeids"])
    # A failed diagnostic write must not truncate a previous valid report.
    with TemporaryDirectory(prefix='.coverage-report-', dir=args.output.parent) as directory:
        pending = Path(directory) / 'report.json'
        pending.write_text(json.dumps(report, indent=2) + "\n")
        pending.replace(args.output)
    print(f"{report['observed_tests']} observed tests, {report['ended_tests']} ended; "
          f"{report['shared_lines']}/{report['observed_lines']} observed lines shared "
          f"in {report['phase']} contexts; {len(report['identical_line_groups'])} identical line groups")
    child = report['child_workflow_coverage']
    for node in sorted(set(child['without_run_phase_lines']) | set(child['without_end_receipt'])):
        print(f'Child workflow coverage gap: {node}; '
              f'production run lines={child["run_lines"][node]}, '
              f'end receipt={node not in child["without_end_receipt"]}')
    for row in report["tests"][:20]:
        print(f"{row['selected_seconds']:8.3f}s {report['phase']} / {row['total_seconds']:.3f}s total  "
              f"{row['shared_lines']:5}/{row['lines']:<5} shared  "
              f"{row['exclusive_lines']:4} exclusive  {row['nodeid']}")
    print(f"Report: {args.output}")
    if inputs['excluded']:
        return 1 if inputs['accepted'] else 2
    return 1 if report['child_workflow_coverage']['complete'] is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
