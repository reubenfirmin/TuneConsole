"""Opt-in actual-library adapters; normal CI retains its existing fixtures."""
import json
import os
from pathlib import Path


def report_file(path):
    """Keep existing check reports outside the container's disposable /tmp."""
    path = Path(path)
    return Path('/reports') / path.name if os.environ.get('TUNECONSOLE_IMAGE_RUN') == '1' else path


def managed_database(source=None, report=None):
    if os.environ.get('TUNECONSOLE_IMAGE_RUN') != '1':
        return None
    selected = os.environ.get('TUNECONSOLE_TEST_LIBRARY')
    if not selected:
        raise RuntimeError('Managed run has no library coordinate')
    path = Path(selected).resolve(strict=True)
    if source is not None and Path(source).resolve() != path:
        return None  # Synthetic recovery subjects still exercise their own copy.
    if report is not None:
        report['managed_library'] = json.loads(path.with_name('.baseline.json').read_text())
        report['source_version'] = report['managed_library']['schema']
        report['source_access'] = 'private filesystem snapshot; no per-test capture or relocation'
    return path


def benchmark_library(source, destination):
    report = {}
    managed = managed_database(source, report)
    if managed is not None:
        return managed, dict(report, backup_s=0, clone_method='filesystem-snapshot')
    from experiments.investigations.bench_svd_background import _backup_library
    return destination, _backup_library(source, destination)
