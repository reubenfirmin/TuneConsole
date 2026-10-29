"""Candidate provenance, retained artifacts and bounded setup recovery."""
import importlib
import json
import os
from pathlib import Path
import signal
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
ci_stage = importlib.import_module("ci_stage")
candidates = importlib.import_module("release_candidates")
summary = importlib.import_module("release_summary")


class Runs:
    repository = "owner/repo"

    def __init__(self):
        self.run_info = {"head_sha": "a" * 40, "path": ".github/workflows/release.yml", "event": "push",
                         "head_branch": "v1.2.3", "status": "completed", "conclusion": "failure"}
        self.artifacts = [{"name": name, "id": n + 1, "size_in_bytes": 100, "expired": False}
                          for n, name in enumerate(candidates.EXPECTED)]

    def api(self, path):
        return {"artifacts": self.artifacts} if "/artifacts?" in path else self.run_info

    def run(self, *args):
        directory = Path(args[args.index("--dir") + 1])
        (directory / "release-provenance.json").write_text(json.dumps({
            "schema": 1, "tag": "v1.2.3", "version": "1.2.3", "revision": "a" * 40,
            "source_run": "123", "repository": self.repository,
            "architectures": {"macos": ["arm64", "x86_64"], "flatpak": ["aarch64", "x86_64"],
                              "container": ["amd64", "arm64"]},
        }))


def inspect(backend, tmp_path, **kwargs):
    return candidates.inspect(backend, "123", "a" * 40, "v1.2.3", status_file=tmp_path / "status.json", **kwargs)


def test_complete_candidates_survive_failed_promotion_and_repeated_inspection(tmp_path):
    backend = Runs()
    assert inspect(backend, tmp_path) == "1,2,3,4,5,6,7,8,9"
    assert inspect(backend, tmp_path) == "1,2,3,4,5,6,7,8,9"
    assert all(row["state"] == "complete" for row in json.loads((tmp_path / "status.json").read_text())["assets"])


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("head_branch", "v1.2.4"),
                                         ("path", ".github/workflows/other.yml"), ("event", "pull_request")])
def test_source_revision_tag_and_workflow_must_all_match(tmp_path, field, value):
    backend = Runs()
    backend.run_info[field] = value
    with pytest.raises(ValueError, match="exact tag and commit"):
        inspect(backend, tmp_path)


@pytest.mark.parametrize("state", ["missing", "expired", "empty", "ambiguous"])
def test_incomplete_or_expired_candidates_report_exact_platform(tmp_path, state):
    backend = Runs()
    if state == "missing":
        backend.artifacts.pop()
    elif state == "expired":
        backend.artifacts[-1]["expired"] = True
    elif state == "empty":
        backend.artifacts[-1]["size_in_bytes"] = 0
    else:
        backend.artifacts.append(backend.artifacts[-1].copy())
    with pytest.raises(ValueError, match="incomplete"):
        inspect(backend, tmp_path)
    rows = json.loads((tmp_path / "status.json").read_text())["assets"]
    assert rows[-1]["name"] == "release-flatpak-repository" and rows[-1]["state"] == state
    assert all(row["state"] == "complete" for row in rows[:-1])


def test_current_build_can_inspect_after_all_builds_but_other_active_source_cannot(tmp_path):
    backend = Runs()
    backend.run_info["status"] = "in_progress"
    with pytest.raises(ValueError, match="complete"):
        inspect(backend, tmp_path)
    assert inspect(backend, tmp_path, allow_active=True)


def test_artifact_outage_is_not_reported_as_a_missing_platform(tmp_path):
    backend = Runs()
    original = backend.api
    backend.api = lambda path: None if "/artifacts?" in path else original(path)
    with pytest.raises(RuntimeError, match="outage"):
        inspect(backend, tmp_path)


def test_timed_out_downloader_is_reaped_and_summary_preserves_budget(tmp_path):
    timings = tmp_path / "timings.json"
    result = ci_stage.execute("download", [sys.executable, "-c", "import time; time.sleep(30)"],
                              timeout=0.05, timings=timings)
    assert result == 124
    rows = json.loads(timings.read_text())
    assert len(rows) == 1 and rows[0]["status"] == "timed out"
    assert rows[0]["budget_seconds"] == 0.05
    assert rows[0]["elapsed_seconds"] < 5


@pytest.mark.skipif(sys.platform != "linux", reason="Checks descendant state through Linux procfs")
def test_timeout_kills_sigterm_ignoring_descendant_after_leader_exits(tmp_path):
    child_pid = tmp_path / "child.pid"
    child = ("import os,signal,sys,time; from pathlib import Path; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)")
    parent = ("import subprocess,sys,time; from pathlib import Path; "
              "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
              "time.sleep(30)")
    pid = None
    try:
        assert ci_stage.execute("download", [sys.executable, "-c", parent, child, str(child_pid)],
                                timeout=1, timings=tmp_path / "timings.json") == 124
        pid = int(child_pid.read_text())
        state = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 2
        while state.exists() and state.read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}:
            assert time.monotonic() < deadline, "Timed-out descendant survived its process-group leader"
            time.sleep(.01)
    finally:
        # Clean up only this test's child if the regression fails.
        if pid is None and child_pid.exists():
            pid = int(child_pid.read_text())
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_only_failed_setup_attempts_are_retried_and_success_retained(tmp_path):
    attempts = tmp_path / "attempts"
    program = "from pathlib import Path; import sys; p=Path(sys.argv[1]); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); sys.exit(0 if n else 1)"
    timings = tmp_path / "timings.json"
    assert ci_stage.execute("setup", [sys.executable, "-c", program, str(attempts)],
                            timeout=5, attempts=2, timings=timings) == 0
    assert [row["status"] for row in json.loads(timings.read_text())] == ["failed", "success"]
    assert attempts.read_text() == "2"


def test_summary_preserves_failed_stage_and_publication_only_recovery(tmp_path):
    (tmp_path / "candidate-status.json").write_text(json.dumps({"assets": [
        {"name": "release-flatpak-aarch64", "state": "expired", "bytes": 123},
    ]}))
    text = summary.render({"flatpak": {"result": "success"}, "promote": {"result": "failure"}},
                          "v1.2.3", "123", evidence=tmp_path)
    assert "expired" in text
    assert "gh workflow run release.yml -f tag=v1.2.3 -f source_run=123" in text
    assert "retain the draft/checkpoint" in text
    assert "All downloads" not in text


def test_release_summary_accepts_ci_shard_timings_alongside_platform_setup(tmp_path):
    (tmp_path / "unit-timings.json").write_text(json.dumps({"suite": "unit", "cases": 42, "shards": 2}))
    (tmp_path / "ci-stage-timings.json").write_text(json.dumps([
        {"stage": "wheel-dependencies", "status": "success", "elapsed_seconds": 1.2, "budget_seconds": 120, "attempt": 1},
    ]))
    text = summary.render({"promote": {"result": "success"}}, "v1.2.3", "123", evidence=tmp_path)
    assert "unit checks: 42 cases across 2 serial shards" in text
    assert "wheel-dependencies: success, 1.2s" in text
    assert "All downloads" in text
