"""Exact-revision test reuse, final-version provenance and honest release timings."""
import importlib
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
ci_plan = importlib.import_module("ci_plan")
provenance = importlib.import_module("candidate_provenance")
candidates = importlib.import_module("release_candidates")
timing = importlib.import_module("release_timing")


class Source:
    repository = "owner/repo"

    def __init__(self, sha="a" * 40, event="push", conclusion="success"):
        self.prior = {"id": 123, "head_sha": sha, "event": event, "status": "completed", "conclusion": conclusion}

    def api(self, path):
        if '/pulls?' in path:
            return []
        return {"workflow_runs": [self.prior]}


@pytest.mark.parametrize("sha,event,conclusion,reuse", [
    ("a" * 40, "push", "success", "true"),
    ("a" * 40, "workflow_dispatch", "success", "true"),
    ("b" * 40, "push", "success", "false"),
    ("a" * 40, "pull_request", "success", "false"),
    ("a" * 40, "push", "failure", "false"),
])
def test_preparation_reuses_only_success_for_this_exact_source(sha, event, conclusion, reuse):
    result = ci_plan.plan(Source(sha, event, conclusion), "v1.2.3", "a" * 40, "workflow_dispatch", "refs/heads/main")
    assert result["version"] == "1.2.3"
    assert result["reuse_tests"] == reuse


def test_final_version_preparation_cannot_receive_secrets_on_a_pr():
    with pytest.raises(ValueError, match="trusted manual CI"):
        ci_plan.plan(Source(), "v1.2.3", "a" * 40, "pull_request", "refs/pull/1/merge")


class MergedSource:
    repository = 'owner/repo'
    landed, tested, base, head, tree = (character * 40 for character in 'abcde')

    def __init__(self):
        self.pr = {'number': 351, 'merged_at': '2026-10-24T12:00:00Z', 'merge_commit_sha': self.landed,
                   'base': {'ref': 'main', 'sha': self.base, 'repo': {'full_name': self.repository}},
                   'head': {'sha': self.head, 'repo': {'full_name': self.repository}}}
        self.run_info = {'id': 456, 'head_sha': self.head, 'event': 'pull_request', 'status': 'completed',
                         'conclusion': 'success', 'path': '.github/workflows/ci.yml', 'run_attempt': 2,
                         'head_repository': {'full_name': self.repository}}
        self.artifact = {'name': 'source-test-run-metrics-2', 'expired': False, 'size_in_bytes': 100}
        self.report = {'complete': True, 'run_id': '456', 'run_attempt': '2', 'head_revision': self.head,
                       'revision': self.tested, 'shards': 4,
                       'suites': {suite: {'suite': suite, 'revision': self.tested, 'shards': 4,
                                          'cases': 123, 'case_digest': 'f' * 64}
                                  for suite in ('unit', 'browser')}}
        self.commits = {self.landed: {'sha': self.landed, 'tree': {'sha': self.tree}},
                        self.tested: {'sha': self.tested, 'tree': {'sha': self.tree},
                                      'parents': [{'sha': self.base}, {'sha': self.head}]}}
        self.downloads = 0
        # A platform-only retry clones the source jobs under new IDs and a new
        # run_attempt, while retaining the actual execution and step timestamps.
        roles = ['plan', 'coverage', 'checks'] + [
            f'{suite} ({index}/4)' for suite in ('unit', 'browser') for index in range(1, 5)]
        jobs = [{'name': role, 'status': 'completed', 'conclusion': 'success',
                 'started_at': '2026-10-24T12:00:00Z', 'completed_at': '2026-10-24T12:05:00Z',
                 'steps': ([{'name': 'Run the unit / route suite' if role.startswith('unit')
                                    else 'Run the live-browser suite',
                            'status': 'completed', 'conclusion': 'success',
                            'started_at': '2026-10-24T12:01:00Z',
                            'completed_at': '2026-10-24T12:04:00Z'}]
                           if role.startswith(('unit', 'browser')) else [])}
                for role in roles]
        self.job_history = {attempt: [dict(deepcopy(job), id=attempt * 100 + index, run_attempt=attempt)
                                     for index, job in enumerate(jobs)] for attempt in (1, 2)}
        self.job_queries = []

    def api(self, path):
        if '/pulls?' in path:
            return [self.pr]
        if '/artifacts?' in path:
            return {'artifacts': [self.artifact]}
        if '/attempts/' in path and '/jobs?' in path:
            attempt = int(path.split('/attempts/')[1].split('/')[0])
            self.job_queries.append(attempt)
            return {'jobs': self.job_history[attempt]}
        if '/git/commits/' in path:
            return self.commits[path.rsplit('/', 1)[1]]
        assert '/actions/workflows/ci.yml/runs?' in path
        return {'workflow_runs': [self.run_info] if 'event=pull_request' in path else []}

    def run(self, *args):
        assert args[:3] == ('gh', 'run', 'download')
        assert args[args.index('--name') + 1] == self.artifact['name']
        self.downloads += 1
        (Path(args[args.index('--dir') + 1]) / 'ci-test-metrics.json').write_text(json.dumps(self.report))


