"""Immutable JSON evidence, separate from compact SQLite serving projections.

One record per file gives an exact idempotency boundary without rewriting a growing JSON array or
recovering torn JSONL appends. Publication is exclusive and atomic across threads/processes; readers
never see a half-written record. All paths are derived from hashes, never supplied filenames.
"""
import hashlib
from . import fao
import json
import os
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager, nullcontext
from collections import Counter

from yt_playlist.core.activity import check_cancellable_work
from .evidence_contract import artifact_components as _components, canonical


class EvidenceConflict(ValueError):
    """An immutable record ID was reused for different evidence."""


class EvidenceBudgetExceeded(ValueError):
    """Evidence capacity is full; serving continues with unknown measurement coverage."""

    def __init__(self, message, *, required_bytes=0, used_bytes=None, budget_bytes=None):
        super().__init__(message)
        self.required_bytes = required_bytes
        self.used_bytes = used_bytes
        self.budget_bytes = budget_bytes


def root_for_database(path, memory_key):
    from . import paths
    return (Path(path).with_name(Path(path).name + '.evidence') if path else
            paths.data_dir() / 'test-evidence' / str(memory_key))


def directory(conn):
    return root_for_database(conn.execute('PRAGMA database_list').fetchone()[2],
                             getattr(conn, '_policy_memory_key', id(conn)))


def library_byte_budget(recordings):
    """Reserve base event space plus replay context space proportional to saved music.

    Context arrays grow with the library. Allow 32 KiB per distinct library recording
    across retained snapshots, plus 32 MiB for menus/responses even in an empty library.
    There is no fixed upper ceiling; orphan collection remains independent of capacity.
    """
    return 32 * 1024 * 1024 + max(0, recordings) * 32 * 1024


def _sync_directory(path):
    fao._sync(path)


def _mkdir(path):
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    # A durable file also needs durable links to each newly created ancestor.
    for directory in reversed(missing):
        _sync_directory(directory.parent)


