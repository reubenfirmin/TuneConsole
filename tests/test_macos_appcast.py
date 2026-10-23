import base64
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "verify_appcast", Path(__file__).parents[1] / "packaging/macos/verify_appcast.py")
appcast = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(appcast)


@pytest.fixture
def update_feed(tmp_path):
    archive = tmp_path / "TuneConsole-macos-arm64.dmg"
    archive.write_bytes(b"test archive")
    feed = tmp_path / "appcast-macos-arm64.xml"
    root = ET.Element("rss")
    item = ET.SubElement(ET.SubElement(root, "channel"), "item")
    ET.SubElement(item, appcast.SPARKLE + "version").text = "0.2.0"
    enclosure = ET.SubElement(item, "enclosure", {
        "url": "https://github.com/reubenfirmin/TuneConsole/releases/download/v0.2.0/TuneConsole-macos-arm64.dmg",
        "length": str(archive.stat().st_size),
        appcast.SPARKLE + "edSignature": base64.b64encode(bytes(64)).decode(),
    })
    return feed, archive, root, item, enclosure


def test_complete_architecture_specific_feed_returns_signature_for_crypto_verification(update_feed):
    feed, archive, root, _, enclosure = update_feed
    ET.ElementTree(root).write(feed)
    assert appcast.verify(feed, archive, "v0.2.0", "arm64") == enclosure.get(appcast.SPARKLE + "edSignature")


def test_prerelease_feed_uses_normalized_package_version_and_original_tag_url(update_feed):
    feed, archive, root, item, enclosure = update_feed
    item.find(appcast.SPARKLE + "version").text = "0.2.0rc1"
    enclosure.set("url", enclosure.get("url").replace("v0.2.0/", "v0.2.0-rc.1/"))
    ET.ElementTree(root).write(feed)
    assert appcast.verify(feed, archive, "v0.2.0-rc.1", "arm64")


@pytest.mark.parametrize("problem", ["missing-signature", "truncated-signature", "wrong-arch", "mutable-url",
                                     "wrong-version", "changed-archive", "empty-feed"])
def test_bad_or_incomplete_feed_prevents_release(update_feed, problem):
    feed, archive, root, item, enclosure = update_feed
    if problem == "missing-signature":
        enclosure.attrib.pop(appcast.SPARKLE + "edSignature")
    elif problem == "truncated-signature":
        enclosure.set(appcast.SPARKLE + "edSignature", base64.b64encode(b"short").decode())
    elif problem == "wrong-arch":
        enclosure.set("url", enclosure.get("url").replace("arm64", "x86_64"))
    elif problem == "mutable-url":
        enclosure.set("url", enclosure.get("url").replace("download/v0.2.0", "latest/download"))
    elif problem == "wrong-version":
        item.find(appcast.SPARKLE + "version").text = "0.1.9"
    elif problem == "changed-archive":
        archive.write_bytes(b"changed")
    else:
        root.find("channel").remove(item)
    ET.ElementTree(root).write(feed)
    with pytest.raises(ValueError):
        appcast.verify(feed, archive, "v0.2.0", "arm64")