@pytest.mark.parametrize('tag,event', [('', 'push'), ('v1.2.3', 'workflow_dispatch')])
def test_main_reuses_verified_pr_merge_tree_but_retains_both_commit_identities(tag, event):
    source = MergedSource()
    result = ci_plan.plan(source, tag, source.landed, event, 'refs/heads/main')
    assert result['reuse_tests'] == 'true'
    assert result['checked_run'] == '456'
    assert result['checked_attempt'] == '2'
    assert result['checked_revision'] == source.tested
    assert result['checked_tree'] == source.tree
    assert result['checked_pr'] == '351'
    assert source.downloads == 1
    assert source.job_queries == []


@pytest.mark.parametrize('tag,event', [('', 'push'), ('v1.2.3', 'workflow_dispatch')])
def test_platform_retry_reuses_original_source_attempt_for_the_identical_merged_tree(tag, event):
    source = MergedSource()
    source.artifact['name'] = 'source-test-run-metrics-1'
    source.report['run_attempt'] = '1'
    result = ci_plan.plan(source, tag, source.landed, event, 'refs/heads/main')
    assert result['reuse_tests'] == 'true'
    assert result['checked_attempt'] == '1'
    assert result['checked_revision'] == source.tested
    assert result['checked_tree'] == source.tree
    assert sorted(source.job_queries) == [1, 2]


@pytest.mark.parametrize('role', ['unit (1/4)', 'browser (4/4)', 'coverage', 'checks', 'plan'])
def test_source_or_gate_retry_cannot_reuse_an_earlier_attempt_report(role):
    source = MergedSource()
    source.artifact['name'] = 'source-test-run-metrics-1'
    source.report['run_attempt'] = '1'
    job = next(job for job in source.job_history[2] if job['name'] == role)
    job['completed_at'] = '2026-10-24T12:06:00Z'
    assert ci_plan.plan(source, '', source.landed, 'push', 'refs/heads/main')['reuse_tests'] == 'false'


@pytest.mark.parametrize('attempt', [1, 2])
@pytest.mark.parametrize('invalid', ['missing_job', 'duplicate_job', 'failed_job', 'missing_timestamp',
                                   'missing_test_step', 'changed_test_step', 'failed_test_step'])
def test_retained_source_proof_requires_complete_unchanged_execution_history(attempt, invalid):
    source = MergedSource()
    source.artifact['name'] = 'source-test-run-metrics-1'
    source.report['run_attempt'] = '1'
    jobs = source.job_history[attempt]
    job = next(job for job in jobs if job['name'] == 'browser (1/4)')
    if invalid == 'missing_job': jobs.remove(job)
    if invalid == 'duplicate_job': jobs.append(deepcopy(job))
    if invalid == 'failed_job': job['conclusion'] = 'failure'
    if invalid == 'missing_timestamp': job['completed_at'] = None
    if invalid == 'missing_test_step': job['steps'] = []
    if invalid == 'changed_test_step': job['steps'][0]['completed_at'] = '2026-10-24T12:04:30Z'
    if invalid == 'failed_test_step': job['steps'][0]['conclusion'] = 'failure'
    assert ci_plan.plan(source, '', source.landed, 'push', 'refs/heads/main')['reuse_tests'] == 'false'


