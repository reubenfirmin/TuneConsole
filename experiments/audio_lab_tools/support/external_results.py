"""Consolidate completed offline experiment evidence without changing models."""
import argparse
import json
from pathlib import Path
import time

from .external_experiment import OUT, save, stamp


def compact_report(path):
    """Avoid reparsing large per-fold exhaustive searches every watcher tick."""
    stat = path.stat()
    version = [stat.st_mtime_ns, stat.st_size]
    cached_path = OUT / 'report-summaries' / path.name
    if cached_path.exists():
        cached = json.loads(cached_path.read_text())
        if cached['source_version'] == version:
            return cached['report']
    report = json.loads(path.read_text())
    report.pop('teacher_unseen_subset', None)
    report.pop('choices_by_fold', None)
    if 'combination_search' in report:
        report['combination_search'].pop('folds', None)
    if 'methods' in report:
        methods = report['methods']
        fixed = {name: value for name, value in methods.items() if name.startswith(('equal:', 'weighted_per_vibe:'))
                 and 'macro_average_precision' in value}
        if fixed:
            best = max(fixed, key=lambda name: fixed[name]['macro_average_precision'])
            report['best_fixed_by_ap'] = {'name': best, 'metrics': fixed[best]}
        report['methods'] = {name: value for name, value in methods.items() if name.startswith('expert:') or
                             name in ('selected_combination', 'selected_consensus', 'selected_candidate',
                                      'equal_external', 'equal_all', 'weighted_external', 'weighted_all')}
    save(cached_path, {'source_version': version, 'report': report})
    return report


