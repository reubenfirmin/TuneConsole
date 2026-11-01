"""Versioned human judgments, separate from product vibe calculations."""

FILE_FORMAT = 'audio-annotation-1'
LEGACY_FEEDBACK_TAXONOMY = 'audio-feedback-presets-1'
TAXONOMIES = {
    LEGACY_FEEDBACK_TAXONOMY: ('gentle', 'boost', 'focus', 'bright', 'unwind', 'move'),
    'audio-session-targets-1': ('relax', 'concentrate', 'cruise', 'release', 'energize', 'uplift'),
}
KINDS = ('feedback', 'development', 'session')
STATES = ('labeled', 'empty', 'withdrawn')
