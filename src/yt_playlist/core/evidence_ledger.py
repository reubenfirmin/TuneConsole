"""Immutable JSON evidence, separate from compact SQLite serving projections.

One record per file gives an exact idempotency boundary without rewriting a growing JSON array or
recovering torn JSONL appends. Publication is exclusive and atomic across threads/processes; readers
never see a half-written record. All paths are derived from hashes, never supplied filenames.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager

from yt_playlist.core.activity import check_cancellable_work


class EvidenceConflict(ValueError):
    """An immutable record ID was reused for different evidence."""


class EvidenceBudgetExceeded(ValueError):
    """Evidence capacity is full; serving continues with unknown measurement coverage."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def library_byte_budget(recordings):
    """Reserve base event space plus replay context space proportional to saved music.

    Context arrays grow with the library. Allow 32 KiB per distinct library recording
    across retained snapshots, plus 32 MiB for menus/responses even in an empty library.
    There is no fixed upper ceiling; orphan collection remains independent of capacity.
    """
    return 32 * 1024 * 1024 + max(0, recordings) * 32 * 1024


def _components(node):
    if isinstance(node, dict):
        if set(node) == {'$artifact'}:
            yield node['$artifact']
        else:
            for item in node.values():
                yield from _components(item)
    elif isinstance(node, list):
        for item in node:
            yield from _components(item)


def _sync_directory(path):
    if hasattr(os, 'O_DIRECTORY'):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


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
    KINDS = {"opportunity", "response", "forecast", "artifact", "report"}

    def __init__(self, directory, byte_budget):
        self.directory = Path(directory)
        self.byte_budget = byte_budget

    @contextmanager
    def _budget(self):
        """Serialize reservations across processes; an interrupted reservation stays conservative."""
        _mkdir(self.directory)
        with (self.directory / ".budget").open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0"); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                stream.seek(0)
                try:
                    used = int(stream.read())
                except ValueError:
                    used = sum(path.stat().st_size for path in self.directory.glob("*/*.json"))
                    used += sum(path.stat().st_size for path in self.directory.glob("*/*/*.json"))
                def save(value):
                    stream.seek(0)
                    stream.truncate()
                    stream.write(str(value).encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                yield used, save
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def _path(self, kind, identity):
        if kind not in self.KINDS:
            raise ValueError("Unsupported evidence kind")
        token = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()
        return self.directory / kind / token[:2] / (token + ".json")

    def get(self, kind, identity):
        try:
            return json.loads(self._path(kind, identity).read_bytes())
        except FileNotFoundError:
            return None

    def put(self, kind, identity, value, *, artifact=None):
        """Publish once; identical retries reuse the record, conflicting IDs fail explicitly."""
        encoded = canonical(value)
        path = self._path(kind, identity)
        with self._budget() as (used, save):
            if path.exists():
                if path.read_bytes() != encoded:
                    raise EvidenceConflict(f"Conflicting {kind} evidence: {identity}")
                return False
            artifact_data = canonical(artifact) if artifact is not None else None
            artifact_path = (self._path("artifact", hashlib.sha256(artifact_data).hexdigest())
                             if artifact_data is not None else None)
            artifact_size = len(artifact_data) if artifact_path is not None and not artifact_path.exists() else 0
            if used + len(encoded) + artifact_size > self.byte_budget:
                raise EvidenceBudgetExceeded("Listening evidence storage is full")
            save(used + len(encoded) + artifact_size)
            try:
                if artifact_size:
                    self._publish(artifact_path, artifact_data, "artifact", "context")
                return self._publish(path, encoded, kind, identity)
            except BaseException:
                save(used + (len(encoded) if path.exists() else 0)
                     + (artifact_size if artifact_path is not None and artifact_path.exists() else 0))
                raise

    def _publish(self, path, encoded, kind, identity):
        _mkdir(path.parent)
        # A unique staging file lives on the destination filesystem. Hard-link publication does
        # not overwrite a competing writer; process death leaves only an ignored staging file.
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
                pending = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(pending, path)
            except FileExistsError:
                if path.read_bytes() != encoded:
                    raise EvidenceConflict(f"Conflicting {kind} evidence: {identity}")
                return False
            _sync_directory(path.parent)
            return True
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

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
                records.append(json.loads(path.read_bytes()))
            except FileNotFoundError:
                # Another application's maintenance can retire an unanswered old menu.
                continue
        return records

    def maintain(self, before, *, protected_artifacts=(), orphan_before=None):
        """Retire old unanswered menus and orphan artifacts; preserve choice/correction inputs.

        Chosen menus are retained within the byte budget. At capacity, new telemetry abstains
        rather than silently deleting durable interest evidence. The UI reports this limit.
        """
        with self._budget() as (_used, save):
            offers = {row["id"]: row for row in self.records("opportunity")}
            events = self.records("response")
            protected = {event["opportunity_id"] for event in events
                         if event["kind"] not in {"visible", "open"} or event["observed_at"] >= before}
            retired = {oid for oid, offer in offers.items()
                       if offer["observed_at"] < before and oid not in protected}
            for oid in retired:
                self._path("opportunity", oid).unlink(missing_ok=True)
            for event in events:
                if event["opportunity_id"] in retired:
                    self._path("response", event["id"]).unlink(missing_ok=True)
            referenced = {offer["context"] for oid, offer in offers.items() if oid not in retired}
            referenced.update(protected_artifacts)
            orphan_before = before if orphan_before is None else orphan_before
            artifacts = list((self.directory / "artifact").glob("*/*.json"))
            # A fresh in-flight manifest may reuse old arrays before its proposal
            # is published. Its components need the same grace as the manifest.
            for path in artifacts:
                if path.stat().st_mtime >= orphan_before:
                    referenced.update(_components(json.loads(path.read_bytes())))
            # Context manifests share immutable centroid/source arrays. Retaining
            # the manifest also retains its component graph, including before exposure.
            pending = list(referenced)
            while pending:
                value = self.get('artifact', pending.pop())

                for identity in _components(value):
                    if identity not in referenced:
                        referenced.add(identity)
                        pending.append(identity)
            retained_paths = {self._path('artifact', identity) for identity in referenced}
            for path in artifacts:
                # Content identities already determine paths. Do not decode every
                # abandoned model array just to establish that it is unreferenced.
                if path not in retained_paths and path.stat().st_mtime < orphan_before:
                    path.unlink()
            for path in (self.directory / "report").glob("*/*.json"):
                value = json.loads(path.read_bytes())
                if value["report"]["at"] < before:
                    path.unlink()
            # The lock excludes writers; only old abandoned staging files may be removed.
            for path in self.directory.glob("*/*/.pending-*"):
                path.unlink(missing_ok=True)
            used = sum(path.stat().st_size for path in self.directory.glob("*/*/*.json"))
            save(used)
            return {"used_bytes": used, "budget_bytes": self.byte_budget,
                    "retired_unanswered": len(retired), "full": used >= self.byte_budget}
