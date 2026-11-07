"""Invalidate runtime selections without exposing operational job progress.

An inactive head is unchanged by further preparation/reference writes. Background
calculations consuming the retained source can therefore publish their own new
reference owners without mistaking reconciliation counters for logical inputs.
"""


def invalidators(head, jobs, *, names=None):
    for name, job in jobs.items():
        selection = (names or {}).get(name, name)
        for owner in (*job.keys, *job.dependencies):
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                sides = ('OLD', 'NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
                condition = (' OR '.join(job._filter(owner, side) for side in sides)
                             if owner in job.keys and job._filter(owner) else None)
                trigger = head + '_' + name.replace('-', '_') + '_' + owner + '_' + operation.lower()
                yield trigger, (f'CREATE TRIGGER {trigger} AFTER {operation} ON main.{owner}' +
                    (' WHEN ' + condition if condition else '') + ' BEGIN ' +
                    f"UPDATE {head} SET active=0 WHERE name='{selection}' AND active=1; END")
