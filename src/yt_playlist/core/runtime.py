"""Mutable, reloadable app state: lets the server start unconfigured and reload after setup."""
import logging
import threading
from pathlib import Path

from yt_playlist.core.config import account_configs, load_accounts
from yt_playlist.core.identities import build_client

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store, config_path):
        self.store = store
        self.config_path = Path(config_path)
        self._provider = None
        self._configured = False
        self._lock = threading.RLock()
        self.bridge = None  # shared Bridge instance; wired in __main__, used by the client provider

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def credentials_present(self) -> bool:
        return self.store.settings.get_setting("bridge_paired") == "1"

    def clients(self) -> dict:
        """Client provider passed to the web app. Returns {} while unconfigured (no identity yet) so
        callers degrade gracefully now that the dashboard is reachable before setup, instead of the
        old forced /setup redirect."""
        with self._lock:
            return self._provider() if self._provider else {}

    def _validate_clients(self, configs):
        # Client construction performs no network I/O, but can fail; do it before publication.
        for cfg in configs:
            build_client(cfg, self.bridge)

    def _provider_for(self, configs, ids):
        # The bridge is wired after startup load; resolve its current value on each call.
        return lambda: {ids[c.label]: build_client(c, self.bridge) for c in configs}

    def load(self) -> None:
        """Import existing bindings once, then build clients from the database."""
        with self._lock:
            accounts = self.store.accounts.get_accounts()
            try:
                if not self.store.activity_paused and not self.store.accounts.bindings_saved():
                    try:
                        configs = load_accounts(self.config_path)
                    except FileNotFoundError:
                        configs = []
                    if not configs:
                        configs = account_configs([
                            {'id': row.id, 'label': row.label, 'brand_account_id': row.brand_account_id,
                             'is_master': row.is_master} for row in accounts
                        ] or [{'label': 'main', 'is_master': True}])
                    self._validate_clients(configs)
                    self.store.accounts.replace_accounts(configs, importing=True)
                    accounts = self.store.accounts.get_accounts()
            except Exception:
                logger.warning('could not import account bindings', exc_info=True)
                accounts = self.store.accounts.get_accounts()
            if accounts:
                try:
                    self._validate_clients(accounts)
                except Exception:
                    logger.warning('account bindings could not build clients', exc_info=True)
                    return
                self._provider = self._provider_for(accounts, {row.label: row.id for row in accounts})
                self._configured = True

    def apply_setup(self, identities) -> None:
        """Validate clients, commit account bindings, then update the runtime.

        Raises ValueError on bad input. Account definition (labels, brand_account_id, master) is
        independent of the credential: the extension bridge is paired separately and live (see
        credentials_present / bridge_paired), so identities can be saved on their own even before
        pairing completes.
        """
        cfgs = account_configs(identities)
        with self._lock:
            try:
                self._validate_clients(cfgs)
                ids = self.store.accounts.replace_accounts(cfgs)
                self._provider, self._configured = self._provider_for(cfgs, ids), True
            except Exception as error:
                raise ValueError(f"Configuration was not saved: {error}") from error
