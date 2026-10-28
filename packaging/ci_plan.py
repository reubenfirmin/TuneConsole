"""Plan candidates and reuse source checks only for the exact revision or verified merge tree."""
import json
import os
from datetime import datetime
from pathlib import Path
import re
import tempfile

from promote_release import Backend, classify


def successful_runs(backend, revision):
    page = 1
    while True:
        result = backend.api(f"repos/{backend.repository}/actions/workflows/ci.yml/runs?head_sha={revision}&status=success&per_page=100&page={page}")
        if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
            raise RuntimeError("Cannot verify prior source checks")
        for run in result["workflow_runs"]:
            if (run["head_sha"] == revision and run["status"] == "completed" and run["conclusion"] == "success"
                    and run["event"] in {"push", "workflow_dispatch"}):
                yield run
        if len(result["workflow_runs"]) < 100:
            return
        page += 1


def _pages(backend, path, field=None):
    page = 1
    while True:
        response = backend.api(f"{path}&per_page=100&page={page}")
        rows = response.get(field) if field and isinstance(response, dict) else response
        if not isinstance(rows, list):
            raise RuntimeError("Cannot verify merged PR source checks")
        yield from rows
        if len(rows) < 100:
            return
        page += 1


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _metrics(backend, run):
    """Read complete, attempt-specific suite proof from the authenticated Actions artifact."""
    attempt = run.get('run_attempt')
    if type(attempt) is not int or attempt < 1:
        return None
    name = f"source-test-run-metrics-{attempt}"
    artifacts = [row for row in _pages(backend,
        f"repos/{backend.repository}/actions/runs/{run['id']}/artifacts?", 'artifacts')
        if row.get('name') == name]
    if (len(artifacts) != 1 or artifacts[0].get('expired') is not False
            or artifacts[0].get('size_in_bytes', 0) <= 0):
        return None
    with tempfile.TemporaryDirectory(prefix='source-test-proof-') as directory:
        backend.run('gh', 'run', 'download', str(run['id']), '--repo', backend.repository,
                    '--name', name, '--dir', directory)
        try:
            report = json.loads((Path(directory) / 'ci-test-metrics.json').read_text())
        except (OSError, ValueError):
            return None
    if not isinstance(report, dict):
        return None
    if (report.get('complete') is not True or str(report.get('run_id')) != str(run['id'])
            or str(report.get('run_attempt')) != str(attempt)
            or report.get('head_revision') != run['head_sha'] or not _sha(report.get('revision'))
            or type(report.get('shards')) is not int or report['shards'] not in (1, 2, 4)):
        return None
    suites = report.get('suites')
    if not isinstance(suites, dict) or set(suites) != {'unit', 'browser'}:
        return None
    for name, suite in suites.items():
        if (not isinstance(suite, dict) or suite.get('suite') != name
                or suite.get('revision') != report['revision'] or suite.get('shards') != report['shards']
                or type(suite.get('cases')) is not int or suite['cases'] < 1
                or not isinstance(suite.get('case_digest'), str)
                or re.fullmatch(r'[0-9a-f]{64}', suite['case_digest']) is None):
            return None
    return report


def merged_pr_checks(backend, revision):
    """Keep the tested synthetic merge identity and prove its full tree equals the landed tree."""
    repository = backend.repository
    landed = None
    for pr in _pages(backend, f'repos/{repository}/commits/{revision}/pulls?'):
        base, head = pr.get('base') or {}, pr.get('head') or {}
        if (not pr.get('merged_at') or pr.get('merge_commit_sha') != revision
                or base.get('ref') != 'main' or (base.get('repo') or {}).get('full_name') != repository
                or (head.get('repo') or {}).get('full_name') != repository
                or not _sha(base.get('sha')) or not _sha(head.get('sha'))):
            continue
        for run in _pages(backend,
                f"repos/{repository}/actions/workflows/ci.yml/runs?head_sha={head['sha']}&event=pull_request&status=success",
                'workflow_runs'):
            if (run.get('event') != 'pull_request' or run.get('status') != 'completed'
                    or run.get('conclusion') != 'success' or run.get('head_sha') != head['sha']
                    or run.get('path') != '.github/workflows/ci.yml'
                    or (run.get('head_repository') or {}).get('full_name') != repository
                    or type(run.get('id')) is not int or run['id'] < 1):
                continue
            report = _metrics(backend, run)
            if report is None:
                continue
            tested = backend.api(f"repos/{repository}/git/commits/{report['revision']}")
            if landed is None:
                landed = backend.api(f'repos/{repository}/git/commits/{revision}')
            if not isinstance(tested, dict) or not isinstance(landed, dict):
                continue
            tree = (tested.get('tree') or {}).get('sha')
            if (tested.get('sha') == report['revision'] and landed.get('sha') == revision
                    and _sha(tree) and (landed.get('tree') or {}).get('sha') == tree
                    and [parent.get('sha') for parent in tested.get('parents', [])] == [base['sha'], head['sha']]):
                return {'checked_run': str(run['id']), 'checked_revision': report['revision'],
                        'checked_tree': tree, 'checked_pr': str(pr['number'])}
    return None


