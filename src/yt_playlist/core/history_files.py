"""Recovery files for explicitly verified listening-history repair."""
import time

from yt_playlist.core import paths
from yt_playlist.core.repair import _publish_backup


class HistoryRepairFiles:
    def publish(self, rows, watermark):
        path = paths.backups_dir() / f'phantom_history_purge_{watermark}_{time.time_ns()}.jsonl'
        _publish_backup(path, rows)
        return path
