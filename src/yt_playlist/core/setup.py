"""Guided setup: turn identity rows into a config file. Credentials come from the browser-extension
bridge pairing now, so this module only validates identities and writes config.toml."""
import logging
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from yt_playlist.core import config

logger = logging.getLogger(__name__)

# All identities created via the UI share one captured credential, differing by brand_account_id.
BROWSER_CREDENTIAL_FILENAME = "browser.json"

def _toml_str(s) -> str:
    # JSON's quoted strings use TOML-compatible escapes, including all ASCII control characters.
    # Leave non-ASCII characters intact so supplementary codepoints do not become surrogate pairs.
    return json.dumps(str(s), ensure_ascii=False).replace("\x7f", "\\u007f")

def serialize_config(identities) -> str:
    config.validate_identities(identities)
    if any(not isinstance(row.get("credential_ref"), str) for row in identities):
        raise ValueError("every identity needs a credential reference")
    blocks = []
    for i in identities:
        lines = ["[[identity]]",
                 f"label = {_toml_str(i['label'].strip())}",
                 f"credential_ref = {_toml_str(i['credential_ref'])}"]
        if i.get("id") is not None:
            lines.append(f"id = {i['id']}")
        brand = (i.get("brand_account_id") or "")
        brand = brand.strip() if isinstance(brand, str) else brand
        if brand:
            lines.append(f"brand_account_id = {_toml_str(brand)}")
        if i.get("is_master"):
            lines.append("is_master = true")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


@contextmanager
def prepared_config(identities, config_path):
    """Fully write, flush, parse and validate a candidate beside its atomic publication target."""
    content = serialize_config(identities)
    path = Path(config_path)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as file:
            staged = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        configs = config.load_identities(staged)
        yield staged, configs
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def write_config(identities, config_path) -> None:
    with prepared_config(identities, config_path) as (staged, _):
        os.replace(staged, config_path)


def restore_config(content, config_path):
    """Restore an already validated last-good file after a failed multi-resource publication."""
    path = Path(config_path)
    if content is None:
        path.unlink(missing_ok=True)
        return
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
            staged = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
