import subprocess
import sys
from pathlib import Path
import runpy

import pytest

classify = runpy.run_path(Path(__file__).parents[1] / "packaging/release_policy.py")["classify"]


@pytest.mark.parametrize("tag,version,channel", [
    ("v1.2.3", "1.2.3", "stable"),
    ("v0.0.0", "0.0.0", "stable"),
    ("v1.2.3rc1", "1.2.3rc1", "prerelease"),
    ("v1.2.3-rc.2", "1.2.3rc2", "prerelease"),
    ("v1.2.3-beta.1", "1.2.3b1", "prerelease"),
    ("v1.2.3b1", "1.2.3b1", "prerelease"),
    ("v1.2.3-alpha.1", "1.2.3a1", "prerelease"),
    ("v1.2.3.dev4", "1.2.3.dev4", "development"),
    ("v1.2.3-dev.4", "1.2.3.dev4", "development"),
])
def test_channel_policy_for_all_destinations(tag, version, channel):
    release = classify(tag)
    assert (release.version, release.channel) == (version, channel)
    outputs = release.outputs()
    assert outputs["prerelease"] == str(channel != "stable").lower()
    assert outputs["latest"] == outputs["flatpak_publish"] == str(channel == "stable").lower()


@pytest.mark.parametrize("tag", ["1.2.3", "v1", "v1.2", "v01.2.3", "v1.2.3.4",
                                    "v1.2.3-rc", "v1.2.3+local", "v1.2.3-beta.01",
                                    "v1.2.3\nlatest=true", "v1.2.3-nightly"])
def test_unsupported_tag_cannot_produce_publication_outputs(tag, tmp_path):
    with pytest.raises(ValueError):
        classify(tag)
    output = tmp_path / "outputs"
    import os
    result = subprocess.run([sys.executable, "packaging/release_policy.py", tag],
                            env={**os.environ, "GITHUB_OUTPUT": str(output)},
                            capture_output=True, text=True)
    assert result.returncode != 0 and not output.exists()
