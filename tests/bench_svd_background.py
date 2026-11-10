"""Opt-in recurring SVD, generation and refresh foreground-availability budgets.

uv run pytest tests/bench_svd_background.py -s
Historical publication comparisons live in experiments/investigations/.
"""
import pytest

from experiments.investigations.bench_svd_background import (
    _backup_library, _saved_library_run, _saved_library_source,
)


def test_saved_library_full_svd_in_background(tmp_path):
    _saved_library_run(tmp_path)


def test_saved_library_complete_generation_in_background(tmp_path):
    _saved_library_run(tmp_path, full_generation=True)


@pytest.mark.parametrize('refresh', ['partial', 'unchanged'])
def test_saved_library_refresh_in_background(tmp_path, refresh):
    _saved_library_run(tmp_path, refresh=refresh)
