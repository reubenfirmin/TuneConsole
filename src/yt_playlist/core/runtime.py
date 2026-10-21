"""Mutable, reloadable app state: lets the server start unconfigured and reload after setup."""
import logging
import os
import threading
from pathlib import Path

from yt_playlist.core.config import load_identities
from yt_playlist.core.identities import build_client
from yt_playlist.core.setup import (write_config, validate_identities, prepared_config,
                                    restore_config, BROWSER_CREDENTIAL_FILENAME)

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store, config_path, creds_dir):
        self.store = store
        self.config_path = Path(config_path)
        self.creds_dir = Path(creds_dir)
        self._provider = None
        self._configured = False
        self._lock = threading.RLock()
        self._last_config = None
        self.bridge = None  # shared Bridge instance; wired in __main__, used by the client provider

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def credentials_present(self) -> bool:
        return self.store.get_setting("bridge_paired") == "1"

    def clients(self) -> dict:
        """Client provider passed to the web app. Returns {} while unconfigured (no identity yet) so
        callers degrade gracefully now that the dashboard is reachable before setup, instead of the
        old forced /setup redirect."""
        with self._lock:
            return self._provider() if self._provider else {}

    def _prepare_provider(self, configs, ids):
        # Client construction performs no network I/O, but can fail; do it before publication.
        for cfg in configs:
            build_client(cfg, self.bridge)
        # The bridge is wired after startup load; resolve its current value on each call.
        return lambda: {ids[c.label]: build_client(c, self.bridge) for c in configs}

    def load(self) -> None:
        """(Re)load config + credentials and rebuild the client provider.

        Startup failures leave setup available; reload failures retain the last usable state.
        """
        with self._lock:
            if not self.config_path.exists():
                self._provision_default()
            if not self.config_path.exists():
                return
            try:
                cfgs = load_identities(self.config_path)
                content = self.config_path.read_bytes()
                with self.store.identities.reconciled(cfgs) as ids:
                    provider = self._prepare_provider(cfgs, ids)
            except Exception as e:
                if self._last_config is not None:
                    restore_config(self._last_config, self.config_path)
                logger.warning("config not usable; retaining previous runtime: %s", e)
                return
            self._provider, self._configured, self._last_config = provider, True, content

    def _provision_default(self) -> None:
        """First run has no config yet. Credentials are live via the extension bridge now, so a single
        default identity is all a one-account user needs: write it so the app is usable immediately
        (no "Save an identity" gate) and the /setup wizard is only for people who actually have
        multiple (brand) identities. Best-effort: never let provisioning crash startup."""
        try:
            self.creds_dir.mkdir(parents=True, exist_ok=True)
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            write_config([{"label": "main", "is_master": True,
                           "credential_ref": BROWSER_CREDENTIAL_FILENAME}], self.config_path)
        except Exception:                       # noqa: BLE001 - app still works via /setup
            logger.warning("could not provision default identity", exc_info=True)

    def apply_setup(self, identities) -> None:
        """Prepare a complete usable candidate, then atomically publish it with reconciliation.

        Raises ValueError on bad input. Identity definition (labels, brand_account_id, master) is
        independent of the credential: the extension bridge is paired separately and live (see
        credentials_present / bridge_paired), so identities can be saved on their own even before
        pairing completes.
        """
        validate_identities(identities)
        self.creds_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            previous = self.config_path.read_bytes() if self.config_path.exists() else None
            published = False
            try:
                with prepared_config(identities, self.config_path) as (_, cfgs):
                    with self.store.identities.reconciled(cfgs) as ids:
                        rows = [{"id": ids[c.label], "label": c.label, "credential_ref": c.credential_ref,
                                 "brand_account_id": c.brand_account_id, "is_master": c.is_master} for c in cfgs]
                        with prepared_config(rows, self.config_path) as (staged, final_cfgs):
                            provider = self._prepare_provider(final_cfgs, ids)
                            content = staged.read_bytes()
                            os.replace(staged, self.config_path)
                            published = True
                self._provider, self._configured, self._last_config = provider, True, content
            except BaseException as error:
                if published:
                    restore_config(previous, self.config_path)
                if not isinstance(error, Exception):
                    raise
                raise ValueError(f"Configuration was not saved: {error}") from error
