"""Resume evidence maintenance and capture protection without owning storage mechanics."""
from yt_playlist.core.activity import check_cancellable_work


class EvidenceProtection:
    def __init__(self, proposals, radio, selection, settings, *, home, comparison):
        self.proposals, self.radio, self.selection, self.settings = proposals, radio, selection, settings
        self.home, self.comparison = home, comparison

    def capture(self):
        protected, opportunities = set(), set()
        for format in ('albums', 'mixes', 'radio'):
            window = self.proposals.get_proposals('choice_window:' + format) or {}
            if window.get('opportunity_id'):
                opportunities.add(window['opportunity_id'])
        live, monitor = self.comparison()
        if live.get('phase') in ('preparing', 'observing') and live.get('vectors'):
            protected.add(live['vectors'])
        try:
            radio = self.radio.load()[0] or {}
            if not radio.get('stopped'):
                direction = radio.get('directions') or {}
                if direction.get('menu_id'):
                    opportunities.add(direction['menu_id'])
        except (TypeError, ValueError):
            pass
        pair_artifacts, surfaces = self.selection.retained_pair_artifacts()
        protected.update(pair_artifacts)
        for surface in surfaces:
            pair = self.proposals.get_proposals(surface) or {}
            data = pair.get('data') if isinstance(pair.get('data'), dict) else pair
            context = data.get('context') or data.get('choice_context') or {}
            if context.get('artifact'):
                protected.add(context['artifact'])
        runs = self.selection.selection_runs(limit=12)
        active = self.settings.get_setting('rec_selection_active_run')
        if active and not any(str(run['id']) == active for run in runs):
            run = self.selection.selection_run(active)
            if run:
                runs.append(run)
        protected.update(run['serving_vectors'] for run in runs if run.get('serving_vectors'))
        protected.update(note['evidence'] for note in (self.proposals.get_proposals('album_notes') or {}).values()
                         if note.get('evidence'))
        for surface in ('album_prepared', 'home_prepared', 'home_tuned', 'home_choice'):
            payload = self.home(surface) or {}
            for field in ('context', 'choice_context', 'album_choice_context'):
                context = payload.get(field) or {}
                if context.get('artifact'):
                    protected.add(context['artifact'])
        runs = {live.get('run_id') if live.get('phase') == 'observing' else None,
                monitor.get('run_id') if not monitor.get('complete') else None} - {None}
        return protected, opportunities, runs


class ChoiceStorage:
    def __init__(self, ledger, settings, *, phase_key, inventory, acknowledge, more, capture,
                 pending, protected, pending_protection, retired, phase, progress, retention_days,
                 diagnostic_days, diagnostic_fraction):
        self.ledger, self.settings, self.phase_key = ledger, settings, phase_key
        self.inventory, self.acknowledge, self.more, self.capture = inventory, acknowledge, more, capture
        self.pending, self.protected = pending, protected
        self.pending_protection, self.retired = pending_protection, retired
        self.phase, self.progress, self.retention_days = phase, progress, retention_days
        self.diagnostic_days, self.diagnostic_fraction = diagnostic_days, diagnostic_fraction

    def run(self, now, *, required_bytes=0, protected_artifacts=()):
        phase = self.settings.get_setting(self.phase_key) or 'inventory'
        if phase not in {'inventory', 'checkpoint', 'retention'}:
            raise ValueError('Unknown listening evidence maintenance phase: ' + phase)
        self.phase(phase)
        if phase == 'inventory':
            while self.inventory():
                check_cancellable_work()
            self.settings.set_setting(self.phase_key, 'checkpoint')
            phase = 'checkpoint'
            self.phase(phase)
        if phase == 'checkpoint':
            while True:
                self.acknowledge(now)
                if not self.more():
                    break
                check_cancellable_work()
            self.settings.set_setting(self.phase_key, 'retention')
        protected, opportunities = self.pending()
        protected, opportunities = set(protected) | set(protected_artifacts), set(opportunities)
        # The context ends before ledger collection takes its exclusive file fence.
        with self.capture() as inputs:
            artifacts, offers, runs = inputs.capture()
        protected.update(artifacts)
        opportunities.update(offers)
        self.phase('retention')
        progress = {}
        self.progress(progress)
        result = self.ledger.maintain(now - self.retention_days() * 86400,
            protected_artifacts=protected, protected_runs=runs, protected_opportunities=opportunities,
            orphan_before=now - 600, pressure_before=now - 3600, compacted_contexts=None,
            context_before=now - self.diagnostic_days * 86400,
            context_byte_budget=int(self.ledger.byte_budget * self.diagnostic_fraction),
            required_bytes=required_bytes, progress=progress, pending_protection=self.pending_protection,
            retire_references=self.retired)
        if self.more() or self.protected():
            result['deferred'] = True
        return result
