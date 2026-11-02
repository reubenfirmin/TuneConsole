"""SettingsRepo: small key/value app settings (e.g. the Last.fm API key)."""
from yt_playlist.repos.base import Repo, synchronized, lookup_read
from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.core import (album_tuning_state, content_preparation_state, experiment_decision_state,
                              group_preparation_state, model_selection_heads, model_state,
                              ranking_checkpoints, spotify_job_state, sync_state, recommendation_setting_outputs, taste_control_state, workflow_settings)


class SettingsRepo(Repo):
    def _publish_owned(self,values):
        workflows = {key:value for key,value in values.items() if workflow_settings.owns(key)} if workflow_settings.available(self.conn) else {}
        if workflows:
            workflow_settings.put(self.conn,workflows)
            values = {key:value for key,value in values.items() if key not in workflows}
        for owner in (recommendation_setting_outputs,album_tuning_state,model_selection_heads,ranking_checkpoints,taste_control_state,experiment_decision_state,spotify_job_state):
            selected = {key:value for key,value in values.items() if key in owner.KEYS} if owner.available(self.conn) else {}
            if selected:
                owner.put(self.conn,selected)
                values = {key:value for key,value in values.items() if key not in selected}
        checkpoints = {key:value for key,value in values.items() if key in content_preparation_state.KEYS} if content_preparation_state.available(self.conn) else {}
        if checkpoints:
            content_preparation_state.put(self.conn,checkpoints)
            values = {key:value for key,value in values.items() if key not in checkpoints}
        jobs = {key:value for key,value in values.items() if key in group_preparation_state.KEYS} if group_preparation_state.available(self.conn) else {}
        if jobs:
            group_preparation_state.put(self.conn,jobs)
            values = {key:value for key,value in values.items() if key not in jobs}
        owned = {key:value for key,value in values.items() if key in model_state.FIELDS} if model_state.available(self.conn) else {}
        if owned:
            model_state.put(self.conn,owned)
        return {key:value for key,value in values.items() if key not in owned}

    @lookup_read
    def get_settings_with_prefix(self, prefix):
        """Read a settings namespace, treating SQL pattern characters literally."""
        own_snapshot = not self.conn.in_transaction
        if own_snapshot:
            self.conn.execute('BEGIN')
        try:
            return self._settings_with_prefix(prefix)
        finally:
            if own_snapshot:
                self.conn.rollback()

    def _settings_with_prefix(self, prefix):
        from yt_playlist.core.preparation import workflow_publication
        selections = [(owner, stage) for owner in workflow_publication.OWNERS.values()
                      if (stage := owner.selected(self.conn)) is not None]
        pattern = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        exclude = (' AND NOT ('+' OR '.join(owner.job._filter('settings') for owner, _ in selections)+')'
                   if selections else '')
        values = dict(self.conn.execute(
            "SELECT key,value FROM settings WHERE key LIKE ? ESCAPE '!'"+exclude, (pattern,)))
        if workflow_settings.available(self.conn):
            values.update(workflow_settings.settings_with_prefix(self.conn,prefix))
        else:
            values.update(workflow_publication.values_with_prefix(self.conn,prefix,selections))
        return values

    @synchronized
    def set_values(self, values):
        """Save related settings together, so workers cannot observe half a form."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            values = self._publish_owned(values)
            self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                [(key, value or '') for key, value in values.items()])

    @synchronized
    def publish_values(self, values):
        """Publish serialized settings in the caller's atomic transaction."""
        if not self.conn.in_transaction:
            raise RuntimeError('Prepared settings require an outer transaction')
        values = self._publish_owned(values)
        self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value',
            values.items())

    @lookup_read
    def get_setting(self, key, default=None):
        with setting_reads(self._store, (key,)):
            return self._get_setting(key, default)

    def _get_setting(self, key, default):
        if workflow_settings.owns(key):
            from yt_playlist.core.preparation.workflow_publication import owner_for
            owner = owner_for(key)
            revision = owner.selected(self.conn)
            if revision is not None:
                return owner.value(self.conn,revision,key,default)
        if key == spotify_job_state.KEY:
            from yt_playlist.core.preparation.import_publication import OWNER
            revision = OWNER.selected(self.conn)
            if revision is not None:
                return OWNER.value(self.conn,revision,default)
        if key in (*taste_control_state.KEYS, *experiment_decision_state.KEYS):
            from yt_playlist.core.preparation import taste_control_publication
            owner = taste_control_publication.SOURCES[key]
            revision = owner.selected(self.conn)
            if revision is not None:
                return owner.value(self.conn,revision,default)
        if key == sync_state.KEY or key in sync_state.STAMPS:
            from yt_playlist.core.preparation.library_sync_publication import RESULT, STAMPS
            owner = RESULT if key == sync_state.KEY else STAMPS
            revision = owner.selected(self.conn)
            if revision is not None:
                if key == sync_state.KEY:
                    return RESULT.value(self.conn,revision,default)
                return STAMPS.value(self.conn,key,revision,default)
        if key in ('listening_playback_bindings', 'listening_album_pending'):
            from yt_playlist.core.preparation import playback_publication
            owner = playback_publication.SOURCES[key]
            revision = owner.selected(self.conn)
            if revision is not None:
                return owner.value(self.conn,revision,default)
        if key in ('album_feedback', 'auth_expired_identities'):
            from yt_playlist.core.preparation import collection_feedback_publication
            owner = collection_feedback_publication.SOURCES[key]
            revision = owner.selected(self.conn)
            if revision is not None:
                return owner.value(self.conn,revision,default)
        from yt_playlist.core import radio_provenance
        if key in radio_provenance.KEYS:
            from yt_playlist.core import radio_reads
            revision = radio_reads.origin_selected(self.conn)
            if revision is not None:
                return radio_reads.origin_value(self.conn,key,revision,default)
        if key in recommendation_setting_outputs.KEYS:
            from yt_playlist.core.preparation import saved_output_publication
            revision = saved_output_publication.selected(self.conn,key)
            if revision is not None:
                return saved_output_publication.value(self.conn,key,revision,default)
        if key == 'radio_current_session':
            from yt_playlist.core import radio_reads
            revision = radio_reads.session_selected(self.conn)
            if revision is not None:
                return radio_reads.session_value(self.conn,revision,default)
        if workflow_settings.owns(key) and workflow_settings.available(self.conn):
            return workflow_settings.get(self.conn,key,default)
        for owner in (recommendation_setting_outputs,album_tuning_state,model_selection_heads,ranking_checkpoints,taste_control_state,experiment_decision_state,spotify_job_state):
            if key in owner.KEYS and owner.available(self.conn):
                return owner.get(self.conn,key,default)
        if key in content_preparation_state.KEYS and content_preparation_state.available(self.conn):
            return content_preparation_state.get(self.conn,key,default)
        if key in group_preparation_state.KEYS and group_preparation_state.available(self.conn):
            return group_preparation_state.get(self.conn,key,default)
        if key in model_state.FIELDS and model_state.available(self.conn):
            return model_state.get(self.conn,key,default)
        if key in sync_state.STAMPS and sync_state.available(self.conn):
            return sync_state.stamp(self.conn,key,default)
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    @synchronized
    def set_setting(self, key, value) -> None:
        if workflow_settings.owns(key) and workflow_settings.available(self.conn):
            with self.conn:
                workflow_settings.put(self.conn,{key:value})
            return
        for owner in (recommendation_setting_outputs,album_tuning_state,model_selection_heads,ranking_checkpoints,taste_control_state,experiment_decision_state,spotify_job_state):
            if key in owner.KEYS and owner.available(self.conn):
                with self.conn:
                    if not self.conn.in_transaction:
                        self.conn.execute('BEGIN IMMEDIATE')
                    owner.put(self.conn,{key:value})
                return
        if key in content_preparation_state.KEYS and content_preparation_state.available(self.conn):
            with self.conn:
                content_preparation_state.put(self.conn,{key:value})
            return
        if key in group_preparation_state.KEYS and group_preparation_state.available(self.conn):
            with self.conn:
                group_preparation_state.put(self.conn,{key:value})
            return
        if key in sync_state.STAMPS and sync_state.available(self.conn):
            with self.conn:
                sync_state.set_stamp(self.conn,key,value)
            return
        if key in model_state.FIELDS and model_state.available(self.conn):
            with self.conn:
                model_state.put(self.conn,{key:value})
            return
        self.conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) "
                          "DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value",
                          (key, value or ""))
        self.conn.commit()

    @synchronized
    def delete_setting(self, key) -> None:
        if workflow_settings.owns(key) and workflow_settings.available(self.conn):
            with self.conn:
                workflow_settings.put(self.conn,{key:None})
            return
        for owner in (recommendation_setting_outputs,album_tuning_state,model_selection_heads,ranking_checkpoints,taste_control_state,experiment_decision_state,spotify_job_state):
            if key in owner.KEYS and owner.available(self.conn):
                with self.conn:
                    if not self.conn.in_transaction:
                        self.conn.execute('BEGIN IMMEDIATE')
                    owner.put(self.conn,{key:None})
                return
        if key in content_preparation_state.KEYS and content_preparation_state.available(self.conn):
            with self.conn:
                content_preparation_state.put(self.conn,{key:None})
            return
        if key in group_preparation_state.KEYS and group_preparation_state.available(self.conn):
            with self.conn:
                group_preparation_state.put(self.conn,{key:None})
            return
        if key in sync_state.STAMPS and sync_state.available(self.conn):
            with self.conn:
                sync_state.set_stamp(self.conn,key,None)
            return
        if key in model_state.FIELDS and model_state.available(self.conn):
            with self.conn:
                model_state.clear(self.conn,key)
            return
        self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        self.conn.commit()

    @lookup_read
    def spotify_import_job(self):
        if spotify_job_state.available(self.conn):
            return spotify_job_state.load(self.conn,token=True)
        import json
        from yt_playlist.core import entity_names
        return entity_names.account_document(json.loads(self.get_setting(spotify_job_state.KEY) or 'null'))

    @synchronized
    def save_spotify_import_job(self,job):
        if spotify_job_state.available(self.conn):
            with self.conn:
                return spotify_job_state.save(self.conn,job)
        import json
        from yt_playlist.core import entity_names
        self.set_setting(spotify_job_state.KEY,json.dumps(
            entity_names.external_account_document(entity_names.account_document(job)),sort_keys=True))

    @lookup_read
    def spotify_transfer_receipts(self,account_id):
        return workflow_settings.receipts(self.conn,'playlist',account_id,token=True)

    @synchronized
    def save_spotify_transfer(self,account_id,fingerprint,value):
        with self.conn:
            return workflow_settings.receipt_save(self.conn,'playlist',account_id,fingerprint,value,
                expected_revision=value.get(workflow_settings.TOKEN,''))
