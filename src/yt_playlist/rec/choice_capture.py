"""Frozen listening events admitted to the existing recommendation worker."""
from copy import deepcopy
import threading


class CaptureQueue:
    def __init__(self, submit):
        self.submit = submit
        self._lock = threading.RLock()
        self._opportunities = {}

    def get(self, identity):
        """Serving may need menu metadata before its queued durable write completes."""
        with self._lock:
            capture = self._opportunities.get(identity)
            return deepcopy(capture.opportunity) if capture else None

    def artifacts(self):
        """Retention must keep contexts referenced by not-yet-persisted menus."""
        with self._lock:
            return {artifact for capture in self._opportunities.values() for artifact in capture.artifacts}

    def enqueue(self, surface, target, *args, opportunity=None, artifacts=(), deduplicate=False, **kwargs):
        capture = Capture(self, surface, target, args, kwargs, opportunity, artifacts)
        with self._lock:
            if opportunity:
                identity = opportunity['id']
                if deduplicate and identity in self._opportunities:
                    return True
                self._opportunities[identity] = capture
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

    def __init__(self, owner, surface, target, args, kwargs, opportunity, artifacts):
        self.owner, self.surface, self.target = owner, surface, target
        self.args, self.kwargs, self.opportunity = args, kwargs, opportunity
        self.artifacts = tuple(artifacts)

    def run(self):
        return self.target(*self.args, **self.kwargs)

    def close(self):
        if self.opportunity:
            with self.owner._lock:
                identity = self.opportunity['id']
                if self.owner._opportunities.get(identity) is self:
                    self.owner._opportunities.pop(identity)
