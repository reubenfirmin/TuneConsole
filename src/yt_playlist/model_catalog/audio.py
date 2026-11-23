"""Audio inference, independent vibe heads, and recording-level propagation."""
from .contracts import Edge, Node


GROUP = "Audio & musical character"

MODELS = (
    Node("audio-excerpts", "Captured audio excerpts", GROUP, "Observed audio",
         "A shared input for local audio analysis",
         "Audible playback excerpts with sample rate and positions within the recording.",
         "The selected YouTube Music media and the active sampling window.",
         "Capture bounded excerpts during audible playback, including when a retained run lacks compatible embeddings. "
         "Release each capture before scheduling its successor. Retain identities and positions with derived observations.",
         "Excerpt positions within a recording.",
         ("extension/worker/audio_collection.js", "extension/worker/audio_capture.js", "extension/audio_model/model_worker.js")),
    Node("musicnn", "MusiCNN", GROUP, "Frozen neural model",
         "50 music tags and a 200-dimensional audio representation",
         "Music-tag scores and frozen dense-layer features describing an excerpt's sound.",
         "Audible playback excerpts, downmixed and resampled in the YouTube Music tab; the bundled MSD MusiCNN graph.",
         "The extension checks the versioned weights and runtime against integrity manifests and runs inference locally. "
         "Captures keep artifact hashes and excerpt positions. After restart the collector retries queued metadata by "
         "sample ID and allows a replacement for an unqueued capture within its sampling window.",
         "A recording property, independent of listener history. Excerpt position captures within-song change, not a taste time layer.",
         ("extension/audio_model/model_worker.js", "extension/worker/audio_collection.js", "src/yt_playlist/core/audio_embedding.py", "scripts/audio_model/README.md")),
    Node("audio-descriptors", "Audio descriptors", GROUP, "Measurement policy",
         "Tempo, loudness, spectrum and dynamics",
         "Measured BPM, beat regularity, loudness and spectral descriptors; these are measurements, not learned taste predictions.",
         "The same audible excerpts, processed by the packaged Essentia and Meyda runtime.",
         "Capture and derivation versions accompany each observation. Recompute when measurements or runtime versions change; absent measurements remain unknown.",
         "Excerpt and recording scale; outside the listener's four time layers.",
         ("extension/audio_model/audio_attributes.js", "extension/audio_model/features.js")),
    Node("vibe-intensity", "On Air intensity", GROUP, "Intensity policy",
         "Intensity from activation and cathartic force",
         "Use the stronger supported Energize or Release score on the existing zero-to-one intensity scale. This is an engineering control signal, not an enjoyment probability or a six-way winner.",
         "Current Energize and Release specialist measurements from captured audio, or qualified Bayesian estimates trained on the corresponding measurements. If both are missing, On Air ranks the recording at the neutral midpoint and keeps its intensity unknown.",
         "Consumers verify the score version on read and keep the support and source of the stronger score. On Air reads current scores on Apply, during background fill and during tuned automatic continuation. Untuned queue replenishment uses search bands built by the recommendation worker. Track content admits compatible fits under its own evidence policy.",
         "Recording character and within-song arc, outside the listener's four taste layers.",
         ("src/yt_playlist/rec/vibe_intensity.py", "src/yt_playlist/rec/musical_character.py", "src/yt_playlist/rec/vibe_recording_rules.py")),
    Node("joint-vibe", "Vibe Prediction Model", GROUP, "Local statistical model",
         "Estimates for missing vibe and intensity scores",
         "Estimates missing scores when a recording with sampled audio shares a canonical artist credit. An artist and genre projection requires the same artist and specific subgenre on a sampled recording. Each estimate includes an uncertainty range.",
         "Specialist scores from sampled audio, canonical artist credits, album and specific genre relationships. Each original score records its exact model version and sample references. One recording supplies at most one measurement per vibe. Its weight is one-third, two-thirds or full based on the number of excerpts.",
         "The Vibe work queue records the rebuild policy and evidence revision. A policy change queues new measurements and a catalog sweep to reconcile predictions. Each Vibe definition and intensity definition records its last fit and its outcome. Completed fits queue recordings that share a sampled artist credit in bounded pages ahead of the catalog sweep. Shared credits on collaborations count. An estimate can serve while a rebuild waits if its measurement and relationship policies, masked profile and model configuration, and target conditioning still match.",
         "A recording-property model rebuilt as coverage grows, outside the four taste layers. The prediction row identifies its model generation and provenance file address. The shared receipt store verifies the model and provenance files. Original model inputs remain available to explain fitted scores.",
         ("src/yt_playlist/rec/vibe_joint_calibrated.py", "src/yt_playlist/rec/vibe_joint_state.py", "src/yt_playlist/rec/vibe_recording_rules.py", "src/yt_playlist/rec/vibe_score_policy.py",
          "src/yt_playlist/core/vibe_definitions.py", "src/yt_playlist/core/vibe_model_queries.py",
          "src/yt_playlist/core/vibe_provenance.py", "src/yt_playlist/repos/vibe_model_file_writes.py"),
         caution="Uncertainty is conditional on fitted variance components. Specialist bias is not identified by a measurement holdout; these are not enjoyment probabilities."),
    Node("acousticbrainz-vibes", "AcousticBrainz Vibe estimates", GROUP, "Linear regression",
         "Estimates for the six Vibes from matched classifier scores",
         "Estimates each specialist score independently. A direct specialist measurement takes precedence.",
         "Eight native AcousticBrainz classifier probabilities saved on the verified recording-to-source link. The bundled fit uses paired direct specialist measurements. Played recordings with both inputs supply the local adjustment.",
         "The worker checks at most once a day and stores the listener adjustment with the Vibe work queue. When eligible observations change, fit an adjustment constrained by the bundled coefficients. Republish versioned estimates on the affected recordings. These estimates are stored independently of the selected Vibe scores.",
         "Recording properties. Listening history determines which direct measurements can adjust the local fit.",
         ("src/yt_playlist/rec/acousticbrainz_vibes.py", "src/yt_playlist/rec/acousticbrainz_vibe_preparation.py",
          "src/yt_playlist/core/acousticbrainz_projection.py",
          "experiments/audio_lab_tools/studies/acousticbrainz_vibe_export.py")),
    Node("musical-profile", "Musical character profile", GROUP, "Evidence policy",
         "A source-aware description of each recording",
         "Supported recording dimensions such as intensity, relaxation, happiness, sadness and aggression, plus vibe suitability and provenance.",
         "Source-native recording observations such as AcousticBrainz mood scores, genre priors, user corrections, specialist scores and qualified recording-level estimates.",
         "Versioned preparation resolves each vibe in order of direct specialist measurement, fitted AcousticBrainz estimate, then audio-trained Bayesian estimate. Each provider source keeps its current field selections and references the original observations. Shared receipts verify the selected profile file. Missing evidence stays unknown.",
         "Recording properties. A user's listening changes which profiles are requested, not the definition of the sound.",
         ("src/yt_playlist/rec/musical_profiles.py", "src/yt_playlist/rec/musical_preparation.py", "src/yt_playlist/rec/musical_character.py")),
)

