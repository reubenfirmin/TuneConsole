# Served recommendation storage (#687)

SQL retains the existing impression identity, request/surface/rank uniqueness,
lane, optional mode, score, serving policy, captured and resolved recording
identity, and offer time. A directly owned fact row retains policy kind, ranker,
captured video/artist/genre, ownership, last-play time and its presence flag,
coverage and its presence flag, comparison, cluster, cohort, controlled-assignment
flag, assignment probability, and the legacy-policy flag. Existing explicit
primary/pool policy links remain authoritative, including unresolved references.
No report opens diagnostic files or looks up today's candidate metadata.

The rest of each provenance object is a diagnostic JSON document, addressed by
the impression's integer primary key, with an embedded checksum and owner header.
Publication writes and syncs the document before committing its fact row. SQL
records whether that document was deliberately expired. Unexpected absence and
corruption are errors, not expiry. IDs used by compact writes are monotonic.

Diagnostics are retained for **90 days**, matching the existing reporting window.
The provider-alternative seven-day policy does not apply here. Expiry and existing
impression/response pruning run in bounded idle maintenance batches too. A reader
holds the file lifetime fence across its SQL snapshot and trace read.

Installation is additive and bounded. Explicit preparation captures existing
immutable provenance in restartable batches; concurrent legacy writes remain
usable and are picked up by later batches. Cutover requires no uncaptured rows
under the writer reservation. Old JSON, expression indexes and compatibility
triggers remain installed for the separately tracked #626 cleanup. Preparation
does not rewrite applied migration history. A fresh-copy experiment can omit
redundant old provenance only after checking reporting and policy equivalence;
that copy is not an in-place migration or permission to alter the user's database.
