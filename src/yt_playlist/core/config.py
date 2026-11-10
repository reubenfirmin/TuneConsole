import tomllib
from dataclasses import dataclass
from pathlib import Path

@dataclass
class AccountConfig:
    label: str
    credential_ref: str
    brand_account_id: str | None
    is_master: bool
    account_id: int | None = None


def account_flag(value, field='is_master') -> int:
    """Accept explicit booleans and stored integer flags without truthiness coercion."""
    if type(value) not in (bool, int) or value not in (0, 1):
        raise ValueError(f'{field} must be a boolean or integer 0 or 1')
    return int(value)


def validate_accounts(identities) -> None:
    """Validate the identity rows; raise ValueError on the first problem."""
    if not identities:
        raise ValueError("add at least one identity")
    for row in identities:
        account_flag(row.get('is_master', False))
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


def load_accounts(path) -> list[AccountConfig]:
    data = tomllib.loads(Path(path).read_text())
    entries = data.get("identity", [])
    out = [AccountConfig(
        label=e["label"], credential_ref=e["credential_ref"],
        brand_account_id=e.get("brand_account_id"),
        is_master=e.get("is_master", False), account_id=e.get("id")) for e in entries]
    validate_accounts([{"label": c.label, "credential_ref": c.credential_ref,
                          "brand_account_id": c.brand_account_id, "is_master": c.is_master,
                          "id": c.account_id} for c in out])
    for config in out:
        config.is_master = bool(config.is_master)
    return out
