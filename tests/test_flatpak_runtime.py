from datetime import date
from pathlib import Path
import tomllib


def test_runtime_abi_manifest_and_support_review_stay_aligned():
    root = Path(__file__).parents[1]
    platform = tomllib.loads((root / "packaging/platforms.toml").read_text())["flatpak"]
    manifest = (root / "packaging/flatpak/com.tuneconsole.TuneConsole.yaml").read_text()
    assert f"runtime-version: '{platform['runtime']}'" in manifest
    assert platform["runtime"] == "25.08" and platform["python"] == "3.13"
    assert date.today() < date.fromisoformat(platform["review_by"]), "Review Flatpak runtime support now"
    assert platform["review_by"] < platform["support_until"]
    assert platform["owner"]
    assert "24.08" not in manifest
