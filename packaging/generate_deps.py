"""Generate Flatpak's offline wheels from uv.lock, without resolving dependencies again."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packaging/flatpak/python3-requirements.json"


def requirements(python, arch, exported):
    env = {**default_environment(), "sys_platform": "linux", "os_name": "posix",
           "platform_system": "Linux", "platform_machine": arch,
           "implementation_name": "cpython", "platform_python_implementation": "CPython",
           "python_version": python, "python_full_version": python + ".0",
           "implementation_version": python + ".0", "extra": ""}
    result = {}
    for line in exported.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        req = Requirement(line)
        if req.marker and not req.marker.evaluate(env):
            continue
        specs = list(req.specifier)
        if req.url or len(specs) != 1 or specs[0].operator != "==":
            raise ValueError(f"Expected a locked version: {line}")
        result[canonicalize_name(req.name)] = specs[0].version
    return result


def wheel_for(package, python, arch):
    version = tuple(map(int, python.split(".")))
    # Both maintained runtimes support at least glibc 2.28. Prefer portable binary wheels.
    platforms = [f"manylinux_2_{minor}_{arch}" for minor in range(28, 16, -1)]
    platforms.append(f"manylinux2014_{arch}")
    tags = list(cpython_tags(version, platforms=platforms))
    tags += list(compatible_tags(version, interpreter="cp" + python.replace(".", ""),
                                 platforms=platforms))
    rank = {tag: i for i, tag in enumerate(tags)}
    candidates = []
    for wheel in package.get("wheels", []):
        filename = unquote(Path(urlsplit(wheel["url"]).path).name)
        _, _, _, wheel_tags = parse_wheel_filename(filename)
        priority = min((rank[tag] for tag in wheel_tags if tag in rank), default=None)
        if priority is not None:
            candidates.append((priority, filename, wheel))
    if not candidates:
        raise ValueError(f"No locked CPython {python} {arch} wheel for {package['name']}=={package['version']}")
    _, filename, wheel = min(candidates, key=lambda item: (item[0], item[1]))
    algorithm, digest = wheel["hash"].split(":", 1)
    if algorithm != "sha256" or len(digest) != 64:
        raise ValueError(f"Missing SHA256 for {filename}")
    return {"type": "file", "url": wheel["url"], "sha256": digest, "dest-filename": filename}


def render(lock, config, exported):
    modules = []
    for arch in config["arches"]:
        selected = requirements(config["python"], arch, exported)
        sources = []
        for name, version in sorted(selected.items()):
            package = next((p for p in lock["package"]
                            if canonicalize_name(p["name"]) == name and p["version"] == version), None)
            if package is None:
                raise ValueError(f"Export is absent from uv.lock: {name}=={version}")
            sources.append(wheel_for(package, config["python"], arch))
        pins = " ".join(f"{name}=={version}" for name, version in sorted(selected.items()))
        modules.append({"name": f"python3-deps-{arch}", "only-arches": [arch],
                        "buildsystem": "simple", "build-commands": [
                            "pip3 install --no-index --no-deps --no-build-isolation "
                            "--find-links=file://${PWD} --prefix=${FLATPAK_DEST} " + pins],
                        "sources": sources})
    return json.dumps({"name": "python3-requirements", "buildsystem": "simple",
                       "build-commands": [], "modules": modules}, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    config = tomllib.loads((ROOT / "packaging/platforms.toml").read_text())["flatpak"]
    exported = subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project",
                               "--no-hashes", "--no-header", "--no-annotate"],
                              cwd=ROOT, check=True, capture_output=True, text=True).stdout
    content = render(lock, config, exported)
    if args.check:
        if OUTPUT.read_text() != content:
            sys.exit("Flatpak dependencies are stale; run packaging/flatpak/generate-pip-sources.sh")
    else:
        OUTPUT.write_text(content)


if __name__ == "__main__":
    main()
