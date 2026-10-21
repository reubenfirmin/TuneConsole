import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

@dataclass
class IdentityConfig:
    label: str
    credential_ref: str
    brand_account_id: str | None
    is_master: bool
    identity_id: int | None = None

def credential_path(base_dir, credential_ref) -> Path:
    """Resolve an identity's credential file, refusing references that escape base_dir.

    credential_ref comes from config.toml; an absolute path or "../" would otherwise
    let it read an arbitrary file (the resulting auth blob is a secret).
    """
    base = Path(base_dir).resolve()
    path = (base / credential_ref).resolve()
    if base not in path.parents:
        raise ValueError(f"credential_ref {credential_ref!r} must be a file inside {base}")
    return path

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


def load_identities(path) -> list[IdentityConfig]:
    data = tomllib.loads(Path(path).read_text())
    entries = data.get("identity", [])
    out = [IdentityConfig(
        label=e["label"], credential_ref=e["credential_ref"],
        brand_account_id=e.get("brand_account_id"),
        is_master=bool(e.get("is_master", False)), identity_id=e.get("id")) for e in entries]
    validate_identities([{"label": c.label, "credential_ref": c.credential_ref,
                          "brand_account_id": c.brand_account_id, "is_master": c.is_master,
                          "id": c.identity_id} for c in out])
    masters = sum(i.is_master for i in out)
    if masters != 1:
        raise ValueError(f"exactly one identity must have is_master=true, found {masters}")
    return out