SPECIALISTS = (
    ("relax", "Relax", "Calm, low-tension suitability", "Emotify, Soundtracks, ManyMusic and Memo native emotion targets."),
    ("focus", "Focus", "Unobtrusive reading / studying suitability", "Frozen Reading and Studying activity heads, with equal contribution."),
    ("cruise", "Cruise", "Easygoing listening suitability", "Trained on human valence and arousal annotations from PMEmo and DEAM."),
    ("energize", "Energize", "Momentum and activation suitability", "Trained on arousal labels from DEAM and PMEmo and activation labels from Soundtracks, Emotify, ManyMusic and Memo."),
    ("uplift", "Uplift", "Positive, uplifting suitability", "Trained on valence labels from DEAM and PMEmo and positive-affect labels from Soundtracks, Jamendo, Emotify, ManyMusic and Memo."),
    ("release", "Release", "Cathartic force suitability", "The source-native emotion and force targets recorded in the bundled Release artifact."),
)

MODELS += tuple(Node(
    "vibe-" + key, title + " specialist", GROUP, "Frozen specialist", output,
    output + ". An independent score for ranking within this selected purpose.",
    "Compatible frozen MusiCNN embeddings from saved excerpts and the bundled specialist artifact.",
    "Artifact provenance comes from offline fitting, evaluation and export. " + data + " "
    "Runtime validates the artifact. Supported modes are " + ("active/shadow/off" if key == "cruise" else "active/off")
    + ". Remeasure from verified compatible excerpts in a single playback. Prefer more usable excerpts, then the newest run. "
    "Keep looking through older runs when newer excerpts lack compatible embeddings. If no retained run is usable, fresh playback capture supplies new evidence. "
    "Support reflects the fraction of the three excerpts captured.",
    "Within-song temporal structure only: calm/easygoing/focus retain the weakest excerpt; Release retains the peak; Energize/Uplift reward sustained or building character.",
    ("src/yt_playlist/rec/audio_vibe_" + key + ".py", "src/yt_playlist/rec/audio_vibe_arc.py",
     "src/yt_playlist/controllers/vibe_prediction.py", "src/yt_playlist/controllers/vibe_predictions.py",
     "src/yt_playlist/rec/audio_vibe_values.py",
     "experiments/audio_lab/models/" + key + ".md"),
    caution="Suitability is not calibrated enjoyment, and independent specialist scores should not be compared across purposes."
) for key, title, output, data in SPECIALISTS)

