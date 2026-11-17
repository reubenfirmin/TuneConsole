"""Validate an entire transfer selection before any provider mutation."""


def transfer_selection(ids, source, target, accounts, playlists):
    active = {account.id for account in accounts}
    if source not in active or target not in active:
        raise ValueError('An identity has been removed. Choose identities again.')
    if source == target:
        raise ValueError('Pick two different identities.')
    try:
        selected = list(dict.fromkeys(int(pid) for pid in ids.split(',') if pid.strip()))
    except ValueError:
        raise ValueError('The playlist selection is invalid. Select playlists again.') from None
    if not selected:
        raise ValueError('Select at least one playlist.')
    if any(playlists.get(pid) is None or playlists[pid].account_id != source for pid in selected):
        raise ValueError('The playlist selection has changed. Select playlists again.')
    return selected


class TransferCommands:
    def __init__(self, playlists, accounts, *, move, refresh, logger):
        self.playlists, self.accounts = playlists, accounts
        self.move, self.refresh, self.logger = move, refresh, logger

    def panel(self, source=None, target=None):
        accounts = self.accounts.get_accounts()
        labels = {account.id: account.label for account in accounts}
        rows = sorted((p for p in self.playlists.get_playlists() if p.account_id in labels),
                      key=lambda p: (labels[p.account_id], p.title.lower()))
        source, target = panel_accounts(accounts, source, target)
        return dict(playlists=rows, identities=accounts, labels=labels, source=source, target=target,
                    selection_rows=[{'id': p.id, 'account_id': p.account_id} for p in rows])

    def row(self, pid):
        return self.playlists.get_playlist(pid)

    def bulk(self, ids, source, target, copy_only):
        selected = transfer_selection(ids, source, target, self.accounts.get_accounts(),
                                      {p.id: p for p in self.playlists.get_playlists()})
        return [self.transfer(pid, target, copy_only) for pid in selected]

    def transfer(self, pid, target, copy_only):
        source = self.playlists.get_playlist(pid)
        result = dict(title=source.title if source else 'Unavailable playlist', warning=False)
        if source is not None and source.account_id == target:
            return {**result, 'state': 'error', 'message': 'The source and target identity are the same.', 'warning': True}
        try:
            receipt = self.move(pid, target, copy_only=bool(copy_only))
        except Exception:
            self.logger.exception('move/copy failed')
            return {**result, 'state': 'error', 'message': 'Transfer not confirmed. Review Actions before trying again.', 'warning': True}
        count = receipt['added']
        tracks = f"{count} track{'s' if count != 1 else ''}"
        if receipt['deleted']:
            self.refresh(source.provider_playlist_id)
            return {**result, 'state': 'moved', 'message': f'Moved {tracks}.'}
        extra = f"; {receipt['unresolved']} couldn’t be matched" if receipt['unresolved'] else ''
        if receipt.get('skipped'):
            extra += f"; {receipt['skipped']} couldn’t be added"
        message = f'Copied {tracks}{extra}.'
        if receipt.get('delete_error'):
            message = 'Copy created. Source deletion needs review in Actions.'
        elif not copy_only:
            message += ' Source kept because some tracks couldn’t be recreated.'
        return {**result, 'state': 'copied', 'message': message,
                'warning': bool(receipt['unresolved'] or receipt.get('skipped') or receipt.get('delete_error'))}


def panel_accounts(accounts, source, target):
    active = {a.id for a in accounts}
    if source not in active:
        source = accounts[0].id if accounts else None
    if target not in active:
        target = next((a.id for a in accounts if a.id != source), source)
    return source, target
