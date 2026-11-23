"""Release signing stops before recording an unverified download."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
from macos import notarize

SUBMISSION = "35e5054f-8e74-4a64-8533-6069198f4034"
TEAM = "ABCDEF1234"


@pytest.mark.parametrize("missing", ["MACOS_CERTIFICATE_P12", "MACOS_NOTARY_APP_PASSWORD"])
def test_release_build_requires_credentials_before_signing(missing):
    env = dict(RELEASE_TAG="v1.2.3", MACOS_TEAM_ID=TEAM,
               MACOS_SIGNING_IDENTITY=f"Developer ID Application: TuneConsole ({TEAM})",
               MACOS_CERTIFICATE_P12="test", MACOS_CERTIFICATE_PASSWORD="test",
               MACOS_NOTARY_APPLE_ID="test@example.com", MACOS_NOTARY_APP_PASSWORD="test")
    del env[missing]
    completed = subprocess.run(["bash", ROOT / "packaging/macos/build-release.sh"],
                               env=env, capture_output=True, text=True)
    assert completed.returncode != 0
    assert f"Set {missing}" in completed.stderr


@pytest.mark.parametrize("failure", [None, "signature", "submission", "wait", "staple", "app assessment"])
def test_receipt_requires_accepted_notarization_and_verification(tmp_path, monkeypatch, failure):
    dmg = tmp_path / "TuneConsole-macos-arm64.dmg"
    dmg.write_bytes(b"signed image before stapling")
    commands = []

    def execute(*args, timeout=None):
        commands.append(args)
        if args[:2] == ("codesign", "--verify") and failure == "signature":
            raise subprocess.CalledProcessError(1, args)
        if args[:2] == ("codesign", "--display"):
            return "CodeDirectory flags=0x10000(runtime)\nTimestamp=Jan 1, 2026\n"
        if args[:3] == ("xcrun", "notarytool", "submit"):
            return json.dumps({"id": SUBMISSION})
        if args[:3] == ("xcrun", "notarytool", "wait"):
            if failure == "wait":
                raise subprocess.TimeoutExpired(args, timeout)
            status = "Invalid" if failure == "submission" else "Accepted"
            return json.dumps({"id": SUBMISSION, "status": status})
        if args[:3] == ("xcrun", "stapler", "staple"):
            dmg.write_bytes(dmg.read_bytes() + b" with notarization ticket")
        if args[:3] == ("xcrun", "stapler", "validate") and failure == "staple":
            raise subprocess.CalledProcessError(1, args)
        if args[:2] == ("spctl", "--assess"):
            return "source=Unnotarized Developer ID" if failure == "app assessment" and "execute" in args else (
                "source=Notarized Developer ID")
        if args[:2] == ("lipo", "-archs"):
            return "arm64\n"
        return ""

    monkeypatch.setattr(notarize, "execute", execute)
    receipt = tmp_path / notarize.receipt_name("arm64")
    if failure:
        with pytest.raises((ValueError, subprocess.SubprocessError)):
            notarize.notarize(dmg, "arm64", TEAM, tmp_path / "signing.keychain")
        assert not receipt.exists()
        if failure in {"submission", "wait"}:
            assert any(c[:3] == ("xcrun", "notarytool", "log") for c in commands)
            assert all(c[:2] != ("xcrun", "stapler") for c in commands)
        if failure == "app assessment":
            assert commands[-1][:2] == ("hdiutil", "detach")
        return

    notarize.notarize(dmg, "arm64", TEAM, tmp_path / "signing.keychain")
    assert notarize.verify_receipt(tmp_path, "arm64", notarize.image_digest(dmg)) == TEAM
    assert json.loads(receipt.read_text())["submission_id"] == SUBMISSION
    assert len([c for c in commands if c[:2] == ("spctl", "--assess")]) == 2
