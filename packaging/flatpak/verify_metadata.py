"""Check the per-architecture AppStream catalog already verified by Flatpak."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def verify_cache(data_home, remote, arch, version):
    catalog = Path(data_home) / "flatpak/appstream" / remote / arch / "active/appstream.xml"
    root = ET.parse(catalog).getroot()
    for component in root.findall("component"):
        if component.findtext("id") != "com.tuneconsole.TuneConsole":
            continue
        release = component.find("releases/release")
        bundles = [bundle.text for bundle in component.findall("bundle") if bundle.get("type") == "flatpak"]
        if (release is not None and release.get("version") == version and
                any(bundle and bundle.startswith(f"app/com.tuneconsole.TuneConsole/{arch}/") for bundle in bundles)):
            return
    raise ValueError(f"Signed Flatpak catalog is missing {arch} / {version}")


if __name__ == "__main__":
    verify_cache(*sys.argv[1:])
