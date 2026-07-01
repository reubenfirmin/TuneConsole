"""Guided setup: turn identity rows into a config file. Credentials come from the browser-extension
bridge pairing now, so this module only validates identities and writes config.toml."""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# All identities created via the UI share one captured credential, differing by brand_account_id.
BROWSER_CREDENTIAL_FILENAME = "browser.json"

def validate_identities(identities) -> None:
    """Validate the identity rows; raise ValueError on the first problem."""
    if not identities:
        raise ValueError("add at least one identity")
    labels = [(i.get("label") or "").strip() for i in identities]
    if any(not label for label in labels):
        raise ValueError("every identity needs a label")
    if len(set(labels)) != len(labels):
        raise ValueError("identity labels must be unique")
    masters = sum(1 for i in identities if i.get("is_master"))
    if masters != 1:
        raise ValueError(f"exactly one identity must be the master, found {masters}")

def _toml_str(s) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'

def write_config(identities, config_path) -> None:
    """Write config.toml from validated identity rows (each needs label + credential_ref)."""
    validate_identities(identities)
    blocks = []
    for i in identities:
        lines = ["[[identity]]",
                 f"label = {_toml_str(i['label'].strip())}",
                 f"credential_ref = {_toml_str(i['credential_ref'])}"]
        brand = (i.get("brand_account_id") or "")
        brand = brand.strip() if isinstance(brand, str) else brand
        if brand:
            lines.append(f"brand_account_id = {_toml_str(brand)}")
        if i.get("is_master"):
            lines.append("is_master = true")
        blocks.append("\n".join(lines))
    Path(config_path).write_text("\n\n".join(blocks) + "\n")