def write_summary():
    reports = []
    for dataset in ['soundtracks-set2', 'cal500', 'emotify', 'pmemo', 'soundtracks-all', 'vgmidi-original',
                    'cal500-raw-activity', 'jamendo-human-00', 'coimbra-mirex-like', '4q-emotion',
                    'deam', 'jamendo-human-4shards', 'pmemo-dynamic', 'deam-dynamic',
                    'amg1608-matched', 'ntwicm-matched', 'emotify-multiwindow', 'emma-preview-matched',
                    'merge-quadrants', 'merge-tag-va']:
        path = OUT / f'{dataset}.report.json'
        if path.exists():
            reports.append(json.loads(path.read_text()))
    lines = ['# External audio experiments', '', f'Updated {stamp()}.', '',
             'All artifacts are offline research. The database, active model, six vibe meanings, and human labels are unchanged.', '']
    suite_path = OUT / 'suite-status.json'
    if suite_path.exists():
        suite = json.loads(suite_path.read_text())
        active = [row for row in suite['datasets'] if row['state'] in ('extracting', 'training')]
        lines.append(f"Native suite: {suite['state']}.")
        for row in active:
            path = OUT / f"{row['dataset']}.status.json"
            progress = json.loads(path.read_text()) if path.exists() else {}
            lines.append(f"Current corpus: {row['dataset']} ({row['state']}), "
                         f"{progress.get('completed', 0)} / {progress.get('total', 'unknown')} extracted.")
        lines.append('')
    additional_path = OUT / 'additional-datasets-status.json'
    if additional_path.exists():
        additional = json.loads(additional_path.read_text())
        lines.extend([f"Additional dataset: {additional['dataset']} ({additional['state']}).", ''])
    campaign_path = OUT / 'campaign-status.json'
    if campaign_path.exists():
        campaign = json.loads(campaign_path.read_text())
        lines.extend([f"Expanded campaign: {campaign['state']}; current step: {campaign.get('active') or 'none'}.", ''])
        for row in {row['name']: row for row in campaign['steps']}.values():
            if row['state'] == 'failed':
                lines.append(f"Failed trial: {row['name']}: {row['error']}")
    followups_path = OUT / 'followups-status.json'
    if followups_path.exists():
        followups = json.loads(followups_path.read_text())
        lines.extend([f"Follow-ups: {followups['state']}; current step: {followups.get('active') or 'none'}.", ''])
        for row in {row['name']: row for row in followups['steps']}.values():
            if row['state'] == 'failed':
                lines.append(f"Failed follow-up: {row['name']}: {row['error']}")
    lines.extend([
             '## Native-label comparisons', '',
             'Targets retain each dataset’s native meanings, normalized to 0–1. Lower MAE is better. '
             'Five outer and three inner grouped folds; each dataset uses the same folds across feature families.', '',
             '| Dataset | Examples | Groups | Mean baseline | Median baseline | Attributes | Embeddings | Combined |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |'])
    for report in reports:
        family = report['families']
        lines.append(f"| {report['dataset']} | {report['rows']} | {report['unique_groups']} | "
                     f"{report['baseline']['mae']:.4f} | {report['median_baseline']['mae']:.4f} | "
                     f"{family['attributes']['mae']:.4f} | {family['embedding']['mae']:.4f} | "
                     f"{family['attributes_embedding_moods']['mae']:.4f} |")
    cal = next((report for report in reports if report['dataset'] == 'cal500'), None)
    if cal:
        lines.extend(['', '## CAL500 activity judgments', '',
                      'Published soft annotation weights are the targets. The activity matrices discard single-Yes choices. Rare positives can make '
                      'a constant prediction competitive; assess correlations as well as error.', '',
                      '| Activity | Mean MAE | Median MAE | Attribute MAE / Pearson | Embedding MAE / Pearson |',
                      '| --- | ---: | ---: | ---: | ---: |'])
        for name in cal['target_names']:
            if not name.startswith('Usage-'):
                continue
            attribute = cal['families']['attributes']['targets'][name]
            embedding = cal['families']['embedding']['targets'][name]
            def number(value):
                return 'undefined' if value is None else f'{value:.3f}'
            lines.append(f"| {name[6:].replace('_', ' ')} | {cal['baseline']['targets'][name]['mae']:.3f} | "
                         f"{cal['median_baseline']['targets'][name]['mae']:.3f} | "
                         f"{attribute['mae']:.3f} / {number(attribute['pearson'])} | "
                         f"{embedding['mae']:.3f} / {number(embedding['pearson'])} |")
        lines.extend(['', 'Activity ranking is also reported in [cal500.ranking.md](cal500.ranking.md). '
                      'Its AP and AUC use the supplied binary targets; top-ten lift uses the supplied filtered soft weights.'])
    audit_path = OUT / 'annotation-audit.json'
    if audit_path.exists():
        audit = json.loads(audit_path.read_text())
        lines.extend(['', '## Annotation audit', '',
                      f"CAL500 supplies {audit['cal500']['responses']} raw responses for {audit['cal500']['tracks']} tracks. "
                      'For activity targets, the supplied matrices implement Yes fraction when at least two people select '
                      'the activity, otherwise zero. Their binary targets indicate nonzero weight. '
                      'This differs from the README’s stated 80 percent consensus rule.', '',
                      'The separate cal500-raw-activity experiment uses every raw Yes fraction, with omitted checkboxes '
                      'treated as unselected. Its absolute errors concern a different target definition and should not be '
                      'compared numerically with the published benchmark.', '',
                      f"Emotify supplies {audit['emotify']['responses']} responses for {audit['emotify']['tracks']} tracks. "
                      'The audit records repeated listener split-half correlations for both corpora; these are descriptive stability '
                      'measurements, not accuracy ceilings or confidence intervals.'])
    genre_path = OUT / 'emotify.genre-check.json'
    if genre_path.exists():
        genre = json.loads(genre_path.read_text())['overall']
        lines.extend(['', '## Emotify genre check', '',
                      f"The training-genre-mean baseline has MAE {genre['training_genre_mean']['mae']:.4f}; "
                      f"embeddings have MAE {genre['embedding']['mae']:.4f}. "
                      'See [within-genre correlations](emotify.genre-check.md). '
                      'Genre identities are supplied only to the diagnostic baseline.'])
    transfers = [json.loads(path.read_text()) for path in sorted(OUT.glob('transfer-*.report.json'))]
    if transfers:
        lines.extend(['', '## Existing six-vibe diagnostic', '',
                      '55 human-labeled songs have compatible embeddings; the remaining gold label is preserved. '
                      'The 63 silver predictions are excluded. This small, previously examined snapshot is exploratory.', '',
                      '| External heads added | Embedding top choice acceptable | Qualities only | Embedding plus qualities | '
                      'Embedding log loss | Combined log loss |',
                      '| --- | ---: | ---: | ---: | ---: | ---: |'])
        for report in sorted(transfers, key=lambda r: len(r['datasets'])):
            f = report['families']
            lines.append(f"| {', '.join(report['datasets'])} | {f['embedding']['top_1_acceptable']:.1%} | "
                         f"{f['external_qualities']['top_1_acceptable']:.1%} | "
                         f"{f['embedding_external_qualities']['top_1_acceptable']:.1%} | "
                         f"{f['embedding']['acceptable_log_loss']:.3f} | "
                         f"{f['embedding_external_qualities']['acceptable_log_loss']:.3f} |")
    consensus_reports = [compact_report(path) for path in sorted(OUT.glob('consensus-*.report.json'))]
    if consensus_reports:
        def consensus_label(report):
            label = ', '.join(report['datasets'])
            native, calibration = report.get('native_learner', 'ridge'), report.get('calibrator_learner', 'linear')
            if native != 'ridge' or calibration != 'linear':
                label += f' [native {native}; calibration {calibration}]'
            return label
        lines.extend(['', '## Dataset-expert consensus', '',
                      'Each native dataset expert is separately calibrated to the preserved six vibes. '
                      'Mixture weights use artist-held-out predictions inside the outer training fold. '
                      'The same small gold snapshot is exploratory; shared embeddings allow correlated mistakes.', '',
                      '| Datasets | Embedding alone | Best single external expert | Equal external votes | Weighted external votes | '
                      'Embedding log loss | Weighted external log loss |',
                      '| --- | ---: | --- | ---: | ---: | ---: | ---: |'])
        for report in sorted(consensus_reports, key=lambda value: len(value['datasets'])):
            methods = report['methods']
            best = min(('expert:' + name for name in report['datasets']),
                       key=lambda name: methods[name]['acceptable_log_loss'])
            lines.append(f"| {consensus_label(report)} | {methods['expert:embedding']['top_1_acceptable']:.1%} | "
                         f"{best[7:]}: {methods[best]['top_1_acceptable']:.1%} | "
                         f"{methods['equal_external']['top_1_acceptable']:.1%} | "
                         f"{methods['weighted_external']['top_1_acceptable']:.1%} | "
                         f"{methods['expert:embedding']['acceptable_log_loss']:.3f} | "
                         f"{methods['weighted_external']['acceptable_log_loss']:.3f} |")
        lines.extend(['', 'The best single expert is shown descriptively after evaluation; it is not an independently selected final model. '
                      'Per-fold weights, error correlations, and disagreement checks are retained in consensus report JSON. '
                      'Agreement has not been established as a reliable confidence estimate.'])
        searched = [report for report in consensus_reports if report.get('combination_search')]
        if searched:
            lines.extend(['', '### Consensus subset search', '',
                          'Every expert subset is compared, with equal and learned pooling for multi-expert subsets. '
                          'Selected methods choose the members and pooling policy inside each outer training fold. '
                          'Inner objectives reuse weight-fitting labels; outer folds assess the complete selection procedure. '
                          'The highest-scoring fixed subset is descriptive after inspection.', '',
                          '| Datasets | Nonempty subsets | Selected consensus acceptable / log loss | Selected candidate acceptable / log loss |',
                          '| --- | ---: | ---: | ---: |'])
            for report in sorted(searched, key=lambda value: len(value['datasets'])):
                methods = report['methods']
                consensus, candidate = methods['selected_combination'], methods['selected_candidate']
                lines.append(f"| {consensus_label(report)} | {report['combination_search']['subsets']} | "
                             f"{consensus['top_1_acceptable']:.1%} / {consensus['acceptable_log_loss']:.3f} | "
                             f"{candidate['top_1_acceptable']:.1%} / {candidate['acceptable_log_loss']:.3f} |")
    completed_consistency = sorted(OUT.glob('unlabeled-consistency*/*.report.json'))
    if completed_consistency:
        lines.extend(['', '## Learning from unlabeled audio', '',
                      'Real audio pairs differ by gentle gain attenuation and 35 dB SNR noise. '
                      'Known CAL500/gold artist, title, and recording matches are excluded from the unlabeled pool. '
                      'These pairs contribute a consistency penalty, with no pseudo-label targets.', ''])
        for path in completed_consistency:
            report = json.loads(path.read_text())
            results = report['methods']
            lines.append(f"Trial `{path.parent.name}`: {report['unlabeled_pairs']} training pairs, "
                         f"{report.get('unique_unlabeled_recordings', '159 unique recordings across the original 160 training/holdout entries; duplicate within training only')}.")
            if report['dataset'] == 'gold-vibes':
                lines.append(f"- Gold vibes: ordinary ridge {results['ordinary_ridge']['top_1_acceptable']:.1%} acceptable; "
                             f"consistency {results['unlabeled_consistency']['top_1_acceptable']:.1%}. "
                             f"Acceptable-label log loss {results['ordinary_ridge']['acceptable_log_loss']:.3f} "
                             f"and {results['unlabeled_consistency']['acceptable_log_loss']:.3f}.")
            else:
                lines.append(f"- {report['dataset']}: ordinary ridge MAE {results['ordinary_ridge']['mae']:.4f}; "
                             f"consistency MAE {results['unlabeled_consistency']['mae']:.4f}.")
            robustness = report.get('unlabeled_holdout_robustness')
            if robustness:
                ordinary, regularized = robustness['ordinary_ridge'], robustness['unlabeled_consistency']
                lines.append(f"  On {ordinary['recordings']} separate unlabeled recordings, mean prediction change "
                             f"under augmentation is {ordinary['mean_absolute_prediction_change']:.4f} versus "
                             f"{regularized['mean_absolute_prediction_change']:.4f}; clean prediction variability "
                             f"is {ordinary['mean_clean_prediction_std']:.4f} versus "
                             f"{regularized['mean_clean_prediction_std']:.4f}. These pairs never enter fitting or selection.")
    learner_reports = [json.loads(path.read_text()) for path in sorted(OUT.glob('*.learners.report.json'))]
    if learner_reports:
        lines.extend(['', '## Alternative learners', '',
                      'Linear ridge, radial-basis kernel ridge, and nearest neighbours use the same grouped outer folds. '
                      'Parameters and the selected-learner method are chosen inside outer training folds. '
                      'Each feature family is an exploratory comparison, not an independently selected winner.', '',
                      '| Dataset / features | Linear | Radial basis | Neighbours | Selected learner |',
                      '| --- | ---: | ---: | ---: | ---: |'])
        for report in learner_reports:
            for family, result in report['features'].items():
                def value(method):
                    metrics = result['methods'][method]
                    return (f"{metrics['top_1_acceptable']:.1%} / {metrics['acceptable_log_loss']:.3f}"
                            if report['dataset'] == 'gold-vibes' else f"{metrics['mae']:.4f}")
                label = report['dataset'] + ' / ' + family
                if report['dataset'] == 'gold-vibes':
                    label += ' (' + ', '.join(report['external_datasets']) + ')'
                lines.append(f"| {label} | {value('linear')} | {value('rbf')} | {value('neighbours')} | {value('selected_learner')} |")
        lines.extend(['', 'Native numbers are MAE; gold-vibe numbers are acceptable top choice / acceptable-label log loss.'])
    classification_reports = [json.loads(path.read_text()) for path in sorted(OUT.glob('*.classification.report.json'))]
    if classification_reports:
        lines.extend(['', '## Native mood classification', '',
                      'Parameters are selected by multiclass log loss inside grouped folds. Each cell reports accuracy / balanced accuracy / log loss.', '',
                      '| Dataset / features | Linear | Radial basis | Neighbours | Softmax | Selected learner |',
                      '| --- | ---: | ---: | ---: | ---: | ---: |'])
        for report in classification_reports:
            for family, result in report['features'].items():
                cells = []
                for method in ('linear', 'rbf', 'neighbours', 'softmax', 'selected_learner'):
                    value = result['methods'][method]
                    cells.append(f"{value['accuracy']:.1%} / {value['balanced_accuracy']:.1%} / {value['log_loss']:.3f}")
                lines.append(f"| {report['dataset']} / {family} | " + ' | '.join(cells) + ' |')
    expansion_path = OUT / 'merge-4q-expansion.report.json'
    if expansion_path.exists():
        report = json.loads(expansion_path.read_text())
        lines.extend(['', '## Added MERGE training audio on identical held-out 4Q songs', '',
                      'Both methods use the same global artist/audio folds and held-out 4Q targets. These folds can differ from the earlier standalone 4Q diagnostics.', '',
                      '| Features | 4Q-only accuracy / balanced accuracy | With MERGE accuracy / balanced accuracy |',
                      '| --- | ---: | ---: |'])
        for family, methods in report['methods'].items():
            baseline, expanded = methods['4q_only'], methods['4q_plus_merge']
            lines.append(f"| {family} | {baseline['accuracy']:.1%} / {baseline['balanced_accuracy']:.1%} | "
                         f"{expanded['accuracy']:.1%} / {expanded['balanced_accuracy']:.1%} |")
    temporal_reports = [json.loads(path.read_text()) for path in sorted(OUT.glob('*.temporal-comparison.json'))]
    if temporal_reports:
        lines.extend(['', '## Time-aligned supervision', '',
                      'Both supervision methods are scored against the same held-out dynamic targets on identical recordings and grouped folds.', '',
                      '| Dataset / features | Recordings | Static-trained MAE | Aligned-trained MAE |',
                      '| --- | ---: | ---: | ---: |'])
        for report in temporal_reports:
            for family, methods in report['methods'].items():
                lines.append(f"| {report['dataset']} / {family} | {report['rows']} | "
                             f"{methods['static']['mae']:.4f} | {methods['aligned_dynamic']['mae']:.4f} |")
    projection_reports = [json.loads(path.read_text()) for path in sorted(OUT.glob('*.unlabeled-projection.report.json'))]
    if projection_reports:
        lines.extend(['', '## Unlabeled representation learning', '',
                      'PCA dimensions, whitening and ridge strength are chosen inside grouped folds. The projection sees only other corpora, with known target identities excluded.', '',
                      '| Dataset | Labeled examples | Unlabeled recordings | Held-out score |',
                      '| --- | ---: | ---: | ---: |'])
        for report in projection_reports:
            value = report['metrics']
            cell = (f"{value['top_1_acceptable']:.1%} / {value['acceptable_log_loss']:.3f}"
                    if report['dataset'] == 'gold-vibes' else f"MAE {value['mae']:.4f}")
            lines.append(f"| {report['dataset']} | {report['rows']} | {report['unlabeled_recordings']} | {cell} |")
    ranking_reports = [compact_report(path) for path in sorted(OUT.glob('vibe-ranking-*.report.json'))]
    if ranking_reports:
        lines.extend(['', '## Ranking songs for each preserved vibe', '',
                      'Independent class-balanced suitability scores match the product operation of ranking songs within a selected vibe. '
                      'Average precision and AUC are averaged equally across the six vibes. Unselected labels are comparison negatives; they are not explicit listener rejections. '
                      'The same previously examined 55 songs remain exploratory.', '',
                      '| Sources / method | Macro average precision | Macro AUC | Balanced binary log loss |',
                      '| --- | ---: | ---: | ---: |'])
        for report in ranking_reports:
            for method in ('expert:embedding', 'expert:emotify', 'selected_candidate', 'selected_consensus', 'equal_all', 'weighted_all'):
                value = report['methods'][method]
                lines.append(f"| {', '.join(report['sources'])} / {method} | {value['macro_average_precision']:.3f} | "
                             f"{value['macro_roc_auc']:.3f} | {value['balanced_binary_log_loss']:.3f} |")
            lines.append(f"Search covered {report['nonempty_subsets']} nonempty subsets. Per-vibe pooling and subset choice stay within outer training folds.")
            best = report.get('best_fixed_by_ap')
            if best:
                lines.append(f"Best fixed subset after inspection: `{best['name']}`, macro AP {best['metrics']['macro_average_precision']:.3f}; exploratory, not separately selected validation.")
        lines.extend(['', '| Sources / selected candidate | Relax AP | Focus AP | Cruise AP | Energize AP | Uplift AP | Release AP |',
                      '| --- | ---: | ---: | ---: | ---: | ---: | ---: |'])
        for report in ranking_reports:
            values = report['methods']['selected_candidate']['per_vibe']
            cells = [f"{values[name]['average_precision']:.3f}" for name in ('relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release')]
            lines.append(f"| {', '.join(report['sources'])} | " + ' | '.join(cells) + ' |')
    shared_path = OUT / 'multitask.report.json'
    if shared_path.exists():
        report = json.loads(shared_path.read_text())
        lines.extend(['', '## Shared representation with native tasks', '',
                      'Separate native target heads share a learned audio representation. These comparisons use new global artist/audio-component folds, so compare methods within this table.', '',
                      '| Native corpus | Individual ridge MAE | Supervised shared MAE | Shared with optional unlabeled consistency MAE |',
                      '| --- | ---: | ---: | ---: |'])
        for source, methods in report['native_results'].items():
            lines.append(f"| {source} | {methods['individual_ridge']['mae']:.4f} | "
                         f"{methods['supervised_shared']['mae']:.4f} | {methods['unlabeled_shared']['mae']:.4f} |")
    for path in sorted(OUT.glob('multitask-*.gold.report.json')):
        report = json.loads(path.read_text())
        lines.extend(['', f"Shared-model ranking diagnostic: `{path.stem}`.", ''])
        for family, value in report.get('ranking_methods', {}).items():
            lines.append(f"- {family}: macro AP {value['macro_average_precision']:.3f}; AUC {value['macro_roc_auc']:.3f}; balanced log loss {value['balanced_binary_log_loss']:.3f}.")
    projection_ranking = OUT / 'gold-vibes.unlabeled-projection-ranking.report.json'
    if projection_ranking.exists():
        report = json.loads(projection_ranking.read_text())
        value = report['metrics']
        lines.extend(['', '## Unlabeled projection for vibe ranking', '',
                      f"Nested selected PCA dimension, whitening and independent binary calibration: macro AP {value['macro_average_precision']:.3f}, AUC {value['macro_roc_auc']:.3f}, balanced log loss {value['balanced_binary_log_loss']:.3f}.",
                      'The projection uses only other corpora’s unlabeled audio, with known target identities excluded. The small gold snapshot remains exploratory.'])
    for path in sorted(OUT.glob('*.rater-weights.report.json')):
        report = json.loads(path.read_text())
        lines.extend(['', '## Listener-count weighting', '',
                      'Listener-count powers and regularization are selected using native validation. Unweighted training remains a candidate.', '',
                      '| Policy | Native MAE | Gold ranking macro AP | Gold ranking AUC |',
                      '| --- | ---: | ---: | ---: |'])
        for method, native in report['native_methods'].items():
            ranking = report['ranking_methods'][method]
            lines.append(f"| {method} | {native['mae']:.4f} | {ranking['macro_average_precision']:.3f} | {ranking['macro_roc_auc']:.3f} |")
    cross_path = OUT / 'cross-corpus.report.json'
    if cross_path.exists():
        report = json.loads(cross_path.read_text())
        lines.extend(['', '## Transfer between native VA corpora', '',
                      'Fit and select on the source corpus only, exclude known target artist/title/audio overlaps, and score a separate corpus. Native scales and listener instructions still differ.', '',
                      '| Source to target | Disjoint training examples | Test examples | Source-mean MAE | Embedding ridge MAE | Embedding RBF MAE | Inner-selected learner MAE |',
                      '| --- | ---: | ---: | ---: | ---: | ---: | ---: |'])
        for case, result in report['directions'].items():
            if result.get('state') != 'complete':
                continue
            methods = result['methods']
            lines.append(f"| {case.replace('__to__', ' to ')} | {result['training_rows']} | {result['test_rows']} | "
                         f"{methods['source_mean']['mae']:.4f} | {methods['embedding:linear']['mae']:.4f} | "
                         f"{methods['embedding:rbf']['mae']:.4f} | {methods['embedding:selected_learner']['mae']:.4f} |")
    semantic_path = OUT / 'semantic-fixed.report.json'
    if semantic_path.exists():
        report = json.loads(semantic_path.read_text())
        lines.extend(['', '## Fixed mappings without developer-label fitting', '',
                      'Native heads and reference percentiles use external data only. Equal-weight semantic rules and source pools are fixed before this trial; the previously examined developer set remains exploratory.', '',
                      '| Mapping | Macro average precision | Macro AUC | Acceptable top choice |',
                      '| --- | ---: | ---: | ---: |'])
        for method, value in report['methods'].items():
            lines.append(f"| {method} | {value['macro_average_precision']:.3f} | {value['macro_roc_auc']:.3f} | {value['top_choice']['top_1_acceptable']:.1%} |")
    export_path = OUT / 'export-replay.report.json'
    if export_path.exists():
        report = json.loads(export_path.read_text())
        lines.extend(['', f"Export verification: {len(report['models'])} complete expert pipelines independently replayed. This establishes numerical export completeness, not product integration or predictive quality."])
    lines.extend(['', '## Limits', '',
                  '- Native label quality and transfer to the six product vibes are separate outcomes.',
                  '- Upstream pretrained-model overlap is not fully known; validation does not establish entirely unseen pretraining audio.',
                  '- Whole-song judgments are weak supervision for one ten-second excerpt.',
                  '- Soundtracks Set 1 and Set 2 overlap, and Set 1 grouping metadata is incomplete.',
                  '- Product users are never asked to rate vibes; these developer labels only diagnose the preserved meanings.',
                  '- All candidate models remain research artifacts. No candidate is activated automatically.', ''])
    (OUT / 'RESULTS.md').write_text('\n'.join(lines))
    save(OUT / 'results-status.json', {'updated_at': stamp(), 'native_reports': [r['dataset'] for r in reports],
                                     'transfer_reports': len(transfers), 'consensus_reports': len(consensus_reports),
                                     'consistency_reports': len(completed_consistency), 'learner_reports': len(learner_reports)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    while True:
        write_summary()
        if not args.watch:
            return
        suite_path = OUT / 'suite-status.json'
        consistency_path = OUT / 'unlabeled-consistency/status.json'
        if suite_path.exists() and consistency_path.exists():
            native = json.loads(suite_path.read_text())['state']
            consistency = json.loads(consistency_path.read_text())['state']
            additional_path = OUT / 'additional-datasets-status.json'
            additional = json.loads(additional_path.read_text())['state'] if additional_path.exists() else 'complete'
            campaign_path = OUT / 'campaign-status.json'
            campaign = json.loads(campaign_path.read_text())['state'] if campaign_path.exists() else 'not_started'
            followups_path = OUT / 'followups-status.json'
            followups = json.loads(followups_path.read_text())['state'] if followups_path.exists() else 'not_started'
            if native.startswith('complete') and consistency in ('complete', 'failed') and additional in ('complete', 'failed') and campaign.startswith('complete') and followups.startswith('complete'):
                return
        time.sleep(30)


if __name__ == '__main__':
    main()
