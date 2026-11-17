"""Mutable, reloadable app state: lets the server start unconfigured and reload after setup."""
import logging
import threading
from pathlib import Path

from yt_playlist.core.config import parse_accounts
from yt_playlist.core.config_files import ConfigFiles
from yt_playlist.core.identities import build_client
from yt_playlist.core.setup import serialize_config, BROWSER_CREDENTIAL_FILENAME

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store, config_path, creds_dir, *, files=None):
        self.store = store
        self.config_path = Path(config_path)
        self.creds_dir = Path(creds_dir)
        self.files = ConfigFiles() if files is None else files
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
        """(Re)load config + credentials and rebuild the client provider.

        Startup failures leave setup available; reload failures retain the last usable state.
        """
        with self._lock:
            if not self.files.exists(self.config_path):
                self._provision_default()
            if not self.files.exists(self.config_path):
                return
            try:
                content = self.files.read(self.config_path)
                cfgs = parse_accounts(content.decode('utf-8'))
                self._validate_clients(cfgs)
                if self.store.activity_paused:
                    ids = self.store.accounts.configured_ids(cfgs)
                else:
                    ids = self.store.accounts.replace_accounts(cfgs)
                provider = self._provider_for(cfgs, ids)
            except Exception as e:
                if self._last_config is not None:
                    self.files.restore(self._last_config, self.config_path)
                logger.warning("config not usable; retaining previous runtime: %s", e)
                return
            self._provider, self._configured, self._last_config = provider, True, content

    def _provision_default(self) -> None:
        """First run has no config yet. Credentials are live via the extension bridge now, so a single
        default identity is all a one-account user needs: write it so the app is usable immediately
        (no "Save an identity" gate) and the /setup wizard is only for people who actually have
        multiple (brand) identities. Best-effort: never let provisioning crash startup."""
        try:
            self.files.mkdir(self.creds_dir)
            self.files.mkdir(self.config_path.parent)
            content = serialize_config([{"label": "main", "is_master": True,
                                        "credential_ref": BROWSER_CREDENTIAL_FILENAME}]).encode('utf-8')
            with self.files.stage(self.config_path, content) as staged:
                self.files.publish(staged, self.config_path)
        except Exception:                       # noqa: BLE001 - app still works via /setup
            logger.warning("could not provision default identity", exc_info=True)

    def apply_setup(self, identities) -> None:
        """Validate clients, commit account bindings, then publish config and runtime.

        Raises ValueError on bad input. Account definition (labels, brand_account_id, master) is
        independent of the credential: the extension bridge is paired separately and live (see
        credentials_present / bridge_paired), so identities can be saved on their own even before
        pairing completes.
        """
        cfgs = parse_accounts(serialize_config(identities))
        self.files.mkdir(self.creds_dir)
        self.files.mkdir(self.config_path.parent)
        with self._lock:
            try:
                self._validate_clients(cfgs)
                with self.files.stage(self.config_path, serialize_config(identities).encode('utf-8')):
                    ids = self.store.accounts.replace_accounts(cfgs)
                    rows = [{"id": ids[c.label], "label": c.label, "credential_ref": c.credential_ref,
                             "brand_account_id": c.brand_account_id, "is_master": c.is_master} for c in cfgs]
                    content = serialize_config(rows).encode('utf-8')
                    final_cfgs = parse_accounts(content.decode('utf-8'))
                    with self.files.stage(self.config_path, content) as staged:
                        self.files.publish(staged, self.config_path)
                    provider = self._provider_for(final_cfgs, ids)
                self._provider, self._configured, self._last_config = provider, True, content
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                raise ValueError(f"Configuration was not saved: {error}") from error
