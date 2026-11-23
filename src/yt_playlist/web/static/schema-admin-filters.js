// An FK makes a connection drawable. Membership belongs to the feature that
// owns the data. List a relation twice only when it is part of both schemas.
const ownership = {
    accounts: `
        account_albums
    `,
    actions: `
        action_steps action_targets
    `,
    album_feedback: '',
    albums: `
        account_albums album_artists album_item_observations album_occurrence_sources
        album_provider_ids album_track_occurrences album_tracklists album_tracks saved_album_memberships
    `,
    album_description_requests: '',
    artists: `
        album_artists artist_aliases
        artist_credit_observations artist_credit_replays
        artist_credits artist_embedding_files artist_listening_summary
        artist_provider_ids
        artist_similarity_targets entry_artists recording_additional_artists
    `,
    audio_annotations: '',
    choice_learning_publications: '',
    discovery_origins: `
        artists discovered_albums discovered_artists discovered_tracks
        discovery_artist_sources
        discovery_origin_tokens discovery_sources recordings
    `,
    genre_catalog: `
        genre_alias genre_family
    `,
    group_generations: `
        settings group_preparation_state
    `,
    group_versions: 'group_members settings',
    home_display_publications: `
        home_genre_requests
    `,
    measurements: `
        measurement_samples sample_attributes
    `,
    metadata_deliveries: `
        collection_file_writes enrichment_provider_config enrichment_provider_state
        metadata_delivery_capture_allowed
        metadata_delivery_locations provider_attempts
    `,
    metadata_observations: `
        catalog_field_hints enrichment_conflict enrichment_log item_field_sources
        metadata_choice_heads metadata_conflicts metadata_decisions
        metadata_hints
        metadata_runtime_state
        native_metadata_observations recording_field_sources recording_metadata_selections
    `,
    mix_choice_menus: `
        choice_context_projections mix_choice_descriptors
    `,
    play_events: `
        artist_listening_summary eligible_listening_evidence listening_activity
        listening_daily_items
        listening_daily_snapshots listening_day_counts listening_days listening_evidence
        listening_observation_subjects listening_observations
        listening_output_publications
        play_event_calendar play_event_sources play_event_subjects playback_feedback_receipts
        player_events recording_listening_artists recording_listening_summary
        spotify_history_listens takeout_imports trend_first_play
    `,
    playlist_scopes: `
        playlist_overlap_decisions quarantined_playlists
    `,
    playlists: `
        active_playlists playlist_items
        playlist_tracks
    `,
    predictions: `
        acousticbrainz_vibe_estimates collection_file_writes
        served_vibe_fits vibe_predictions vibe_recording_fits
    `,
    radio_sessions: `
        radio_queue_items radio_seed_output
    `,
    rec_model_selection_runs: `
        content_full_checkpoint model_autotune_output model_selection_heads
        rec_model_forecasts rec_model_previous_vectors
    `,
    rec_served_impressions: `
        rec_feedback rec_response_events taste_mode_offers
    `,
    recordings: `
        catalog_tracks collection_file_writes item_recording_associations rec_musical_links
        recording_additional_artists recording_aliases
        recording_embedding_files recording_identifiers
        recording_vibes
    `,
    recommendation_generations: `
        prepared_recording_inputs
        rec_content_pending rec_proposals rec_vibe_pending
        recommendation_build_pools
        recommendation_candidates recommendation_generation_refs
        recommendation_generation_surfaces recommendation_pools
        recommendation_report_parts
        recommendation_report_publications recommendation_surface_pools
        resolved_recommendation_candidates
    `,
    recommendation_policies: `
        recommendation_policy_body_refs recommendation_policy_archive_refs
    `,
    road_trip_recipes: '',
    schema_changes: 'schema_migrations',
    settings: '',
    spotify_import_jobs: 'spotify_transfer_receipts',
    sync_cursors: 'collection_file_writes',
    taste_axes: `
        mood_events recommendation_gradient_events taste_comparison_baselines taste_settings_checkpoints
    `,
    taste_groups: `
        group_generations group_versions group_members
        settings group_preparation_state legacy_mode_payloads
    `,
    track_sources: `
        entry_artists item_ratings
        collection_file_writes rec_musical_sources unresolved_items
    `,
    tv_launches: `
        tv_append_intents tv_artist_artwork tv_artist_requests tv_continuation_items tv_continuation_receipts
        tv_pool_items tv_preparation_state tv_visual_results
    `,
    vibe_definitions: 'vibe_versions',
};

