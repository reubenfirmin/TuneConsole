"""Additional identity-only audit of the fixed shared model's native cohort.

This records a provenance check after the baseline evaluation. It changes no
cohort, fit, score or selection and does not reopen human outcomes for scoring.
"""
import argparse
import json

from .candidate_followup import DEFAULT as FOLLOWUP
from .cruise_baselines import DEFAULT, identities, validate
from .external_experiment import digest, save, stamp
from .transfer_identity import exclusion
from .vibe_specialist_round import load


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    plan = validate(DEFAULT)
    output = DEFAULT / 'shared-native-exposure.json'
    if output.exists():
        raise ValueError('Preserve the existing identity audit')
    source = FOLLOWUP / 'inputs/emotify-artist.json'
    reserved = DEFAULT / 'inputs/reserved.json'
    def only_identities(rows):
        return identities([{k: r[k] for k in ('video_id', 'artist', 'title') if k in r} for r in rows])
    current = only_identities(load(reserved)['rows'])
    # The product export declares the complete original 400-row Emotify cohort.
    native = load(source)['rows']
    if len(native) != load(DEFAULT / 'checkpoint.json')['training']['native_songs']:
        raise ValueError('Original native cohort size differs from the checkpoint')
    keep, audit = exclusion(current, native)
    result = {'created_at': stamp(), 'plan_sha256': digest(DEFAULT / 'plan.json'),
              'evaluation_receipt_sha256': digest(DEFAULT / 'evaluation.receipt.json'),
              'inputs': {str(source): digest(source), str(reserved): digest(reserved)},
              'code_sha256': digest(__file__), 'native_rows': len(native), 'reserved_rows': len(current),
              'matched_reserved_ids': [r['id'] for r, retained in zip(current, keep, strict=True) if not retained],
              'audit': audit, 'outcomes_scored': False,
              'scope': 'Additional recorded shared-native identity audit; no fits, scores, cohort changes or model selection.'}
    if digest(source) != plan['references'][str(source)]:
        raise ValueError('Native reference changed')
    save(output, result)
    print(json.dumps({'audit': str(output), 'reserved_rows': len(current),
                      'matched_reserved_ids': result['matched_reserved_ids'], 'outcomes_scored': False}), flush=True)


if __name__ == '__main__':
    main()
