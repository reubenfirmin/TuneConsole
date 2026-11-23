"""Notarize and verify a Developer ID signed DMG before recording its final bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

ARCHITECTURES = ("arm64", "x86_64")
TEAM_ID = re.compile(r"[A-Z0-9]{10}")
SUBMISSION_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.IGNORECASE)


def receipt_name(arch):
    return f"macos-notarization-{arch}.json"


def image_digest(dmg):
    with dmg.open("rb") as image:
        return hashlib.file_digest(image, "sha256").hexdigest()


def verify_receipt(root, arch, sha256):
    receipt_path = root / receipt_name(arch)
    if not receipt_path.is_file():
        raise ValueError(f"Missing macOS notarization receipt for {arch}")
    receipt = json.loads(receipt_path.read_text())
    if (not isinstance(receipt, dict) or receipt.get("architecture") != arch
            or receipt.get("status") != "Accepted"
            or not SUBMISSION_ID.fullmatch(str(receipt.get("submission_id")))
            or not TEAM_ID.fullmatch(str(receipt.get("team_id")))
            or receipt.get("sha256") != sha256):
        raise ValueError(f"Invalid macOS notarization receipt for {arch}")
    return receipt["team_id"]


def execute(*args, timeout=60):
    completed = subprocess.run([str(arg) for arg in args], text=True, timeout=timeout,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(completed.stdout, end="", flush=True)
    completed.check_returncode()
    return completed.stdout


def verify_signature(path, team_id, *, app=False):
    requirement = ("=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists "
                   f'and certificate leaf[subject.OU] = "{team_id}"')
    execute("codesign", "--verify", "--verbose=2", "-R", requirement, path)
    details = execute("codesign", "--display", "--verbose=4", path)
    if not re.search(r"^Timestamp=.+", details, re.MULTILINE):
        raise ValueError(f"No secure signing timestamp: {path}")
    if app:
        if not re.search(r"flags=[^\n]*\bruntime\b", details):
            raise ValueError(f"Hardened Runtime is required: {path}")
        execute("codesign", "--verify", "--deep", "--strict", "--verbose=2", path)


def verify_gatekeeper(*args):
    result = execute("spctl", "--assess", "--verbose=2", *args)
    if "source=Notarized Developer ID" not in result:
        raise ValueError("Gatekeeper did not report a notarized Developer ID signature")


def notarize(dmg, arch, team_id, keychain):
    if not TEAM_ID.fullmatch(team_id):
        raise ValueError("Expected a 10-character Apple Team ID")
    started = time.monotonic()
    credentials = ("--keychain-profile", "TuneConsole", "--keychain", keychain)
    notarytool = ("xcrun", "notarytool")
    verify_signature(dmg, team_id)
    submission = json.loads(execute(*notarytool, "submit", dmg, *credentials, "--output-format", "json", timeout=180))
    submission_id = submission["id"]
    if not SUBMISSION_ID.fullmatch(submission_id):
        raise ValueError("Unexpected notarization submission ID")
    try:
        status = json.loads(execute(*notarytool, "wait", submission_id, *credentials, "--timeout", "6m",
                                    "--output-format", "json", timeout=380))
    finally:
        try:
            execute(*notarytool, "log", submission_id, *credentials, dmg.with_suffix(".notary-log.json"), timeout=30)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(f"Could not fetch the notarization log for {submission_id}", flush=True)
    if status.get("status") != "Accepted" or str(status.get("id")).lower() != submission_id.lower():
        raise ValueError(f"Apple did not accept notarization of {dmg.name} ({submission_id})")

    # Apple produces tickets for the embedded app and the image. Staple the outer download.
    execute("xcrun", "stapler", "staple", dmg)
    execute("xcrun", "stapler", "validate", dmg)
    verify_signature(dmg, team_id)
    verify_gatekeeper("--type", "open", "--context", "context:primary-signature", dmg)
    with tempfile.TemporaryDirectory(prefix="tuneconsole-verify-") as mount:
        execute("hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, dmg)
        try:
            app = Path(mount) / "TuneConsole.app"
            executable = app / "Contents/MacOS/yt-playlist"
            if execute("lipo", "-archs", executable).strip() != arch:
                raise ValueError(f"Expected a {arch} TuneConsole executable")
            verify_signature(app, team_id, app=True)
            verify_gatekeeper("--type", "execute", app)
        finally:
            execute("hdiutil", "detach", mount)

    receipt = {"architecture": arch, "team_id": team_id, "status": status["status"],
               "submission_id": submission_id, "sha256": image_digest(dmg)}
    dmg.with_name(receipt_name(arch)).write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Notarization and verification of {arch} took {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dmg", type=Path)
    parser.add_argument("arch", choices=ARCHITECTURES)
    parser.add_argument("team_id")
    parser.add_argument("keychain", type=Path)
    args = parser.parse_args()
    notarize(args.dmg, args.arch, args.team_id, args.keychain)
