# Mid-level acquisition and pilot, #811

First acquire publisher metadata, then audit recording/artist/interval identity.
The official API publishes metadata_annotations.zip (318,090 bytes, SHA256
f0a36e901ed6d9e4f21d61d49cd2631642140e3329bb7e41c7524e09688fe1ac) and
audio.zip (1,182,493,865 bytes, SHA256
69c33f323ab8cdcbf915be643088a5ec88aee167b2b4543c9fc4e1a5a7d78c0e).
API metadata is not the annotation file. Never replace missing human property
labels with a pretrained model's predictions.

Freeze this acquisition workflow before network attempts. Bound metadata work
to five minutes, six official download routes, 20 seconds per request, 2 MiB
per response and 16 MiB total; audit two public author repositories for an
annotation mirror within that budget. Keep exact URLs, status/errors, sizes,
redirects and hashes. Stop official attempts after the published size/hash match.
No account access, source circumvention or messages to authors are requested.
Use TemporaryDirectory for partial downloads and clean on success/failure.
Keep publisher snapshots, source archives and completed evidence in experiments/.

If verified metadata arrives, inspect its README, terms, scales and source
identifiers. Strictly intersect original Soundtracks/Coimbra/DEAM excerpts with
existing local audio before downloading the 1.18 GB archive. Artist/title alone
does not prove edition or excerpt alignment. Do not assume all 5,000 items are
new emotion data. Freeze a second pilot protocol after identity auditing and
before fitting: at most 512 audio items, two hours extraction, one hour fitting,
4 GiB RSS; at least 64 aligned items / 12 connected groups for a property pilot.
If additional audio is necessary and metadata/terms support it, bound acquisition
to the publisher archive size plus 16 MiB overhead and 30 minutes.

The pilot must compare direct native prediction with training-only predicted
property augmentation/bottleneck, using component-held outer folds and nested
selection. Exclude protected components from auxiliary property fitting and
downstream fitting; generate training auxiliary features out of fold. Oracle
human test properties are a diagnostic only. Preserve native target scales,
report every property and downstream task, use paired component uncertainty
and the existing .02 NDCG/.01 lift/no-domain-regression gates for any proposed
vibe advancement. Rhythmic properties are not Focus truth. All nonpilot Memo
remains reserved; existing pretrained mid-level models may contain Soundtracks.

If bounded metadata/mirror attempts fail, finish as acquisition-limited with
reproducible errors, not a training result or evidence against mid-level models.
Do not download a large audio archive without its labels and identity metadata.