@pytest.mark.parametrize('invalid', [
    'tree', 'tested_identity', 'parents', 'unmerged', 'different_merge', 'fork', 'different_base',
    'failed_run', 'different_workflow', 'different_head', 'expired', 'old_attempt', 'empty_artifact',
    'incomplete', 'wrong_run', 'wrong_attempt', 'wrong_report_head', 'missing_suite',
    'different_suite_revision', 'empty_suite', 'bad_digest', 'different_shards',
])
def test_missing_or_different_merge_proof_runs_fresh_tests(invalid):
    source = MergedSource()
    if invalid == 'tree': source.commits[source.landed]['tree']['sha'] = 'f' * 40
    if invalid == 'tested_identity': source.commits[source.tested]['sha'] = 'f' * 40
    if invalid == 'parents': source.commits[source.tested]['parents'].pop()
    if invalid == 'unmerged': source.pr['merged_at'] = None
    if invalid == 'different_merge': source.pr['merge_commit_sha'] = 'f' * 40
    if invalid == 'fork': source.pr['head']['repo']['full_name'] = 'other/repo'
    if invalid == 'different_base': source.pr['base']['ref'] = 'develop'
    if invalid == 'failed_run': source.run_info['conclusion'] = 'failure'
    if invalid == 'different_workflow': source.run_info['path'] = '.github/workflows/other.yml'
    if invalid == 'different_head': source.run_info['head_sha'] = 'f' * 40
    if invalid == 'expired': source.artifact['expired'] = True
    if invalid == 'old_attempt': source.artifact['name'] = 'source-test-run-metrics-1'
    if invalid == 'empty_artifact': source.artifact['size_in_bytes'] = 0
    if invalid == 'incomplete': source.report['complete'] = False
    if invalid == 'wrong_run': source.report['run_id'] = '999'
    if invalid == 'wrong_attempt': source.report['run_attempt'] = '1'
    if invalid == 'wrong_report_head': source.report['head_revision'] = 'f' * 40
    if invalid == 'missing_suite': source.report['suites'].pop('browser')
    if invalid == 'different_suite_revision': source.report['suites']['browser']['revision'] = 'f' * 40
    if invalid == 'empty_suite': source.report['suites']['browser']['cases'] = 0
    if invalid == 'bad_digest': source.report['suites']['browser']['case_digest'] = ''
    if invalid == 'different_shards': source.report['suites']['browser']['shards'] = 2
    assert ci_plan.plan(source, '', source.landed, 'push', 'refs/heads/main')['reuse_tests'] == 'false'


@pytest.mark.parametrize('event,ref', [('pull_request', 'refs/pull/351/merge'),
                                      ('push', 'refs/heads/feature'), ('workflow_dispatch', 'refs/heads/main')])
def test_pr_feature_and_explicit_test_control_runs_always_run_fresh(event, ref):
    class NoLookup:
        def api(self, path):
            pytest.fail('Fresh source checks must not look up reusable proof')
    assert ci_plan.plan(NoLookup(), '', 'a' * 40, event, ref)['reuse_tests'] == 'false'


def metadata():
    return {"schema": 1, "tag": "v1.2.3", "version": "1.2.3", "revision": "a" * 40,
            "source_run": "123", "repository": "owner/repo", "sha256": {"payload": "b" * 64},
            "architectures": {"macos": ["arm64", "x86_64"], "flatpak": ["aarch64", "x86_64"],
                              "container": ["amd64", "arm64"]}}


@pytest.mark.parametrize("field,value", [("tag", "v1.2.4"), ("version", "0.0.0.dev0"),
                                         ("revision", "b" * 40), ("source_run", "999"),
                                         ("repository", "other/repo")])
