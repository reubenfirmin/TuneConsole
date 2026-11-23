"""Admit listening observations through explicit queues and evidence lifetimes."""
from copy import deepcopy
import logging
import math

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.choice_capture_contract import ACTIONS

logger = logging.getLogger(__name__)


class ChoiceResponses:
    def __init__(self, ledger, *, capture, paused, admission, pending, persist):
        self.ledger, self.capture, self.paused = ledger, capture, paused
        self.admission, self.pending = admission, pending
        self.persist = persist

    def opportunity(self, identity):
        pending = self.capture.get(identity) if self.capture is not None else None
        return pending or self.ledger.get('opportunity', identity)

    def respond(self, opportunity_id, kind, now, *, option_id=None, action_id=None,
                visible=(), details=None, synchronous=False):
        if self.paused():
            return None
        if kind not in ACTIONS or not math.isfinite(now):
            raise ValueError('Unknown listening response')
        if kind not in {'visible', 'withdraw', 'close'} and option_id is None:
            raise ValueError('A listening response must name its option')
        options = dict(option_id=str(option_id) if option_id is not None else None,
            action_id=action_id, visible=tuple(map(str, visible)), details=deepcopy(details))
        capture = None if synchronous else self.capture
        if capture is not None:
            close = None
            captured = capture.context(opportunity_id)
            if captured is None:
                offer = self.ledger.get('opportunity', opportunity_id)
                captured = {'artifact': offer['context']} if offer else None
            if captured is not None:
                try:
                    close = self.pending({}, now)
                except WorkBudgetExceeded:
                    return None
            try:
                return capture.enqueue('listening:response', self._persist, opportunity_id, kind, now,
                    references=(opportunity_id,), on_close=close,
                    validate=lambda: capture.get(opportunity_id) is not None
                        or self.ledger.get('opportunity', opportunity_id) is not None, **options)
            except BaseException:
                if close is not None:
                    close()
                raise
        try:
            with self.admission(blocking=False):
                return self._persist(opportunity_id, kind, now, **options)
        except WorkBudgetExceeded:
            return None

    def _persist(self, *args, **kwargs):
        with self.admission():
            return self.persist(*args, **kwargs)

    def safe_respond(self, *args, **kwargs):
        try:
            return self.respond(*args, **kwargs)
        except WorkClosing:
            raise
        except Exception:
            logger.warning('listening response measurement failed', exc_info=True)
            return False
