"""Guided setup: turn identity rows into a config file. Credentials come from the browser-extension
bridge pairing now, so this module only validates identities and writes config.toml."""
import logging
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# All identities created via the UI share one captured credential, differing by brand_account_id.
BROWSER_CREDENTIAL_FILENAME = "browser.json"

def validate_identities(identities) -> None:
    """Validate the identity rows; raise ValueError on the first problem."""
    if not identities:
        raise ValueError("add at least one identity")
    for row in identities:
        for field in ("label", "credential_ref", "brand_account_id"):
            value = row.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be text")
            if isinstance(value, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                raise ValueError(f"{field} contains an unsupported Unicode surrogate")
        if row.get("id") is not None and (type(row["id"]) is not int or row["id"] <= 0):
            raise ValueError("identity ID must be a positive integer")
    ids = [row["id"] for row in identities if row.get("id") is not None]
    if len(ids) != len(set(ids)):
        raise ValueError("identity IDs must be unique")
    labels = [(i.get("label") or "").strip() for i in identities]
    if any(not label for label in labels):
        raise ValueError("every identity needs a label")
    if len(set(labels)) != len(labels):
        raise ValueError("identity labels must be unique")
    masters = sum(1 for i in identities if i.get("is_master"))
    if masters != 1:
        raise ValueError(f"exactly one identity must be the master, found {masters}")

def _toml_str(s) -> str:
    # JSON's quoted strings use TOML-compatible escapes, including all ASCII control characters.
    # Leave non-ASCII characters intact so supplementary codepoints do not become surrogate pairs.
    return json.dumps(str(s), ensure_ascii=False).replace("\x7f", "\\u007f")

def serialize_config(identities) -> str:
    validate_identities(identities)
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
    from yt_playlist.core.config import load_identities

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
        configs = load_identities(staged)
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