def test_different_revision_version_run_or_repository_cannot_reuse_candidates(field, value):
    data = metadata()
    data[field] = value
    with pytest.raises(ValueError, match="exact version"):
        provenance.check_identity(data, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_tampered_payload_cannot_pass_handoff(tmp_path):
    (tmp_path / "release-provenance.json").write_text(json.dumps(metadata()))
    (tmp_path / "payload").write_bytes(b"changed after smoke test")
    with pytest.raises(ValueError, match="smoke-tested bytes"):
        provenance.verify(tmp_path, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_missing_architecture_cannot_pass_handoff():
    data = metadata()
    data["architectures"]["container"].pop()
    with pytest.raises(ValueError, match="architecture"):
        provenance.check_identity(data, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_ci_candidate_lookup_requires_provenance_not_just_success(tmp_path):
    class CI:
        repository = "owner/repo"

        def api(self, path):
            if "/artifacts?" in path:
                return {"artifacts": [{"name": name, "id": n + 1, "expired": False, "size_in_bytes": 1}
                                      for n, name in enumerate((*candidates.EXPECTED, "release-provenance", "release-flatpak-repository"))]}
            return {"path": ".github/workflows/ci.yml", "head_sha": "a" * 40, "event": "workflow_dispatch",
                    "head_branch": "main", "status": "completed", "conclusion": "success"}

        def run(self, *args):
            directory = Path(args[args.index("--dir") + 1])
            (directory / "release-provenance.json").write_text(json.dumps(metadata()))

    assert candidates.inspect(CI(), "123", "a" * 40, "v1.2.3", status_file=tmp_path / "status.json") == "1,2,3,4,5,6,7,8,9"
    with pytest.raises(ValueError, match="exact version"):
        candidates.inspect(CI(), "123", "a" * 40, "v1.2.4", status_file=tmp_path / "status.json")


def jobs(completed="2026-10-23T12:11:00Z", conclusion="success"):
    return [{"name": "promote", "created_at": "2026-10-23T12:01:00Z", "started_at": "2026-10-23T12:01:20Z", "steps": [
        {"name": "Verify anonymous downloads and signed public update channels", "status": "completed",
         "conclusion": conclusion, "started_at": "2026-10-23T12:10:00Z", "completed_at": completed},
    ]}]


def test_clock_includes_time_before_runner_and_does_not_restart_at_promotion():
    metrics = timing.measure(jobs(), "2026-10-23T12:00:00Z")
    assert metrics["elapsed_seconds"] == 660
    assert metrics["within_budget"]
    assert next(row for row in metrics["stages"] if row["stage"] == "promote runner queue")["elapsed_seconds"] == 20


def test_late_or_failed_publication_does_not_claim_budget_success():
    late = timing.measure(jobs("2026-10-23T12:16:00Z"), "2026-10-23T12:00:00Z")
    assert late["elapsed_seconds"] == 960 and not late["within_budget"]
    failed = timing.measure(jobs(conclusion="failure"), "2026-10-23T12:00:00Z")
    assert failed["elapsed_seconds"] is None and not failed["within_budget"]


def test_legacy_recovery_clock_uses_green_ci_before_original_release():
    class History:
        repository = "owner/repo"

        def api(self, path):
            if "/workflows/" not in path:
                return {"path": ".github/workflows/release.yml", "created_at": "2026-10-23T12:01:00Z",
                        "updated_at": "2026-10-23T14:30:00Z"}
            base = {"head_sha": "a" * 40, "event": "push", "status": "completed", "conclusion": "success", "run_attempt": 1}
            return {"workflow_runs": [
                {**base, "id": 3, "updated_at": "2026-10-23T14:29:00Z"},
                {**base, "id": 1, "updated_at": "2026-10-23T11:00:00Z"},
                {**base, "id": 2, "updated_at": "2026-10-23T12:00:00Z"},
                {**base, "id": 4, "head_sha": "b" * 40, "updated_at": "2026-10-23T12:00:30Z"},
            ]}

    clock = candidates.source_clock(History(), "123", "a" * 40)
    assert clock == {"ci_run": "2", "ci_run_attempt": "1", "ci_completed_at": "2026-10-23T12:00:00Z"}


def test_missing_original_ci_clock_does_not_fall_back_to_failed_release_completion():
    class Missing:
        repository = "owner/repo"

        def api(self, path):
            return ({"workflow_runs": []} if "/workflows/" in path else
                    {"path": ".github/workflows/release.yml", "created_at": "2026-10-23T12:01:00Z"})

    with pytest.raises(ValueError, match="green CI"):
        candidates.source_clock(Missing(), "123", "a" * 40)


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("event", "pull_request"),
                                         ("status", "in_progress"), ("conclusion", "failure")])
def test_candidate_ci_clock_cannot_admit_a_different_or_incomplete_check(field, value):
    class Checked:
        repository = "owner/repo"

        def api(self, path):
            return {"path": ".github/workflows/ci.yml", "head_sha": "a" * 40, "event": "workflow_dispatch",
                    "status": "completed", "conclusion": "success", field: value}

    with pytest.raises(ValueError, match="exact revision"):
        candidates.source_clock(Checked(), "123", "a" * 40)
