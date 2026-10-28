# Integration access objects

Each vendor has an integration access object (IAO). Application services and
provider adapters obtain vendor data through that object. SDK clients and shared
HTTP helpers are transport details inside the boundary.

The IAO owns operational availability, credentials where applicable, request
admission and pacing, timeouts, transport error classification, coordinated
cooldowns, recovery admission and bounded sharing of access results. A caller
must not evade those decisions through a different endpoint or helper.

Domain owners retain identity matching, enrichment projection, recommendation
selection, product demand and durable mutation workflows. A vendor outage does
not change the user's enabled-provider configuration. A confirmed miss is
different from unavailable access, and dependency failures identify the upstream
vendor. Ambiguous writes must not acquire automatic read-style retries.

MusicBrainz uses one shared `MusicBrainzIAO`. Its `context` receives the caller's
enrichment-state repository for durable cooldowns; the scope is local to the
request context and restored on exit. Standalone callers still share in-process
availability and pacing. Database locks and transactions finish before pacing or
network I/O. The IAO serializes in-process MusicBrainz requests; its durable
cooldown is shared by callers of the same database and survives restart. This
does not establish request pacing across separate application processes.
Search envelopes, entity identity and consumed nested fields are validated before
cache publication. Explicit errors and malformed responses remain pending, with
a bounded request-specific wait; valid empty search lists remain confirmed misses.
Empty artist metadata must still be a valid artist object. The response shapes
follow the [MusicBrainz API examples](https://musicbrainz.org/doc/MusicBrainz_API/Examples).
Entity IDs may be canonical replacements for
[merged MBIDs](https://musicbrainz.org/doc/MusicBrainz_Identifier); identity
matching remains with domain adapters.

Discogs uses one `DiscogsIAO` for authenticated search and public linked master
and release reads. Search needs a token; public reads remain available without
one and omit a known rejected optional token. Credentials resolve from the
environment, saved settings, then TOML, and are checked again after pacing.
Search cache keys include a credential digest. Public responses exclude
`user_data` before sharing. Access caches retain at most 512 responses, with a
one-day successful-response TTL and a one-hour confirmed-miss TTL. Malformed
responses remain pending. Host outages persist through the same scoped
enrichment-state repository; credential failures defer only that credential.

AcousticBrainz uses one `AcousticBrainzIAO` for low-level and high-level reads.
It shares host cooldowns but caches the two responses independently, retaining
the successful half through a failed refresh. Only an HTTP 404 establishes
endpoint absence. Invalid JSON or descriptor containers remain pending; they
defer that recording/endpoint without blocking unrelated valid requests.
The cache holds at most 512 responses with the same one-day/one-hour TTLs.
MusicBrainz MBID resolution retains its own IAO and cooldown, so its outage
cannot block AcousticBrainz access using a known MBID.

Wikipedia uses one `WikipediaIAO` for English article search, REST summaries and
MediaWiki imageinfo metadata. It preserves 200 ms pacing and the eight-second
timeout. HTTP/network outages and explicit busy/lag API errors share a durable
cooldown across callers and endpoints. MediaWiki may return an
[API error inside HTTP 200](https://www.mediawiki.org/wiki/API:Errors_and_warnings);
such a response cannot establish absence. Malformed results defer their request.
Responses and waits each hold at most 512 entries, with one-day successful and
one-hour confirmed-miss TTLs. Subject matching, musician verification and file
attribution/license decisions remain in the provider adapter. Workers retain
their demand schedules and honor access deadlines; a retained stale story is
not stamped as freshly fetched. Artwork URLs are rendered by the browser.
YouTube storyboard downloads and their byte/pixel/deadline limits belong to the
YouTube integration; they are not Wikimedia requests. The egress allowlist is
unchanged.

Cover Art Archive uses one `CoverArtArchiveIAO` for release image listings, through
the existing guarded urllib transport and Internet Archive redirects. Host
outages defer uncached releases together; a fresh cached cover remains usable.
Only HTTP 404 or a valid listing without a front image establishes absence.
HTTP 400 is an invalid request, and 503 can indicate throttling, as described in
the [Cover Art Archive API](https://musicbrainz.org/doc/Cover_Art_Archive/API).
Malformed responses remain pending per release. Responses and request waits each
hold at most 1,024 entries; cover responses expire after one day and confirmed
misses after six hours. A stale useful cover can fill missing artwork while its
durable refresh remains pending, including after restart. A later user or
other-provider cover completes that demand. Selection, artwork precedence and
per-recording job scheduling remain with the artwork domain owners.

Deezer uses one `DeezerIAO` for public search, track and album reads. Enrichment
and Road Trip supply the same scoped enrichment-state repository. Access retains
the existing 150 ms pacing and 20-second timeout, serializes requests within the
process, and immediately persists host outage deadlines. HTTP-200 quota/busy
errors join HTTP/network outages; other API or malformed-response failures defer
the affected request without blocking unrelated queries. Only explicit no-data
answers, empty valid searches and missing track/album resources establish misses.
Detached raw responses and per-request waits each retain at most 512 entries;
successful responses expire after one day and confirmed misses after one hour.
Road Trip keeps its derived fact cache and retry evidence, while all access
admission remains in the IAO. Durable cooldowns survive restart; separate
application processes do not share the in-memory pacer or response cache.

GitHub release checks use `GitHubReleasesIAO` for the single bodyless public latest
release GET already permitted by the egress guard. It accepts no caller-selected
endpoint or credentials. One detached tag result is shared for 60 seconds;
response reads are capped at 1 MiB. Concurrent checks share transport, and failed
refreshes retain last-good release information with an explicit retry outcome.
The scoped repository persists availability across restart. Retry-After and
exhausted primary-budget reset headers follow
[GitHub's rate-limit guidance](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#exceeding-the-rate-limit).
Update scheduling retains its four-hour interval; version comparison, background
startup, platform instructions and installation workflows remain with existing
owners. There is no automatic retry inside a single request.

Spotify import-report artwork uses one `SpotifyIAO` for public album oEmbed reads.
The report route supplies durable availability; local Account Data parsing stays
offline. Canonical album URLs share requests across callers. Transport is paced
at a local 200 ms minimum interval with the existing eight-second timeout and
no credentials. Responses are limited to 256 KiB and validated against the
[Spotify oEmbed schema](https://developer.spotify.com/documentation/embeds/reference/oembed).
Only a valid oEmbed without artwork or HTTP 404/410 establishes absence. Invalid
responses defer that album for a minute; network failures, throttling and server
errors defer uncached vendor work together and honor Retry-After. Responses and
request waits each hold at most 512 entries. Artwork remains usable for one day,
confirmed misses for one hour, and failed refreshes retain last-good artwork
with a retry outcome. The optional artwork route preserves its 404/307 behavior
without holding import-report or database locks during vendor access.

DeepSeek uses one `DeepSeekIAO` for metered album generation. It resolves
credentials and consent, admits one durable reservation through the existing
request ledger, and gives content projection a single-use send function. The
provider retains public album-field selection, prompt construction and generated
prose validation; the album worker retains demand, identity, abstention caching
and display projection. No unmetered transport helper remains in the provider.
The exact allowed POST, redirect refusal, 20-second timeout, 64 KiB response cap,
daily counts and indefinite retention of successful prose are preserved. Local
pacing is one second; it does not claim a vendor quota. Failure categories follow
[DeepSeek's error reference](https://api-docs.deepseek.com/quick_start/error_codes/).
Auth waits survive restart and remain specific to the rejected credential;
balance and rate-limit waits also use credential digests. Host outages affect all
credentials. Retry-After can extend the existing retry delays. Saving settings
wakes product demand without erasing access evidence or daily counts. Independent
database connections share an atomic lease and budget check, and access evidence
publishes with lease completion. Cancellation retains the counted reservation
because a POST may have reached the vendor. There is no automatic billed retry
within an admission, and neither pacing nor HTTP holds a database lock.
Immediately before transport, an atomic single-use authorization verifies that
the counted lease still belongs to this attempt, credential and UTC day, and
fits the current limit. Pacing across midnight, lease expiry or another owner's
takeover defers without sending or erasing the earlier conservative count.
A valid authorization renews the short lease before releasing the database lock
and starting the bounded HTTP attempt.

YouTube Music clients share one `YouTubeMusicIAO` per browser bridge, even when
runtime creates fresh SDK clients for sync, recommendations or web requests.
Library/history/album/playlist browse, search, watch/radio `next`, player metadata
and account-menu reads use the same boundary as playlist create/edit/delete,
feedback/library edits, likes and subscriptions. Unknown POST endpoints and
taste-profile form submissions are classified as mutations. Credentials remain
entirely in the extension; SDK stub headers are discarded by `BridgeSession`.
The IAO allows only the existing HTTPS InnerTube path on music.youtube.com.
Its local minimum spacing is 100 ms, with at most four requests in flight and
one second waiting for admission. No lock spans pacing or browser I/O.
The existing 30-second bridge timeout remains; disposable reads can stop early
under work cancellation, while dispatched mutations drain normally.

Session failures are scoped by connection and brand identity for 30 seconds;
reconnecting permits a fresh session check. Disconnected/no-tab/timeout failures
remain connection errors, never synthetic authentication failures. Transport
failures back off five seconds on that connection. A lost mutation reply defers
further writes but permits safe domain readback while the bridge remains connected.
HTTP 429/server failures
back off all clients for 30 seconds, increasing to five minutes, or longer when
Retry-After requires it. Only that response header crosses the browser boundary.
Host throttling survives reconnect; all of this state is local to the running
bridge and does not survive application restart. No account response is cached.
Successful in-flight work cannot clear a newer failure. Requests are dispatched
once; ambiguous mutation outcomes retain their uncertainty, leaving domain write
journals, destructive safeguards, reconciliation and retry decisions intact.
DOM playback controls and native media requests retain their existing bridge
intent ordering and browser lifecycle; they do not acquire HTTP replay policy.

`YouTubeStoryboardIAO` owns signed public sprite HTTP separately from account
availability, with the unchanged narrow egress guard, refused redirects, two-MB
limit and caller deadline. Image decoding, pixel limits, comparison and derived
verdict caching remain with the TV domain. An API session failure cannot block
public sprites and a sprite failure cannot block user playback or library edits.

Cached responses are detached raw evidence, so callers can reinterpret them
after a genre-vocabulary edit without refetching. A failed refresh can return
last-good evidence while reporting an `IntegrationUnavailable` retry outcome.
The enrichment worker retains partial findings and keeps that provider pending.
The IAO publishes host cooldowns immediately; the waterfall must not count the
same outage a second time or clear it merely because a cached answer was usable.

Focused checks should exercise the actual access boundary across callers,
including concurrent work, timeout/HTTP failures, Retry-After, cancellation,
cache refresh, dependency isolation, restart and eventual recovery. Controlled
transport checks do not establish live vendor availability or which code a
running native application has loaded.
