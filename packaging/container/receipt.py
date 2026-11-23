"""Record and verify the immutable image pushed to the release registry."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def inspect(reference, template):
    return subprocess.check_output(
        ["docker", "buildx", "imagetools", "inspect", reference, "--format", template],
        text=True, timeout=45,
    ).strip()


def verify(image, digest, arch, version, revision):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("The registry did not return a manifest digest")
    reference = f"{image}@{digest}"
    config = json.loads(inspect(reference, "{{json .Image}}"))
    labels = config.get("config", {}).get("Labels") or {}
    if (config.get("os"), config.get("architecture"), labels.get("org.opencontainers.image.version"),
            labels.get("org.opencontainers.image.revision")) != ("linux", arch, version, revision):
        raise ValueError(f"The registry image for {arch} does not match {version} at {revision}")
    return {"arch": arch, "version": version, "digest": reference}


def repair(image, receipt, arch, version, revision):
    repository, separator, digest = receipt["digest"].partition("@")
    if (receipt["arch"], receipt["version"]) != (arch, version) or not separator or repository not in {
        image, image.rsplit("/", 1)[-1],
    }:
        raise ValueError(f"The retained {arch} receipt does not identify the expected candidate")
    return verify(image, digest, arch, version, revision)


def capture(image, tag, arch, version, revision):
    digest = inspect(f"{image}:{tag}", "{{.Manifest.Digest}}")
    return verify(image, digest, arch, version, revision)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("arch", choices=("amd64", "arm64"))
    parser.add_argument("version")
    parser.add_argument("revision")
    parser.add_argument("output", type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tag", help="Candidate tag in the supplied registry repository")
    source.add_argument("--repair", type=Path, help="Legacy receipt to verify and normalize")
    args = parser.parse_args()
    if args.repair:
        receipt = repair(args.image, json.loads(args.repair.read_text()), args.arch, args.version, args.revision)
    else:
        receipt = capture(args.image, args.tag, args.arch, args.version, args.revision)
    args.output.write_text(json.dumps(receipt) + "\n")
