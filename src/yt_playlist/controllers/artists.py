"""Choose artist identity evidence while the repository stores its rows."""
import json
from yt_playlist.core import artist_credits, artist_policy
from yt_playlist.core.metadata_contract import clock


class Artists:
    def __init__(self, repository, state, reconcile_discovery, originals):
        self.repository = repository
        self.state, self.reconcile_discovery = state, reconcile_discovery
        self.originals = originals

    def ensure_artist(self, name, *, provider=None, provider_id=None, previous=None):
        verified = bool(provider and provider_id)
        context = self.repository.identity_inputs(previous) if previous is not None else None
        if context and verified and context['ambiguous']:
            context = None
        with self.repository.transaction():
            identity = (context['id'] if context and not verified else
                        self.repository.provider_id(provider, provider_id) if verified else
                        self.repository.provisional_id(name))
            reconcile = identity is not None and context is not None and verified
            if identity is None:
                if verified and context and context['exists'] and context['provisional'] and not context['identified']:
                    identity = context['id']
                    self.repository.promote(identity)
                else:
                    identity = self.repository.create(name, provisional=not verified)
                if verified:
                    self.repository.add_provider(identity, provider, provider_id)
            if name and not reconcile:
                self.repository.add_alias(identity, name, provider or 'unidentified_credit')
        if reconcile:
            identity = self.reconcile_provisional(context['id'], identity, source=provider)
            if name:
                with self.repository.transaction():
                    self.repository.add_alias(identity, name, provider)
        return identity

    def reconcile_provisional(self, previous, target, *, source, now=None):
        before = self.repository.identity_inputs(previous)
        after = self.repository.identity_inputs(target)
        previous, target = before['id'], after['id']
        if (previous == target or not before['exists'] or not before['provisional']
                or before['identified'] or before['ambiguous']):
            return target
        if not after['exists'] or after['provisional'] or not after['identified']:
            return previous
        self.reconcile_discovery(previous, target, source=source)
        self.state.reconcile_artist(previous, target, source=source)
        self.repository.reconcile_identity(previous, target, source, clock(now))
        return target

    def credit_subject(self, existing, name):
        candidates = self.repository.credit_subject_candidates(existing, name)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def has_alias(self, identity, name):
        return self.repository.has_alias(identity, name)

    def release_subject(self, album_id, previous):
        if previous is None:
            return None
        inputs = self.repository.release_context(album_id, previous)
        identity = inputs['identity']
        if not identity['exists'] or not identity['provisional']:
            return previous
        if inputs['other_albums'] or inputs['other_entries']:
            self.originals.retain('artist', 'ambiguous_album_artist', str(previous), json.dumps({
                'artist_id': previous, 'album_id': album_id, 'other_albums': inputs['other_albums'],
                'other_entries': inputs['other_entries']}),
                'exact release identifies only its own unresolved credit; shared-name state remains ambiguous')
            self.repository.mark_ambiguity(previous)
            return None
        return None if identity['ambiguous'] else previous

    def record_credit_choice(self, entry_id, value, *, now=None):
        current = self.repository.credit_inputs(entry_id)
        if len(current['artists']) == 1:
            with self.repository.transaction():
                self.repository.add_alias(current['artists'][0], value, 'user_correction')
        elif not current['artists']:
            structured = [{'name': value, 'id': None}]
            credit = artist_credits.normalize_credit(value, structured, 'user_correction')
            identity = self.ensure_artist(value, provider='user_correction')
            credit = artist_credits.bind_credit(credit, [identity])
            self.repository.store_credit(current['entry_id'], credit, structured,
                                         'user_correction', clock(now), typed=current['typed'])

    def observe(self, entry_id, credit, credits=None, *, browse_id=None, source='youtube', now=None,
                delivery_key=None, metadata_delivery_id=None):
        inputs = self.repository.credit_observation_inputs(entry_id)
        existing = inputs['credits']
        structured = [value for value in (credits or []) if isinstance(value, dict) and value.get('name')]
        if not structured and existing and (not browse_id or len(existing) != 1
                                           or self.credit_subject(existing, credit) is None):
            return
        if not structured:
            if not credit:
                return
            structured = [{'name': credit, 'id': browse_id}]
        value = artist_credits.normalize_credit(credit or '', structured, source)
        identities = [self.ensure_artist(member['name'], provider=source,
            provider_id=member.get('id') or member.get('browseId'),
            previous=self.credit_subject(existing, member['name'])) for member in structured]
        value = artist_credits.bind_credit(value, identities)
        self.repository.store_credit(inputs['entry_id'], value, structured, source, clock(now),
            typed=inputs['typed'], delivery_key=delivery_key, metadata_delivery_id=metadata_delivery_id)

    def resolve(self, value):
        return self.resolve_many([value])[value]

    def resolve_exact(self, value):
        return self.resolve_many([value], exact=True)[value]

    def resolve_many(self, values, *, exact=False):
        values = list(dict.fromkeys(values))
        references = {value: artist_policy.reference_id(value) for value in values if artist_policy.is_reference(value)}
        names = [value for value in values if value not in references]
        identities, aliases = self.repository.reference_rows(
            [identity for identity in references.values() if identity is not None], names)
        missing = [name for name in names if not aliases[name]]
        if missing and not exact:
            aliases.update(artist_policy.normalized_matches(self.repository.alias_rows(), missing))
        return {value: identities.get(references[value]) if value in references else artist_policy.unique(aliases[value])
                for value in values}

    def state_id(self, value):
        identity = self.resolve_exact(value)
        if identity is not None:
            return identity
        if artist_policy.is_reference(value):
            raise ValueError(f'Unknown artist reference: {value}')
        return self.ensure_artist(str(value))

    def get_artist(self, value):
        identity = self.resolve(value)
        row = self.repository.artist_row(identity) if identity is not None else None
        return {**row, 'key': artist_policy.token(identity)} if row else None

    def key(self, value):
        identity = self.resolve(value)
        return artist_policy.token(identity) if identity is not None else None

    def public_key(self, value):
        return self.public_keys([value])[value]

    def public_keys(self, values):
        resolved = self.resolve_many(values)
        identities = {identity for identity in resolved.values() if identity is not None}
        names, aliases = self.repository.public_key_inputs(identities)
        keys = artist_policy.public_keys(names, aliases)
        return {value: keys.get(identity, artist_policy.token(identity)) if identity is not None else value
                for value, identity in resolved.items()}

    def name_keys(self):
        return self.public_keys(self.repository.names())

    def album_credits(self, browse_id):
        return self.repository.album_credits(browse_id)

    def track_credits(self, entry_id):
        return self.repository.track_credits(entry_id)

    def track_keys(self, *, library=False, keys=None):
        return {key: {artist_policy.token(identity) for identity in identities} for key, identities
                in self.repository.track_artist_ids(library=library, keys=keys).items()}
