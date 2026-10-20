"""IdentityRepo: the YouTube Music accounts (identities) the app manages."""
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import Identity


_UPSERT = (
    "INSERT INTO identities(label, credential_ref, brand_account_id, is_master, active) "
    "VALUES (?,?,?,?,1) ON CONFLICT(label) DO UPDATE SET "
    "credential_ref=excluded.credential_ref, brand_account_id=excluded.brand_account_id, "
    "is_master=excluded.is_master, active=1"
)


class IdentityRepo(Repo):
    @synchronized
    def upsert_identity(self, label, credential_ref, brand_account_id, is_master):
        self.conn.execute(
            _UPSERT,
            (label, credential_ref, brand_account_id, int(is_master)))
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM identities WHERE label=?", (label,)).fetchone()
        return row["id"]

    @synchronized
    def replace_identities(self, configs) -> dict[str, int]:
        """Reconcile the saved config, retaining removed rows for their history and cached data."""
        if not configs:
            raise ValueError("add at least one identity")
        with self.conn:
            self.conn.execute("UPDATE identities SET active=0, is_master=0")
            self.conn.executemany(_UPSERT, [
                (c.label, c.credential_ref, c.brand_account_id, int(c.is_master)) for c in configs])
        return {r["label"]: r["id"] for r in self.conn.execute(
            "SELECT id, label FROM identities WHERE active=1")}

    @synchronized
    def get_identities(self) -> list[Identity]:
        rows = self.conn.execute("SELECT * FROM identities WHERE active=1").fetchall()
        return [Identity(r["id"], r["label"], r["credential_ref"], r["brand_account_id"],
                         bool(r["is_master"]), r["last_auth_ok"]) for r in rows]

    @synchronized
    def get_master_identity(self):
        try:
            return next(i for i in self.get_identities() if i.is_master)
        except StopIteration:
            raise ValueError("No master identity configured")
