# Database maintenance

## Startup and migrations

The oldest supported installed baseline is [v0.1.19](https://github.com/reubenfirmin/TuneConsole/releases/tag/v0.1.19),
published November 12, 2026: numbered schema 107 and regular change 37.
Supported installations receive structural checks and bounded regular changes.
Fresh databases load the complete compiled schema directly.

Older installations reinitialize their local database atomically and receive a
dismissible early-adopter notice. Saved account bindings and extension pairing
survive the reset so configuration and queued playback keep their account IDs.
YouTube preferences remain on YouTube. An interrupted reset rolls back the
database and notice together. Unknown, malformed or newer migration histories
are rejected without resetting data.

`migrations/regular.py` owns incremental changes and the `schema_changes` ledger.
`migrations/registry.py` retains the numbered history as immutable metadata.
Supported upgrades preserve every existing history row and timestamp. Each new
change and its stamp commit together; bulk data work never belongs in a regular
migration. The pre-release converters and preparation commands have been removed.

```sh
uv run python -m yt_playlist.core.migrations upgrade-schema /path/to/state.db
uv run python -m yt_playlist.core.migrations audit /path/to/state.db
```

`upgrade-schema` applies regular changes to a supported baseline. `audit` checks
a committed snapshot read-only. Neither command reinitializes an older library.

The compiler keeps the frozen release layout and seed SQL, applies bounded owner
definitions to an empty database and exports `schema/current.json`:

```sh
uv run python scripts/export-current-schema.py
```

## Rebuild and compaction

The running app exposes local maintenance endpoints:

| Request | Operation |
| --- | --- |
| `POST /storage-maintenance/rebuild` | Current-schema rebuild |
| `POST /storage-maintenance/rebuild?resume=true` | Resume an eligible interrupted build |
| `POST /storage-maintenance/compact` | Retained-copy compaction in the installed format |
| `GET /storage-maintenance/status` | Progress and phase details |

For a stopped library:

```sh
uv run python -m yt_playlist.core.db_rebuild.job /path/to/state.db --scratch /existing/directory/with/space
```

Compaction preserves the installed format, migration history, row IDs, sequence
high-water marks and required file coordinates. It omits only disposable data
under the retention contract. A rebuild copies a supported release baseline to
the current destination, including explicit Vibe version normalization and
annotation constraint cutover. It verifies retained values, types, identities
and file custody before installation.

Construction serves reads while library writes pause. Playback and Radio remain
available; a durable inbox retains confirmed feedback and observations for local
replay. Replay never repeats remote rating or Next commands. Cutover briefly
fences requests and requires participating database handles to close.

The installer independently verifies SQL and files, then installs a synced SQL
sibling under exclusive admission. Managed file roots stay in place. It never
removes a live WAL. Source, candidate and verified recovery backup survive failure;
`state.db.compaction.json` records recovery coordinates. Startup reconciles an
interrupted installation before opening the database.

When manually copying a stopped library, include its managed files,
playback-feedback sidecar database, lock file and observation directory.
Capacity checks include SQL images, journals, owned files and a 256 MiB reserve
on each filesystem. Retained recovery copies still consume physical space.

## Ownership references

- [Schema owners and identity](../../../SCHEMA_OWNERSHIP.md)
- [Field invariants](../../../SCHEMA_INVARIANTS.md)
- [Retained storage contracts](storage-contracts.md)
- [Managed file storage](../../../experiments/retained_copy/FAO-OWNERS.md)
- [Retained-copy experiment history](../../../experiments/retained_copy/README.md)
- [Recommendation publication](../rec/README.md)
