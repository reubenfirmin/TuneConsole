"""One version/channel policy for every shipped artifact and publication destination."""
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    args = parser.parse_args()
    try:
        release = classify(args.tag)
    except ValueError as exc:
        parser.error(str(exc))
    output = "".join(f"{key}={value}\n" for key, value in release.outputs().items())
    print(output, end="")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a") as stream:
            stream.write(output)


if __name__ == "__main__":
    main()