EDGES = (
    Edge("audio-excerpts", "musicnn", "Run the frozen tagger and embedding on the captured excerpt."),
    Edge("audio-excerpts", "audio-descriptors", "Measure supported tempo, spectrum and dynamics from the same excerpt."),
    Edge("musicnn", "vibe-feedback", "Show native music tags for each saved excerpt.", "Developer feedback enabled"),
    Edge("audio-descriptors", "vibe-feedback", "Show measurements and their availability for each captured excerpt.", "Developer feedback enabled"),
    Edge("musical-profile", "vibe-intensity", "Read the current Energize and Release specialist measurements or predictions from their corresponding models."),
    Edge("vibe-intensity", "content", "Encode a supported intensity value using the content encoder's own admission rules.", "Compatible component fits"),
    Edge("joint-vibe", "musical-profile", "Fill eligible missing recording properties with support and uncertainty.", "Qualified model, sampled artist match and usable support"),
    Edge("acousticbrainz-vibes", "musical-profile", "Supply an estimate for a missing specialist measurement on a verified match."),
    Edge("listening-evidence", "acousticbrainz-vibes", "Admit one verified pair per played recording for the local adjustment."),
) + tuple(edge for key, *_ in SPECIALISTS for edge in (
    Edge("musicnn", "vibe-" + key, "Score each compatible frozen MusiCNN excerpt embedding."),
    Edge("vibe-" + key, "vibe-feedback", "Show the independent score and name this purpose in the prediction when it meets the display cutoff.", "Developer feedback enabled"),
    Edge("vibe-" + key, "joint-vibe", "Use original specialist measurements as channel-specific training labels with own-label masking.", "Compatible measured fit admitted"),
    Edge("vibe-" + key, "acousticbrainz-vibes", "Use direct scores as the target for this vibe on matched recordings."),
    Edge("vibe-" + key, "musical-profile", "Supply within-purpose suitability after ordered excerpt aggregation.", "Specialist enabled and compatible audio"),
))
