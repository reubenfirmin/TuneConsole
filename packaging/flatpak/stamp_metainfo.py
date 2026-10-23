"""Stamp exported AppStream metadata from the exact wheel being packaged."""
import argparse
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile


def stamp(path, wheel_directory, *, date=None):
    wheels = list(Path(wheel_directory).glob("yt_playlist-*.whl"))
    if len(wheels) != 1:
        raise ValueError("AppStream metadata requires exactly one packaged wheel")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("Packaged wheel has ambiguous metadata")
        metadata = BytesParser().parsebytes(wheel.read(names[0]))
    version = metadata.get("Version")
    if metadata.get("Name", "").replace("_", "-").lower() != "yt-playlist" or not version:
        raise ValueError("Packaged wheel is missing the application version")
    tree = ET.parse(path)
    releases = tree.getroot().find("releases")
    if releases is None:
        raise ValueError("AppStream metadata is missing its release history")
    for release in list(releases):
        if release.get("version") == version:
            releases.remove(release)
    current = ET.Element("release", version=version,
                         date=date or datetime.now(timezone.utc).date().isoformat())
    ET.SubElement(ET.SubElement(current, "description"), "p").text = f"TuneConsole {version}."
    releases.insert(0, current)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metainfo", type=Path)
    parser.add_argument("wheel_directory", type=Path)
    args = parser.parse_args()
    print(stamp(args.metainfo, args.wheel_directory))
