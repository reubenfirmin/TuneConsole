"""Setup presentation for neutral live identity-check outcomes."""
from fastapi import APIRouter, Query


_OUTCOMES = {
    'invalid_brand_id': {
        'state': 'invalid', 'heading': 'Check the Brand account ID',
        'message': 'Enter only the account ID from /b/<id>/ in the Brand Accounts URL, '
                   'rather than a full URL, channel ID, or @handle.',
        'help_url': 'https://myaccount.google.com/brandaccounts',
        'help_label': 'Find your Brand account ID'},
    'unavailable': {
        'message': 'No connection is available for this identity. '
                   'Keep a signed-in music.youtube.com tab open in the browser with the '
                   'TuneConsole extension installed. Authentication checks resume automatically '
                   'when the extension connects.'},
    'disconnected': {
        'message': 'Could not reach YouTube Music through the extension. '
                   'Keep a signed-in music.youtube.com tab open in the browser with the '
                   'TuneConsole extension installed. Authentication checks resume automatically '
                   'when it reconnects.'},
    'brand_access_denied': {
        'state': 'invalid', 'heading': 'Cannot access this brand account',
        'message': 'Check the Brand account ID and make sure the Google account '
                   'signed in to YouTube Music owns or manages this brand account. '
                   'Use the steps below to compare the account ID.',
        'help_url': 'https://myaccount.google.com/brandaccounts',
        'help_label': 'Find your Brand account ID'},
    'auth_required': {
        'state': 'invalid', 'heading': 'Sign in required',
        'message': 'YouTube Music could not authenticate this identity. Sign in '
                   'to the right Google account, refresh the YouTube Music tab, '
                   'then return here for an automatic check.',
        'help_url': 'https://music.youtube.com', 'help_label': 'Open YouTube Music'},
    'unexpected_response': {
        'heading': 'Could not verify this identity',
        'message': 'YouTube returned an unexpected response. Make sure you are '
                   'signed in, refresh the YouTube Music tab, then return here '
                   'for an automatic check. Use the steps below to verify the account ID.'},
    'authenticated': {
        'state': 'valid', 'heading': 'Authenticated', 'message': 'Authenticated.'},
}


def present(result):
    return {'identity_id': result.identity_id, 'label': result.label,
            'state': 'unverified', 'heading': 'Not checked',
            'help_url': None, 'help_label': None, **_OUTCOMES[result.outcome]}


def build(checks, auth_labels):
    router = APIRouter()

    @router.get('/setup/identities/check')
    def setup_identity_checks(account_id: int | None = Query(None, alias='identity_id')):
        # Keep bridge calls in the sync request pool, leaving the WebSocket loop free.
        return {'checks': [present(result) for result in checks.check(account_id)],
                'auth_labels': auth_labels()}

    return router
