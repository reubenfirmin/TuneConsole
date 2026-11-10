"""Finite model checkpoint fields shared by readers and explicit preparation."""

KEYS = {'rec_preparation_basis':'content_basis_checkpoint',
        'rec_preparation_full':'content_full_checkpoint','rec_preparation_fit':'content_fit_checkpoint'}
FIELDS = {
 'rec_preparation_basis':('serial','rows','at','reason','estimated_bytes','dimensions','input_fingerprint'),
 'rec_preparation_full':('content','evidence','rows','at'),
 'rec_preparation_fit':('state','reason','attempted_at','attempts','since','next_attempt_at','memory_bytes',
                        'estimated_bytes','rows','dimensions','max_rows')}
MODES = ('mode_count','mode_min_members','mode_match_threshold')
CONFIG = ('embed_method','dimensions_setting','cooc_weighting','restore_run','mode_count_setting',
          'mode_min_members_setting','mode_match_threshold_setting')
PHASES = ('encoding','clustering','publication')
GROUP = ('rec_group_shadow_progress', 'rec_group_shadow_checkpoint', 'rec_mode_fit_inputs')
CANDIDATES = ('rec_candidate_preferences', 'rec_candidates_expire_at')
PREPARED = {
    **{key:'prepared_'+table for key,table in KEYS.items()},
    GROUP[0]:'prepared_group_progress_checkpoint_v2', GROUP[1]:'prepared_group_generation_checkpoint',
    GROUP[2]:'prepared_group_fit_checkpoint', CANDIDATES[0]:'prepared_candidate_preferences_checkpoint',
    CANDIDATES[1]:'prepared_candidate_expiry_checkpoint',
}
