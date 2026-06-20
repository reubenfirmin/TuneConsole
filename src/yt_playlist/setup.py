"""Guided setup: turn a pasted YTM sign-in + identity rows into credential + config files."""
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# All identities created via the UI share one captured credential, differing by brand_account_id.
BROWSER_CREDENTIAL_FILENAME = "browser.json"

_CURL_HEADER_RE = re.compile(r"(?:-H|--header)\s+(['\"])(.*?)\1", re.S)
_CURL_COOKIE_RE = re.compile(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", re.S)

def _headers_from_curl(text) -> str:
    """Pull header lines out of a 'Copy as cURL' command (any browser)."""
    lines = [m.group(2) for m in _CURL_HEADER_RE.finditer(text)]
    lines += [f"cookie: {m.group(2)}" for m in _CURL_COOKIE_RE.finditer(text)]
    return "\n".join(lines)

def normalize_capture(text) -> str:
    """Accept a 'Copy as cURL' command or a raw request-headers block; return header lines."""
    text = (text or "").strip()
    if not text:
        return ""
    if "curl " in text or "-H " in text or "--header" in text:
        extracted = _headers_from_curl(text)
        if extracted:
            return extracted
    return text  # assume it's already a raw "Header: value" block

def verify_capture(capture):
    """Validate a capture against the live API; return (blob, account_name).

    Raises ValueError (user-facing) if it can't be parsed or the sign-in doesn't actually work.
    Unlike store_credentials this makes a network call (get_account_info) so we can confirm auth
    and show who's signed in.
    """
    import ytmusicapi
    from ytmusicapi import YTMusic
    if not (capture or "").strip():
        raise ValueError("provide a sign-in capture to check")
    headers_raw = normalize_capture(capture)
    try:
        blob = ytmusicapi.setup(headers_raw=headers_raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"could not read those headers ({e}).")
    try:
        info = YTMusic(blob).get_account_info()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"sign-in didn't work ({e}). Make sure you're logged in and used a "
                         f"/browse request from music.youtube.com.")
    name = (info or {}).get("accountName") or "your account"
    return blob, name

def store_credentials(capture, dest_path) -> None:
    """Parse a captured YTM sign-in (a 'Copy as cURL' or raw headers) into a credential file.

    Raises ValueError (with a user-facing message) if it can't be parsed or doesn't look like a
    signed-in YTM session. Imports are local so the module stays importable without a live
    ytmusicapi in unrelated tests.
    """
    import ytmusicapi
    from ytmusicapi import YTMusic
    if not (capture or "").strip():
        raise ValueError("paste a 'Copy as cURL' command or your request headers")
    headers_raw = normalize_capture(capture)
    try:
        blob = ytmusicapi.setup(headers_raw=headers_raw)
    except Exception as e:  # noqa: BLE001 - surface any parse failure to the user
        raise ValueError(
            f"could not read those headers ({e}). Use a /browse request from music.youtube.com "
            f"while signed in, copied as cURL (or raw request headers).")
    try:
        YTMusic(blob)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"those headers don't look like a signed-in YTM session ({e}).")
    Path(dest_path).write_text(blob)

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