class EvidenceLedger:
    KINDS = {"opportunity", "response", "forecast", "artifact", "report", "checkpoint"}

    def __init__(self, directory, byte_budget, *, opportunity_projection=None):
        self.directory = Path(directory)
        self.byte_budget = byte_budget
        self.opportunity_projection = opportunity_projection

    def project_opportunity(self, value):
        return self.opportunity_projection(value) if self.opportunity_projection else value

    @contextmanager
    def _budget(self):
        """Serialize reservations across processes; an interrupted reservation stays conservative."""
        _mkdir(self.directory)
        with (self.directory / ".budget").open("a+b") as stream:
            while True:
                check_cancellable_work()
                unlock = fao._try_file_lock(stream, exclusive=True)
                if unlock is not None:
                    break
                time.sleep(.025)
            try:
                stream.seek(0)
                recovered = False
                try:
                    used = int(stream.read())
                except ValueError:
                    recovered = True
                    used = sum(fao.stat(path).logical_bytes for path in self.directory.glob("*/*.json"))
                    used += sum(fao.stat(path).logical_bytes for path in self.directory.glob("*/*/*.json"))
                def save(value):
                    stream.seek(0)
                    stream.truncate()
                    stream.write(str(value).encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                if recovered:
                    save(used)
                yield used, save
            finally:
                unlock()

    def _path(self, kind, identity):
        if kind not in self.KINDS:
            raise ValueError("Unsupported evidence kind")
        token = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()
        return self.directory / kind / token[:2] / (token + ".json")

    def get(self, kind, identity):
        try:
            value = json.loads(fao.read_bytes(self._path(kind, identity)))
            return self.project_opportunity(value) if kind == 'opportunity' else value
        except FileNotFoundError:
            return None

    def generation(self):
        """Constant-size event revision, read under the publication lock across processes."""
        with self._budget():
            try:
                return (self.directory / '.events').read_bytes().hex()
            except FileNotFoundError:
                return 'legacy'

    def _advance_generation(self):
        # Advance before changing events while holding the publication lock.
        # A killed writer may cause an extra replay, but cannot hide a published event.
        with (self.directory / '.events').open('wb') as stream:
            stream.write(os.urandom(16))
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(self.directory)

    def put(self, kind, identity, value, *, artifact=None):
        """Publish once; identical retries reuse the record, conflicting IDs fail explicitly."""
        encoded = canonical(value)
        path = self._path(kind, identity)
        with self._budget() as (used, save):
            existing = path.exists()
            # A collector may have released a shared component since a producer
            # read its manifest. Validate references under the publication lock,
            # so capture fails explicitly instead of publishing a dangling offer.
            dependencies = _components(value if kind == 'artifact' else artifact)
            if ((not existing or kind == 'artifact')
                    and any(not self._path('artifact', component).is_file() for component in dependencies)):
                raise ValueError('Listening context component is no longer retained')
            if existing:
                if fao.read_bytes(path) != encoded:
                    raise EvidenceConflict(f"Conflicting {kind} evidence: {identity}")
                if kind == 'artifact':
                    # Reusing an old immutable manifest starts a fresh preparation
                    # lease for its graph before a proposal/menu can reference it.
                    fao.touch(path)
                return False
            artifact_data = canonical(artifact) if artifact is not None else None
            artifact_path = (self._path("artifact", hashlib.sha256(artifact_data).hexdigest())
                             if artifact_data is not None else None)
            artifact_size = len(artifact_data) if artifact_path is not None and not artifact_path.exists() else 0
            if used + len(encoded) + artifact_size > self.byte_budget:
                raise EvidenceBudgetExceeded("Listening evidence storage is full",
                    required_bytes=len(encoded) + artifact_size, used_bytes=used,
                    budget_bytes=self.byte_budget)
            if kind in {'opportunity', 'response'}:
                self._advance_generation()
            save(used + len(encoded) + artifact_size)
            try:
                if artifact_size:
                    self._publish(artifact_path, artifact_data, "artifact", "context")
                return self._publish(path, encoded, kind, identity)
            except BaseException:
                save(used + (len(encoded) if path.exists() else 0)
                     + (artifact_size if artifact_path is not None and artifact_path.exists() else 0))
                raise

    def _publish(self, path, encoded, kind, identity, *, age=None):
        existing = fao.exists(path)
        try:
            fao.write_bytes(path, encoded, immutable=True, age=age)
        except fao.CorruptFile as exc:
            raise EvidenceConflict(f"Conflicting {kind} evidence: {identity}") from exc
        return not existing

    def artifact(self, value):
        identity = hashlib.sha256(canonical(value)).hexdigest()
        self.put("artifact", identity, value)
        return identity

    def records(self, kind):
        """An immutable inventory for background replay; request paths use exact-ID lookup."""
        if kind not in self.KINDS:
            raise ValueError("Unsupported evidence kind")
        paths = sorted((self.directory / kind).glob("*/*.json"))
        records = []
        for path in paths:
            check_cancellable_work()
            try:
                value = json.loads(fao.read_bytes(path))
                value = self.project_opportunity(value) if kind == 'opportunity' else value
                if value is not None:
                    records.append(value)
            except FileNotFoundError:
                # Another application's maintenance can retire an unanswered old menu.
                continue
        return records

    def maintain(self, *args, **kwargs):
        """Do not retire files belonging to an analytics reader or backup."""
        _mkdir(self.directory)
        with (self.directory / '.readers.lock').open('a+b') as stream:
            unlock = fao._try_file_lock(stream, exclusive=True)
            if unlock is None:
                return {'deferred': True}
            try:
                return self._maintain(*args, **kwargs)
            finally:
                unlock()

    def _maintain(self, before, *, protected_artifacts=(), protected_runs=(), orphan_before=None,
                 protected_opportunities=(), pressure_before=None, compacted_contexts=(),
                 context_before=None, context_byte_budget=0, required_bytes=0, pending_protection=None,
                 retire_references=None, progress=None):
        """Retire old unanswered menus and orphan artifacts; preserve choice/correction inputs.

        At 90% capacity, retire the oldest unanswered menus until 30% is free.
        Explicit responses, current offers and in-flight captures always survive.
        A pending publication may reclaim optional acknowledged diagnostic graphs
        before considering unanswered menus. Protected roots always survive.
        """
        with self._budget() as (_used, save):
            from .evidence_inventory import Inventory
            from .evidence_retirement import Retirements
            retirements = Retirements(self.directory)
            had_retirements = bool(retirements.offers or retirements.checkpoints)
            inventory = Inventory(self, progress=progress).scan()
            offers = {}
            events = []
            for path, entry in inventory.items():
                if path.parts[-3] == 'opportunity':
                    row = self.project_opportunity(entry['data'])
                    if row is not None:
                        offers[row['id']] = row
                elif path.parts[-3] == 'response':
                    events.append(entry['data'])
            durable = {event["opportunity_id"] for event in events
                       if event["kind"] not in {"visible", "open"}}
            protected = durable | set(protected_opportunities)
            resuming = retirements.offers - offers.keys()
            # Capacity can admit menus newer than ordinary expiry. Once their
            # offer is gone, leftover passive events cannot undo that admission.
            # Durable responses and current capture pins still protect them.
            protected.update(event['opportunity_id'] for event in events
                             if event['observed_at'] >= before and event['opportunity_id'] not in resuming)
            protected.update(oid for oid, offer in offers.items()
                if any((offer.get(field) or {}).get('run_id') in protected_runs for field in ('comparison', 'monitor')))
            retired = {oid for oid, offer in offers.items()
                       if offer["observed_at"] < before and oid not in protected}
            # A preceding slice may have removed the offer before its events or
            # SQL owners. Its durable intent supplies the missing association.
            retired.update(resuming - protected)
            orphan_before = before if orphan_before is None else orphan_before
            artifacts = [path for path in inventory if path.parts[-3] == 'artifact']
            sizes = {path: entry['size'] for path, entry in inventory.items()}
            edges = {path: entry['data'] for path, entry in inventory.items() if path.parts[-3] == 'artifact'}

            def closure(roots):
                pending, seen = list(roots), set()
                while pending:
                    identity = pending.pop()
                    if identity in seen:
                        continue
                    seen.add(identity)
                    pending.extend(edges.get(self._path('artifact', identity), ()))
                return {self._path('artifact', identity) for identity in seen}

            # Cache edges, not decoded model arrays. Shared components are read once.
            pinned = set(protected_artifacts)
            for path in artifacts:
                if inventory[path]['mtime'] >= orphan_before:
                    pinned.update(edges[path])
            references = Counter(closure(pinned))
            references.update(path for path in artifacts if inventory[path]['mtime'] >= orphan_before)
            # A caller acknowledges compact, correction-capable inputs only after
            # successful learning publication. Raw contexts then become optional
            # diagnostics, even while their opportunities belong to an active trial.
            compacted_contexts = (set(compacted_contexts) if compacted_contexts is not None else
                {entry['data']['source_context'] for path, entry in inventory.items()
                 if path.parts[-3] == 'checkpoint' and entry['data']['acknowledged']})
            if progress is not None:
                progress['checkpoints'] = len(compacted_contexts)
            mandatory = {offer['context'] for oid, offer in offers.items() if oid not in retired
                         and (offer['context'] not in compacted_contexts or oid in protected_opportunities)}
            mandatory_paths = closure(mandatory)
            optional, optional_paths = set(), set()
            candidates = sorted((offer for oid, offer in offers.items() if oid not in retired
                                 and offer['context'] in compacted_contexts
                                 and offer['context'] not in mandatory),
                                key=lambda offer: (-offer['observed_at'], offer['id']))
            for offer in candidates:
                if context_before is None or offer['observed_at'] < context_before:
                    continue
                paths = closure([offer['context']])
                extra = {path for path in paths if not references[path]} - mandatory_paths - optional_paths
                if sum(sizes.get(path, 0) for path in optional_paths | extra) <= context_byte_budget:
                    optional.add(offer['context'])
                    optional_paths.update(extra)
            contexts = {oid: (closure([offer['context']]) if offer['context'] in mandatory | optional else set())
                        for oid, offer in offers.items()}
            for oid, context in contexts.items():
                if oid not in retired:
                    references.update(context)
            event_paths = {}
            for event in events:
                event_paths.setdefault(event['opportunity_id'], []).append(self._path('response', event['id']))

            def retired_paths(oid):
                return [self._path('opportunity', oid), *event_paths.get(oid, [])]

            removed = {path for oid in retired for path in retired_paths(oid)}
            removed.update(path for path in artifacts if not references[path])
            for path, entry in inventory.items():
                if path.parts[-3] == 'report' and entry['data'] < before:
                    removed.add(path)
            used = sum(size for path, size in sizes.items() if path not in removed)
            reclaimed_optional_paths = set()
            if required_bytes and used + required_bytes > self.byte_budget:
                owners = {}
                for oid, offer in offers.items():
                    if oid not in retired:
                        owners.setdefault(offer['context'], []).append(oid)
                # The diagnostic allowance is a ceiling. Release oldest optional
                # graphs until the complete pending record and manifest can fit.
                # Reference counts preserve shared mandatory/prepared components.
                for offer in reversed(candidates):
                    if used + required_bytes <= self.byte_budget:
                        break
                    identity = offer['context']
                    if identity not in optional:
                        continue
                    optional.remove(identity)
                    freed = set()
                    for oid in owners[identity]:
                        for path in contexts[oid]:
                            references[path] -= 1
                            if not references[path]:
                                freed.add(path)
                        contexts[oid] = set()
                    freed -= removed
                    released = sum(sizes.get(path, 0) for path in freed)
                    used -= released
                    reclaimed_optional_paths.update(freed)
                    removed.update(freed)
            pressure_retired = set()
            if pressure_before is not None and used > self.byte_budget * .9:
                recent = {event['opportunity_id'] for event in events
                          if event['observed_at'] >= pressure_before}
                candidates = sorted((offer for oid, offer in offers.items()
                    if oid not in retired | durable | set(protected_opportunities) | recent
                    and offer['observed_at'] < pressure_before
                    and not any((offer.get(field) or {}).get('run_id') in protected_runs
                                for field in ('comparison', 'monitor'))),
                    key=lambda offer: (offer['observed_at'], offer['id']))
                for offer in candidates:
                    if used <= self.byte_budget * .7:
                        break
                    oid = offer['id']
                    retired.add(oid)
                    pressure_retired.add(oid)
                    freed = set(retired_paths(oid))
                    for path in contexts[oid]:
                        references[path] -= 1
                        if not references[path]:
                            freed.add(path)
                    used -= sum(sizes.get(path, 0) for path in freed - removed)
                    removed.update(freed)
            surviving_contexts = {offer['context'] for oid, offer in offers.items() if oid not in retired}
            retired_checkpoints = {}
            for path, entry in inventory.items():
                if path.parts[-3] == 'checkpoint' and entry['data']['source_context'] not in surviving_contexts:
                    removed.add(path)
                    retired_checkpoints[path] = entry['data']['source_context']
            for identity in retirements.checkpoints - surviving_contexts:
                path = self._path('checkpoint', identity)
                removed.add(path)
                retired_checkpoints[path] = identity
            guard = pending_protection() if pending_protection is not None else nullcontext(((), ()))
            with guard as (pending_artifacts, pending_opportunities):
                # Admissions can continue throughout inventory and planning. Take
                # their current pins under the admission lock only for deletion.
                # The budget lock already excludes durable writers throughout.
                pending_opportunities = set(pending_opportunities)
                pending_roots = set(pending_artifacts)
                pending_paths = set()
                for oid in pending_opportunities:
                    pending_paths.update(retired_paths(oid))
                    retired.discard(oid)
                    pressure_retired.discard(oid)
                    if oid in offers:
                        identity = offers[oid]['context']
                        pending_roots.add(identity)
                        pending_paths.add(self._path('checkpoint', identity))
                pending_artifact_paths = closure(pending_roots)
                pending_paths.update(pending_artifact_paths)
                removed.difference_update(pending_paths)
                # A stale pin to an absent offer cannot authorize deleting its
                # remaining events or acknowledging reconciliation as complete.
                unfinished_protected = (retirements.offers - offers.keys()) & (protected | pending_opportunities)
                retirements.offers = retired | unfinished_protected
                retirements.checkpoints = {identity for path, identity in retired_checkpoints.items()
                                           if path in removed and isinstance(identity, str)}
                # Publish intent before the first unlink. Existing evidence and
                # current pins, rather than this journal, still decide eligibility.
                if had_retirements or removed or retirements.offers or retirements.checkpoints:
                    retirements.save()
                if retired:
                    self._advance_generation()
                planned = removed
                removed = set()
                # Retire menus/events before their now-unreferenced context.
                # Stable ordering also makes every unlink boundary reproducible.
                paths = sorted(planned, key=lambda path:
                               (path.parts[-3] not in {'opportunity', 'response'}, str(path)))
                for path in paths:
                    check_cancellable_work()
                    if fao.delete(path, missing_ok=True):
                        removed.add(path)
                completed_offers = {oid for oid in retired
                                    if all(not path.exists() for path in retired_paths(oid))}
                completed_checkpoints = {identity for path, identity in retired_checkpoints.items()
                                         if path in planned and not path.exists() and isinstance(identity, str)}
                retirements.reconcile(completed_offers, completed_checkpoints, retire_references)
                if retire_references is not None and not (completed_offers or completed_checkpoints):
                    # Preserve ordinary dictionary orphan collection on idle turns.
                    retire_references([], [])
                deferred = bool(retirements.offers or retirements.checkpoints
                                or any(path.exists() for path in planned))
            surviving_contexts = {offer['context'] for oid, offer in offers.items() if oid not in retired}
            # The lock excludes writers; only old abandoned staging files may be removed.
            for path in self.directory.glob("*/*/.pending-*"):
                path.unlink(missing_ok=True)
            used = sum(fao.stat(path).logical_bytes for path in self.directory.glob("*/*/*.json"))
            save(used)
            return {"used_bytes": used, "budget_bytes": self.byte_budget, "deferred": deferred,
                    "required_bytes": required_bytes,
                    "available_bytes": max(0, self.byte_budget - used),
                    "shortfall_bytes": max(0, used + required_bytes - self.byte_budget),
                    "reclaimed_optional_bytes": sum(sizes.get(path, 0) for path in reclaimed_optional_paths & removed),
                    "retired_unanswered": len(completed_offers),
                    "retired_for_capacity": len(pressure_retired & completed_offers),
                    "compacted_contexts": len(surviving_contexts & compacted_contexts),
                    "unacknowledged_contexts": len(surviving_contexts - compacted_contexts),
                    "optional_context_bytes": sum(sizes.get(path, 0) for path in optional_paths
                                                  if path not in removed and path not in pending_artifact_paths),
                    "protected_artifact_bytes": sum(sizes.get(path, 0) for path in artifacts
                        if path not in removed and (path in pending_artifact_paths
                            or references[path] and path not in optional_paths)),
                    "full": used >= self.byte_budget or used + required_bytes > self.byte_budget}
