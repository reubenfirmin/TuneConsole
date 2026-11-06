"""Shared work-admission failures without dependencies on their schedulers."""


class WorkBudgetExceeded(RuntimeError):
    """A disposable calculation used its allotted wall time."""
