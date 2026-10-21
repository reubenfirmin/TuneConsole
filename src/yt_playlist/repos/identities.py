"""IdentityRepo: the YouTube Music accounts (identities) the app manages."""
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import Identity
from contextlib import contextmanager


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
        with self.reconciled(configs) as ids:
            return ids

    @contextmanager
    def reconciled(self, configs):
        """Keep reconciliation unpublished until the caller has prepared its config/runtime."""
        if not configs:
            raise ValueError("add at least one identity")
        with self._lock:
            self.conn.execute("SAVEPOINT setup_identities")
            try:
                existing = list(self.conn.execute("SELECT * FROM identities"))
                selected, used = [], set()
                for cfg in configs:
                    row = next((r for r in existing if r["id"] == cfg.identity_id), None) if cfg.identity_id else None
                    if row is None and cfg.identity_id is None:
                        row = next((r for r in existing if r["label"] == cfg.label), None)
                    if row is None and cfg.identity_id is None:
                        matches = [r for r in existing if r["credential_ref"] == cfg.credential_ref
                                   and (r["brand_account_id"] or None) == (cfg.brand_account_id or None)
                                   and r["id"] not in used]
                        row = matches[0] if len(matches) == 1 else None
                    iid = row["id"] if row else cfg.identity_id
                    if iid in used:
                        raise ValueError("each account may only appear once")
                    if iid is not None:
                        used.add(iid)
                    selected.append((cfg, iid))
                self.conn.execute("UPDATE identities SET active=0, is_master=0")
                # Temporary labels allow two existing accounts to exchange display labels.
                for _, iid in selected:
                    if iid is not None:
                        self.conn.execute("UPDATE identities SET label=? WHERE id=?", (f"@setup:{iid}", iid))
                ids = {}
                for cfg, iid in selected:
                    if iid is None:
                        iid = self.conn.execute("INSERT INTO identities(label,credential_ref,brand_account_id,is_master,active) "
                            "VALUES (?,?,?,?,1)", (cfg.label,cfg.credential_ref,cfg.brand_account_id,int(cfg.is_master))).lastrowid
                    else:
                        self.conn.execute("INSERT INTO identities(id,label,credential_ref,brand_account_id,is_master,active) "
                            "VALUES (?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET label=excluded.label, "
                            "credential_ref=excluded.credential_ref,brand_account_id=excluded.brand_account_id, "
                            "is_master=excluded.is_master,active=1", (iid,cfg.label,cfg.credential_ref,cfg.brand_account_id,int(cfg.is_master)))
                    ids[cfg.label] = iid
                yield ids
                self.conn.execute("RELEASE setup_identities")
            except BaseException:
                self.conn.execute("ROLLBACK TO setup_identities")
                self.conn.execute("RELEASE setup_identities")
                raise

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
