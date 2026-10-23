"""Check that a publication retry uses the completed builds for its original tag."""
import argparse
import json
import subprocess


REQUIRED_JOBS = {
    "version", "wheel",
    "flatpak (x86_64, ubuntu-latest)",
    "flatpak (aarch64, ubuntu-24.04-arm)",
    "macos / build (macos-14, arm64)",
    "macos / build (macos-15-intel, x86_64)",
    "containers / smoke (amd64, ubuntu-latest)",
    "containers / smoke (arm64, ubuntu-24.04-arm)",
}


def validate(run, jobs, tag, sha):
    if (run.get("path") != ".github/workflows/release.yml"
            or run.get("event") != "push" or run.get("head_branch") != tag
            or run.get("head_sha") != sha or run.get("status") != "completed"):
        raise ValueError("Candidate must be the completed release run for the exact tag commit")
    results = {job["name"]: job.get("conclusion") for job in jobs}
    if any(results.get(name) != "success" for name in REQUIRED_JOBS):
        raise ValueError("Every required candidate build must have passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=int)
    parser.add_argument("tag")
    parser.add_argument("sha")
    args = parser.parse_args()
    if args.run_id <= 0:
        parser.error("run_id must be positive")
    base = f"repos/reubenfirmin/TuneConsole/actions/runs/{args.run_id}"

    def api(path):
        return json.loads(subprocess.check_output(["gh", "api", path], text=True))

    validate(api(base), api(base + "/jobs?per_page=100")["jobs"], args.tag, args.sha)