export const entityDescriptions = new Map(Object.entries({
    accounts: "The accounts connected to TuneConsole, including which one is the main account and which are active.",
    actions: "Changes TuneConsole plans or carries out on your behalf, such as moving songs or editing playlists. This records what was requested and how far it got.",
    album_feedback: "What someone said about an album recommendation, such as wanting more like it, already knowing it, or dismissing it.",
    albums: "The albums TuneConsole knows about. Each album can have tracks, artist credits, and listings on different music services.",
    album_description_requests: "Paid album description requests, saved results, and provider usage.",
    artists: "The musicians and bands TuneConsole knows about, with a common identity for names and listings from different music services.",
    audio_annotations: "Labels people assign to audio samples, such as how a song feels. These are saved alongside the computer's guesses so the two can be compared.",
    choice_learning_publications: "What TuneConsole has learned from the choices people make when offered songs, mixes, or albums. These results help shape later suggestions.",
    discovery_origins: "Where Discovery found songs, albums and artists. The recording stores its exact Discovery upload and eligibility when it joins or leaves your Collection.",
    genre_catalog: "The standard list of genre names TuneConsole uses to describe music, including how names fit into broader genre families.",
    group_generations: "Runs that build a new set of track groups for suggestions. Each run records when it was built and which set of groups it produced.",
    group_versions: "The definition, parent group, and display order of a taste group in one generation.",
    home_display_publications: "Saved versions of the suggestions shown on Home. TuneConsole can keep showing the last ready version while it prepares a new one.",
    measurements: "Scores measured from audio samples, such as how energetic a song sounds. The samples show where each score came from.",
    metadata_deliveries: "Responses received from music services, including when they arrived and where the original information was saved.",
    metadata_observations: "Individual facts reported by a source, such as a song's genre or an album's release year. Different sources can report different values.",
    mix_choice_menus: "A record of which mixes people were offered together, including the name and genres shown for each choice.",
    play_events: "A record of songs played, who played them, and when. These records contribute to listening history.",
    playlist_scopes: "Which playlists belong to one account and which are shared across accounts.",
    playlists: "The playlists TuneConsole knows about, including their names and who owns them. The songs and their order are linked to each playlist.",
    predictions: "What the computer inferred about a song, such as how well it fits a Vibe, and which model made that guess.",
    radio_sessions: "Saved On Air sessions, including what was playing and where playback reached in the queue.",
    rec_model_selection_runs: "Runs that compare different ways of making recommendations and record how each one performed.",
    rec_served_impressions: "Recommendations TuneConsole actually showed, including what was offered, where it appeared, and when.",
    recordings: "The songs TuneConsole recognizes as distinct recordings. Several uploads can point to the same recording, while a live version or cover can have its own.",
    recommendation_generations: "Batches of recommended songs and albums prepared together so the app can use a complete set of suggestions.",
    recommendation_policies: "Saved versions of the rules TuneConsole uses to choose or order recommendations.",
    road_trip_recipes: "Saved Road Trip plans, including the choices that tell TuneConsole what kind of playlist to make.",
    schema_changes: "Which changes to the database structure have already run, so TuneConsole knows what still needs to happen after an upgrade.",
    settings: "Saved app preferences and other settings TuneConsole needs to remember between restarts.",
    spotify_import_jobs: "Spotify imports and their progress, so TuneConsole can pick up where it left off.",
    sync_cursors: "Library sync progress for the current attempt, each account, resource and playlist.",
    taste_axes: "The things TuneConsole can track a listening preference for, such as a specific genre or a broader genre family.",
    taste_groups: "The named groups TuneConsole uses to organize recommendations. This also keeps the original sound profile and identity of older groups so past offers and playlist picks still make sense.",
    track_sources: "Individual YouTube uploads and unmatched imports, linked to the recording they represent. On Air can prefer a verified replacement upload.",
    tv_launches: "Each time someone starts a TV session, including which account started it and whether the video lineup is ready.",
    vibe_definitions: "The named Vibes TuneConsole recognizes, such as Relax or Energize, and how each one is used.",
}));

export function installedFilters(entities, edges) {
  const filters = new Map();
  for (const [owner, members] of Object.entries(ownership)) {
    if (!entities.has(owner)) continue;
    const names = members.match(/\S+/g) || [];
    filters.set(owner, new Set([owner, ...names.filter(name => entities.has(name))]));
  }
  for (const {source, target} of edges) {
    if (entities.has(target)) filters.get(source)?.add(target);
    filters.get(target)?.add(source);
  }
  return filters;
}
