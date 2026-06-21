"""IdentityRepo — the YouTube Music accounts (identities) the app manages."""
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import Identity


class IdentityRepo(Repo):
    @synchronized
    def upsert_identity(self, label, credential_ref, brand_account_id, is_master):
        self.conn.execute(
            "INSERT INTO identities(label, credential_ref, brand_account_id, is_master) "
            "VALUES (?,?,?,?) ON CONFLICT(label) DO UPDATE SET "
            "credential_ref=excluded.credential_ref, "
            "brand_account_id=excluded.brand_account_id, "
            "is_master=excluded.is_master",
            (label, credential_ref, brand_account_id, int(is_master)))
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM identities WHERE label=?", (label,)).fetchone()
        return row["id"]

    @synchronized
    def get_identities(self) -> list[Identity]:
        rows = self.conn.execute("SELECT * FROM identities").fetchall()
        return [Identity(r["id"], r["label"], r["credential_ref"], r["brand_account_id"],
                         bool(r["is_master"]), r["last_auth_ok"]) for r in rows]

    @synchronized
    def get_master_identity(self):
        try:
            return next(i for i in self.get_identities() if i.is_master)
        except StopIteration:
            raise ValueError("No master identity configured")
