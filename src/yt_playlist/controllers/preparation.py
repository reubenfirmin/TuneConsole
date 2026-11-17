"""Reusable bounded preparation commands; callers own connection lifetimes."""
from dataclasses import asdict


def _batches(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'Preparation requires 1 to {maximum} batches')


def prepare(job, connection, *, batches=1, batch_size=128, retry=False, status=False):
    _batches(batches, 512)
    if type(batch_size) is not int or not 1 <= batch_size <= 512:
        raise ValueError('Preparation batch size must be from 1 through 512')
    if status:
        return job.progress(connection) if job.initialized(connection) else None
    result = job.install(connection)
    for _ in range(batches):
        result = job.step(connection, batch_size=batch_size, retry=retry)
        if result.status == 'prepared':
            break
    return result


def publication(owner, connection, *, stage=None, repair_source=None, repair=None, batches=1, batch_size=128):
    _batches(batches, 128)
    if stage is not None and repair_source is not None:
        raise ValueError('Select publication or repair separately')
    if stage is not None:
        owner.publish(connection, stage=stage)
        return {'selected': owner.selected(connection)}
    if repair_source is not None:
        repair(connection, repair_source)
        return {'repaired': repair_source}
    if type(batch_size) is not int or not 1 <= batch_size <= 512:
        raise ValueError('Preparation batch size must be from 1 through 512')
    for _ in range(batches):
        result = owner.step(connection, batch_size=batch_size)
        if result.complete:
            break
    return asdict(result)


def compression(work, *, batches, logical_bytes):
    """Stream progress from an explicitly supplied conversion capability."""
    if type(batches) is not int or batches < 1:
        raise ValueError('Preparation batches must be positive')
    for _ in range(batches):
        yield work.step(logical_bytes=logical_bytes)
        if work.complete:
            break


def documents(owner, store, *, domain, batches, clock=None):
    if type(batches) is not int or batches < 1:
        raise ValueError('Preparation batches must be positive')
    for _ in range(batches):
        count = owner.prepare(store, **({'now': clock()} if clock is not None else {}))
        yield {'domain': domain, 'converted': count}
        if not count:
            break
