"""One version/channel policy for every shipped artifact and publication destination."""
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys


@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    channel: str

    def outputs(self):
        stable = self.channel == "stable"
        return {"tag": self.tag, "version": self.version, "channel": self.channel,
                "prerelease": str(not stable).lower(), "latest": str(stable).lower(),
                "flatpak_publish": str(stable).lower()}


def classify(tag):
    # Canonical three-part versions; PEP 440 and explicit SemVer prerelease spellings.
    match = re.fullmatch(
        r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:(a|b|rc|\.dev)(0|[1-9]\d*)|-(alpha|beta|rc|dev)\.(0|[1-9]\d*))?", tag)
    if match is None:
        raise ValueError(f"Unsupported release tag: {tag!r}")
    major, minor, patch, pep, number, sem, sem_number = match.groups()
    suffix = pep or {"alpha": "a", "beta": "b", "rc": "rc", "dev": ".dev"}.get(sem, "")
    version = f"{major}.{minor}.{patch}" + (suffix + (number or sem_number) if suffix else "")
    channel = "development" if suffix == ".dev" else "prerelease" if suffix else "stable"
    return Release(tag, version, channel)


def next_patch(tags):
    latest = (0, 0, 0)
    for tag in tags:
        try:
            release = classify(tag.strip())
        except ValueError:
            continue
        if release.channel == "stable":
            latest = max(latest, tuple(map(int, release.version.split("."))))
    major, minor, patch = latest
    return f"v{major}.{minor}.{patch + 1}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("tag", nargs="?")
    mode.add_argument("--next-patch", action="store_true", help="Read reachable tags from standard input")
    args = parser.parse_args()
    try:
        release = classify(next_patch(sys.stdin) if args.next_patch else args.tag)
    except ValueError as exc:
        parser.error(str(exc))
    output = "".join(f"{key}={value}\n" for key, value in release.outputs().items())
    print(output, end="")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a") as stream:
            stream.write(output)


if __name__ == "__main__":
    main()
