"""Read-only YouTube Music checks for saved identities, with actionable setup feedback."""
from yt_playlist.core.bridge import BridgeError
from yt_playlist.library.sync import _is_auth_error


def check_identities(ctx, identity_id=None):
    identities = ctx.store.identities.get_identities()
    if identity_id is not None:
        identities = [i for i in identities if i.id == identity_id]
    try:
        clients = ctx.clients() or {}
    except Exception:  # noqa: BLE001 - a broken provider must still leave setup usable
        ctx.logger.exception("could not build identity clients for setup checks")
        clients = {}
    results = []
    for identity in identities:
        result = {"identity_id": identity.id, "label": identity.label,
                  "state": "unverified", "heading": "Not checked",
                  "help_url": None, "help_label": None}
        brand = identity.brand_account_id or ""
        if brand.startswith(("http:", "https:", "@", "UC")) or "/" in brand:
            results.append({**result, "state": "invalid", "heading": "Check the Brand account ID",
                            "message": "Enter only the account ID from /b/<id>/ in the Brand Accounts URL, "
                                       "rather than a full URL, channel ID, or @handle.",
                            "help_url": "https://myaccount.google.com/brandaccounts",
                            "help_label": "Find your Brand account ID"})
            continue
        client = clients.get(identity.id)
        if client is None:
            results.append({**result, "message": "No connection is available for this identity. "
                            "Keep a signed-in music.youtube.com tab open in the browser with the "
                            "TuneConsole extension installed. Authentication checks resume automatically "
                            "when the extension connects."})
            continue
        try:
            account = client.get_account_info()
            if not isinstance(account, dict) or not account.get("accountName"):
                raise ValueError("YouTube did not return authenticated account information")
            # Check the library permission as well as the account menu. An empty library is valid.
            playlists = client.get_library_playlists(limit=1)
            if not isinstance(playlists, list):
                raise ValueError("YouTube did not return a playlist library")
        except BridgeError:
            results.append({**result, "message": "Could not reach YouTube Music through the extension. "
                            "Keep a signed-in music.youtube.com tab open in the browser with the "
                            "TuneConsole extension installed. Authentication checks resume automatically "
                            "when it reconnects."})
        except Exception as exc:  # noqa: BLE001 - one identity must not prevent the other checks
            ctx.logger.debug("identity check failed for %s", identity.label, exc_info=True)
            if brand and (_is_auth_error(exc) or "HTTP 400" in str(exc) or "HTTP 404" in str(exc)):
                results.append({**result, "state": "invalid", "heading": "Cannot access this brand account",
                                "message": "Check the Brand account ID and make sure the Google account "
                                           "signed in to YouTube Music owns or manages this brand account. "
                                           "Use the steps below to compare the account ID.",
                                "help_url": "https://myaccount.google.com/brandaccounts",
                                "help_label": "Find your Brand account ID"})
            elif _is_auth_error(exc):
                results.append({**result, "state": "invalid", "heading": "Sign in required",
                                "message": "YouTube Music could not authenticate this identity. Sign in "
                                           "to the right Google account, refresh the YouTube Music tab, "
                                           "then return here for an automatic check.",
                                "help_url": "https://music.youtube.com", "help_label": "Open YouTube Music"})
            else:
                results.append({**result, "heading": "Could not verify this identity",
                                "message": "YouTube returned an unexpected response. Make sure you are "
                                           "signed in, refresh the YouTube Music tab, then return here "
                                           "for an automatic check. Use the steps below to verify the account ID."})
        else:
            ctx.clear_auth_expired(identity.id)  # only a live, successful check proves access is restored
            results.append({**result, "state": "valid", "heading": "Authenticated",
                            "message": "Authenticated.",
                            "help_url": None})
    return results