def plan(backend, tag, revision, event, ref):
    result = {"version": "0.0.0.dev0", "tag": "", "reuse_tests": "false", "checked_run": "",
              "checked_revision": "", "checked_tree": "", "checked_pr": ""}
    if tag:
        if event != "workflow_dispatch" or ref != "refs/heads/main":
            raise ValueError("Release candidates require a trusted manual CI run on main")
        result.update(version=classify(tag).version, tag=tag)
    if ref == 'refs/heads/main' and (event == 'push' or (event == 'workflow_dispatch' and tag)):
        previous = next(successful_runs(backend, revision), None)
        if previous:
            result.update(reuse_tests="true", checked_run=str(previous["id"]), checked_revision=revision)
        else:
            proof = merged_pr_checks(backend, revision)
            if proof:
                result.update(reuse_tests='true', **proof)
    return result


def timing_source(backend, baseline=None):
    """Pin one trusted timing artifact for every shard, even if CI finishes mid-run."""
    if baseline is None:
        baseline = json.loads((Path(__file__).resolve().parents[1] / '.github/test-durations.json').read_text())
    refreshed = baseline.get('refreshed_at')
    refreshed_at = datetime.fromisoformat(refreshed) if refreshed else None
    result = backend.api(f"repos/{backend.repository}/actions/workflows/ci.yml/runs?branch=main&status=success&per_page=20")
    if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
        raise RuntimeError("Cannot read the shard timing baseline")
    for run in result["workflow_runs"]:
        if (run["head_branch"] != "main" or run["status"] != "completed" or run["conclusion"] != "success"
                or run["event"] not in {"push", "workflow_dispatch"}):
            continue
        # An older main artifact must not replace a reviewed, newer weight
        # refresh from complete raw reports. Use main again once it catches up.
        started = run.get('run_started_at')
        if refreshed_at and (not started or datetime.fromisoformat(started) <= refreshed_at):
            continue
        artifacts = backend.api(f"repos/{backend.repository}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
        matches = [artifact for artifact in artifacts
                   if artifact["name"] == f"source-test-timings-{run['run_attempt']}"
                   and not artifact["expired"] and artifact["size_in_bytes"] > 0]
        if len(matches) == 1:
            return {"timing_run": str(run["id"]), "timing_artifact": str(matches[0]["id"])}
    return {"timing_run": "", "timing_artifact": ""}


if __name__ == "__main__":
    backend = Backend(os.environ["GITHUB_REPOSITORY"])
    result = plan(backend, os.environ.get("RELEASE_TAG", ""),
                  os.environ["GITHUB_SHA"], os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"])
    result.update(timing_source(backend))
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write("".join(f"{name}={value}\n" for name, value in result.items()))
    if result['reuse_tests'] == 'true':
        proof = {'revision': os.environ['GITHUB_SHA'], **result}
        Path('source-test-reuse.json').write_text(json.dumps(proof, indent=2) + '\n')
        if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(summary, 'a') as stream:
                stream.write(f"Source checks reused from https://github.com/{backend.repository}/actions/runs/{result['checked_run']}\n\n"
                             f"Tested revision: `{result['checked_revision']}`. Landed revision: `{proof['revision']}`. "
                             + (f"Identical full tree: `{result['checked_tree']}` (merged PR #{result['checked_pr']}).\n"
                                if result['checked_tree'] else 'Exact revision match.\n'))
    print(json.dumps(result))
