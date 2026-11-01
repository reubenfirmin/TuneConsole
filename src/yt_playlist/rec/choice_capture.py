"""Frozen listening events admitted to the existing recommendation worker."""
from copy import deepcopy
from contextlib import contextmanager
import threading


class CaptureQueue:
    def __init__(self, submit):
        self.submit = submit
        self._lock = threading.RLock()
        self._opportunities = {}
        self._pending = set()

    def get(self, identity):
        """Serving may need menu metadata before its queued durable write completes."""
        with self._lock:
            capture = self._opportunities.get(identity)
            return deepcopy(capture.opportunity) if capture else None

    def artifacts(self):
        """Retention must keep contexts referenced by not-yet-persisted menus."""
        with self._lock:
            return {artifact for capture in self._pending for artifact in capture.artifacts}

    def context(self, identity):
        """Exact group context for an offer whose file write is still queued."""
        with self._lock:
            capture = self._opportunities.get(identity)
            return deepcopy(capture.group_context) if capture else None

    def opportunities(self):
        with self._lock:
            return set(self._opportunities) | {identity for capture in self._pending
                                               for identity in capture.references}

    @contextmanager
    def protection(self):
        """Serialize only retention's final deletion with admissions and pin release."""
        with self._lock:
            yield self.artifacts(), self.opportunities()

    def enqueue(self, surface, target, *args, opportunity=None, artifacts=(), references=(),
                deduplicate=False, validate=None, on_close=None, group_context=None, **kwargs):
        capture = Capture(self, surface, target, args, kwargs, opportunity, artifacts, references, on_close, group_context)
        with self._lock:
            # If retirement committed before this admission, reject the stale
            # reference rather than acknowledging a response that cannot be stored.
            if validate is not None and not validate():
                capture.close()
                return False
            if opportunity:
                identity = opportunity['id']
                if deduplicate and identity in self._opportunities:
                    capture.close()
                    return True
                self._opportunities[identity] = capture
            self._pending.add(capture)
            try:
                if self.submit(capture):
                    return True
            except BaseException:
                capture.close()
                raise
            capture.close()
            return False


class Capture:
    work_units = 1

    def __init__(self, owner, surface, target, args, kwargs, opportunity, artifacts, references, on_close=None,
                 group_context=None):
        self.owner, self.surface, self.target = owner, surface, target
        self.args, self.kwargs, self.opportunity = args, kwargs, opportunity
        self.artifacts = tuple(artifacts)
        self.references = tuple(references)
        self._on_close = on_close
        self.group_context = group_context

    def run(self):
        return self.target(*self.args, **self.kwargs)

    def close(self):
        with self.owner._lock:
            self.owner._pending.discard(self)
            if self.opportunity:
                identity = self.opportunity['id']
                if self.owner._opportunities.get(identity) is self:
                    self.owner._opportunities.pop(identity)
            callback, self._on_close = self._on_close, None
        if callback is not None:
            callback()
