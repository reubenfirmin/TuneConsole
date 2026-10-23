"""Write a durable release status and the supported recovery command."""
import json
import os
from pathlib import Path


def render(results, tag, source_run, *, evidence=Path("evidence")):
    lines = [f"Release `{tag}`; candidate source run `{source_run}`.", "",
             "| Stage | Result |", "| --- | --- |"]
    lines.extend(f"| {name} | {value['result']} |" for name, value in results.items())
    lines.append("")
    for path in sorted(evidence.rglob("*.json")) if evidence.exists() else []:
        if path.name == "candidate-status.json":
            data = json.loads(path.read_text())
            lines.extend(["| Asset group | Availability | Bytes |", "| --- | --- | --- |"])
            lines.extend(f"| {row['name']} | {row['state']} | {row['bytes'] or 0} |" for row in data["assets"])
        elif "timings" in path.name:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                for row in data:
                    lines.append(f"{row['stage']}: {row['status']}, {row.get('elapsed_seconds', 0):.1f}s "
                                 f"(budget {row['budget_seconds']}s, attempt {row['attempt']}).")
            elif isinstance(data, dict) and "suite" in data and "cases" in data:
                lines.append(f"Source {data['suite']} checks: {data['cases']} cases across {data['shards']} serial shards.")
    if results.get("promote", {}).get("result") == "success":
        lines.append("All downloads and applicable update channels passed public verification. See the promotion job summary for links.")
    else:
        lines.extend(["", "Recover only incomplete work:", "",
                      "- Missing/expired candidates: rerun the affected source jobs; keep successful architecture artifacts.",
                      "- Publication failed or unresolved: retain the draft/checkpoint; the coordinator reconciles it before retrying.",
                      "- Already public: keep assets immutable and retry public verification with the same candidates.", "",
                      "When all candidates are available, use the publication-only path:", "",
                      "```sh", f"gh workflow run release.yml -f tag={tag} -f source_run={source_run}", "```"])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    text = render(json.loads(os.environ["RESULTS"]), os.environ["RELEASE_TAG"], os.environ["SOURCE_RUN"])
    Path("release-status.md").write_text(text)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
        stream.write(text)
    print(text)
