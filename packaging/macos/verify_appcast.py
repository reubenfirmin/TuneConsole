"""Reject incomplete/wrong-architecture feeds before attaching them to a release.

Print only the archive signature; CI then cryptographically verifies it with
Sparkle's sign_update tool. generate_appcast can warn (without failing) when the
signing key doesn't match SUPublicEDKey, so its exit status alone isn't enough.
"""
import argparse
import base64
from pathlib import Path
import xml.etree.ElementTree as ET

SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def verify(feed, archive, tag, arch):
    archive = Path(archive)
    if arch not in ("arm64", "x86_64"):
        raise ValueError("Unsupported update architecture")
    name = f"TuneConsole-macos-{arch}.dmg"
    if archive.name != name:
        raise ValueError("Archive name doesn't match the update architecture")
    items = ET.parse(feed).findall("./channel/item")
    if len(items) != 1:
        raise ValueError("Expected exactly one update in the feed")
    item = items[0]
    if item.findtext(SPARKLE + "version") != tag.removeprefix("v"):
        raise ValueError("Feed version doesn't match the release tag")
    enclosure = item.find("enclosure")
    expected_url = f"https://github.com/reubenfirmin/TuneConsole/releases/download/{tag}/{name}"
    if enclosure is None or enclosure.get("url") != expected_url:
        raise ValueError("Feed must point to this architecture's immutable release asset")
    if int(enclosure.get("length", "0")) != archive.stat().st_size:
        raise ValueError("Feed archive size doesn't match the release asset")
    signature = enclosure.get(SPARKLE + "edSignature", "")
    if len(base64.b64decode(signature, validate=True)) != 64:
        raise ValueError("Feed must include a complete Ed25519 archive signature")
    return signature


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feed")
    parser.add_argument("archive")
    parser.add_argument("tag")
    parser.add_argument("arch", choices=("arm64", "x86_64"))
    args = parser.parse_args()
    print(verify(args.feed, args.archive, args.tag, args.arch))
