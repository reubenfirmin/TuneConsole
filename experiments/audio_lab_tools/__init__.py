"""Offline audio research, grouped by responsibility and specialist."""
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def source_path(filename):
    """Locate a research source or worker independently of its package."""
    if Path(filename).name != filename:
        raise ValueError('Expected a research source filename')
    matches = sorted(TOOLS.rglob(filename))
    if len(matches) != 1:
        raise ValueError(f'Expected one research source for {filename}; found {len(matches)}')
    return matches[0]


def module_name(name):
    """Resolve a CLI's stable tool name to its current Python module."""
    relative = source_path(name + '.py').relative_to(TOOLS).with_suffix('')
    return 'experiments.audio_lab_tools.' + '.'.join(relative.parts)
