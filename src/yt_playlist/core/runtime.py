"""Mutable, reloadable app state: lets the server start unconfigured and reload after setup."""
import logging
from pathlib import Path

from yt_playlist.core.config import load_identities, credential_path
from yt_playlist.core.identities import build_client
from yt_playlist.core.setup import (
    store_credentials, write_config, validate_identities, verify_capture,
    BROWSER_CREDENTIAL_FILENAME)

logger = logging.getLogger(__name__)

class Runtime:
    def __init__(self, store, config_path, creds_dir):
        self.store = store
        self.config_path = Path(config_path)
        self.creds_dir = Path(creds_dir)
        self._provider = None
        self._configured = False

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def credentials_present(self) -> bool:
        return (self.creds_dir / BROWSER_CREDENTIAL_FILENAME).exists()

    def clients(self) -> dict:
        """Client provider passed to the web app; raises if called while unconfigured."""
        if self._provider is None:
            raise RuntimeError("runtime is not configured")
        return self._provider()

    def load(self) -> None:
        """(Re)load config + credentials and rebuild the client provider.

        Defensive: any config/credential problem leaves the runtime unconfigured (the app then
        shows /setup) rather than crashing the server. Used at startup and after apply_setup.
        """
        self._provider = None
        self._configured = False
        if not self.config_path.exists():
            return
        try:
            cfgs = load_identities(self.config_path)
            cred_paths = {}
            for cfg in cfgs:
                path = credential_path(self.creds_dir, cfg.credential_ref)
                if not path.exists():
                    raise ValueError(f"identity {cfg.label!r}: credential file not found at {path}")
                cred_paths[cfg.label] = path
            label_to_id = {
                c.label: self.store.upsert_identity(
                    c.label, c.credential_ref, c.brand_account_id, c.is_master)
                for c in cfgs}
        except (ValueError, KeyError) as e:
            logger.warning("config not usable, showing setup: %s", e)
            return
        by_label = {c.label: c for c in cfgs}

        def provider():
            clients = {}
            for label, iid in label_to_id.items():
                clients[iid] = build_client(by_label[label], cred_paths[label].read_text())
            return clients

        self._provider = provider
        self._configured = True

    def check_auth(self, capture) -> str:
        """Live-verify a capture and return the signed-in account name (raises ValueError)."""
        return verify_capture(capture)[1]

    def sign_out(self) -> None:
        """Delete the saved sign-in (the local credential file) and reload.

        Only drops the captured cookies — the identity config stays put so re-signing in just
        means pasting a fresh capture. With no credential, load() leaves the runtime unconfigured.
        """
        (self.creds_dir / BROWSER_CREDENTIAL_FILENAME).unlink(missing_ok=True)
        self.load()

    def apply_setup(self, capture, identities) -> None:
        """Validate input, write credential + config, then reload. Raises ValueError on bad input.

        `capture` is the pasted sign-in (a 'Copy as cURL' or raw headers). Each identity dict
        must carry credential_ref. Validation happens before any write.
        """
        validate_identities(identities)
        self.creds_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if (capture or "").strip():
            store_credentials(capture, self.creds_dir / BROWSER_CREDENTIAL_FILENAME)
        elif not self.credentials_present:
            raise ValueError("provide your sign-in capture — there's no saved credential to reuse")
        write_config(identities, self.config_path)
        self.load()
        if not self._configured:
            raise ValueError("configuration saved but could not be loaded; check the values")
