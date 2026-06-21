"""RediscoverRepo — the Rediscover page's snooze/dismiss state for stale playlists."""
from yt_playlist.repos.base import Repo, synchronized


class RediscoverRepo(Repo):
    @synchronized
    def dismiss_stale(self, ytm, until=None) -> None:
        # until=None → dismissed forever; else a unix-ts the snooze expires at
        self.conn.execute("INSERT OR REPLACE INTO stale_dismissed(ytm,until) VALUES (?,?)", (ytm, until))
        self.conn.commit()

    @synchronized
    def restore_stale(self, ytm) -> None:
        self.conn.execute("DELETE FROM stale_dismissed WHERE ytm = ?", (ytm,))
        self.conn.commit()

    @synchronized
    def get_stale_dismissed(self, now) -> list[tuple]:
        # rows still in effect (forever, or snoozed until > now), as (ytm, until)
        rows = self.conn.execute("SELECT ytm, until FROM stale_dismissed").fetchall()
        return [(r["ytm"], r["until"]) for r in rows if r["until"] is None or r["until"] > now]

    def get_stale_hidden_ytm(self, now) -> set:
        return {ytm for ytm, _ in self.get_stale_dismissed(now)}
