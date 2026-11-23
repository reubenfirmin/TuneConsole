"""SettingsRepo: small key/value app settings (e.g. the Last.fm API key)."""
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.repos import collection_file_writes, recommendation_file_writes
from yt_playlist.core import (album_tuning_state, content_preparation_state, experiment_decision_state,
                              group_preparation_state, model_selection_heads,
                              ranking_checkpoints, spotify_job_state, sync_state, recommendation_setting_outputs, taste_control_state, workflow_settings)


class SettingsRepo(Repo):
    @synchronized
    def reserve_output_file(self, key, integrity):
        with self.conn.atomic():
            return recommendation_file_writes.reserve(self.conn, recommendation_setting_outputs.OWNERS[key], key, '', integrity)

    @synchronized
    def reserve_file(self, kind, owner, integrity):
        with self.conn.atomic():
            return collection_file_writes.reserve(self.conn, kind, owner, integrity)

    @snapshot_read
    def taste_document(self, key):
        return taste_control_state.document(self.conn, key)

    @snapshot_read
    def import_job_document(self):
        row = spotify_job_state.current(self.conn)
        return dict(row) if row else None

    @snapshot_read
    def import_job_status(self):
        row = spotify_job_state.current(self.conn)
        return {key: row[key] for key in ('status', 'dismissed')} if row else None

    @snapshot_read
    def import_job_facts(self, account):
        return spotify_job_state.capture(self.conn, account)

    @snapshot_read
    def output_document(self, key):
        return recommendation_setting_outputs.reference(self.conn, key)

    @synchronized
    def replace_keys(self, keys, values):
        """Replace a captured set of ordinary taste parameter rows."""
        with self.conn.atomic():
            self.conn.executemany('DELETE FROM settings WHERE key=?', [(key,) for key in keys])
            self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', values.items())

    def _publish_owned(self,values,outputs=None,import_job=None,controls=None,workflow_records=None,file_writes=()):
        for identity, kind, owner, reference in file_writes:
            collection_file_writes.complete(self.conn, identity, kind, owner, reference)
        selected = values.keys() & recommendation_setting_outputs.KEYS
        if selected:
            if outputs is None or set(outputs[0]) != selected:
                raise ValueError('Saved settings require prepared output and file references')
            prepared, durable, identities = outputs
            if set(identities) != set(durable):
                raise ValueError('Saved outputs require their pending SQL identities')
            for key, identity in identities.items():
                recommendation_file_writes.complete(self.conn, identity, recommendation_setting_outputs.OWNERS[key],
                                                     key, '', durable[key])
            recommendation_setting_outputs.register(self.conn, prepared, durable)
            values = {key: value for key, value in values.items() if key not in selected}
        workflows = {key:value for key,value in values.items() if workflow_settings.owns(key)}
        if workflows:
            if workflow_records is None:
                raise ValueError('Workflow settings require prepared state and file references')
            from yt_playlist.repos.setting_workflows import register
            register(self.conn, workflow_records)
            values = {key:value for key,value in values.items() if key not in workflows}
        if spotify_job_state.KEY in values:
            if import_job is None:
                raise ValueError('Import jobs require prepared state and a file reference')
            prepared, reference = import_job
            if prepared is None:
                spotify_job_state.clear(self.conn)
            else:
                spotify_job_state.register(self.conn, prepared, reference)
                collection_file_writes.complete(self.conn, prepared['file_id'], 'spotify-import',
                                                prepared['identity'], reference)
            values = {key: value for key, value in values.items() if key != spotify_job_state.KEY}
        selected = values.keys() & taste_control_state.KEYS
        if selected:
            if controls is None or set(controls) != selected:
                raise ValueError('Taste controls require prepared state and file references')
            for key, (value, reference) in controls.items():
                taste_control_state.register(self.conn, key, value, reference)
            values = {key: value for key, value in values.items() if key not in selected}
        for owner in (album_tuning_state,model_selection_heads,ranking_checkpoints,experiment_decision_state):
            selected = {key:value for key,value in values.items() if key in owner.KEYS}
            if selected:
                owner.put(self.conn,selected)
                values = {key:value for key,value in values.items() if key not in selected}
        checkpoints = {key:value for key,value in values.items() if key in content_preparation_state.KEYS}
        if checkpoints:
            content_preparation_state.put(self.conn,checkpoints)
            values = {key:value for key,value in values.items() if key not in checkpoints}
        jobs = {key:value for key,value in values.items() if key in group_preparation_state.KEYS}
        if jobs:
            group_preparation_state.put(self.conn,jobs)
            values = {key:value for key,value in values.items() if key not in jobs}
        stamps = values.keys() & sync_state.STAMPS.keys()
        for key in stamps:
            sync_state.set_stamp(self.conn, key, values[key])
        if stamps:
            values = {key: value for key, value in values.items() if key not in stamps}
        return values

    @lookup_read
    def prefix_documents(self, prefix):
        """Capture a namespace and its native owners in one SQL read."""
        pattern = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        values = dict(self.conn.execute(
            "SELECT key,value FROM settings WHERE key LIKE ? ESCAPE '!'", (pattern,)))
        from yt_playlist.repos.setting_workflows import keys
        return values, keys(self.conn, prefix)

    @synchronized
    def set_values(self, values, *, outputs=None, import_job=None, controls=None, workflow_records=None, file_writes=()):
        """Save related settings together, so workers cannot observe half a form."""
        with self.conn.atomic():
            values = self._publish_owned(values, outputs, import_job, controls, workflow_records, file_writes)
            self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                [(key, value or '') for key, value in values.items()])

    @synchronized
    def publish_values(self, values, *, outputs=None, import_job=None, controls=None, workflow_records=None, file_writes=()):
        """Persist supplied serialized settings in this repository's write."""
        with self.conn.atomic(join=True):
            values = self._publish_owned(values, outputs, import_job, controls, workflow_records, file_writes)
            self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value',
                values.items())

    @lookup_read
    def get_setting(self, key, default=None):
        with setting_reads(self._store, (key,)):
            return self._get_setting(key, default)

    def _get_setting(self, key, default):
        for owner in (album_tuning_state,model_selection_heads,ranking_checkpoints,experiment_decision_state):
            if key in owner.KEYS:
                return owner.get(self.conn,key,default)
        if key in content_preparation_state.KEYS:
            return content_preparation_state.get(self.conn,key,default)
        if key in group_preparation_state.KEYS:
            return group_preparation_state.get(self.conn,key,default)
        if key in sync_state.STAMPS:
            return sync_state.stamp(self.conn,key,default)
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    @synchronized
    def set_setting(self, key, value) -> None:
        for owner in (album_tuning_state,model_selection_heads,ranking_checkpoints,experiment_decision_state):
            if key in owner.KEYS:
                with self.conn:
                    if not self.conn.in_transaction:
                        self.conn.execute('BEGIN IMMEDIATE')
                    owner.put(self.conn,{key:value})
                return
        if key in content_preparation_state.KEYS:
            with self.conn:
                content_preparation_state.put(self.conn,{key:value})
            return
        if key in group_preparation_state.KEYS:
            with self.conn:
                group_preparation_state.put(self.conn,{key:value})
            return
        if key in sync_state.STAMPS:
            with self.conn:
                sync_state.set_stamp(self.conn,key,value)
            return
        self.conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) "
                          "DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value",
                          (key, value or ""))
        self.conn.commit()

    @synchronized
    def delete_setting(self, key) -> None:
        for owner in (album_tuning_state,model_selection_heads,ranking_checkpoints,experiment_decision_state):
            if key in owner.KEYS:
                with self.conn:
                    if not self.conn.in_transaction:
                        self.conn.execute('BEGIN IMMEDIATE')
                    owner.put(self.conn,{key:None})
                return
        if key in content_preparation_state.KEYS:
            with self.conn:
                content_preparation_state.put(self.conn,{key:None})
            return
        if key in group_preparation_state.KEYS:
            with self.conn:
                group_preparation_state.put(self.conn,{key:None})
            return
        if key in sync_state.STAMPS:
            with self.conn:
                sync_state.set_stamp(self.conn,key,None)
            return
        self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        self.conn.commit()

    @synchronized
    def save_spotify_import_job(self, prepared, reference):
        with self.conn.atomic():
            revision = spotify_job_state.register(self.conn, prepared, reference)
            collection_file_writes.complete(self.conn, prepared['file_id'], 'spotify-import',
                                            prepared['identity'], reference)
            return revision

    @snapshot_read
    def workflow_documents(self, kind, account):
        from yt_playlist.repos.setting_workflows import documents
        return documents(self.conn, kind, account)

    @snapshot_read
    def workflow_write_facts(self, kind, account, target):
        return workflow_settings.write_facts(self.conn, kind, account, target)

    @synchronized
    def save_workflows(self, plans):
        from yt_playlist.repos.setting_workflows import register
        with self.conn.atomic():
            return register(self.conn, plans)
