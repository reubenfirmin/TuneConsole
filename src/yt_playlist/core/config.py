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

def load_identities(path) -> list[IdentityConfig]:
    data = tomllib.loads(Path(path).read_text())
    entries = data.get("identity", [])
    out = [IdentityConfig(
        label=e["label"], credential_ref=e["credential_ref"],
        brand_account_id=e.get("brand_account_id"),
        is_master=bool(e.get("is_master", False))) for e in entries]
    masters = sum(i.is_master for i in out)
    if masters != 1:
        raise ValueError(f"exactly one identity must have is_master=true, found {masters}")
    return out
