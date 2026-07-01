"""Mutable, reloadable app state: lets the server start unconfigured and reload after setup."""
import logging
from pathlib import Path

from yt_playlist.core.config import load_identities
from yt_playlist.core.identities import build_client
from yt_playlist.core.setup import write_config, validate_identities

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store, config_path, creds_dir):
        self.store = store
        self.config_path = Path(config_path)
        self.creds_dir = Path(creds_dir)
        self._provider = None
        self._configured = False
        self.bridge = None  # shared Bridge instance; wired in __main__, used by the client provider

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def credentials_present(self) -> bool:
        return self.store.get_setting("bridge_paired") == "1"

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
            label_to_id = {
                c.label: self.store.upsert_identity(
                    c.label, c.credential_ref, c.brand_account_id, c.is_master)
                for c in cfgs}
        except (ValueError, KeyError) as e:
            logger.warning("config not usable, showing setup: %s", e)
            return
        by_label = {c.label: c for c in cfgs}

        def provider():
            return {iid: build_client(by_label[label], self.bridge)
                    for label, iid in label_to_id.items()}

        self._provider = provider
        self._configured = True

    def apply_setup(self, identities) -> None:
        """Validate input, write config, then reload.

        Raises ValueError on bad input. Identity definition (labels, brand_account_id, master) is
        independent of the credential: the extension bridge is paired separately and live (see
        credentials_present / bridge_paired), so identities can be saved on their own even before
        pairing completes.
        """
        validate_identities(identities)
        self.creds_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        write_config(identities, self.config_path)
        self.load()
        if not self._configured:
            raise ValueError("configuration saved but could not be loaded; check the values")
