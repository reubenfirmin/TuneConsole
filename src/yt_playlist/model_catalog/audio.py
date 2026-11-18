"""Audio inference, independent vibe heads, and recording-level propagation."""
from .contracts import Edge, Node


GROUP = "Audio & musical character"

MODELS = (
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
    Node("audio-research", "Audio research & development", GROUP, "Training & evaluation",
         "Train, evaluate and export specialist artifacts",
         "Train and evaluate audio recipes on annotated datasets and export approved specialist artifacts for the app. The optional capture preview shows mood across sampled excerpts.",
         "MusiCNN embeddings and dataset labels for the shipped specialists. Mood predictions and audio descriptors are optional features in exploratory comparisons.",
         "Document dataset provenance and evaluated recipes in experiments. Package the approved artifacts with their feature contracts and version identifiers.",
         "Research and artifact versions, outside the four taste layers.",
         ("experiments/audio_lab/README.md", "experiments/audio_lab_tools/training/external_training.py",
          "src/yt_playlist/controllers/audio_capture.py", "src/yt_playlist/rec/audio_vibe_values.py")),
    Node("vibe-intensity", "Activation / force ensemble", GROUP, "Intensity policy",
         "Intensity from activation and cathartic force",
         "Use the stronger supported Energize or Release score on the existing zero-to-one intensity scale. This is an engineering control signal, not an enjoyment probability or a six-way winner.",
         "Ordered-excerpt Energize and Release specialist measurements, or qualified recording-level estimates of those same channels. Explicit intensity corrections take precedence.",
         "Versioned maximum rule retains both component scores, support and provenance. Missing components stay absent; unavailable models never use a shared-classifier fallback. Model changes queue bounded recording rescoring.",
         "Recording character and within-song arc, outside the listener's four taste layers.",
         ("src/yt_playlist/rec/vibe_intensity.py", "src/yt_playlist/rec/musical_character.py", "src/yt_playlist/rec/vibe_recording_rules.py")),
    Node("joint-vibe", "Vibe Prediction Model", GROUP, "Local statistical model",
         "Estimates for missing vibe and intensity scores",
         "Estimates missing scores using measured recordings that share an artist, album or specific genre. Each estimate includes an uncertainty range.",
         "Original audio/provider measurements, canonical artist credits, album and specific genre relationships. One recording contributes once; propagated predictions and duplicate uploads are not fresh training examples.",
         "Qualified, bounded worker fits publish durable per-channel artifacts and score generations. Input revisions trigger preparation; failed or over-budget fits defer without replacing useful prior output.",
         "A recording-property model rebuilt as coverage grows, outside the four taste layers.",
         ("src/yt_playlist/rec/vibe_joint_calibrated.py", "src/yt_playlist/rec/vibe_joint_state.py", "src/yt_playlist/rec/vibe_recording_rules.py"),
         caution="Uncertainty is conditional on fitted variance components. Specialist bias is not identified by a measurement holdout; these are not enjoyment probabilities."),
    Node("musical-profile", "Musical character profile", GROUP, "Evidence policy",
         "A source-aware description of each recording",
         "Supported recording dimensions such as intensity, relaxation, happiness, sadness and aggression, plus vibe suitability and provenance.",
         "Provider and catalog observations, including AcousticBrainz mood scores, genre priors, user corrections, specialist scores and qualified recording-level estimates.",
         "Versioned background preparation reconciles evidence by recording and retains support, conflict and source lineage. Missing evidence stays unknown; predictions cannot become original measurements.",
         "Recording properties. A user's listening changes which profiles are requested, not the definition of the sound.",
         ("src/yt_playlist/rec/musical_profiles.py", "src/yt_playlist/rec/musical_preparation.py", "src/yt_playlist/rec/musical_character.py")),
)

# Keep the extension heads individually visible, matching its production manifest.
MOOD_HEADS = (
    ("relaxed", "Relaxed mood", "relaxed versus non-relaxed character"),
    ("aggressive", "Aggressive mood", "aggressive versus non-aggressive character"),
    ("happy", "Happy mood", "happy versus non-happy character"),
    ("sad", "Sad mood", "sad versus non-sad character"),
    ("deam", "DEAM valence / arousal", "continuous valence and arousal on the source's 1–9 scale"),
)

MODELS += tuple(Node(
    "mood-" + key, title, GROUP, "Frozen audio head", prediction.capitalize(),
    "Predicts " + prediction + " for each captured excerpt. The research preview uses these scores to show changes across excerpts.",
    "MusiCNN features for each audio patch and the corresponding bundled model. Scores are averaged across patches within an excerpt.",
    "Packaged, versioned model graphs are integrity checked with the extension. Captured observations retain each head's version; no per-user fitting.",
    "Excerpt scale. Comparing sampled positions shows within-song change, outside the listener's four taste layers.",
    ("extension/audio_model/mood_models.json", "extension/audio_model/model_worker.js"),
    caution="A description of the music's affect, not the listener's emotional state or taste."
) for key, title, prediction in MOOD_HEADS)

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
    "Frozen MusiCNN excerpt features. " + data,
    "Development recipes export coefficients and provenance to a bundled, validated artifact. Runtime uses the configured active/shadow/off policy where supported. Ordered-arc rules combine observed excerpts; unheard gaps are not interpolated.",
    "Within-song temporal structure only: calm/easygoing/focus retain the weakest excerpt; Release retains the peak; Energize/Uplift reward sustained or building character.",
    ("src/yt_playlist/rec/audio_vibe_" + key + ".py", "src/yt_playlist/rec/audio_vibe_arc.py",
     "src/yt_playlist/controllers/vibe_prediction.py", "src/yt_playlist/rec/audio_vibe_values.py",
     "experiments/audio_lab/models/" + key + ".md"),
    caution="Suitability is not calibrated enjoyment, and independent specialist scores should not be compared across purposes."
) for key, title, output, data in SPECIALISTS)

EDGES = (
    Edge("musicnn", "audio-research", "Compute the frozen embeddings used to fit specialist recipes."),
    Edge("audio-descriptors", "audio-research", "Evaluate captured measurements as optional features in exploratory comparisons."),
    Edge("vibe-energize", "vibe-intensity", "Ordered activation supplies one intensity component."),
    Edge("vibe-release", "vibe-intensity", "Ordered cathartic force supplies the other intensity component."),
    Edge("joint-vibe", "vibe-intensity", "Qualified estimates can supply missing activation or force evidence."),
    Edge("vibe-intensity", "musical-profile", "Retain component evidence and the explicit intensity policy for stream controls."),
    Edge("joint-vibe", "musical-profile", "Fill eligible missing recording properties with support and uncertainty.", "Qualified model and usable support"),
) + tuple(edge for key, *_ in MOOD_HEADS for edge in (
    Edge("musicnn", "mood-" + key, "The packaged head reads MusiCNN audio features."),
    Edge("mood-" + key, "audio-research", "Describe captured excerpts in the preview and supply optional features in experiments."),
)) + tuple(edge for key, *_ in SPECIALISTS for edge in (
    Edge("audio-research", "vibe-" + key, "Supply the evaluated and exported specialist artifact."),
    Edge("musicnn", "vibe-" + key, "The exported specialist reads frozen excerpt features."),
    Edge("vibe-" + key, "joint-vibe", "Original measured suitability can train its recording-level pooling channel."),
    Edge("vibe-" + key, "musical-profile", "Supply within-purpose suitability after ordered excerpt aggregation.", "Specialist enabled and compatible audio"),
))
