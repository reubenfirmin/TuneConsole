"""Build and verify the current specialist-only production wheel."""
import argparse
from pathlib import Path
import subprocess

from ..focus.focus_experiment import inside
from ..focus.focus_package_check import check_installed
from tempfile import TemporaryDirectory


def check(directory):
    if directory.exists():
        raise ValueError('Choose a new packaging check directory')
    directory.mkdir(parents=True)
    subprocess.run(['uv', 'build', '--offline', '--wheel', '--out-dir', str(directory)], check=True)
    wheel, = directory.glob('*.whl')
    with TemporaryDirectory(prefix='wheel-', dir=directory) as installed:
        return check_installed(directory, wheel, Path(installed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    check(inside(args.directory))


if __name__ == '__main__':
    main()
