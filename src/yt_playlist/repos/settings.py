"""SettingsRepo: small key/value app settings (e.g. the Last.fm API key)."""
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.core import (album_tuning_state, content_preparation_state, experiment_decision_state,
                              group_preparation_state, model_selection_heads, model_state,
                              ranking_checkpoints, spotify_job_state, sync_state, recommendation_setting_outputs, taste_control_state, workflow_settings)


class SettingsRepo(Repo):
    @snapshot_read
    def prepared_document(self, key, default=None):
        from yt_playlist.repos.setting_documents import prepared
        with setting_reads(self._store, (key,)):
            return prepared(self.conn, key, default)

    @lookup_read
    def native_models(self):
        from yt_playlist.core.model_reads import native
        return native(self.conn)

    @lookup_read
    def native_import_jobs(self):
        return spotify_job_state.available(self.conn)

    @lookup_read
    def native_taste_controls(self):
        return taste_control_state.available(self.conn)

    @snapshot_read
    def taste_document(self, key):
        return taste_control_state.document(self.conn, key)

    @lookup_read
    def native_decisions(self):
        return experiment_decision_state.available(self.conn)

    @snapshot_read
    def decision_documents(self):
        return experiment_decision_state.documents(self.conn)

    @snapshot_read
    def import_job_document(self):
        row = self.conn.execute('SELECT j.* FROM spotify_import_job_head h '
            'JOIN spotify_import_jobs j ON j.id=h.job_id WHERE h.id=1').fetchone()
        return dict(row) if row else None

    @snapshot_read
    def import_job_facts(self, account):
        return spotify_job_state.capture(self.conn, account)

    @snapshot_read
    def model_document(self, key, default=None):
        from yt_playlist.core import model_reads, checkpoint_reads, model_contract
        with setting_reads(self._store, (key,)):
            owner, column = model_contract.FIELDS[key]
            if model_reads.native(self.conn):
                row = self.conn.execute('SELECT * FROM taste_model_state WHERE id=?', (owner,)).fetchone()
                if row is None:
                    return default, None
                if column != 'model':
                    return row[column] if row[column] is not None else default, None
                if row['recovery_filename'] is None:
                    return default, None
                return None, {'storage': 'collection', 'owner': owner,
                    'reference': tuple(row[field] for field in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))}
            revisions = model_reads.selected(self.conn)
            if revisions is None:
                return checkpoint_reads.fallback(self.conn, key, default), None
            if key in model_contract.FINGERPRINTS:
                return model_reads.read(self.conn, revisions, key, default), None
            row = self.conn.execute('SELECT g.* FROM '+model_reads.LINKS+
                ' l JOIN prepared_content_encoder_generations g USING(source,generation) '
                'WHERE l.artifact_revision=? AND l.fingerprint_revision=? AND l.source=?', (*revisions, key)).fetchone()
            if row is None:
                return default, None
            parts = self.conn.execute('SELECT CAST(position AS TEXT) AS chunk_key,filename,digest AS content_digest,byte_size '
                'FROM prepared_content_encoder_parts WHERE source=? AND generation=? ORDER BY position LIMIT 257',
                (key, row['generation'])).fetchall()
            return None, {**dict(row), 'storage': 'parts', 'parts': [dict(part) for part in parts]}

    @lookup_read
    def saved_outputs_native(self):
        return recommendation_setting_outputs.available(self.conn)

    @snapshot_read
    def output_document(self, key):
        with setting_reads(self._store, (key,)):
            if recommendation_setting_outputs.available(self.conn):
                row = self.conn.execute('SELECT filename,content_digest,byte_size FROM '+
                    recommendation_setting_outputs.TABLES[key]+' WHERE id=1').fetchone()
                return True, {**dict(row), 'prepared': False} if row else None
            from yt_playlist.core.preparation import saved_output_publication as publication
            revision = publication.selected(self.conn, key)
            if revision is None:
                return False, None
            generation = self.conn.execute('SELECT generation FROM '+publication.tables(key)[0]+
                                           ' WHERE revision=?', (revision,)).fetchone()[0]
            if generation is None:
                return True, None
            from yt_playlist.core.preparation.saved_outputs import tables
            row = self.conn.execute('SELECT * FROM '+tables(key)[0]+
                ' WHERE source=? AND preparation_revision=?', (key, generation)).fetchone()
            if row is None:
                from yt_playlist.core.schema_contract import MigrationError
                raise MigrationError('Published saved output file reference is missing')
            return True, {**dict(row), 'prepared': True}

    @synchronized
    def replace_keys(self, keys, values):
        """Replace a captured set of ordinary taste parameter rows."""
        with self.conn.atomic():
            self.conn.executemany('DELETE FROM settings WHERE key=?', [(key,) for key in keys])
            self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', values.items())

    def _publish_owned(self,values,outputs=None,model_references=None,import_job=None,controls=None,decisions=None,workflow_records=None):
        if recommendation_setting_outputs.available(self.conn):
            selected = {key for key in values if key in recommendation_setting_outputs.KEYS}
            if selected:
                if outputs is None or set(outputs[0]) != selected:
                    raise ValueError('Saved settings require prepared output and file references')
                recommendation_setting_outputs.register(self.conn, *outputs)
                values = {key: value for key, value in values.items() if key not in selected}
        workflows = {key:value for key,value in values.items() if workflow_settings.owns(key)} if workflow_settings.available(self.conn) else {}
        if workflows:
            if workflow_records is None:
                raise ValueError('Workflow settings require prepared state and file references')
            from yt_playlist.repos.setting_workflows import register
            register(self.conn, workflow_records)
            values = {key:value for key,value in values.items() if key not in workflows}
        if spotify_job_state.KEY in values and spotify_job_state.available(self.conn):
            if import_job is None:
                raise ValueError('Import jobs require prepared state and a file reference')
            prepared, reference = import_job
            if prepared is None:
                self.conn.execute('DELETE FROM spotify_import_job_head')
                self.conn.execute('DELETE FROM spotify_import_jobs')
            else:
                spotify_job_state.register(self.conn, prepared, reference)
            values = {key: value for key, value in values.items() if key != spotify_job_state.KEY}
        selected = {key for key in values if key in taste_control_state.KEYS} if taste_control_state.available(self.conn) else set()
        if selected:
            if controls is None or set(controls) != selected:
                raise ValueError('Taste controls require prepared state and file references')
            for key, (value, reference) in controls.items():
                taste_control_state.register(self.conn, key, value, reference)
            values = {key: value for key, value in values.items() if key not in selected}
        selected = set(values) & experiment_decision_state.KEYS if experiment_decision_state.available(self.conn) else set()
        if selected:
            if decisions is None:
                raise ValueError('Experiment decisions require prepared state and file references')
            experiment_decision_state.register(self.conn, *decisions)
            values = {key: value for key, value in values.items() if key not in selected}
        for owner in (album_tuning_state,model_selection_heads,ranking_checkpoints):
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
            model_state.register(self.conn, owned, model_references or {})
        return {key:value for key,value in values.items() if key not in owned}

    @lookup_read
    def prefix_documents(self, prefix):
        """Capture a namespace and its selected manifests in one SQL read."""
        from yt_playlist.core.preparation import workflow_publication, workflow_receipts
        from yt_playlist.repos.setting_documents import parts
        selections = [(owner, stage) for owner in workflow_publication.OWNERS.values()
                      if (stage := owner.selected(self.conn)) is not None]
        pattern = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        exclude = (' AND NOT ('+' OR '.join(owner.job._filter('settings') for owner, _ in selections)+')'
                   if selections else '')
        values = dict(self.conn.execute(
            "SELECT key,value FROM settings WHERE key LIKE ? ESCAPE '!'"+exclude, (pattern,)))
        documents = {}
        for owner, stage in selections:
            after = ''
            while True:
                sources = self.conn.execute('SELECT source FROM '+owner.table('map')+
                    ' WHERE stage=? AND substr(source,1,?)=? AND source>? ORDER BY source LIMIT 128',
                    (stage, len(prefix), prefix, after)).fetchall()
                if not sources:
                    break
                for source, in sources:
                    captured = owner.capture(self.conn, stage, source)
                    documents[source] = parts(self.conn, captured, workflow_receipts.table(owner.name, 'parts'),
                        generation='preparation_revision', digest='content_digest', prefix='original:', nullable_part=True, limit=513)
                after = sources[-1][0]
        from yt_playlist.repos.setting_workflows import keys
        native = keys(self.conn, prefix) if workflow_settings.available(self.conn) else []
        return values, documents, native

    @synchronized
    def set_values(self, values, *, outputs=None, model_references=None, import_job=None, controls=None, decisions=None, workflow_records=None):
        """Save related settings together, so workers cannot observe half a form."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            values = self._publish_owned(values, outputs, model_references, import_job, controls, decisions, workflow_records)
            self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                [(key, value or '') for key, value in values.items()])

    @synchronized
    def publish_values(self, values, *, outputs=None, model_references=None, import_job=None, controls=None, decisions=None, workflow_records=None):
        """Persist supplied serialized settings in this repository's write."""
        with self.conn.atomic(join=True):
            values = self._publish_owned(values, outputs, model_references, import_job, controls, decisions, workflow_records)
            self.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value',
                values.items())

    @lookup_read
    def get_setting(self, key, default=None):
        with setting_reads(self._store, (key,)):
            return self._get_setting(key, default)

    def _get_setting(self, key, default):
        if key in sync_state.STAMPS:
            from yt_playlist.core.preparation.library_sync_publication import STAMPS
            revision = STAMPS.selected(self.conn)
            if revision is not None:
                return STAMPS.value(self.conn,key,revision,default)
        for owner in (album_tuning_state,model_selection_heads,ranking_checkpoints):
            if key in owner.KEYS and owner.available(self.conn):
                return owner.get(self.conn,key,default)
        if key in content_preparation_state.KEYS and content_preparation_state.available(self.conn):
            return content_preparation_state.get(self.conn,key,default)
        if key in group_preparation_state.KEYS and group_preparation_state.available(self.conn):
            return group_preparation_state.get(self.conn,key,default)
        if key in sync_state.STAMPS and sync_state.available(self.conn):
            return sync_state.stamp(self.conn,key,default)
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    @synchronized
    def set_setting(self, key, value) -> None:
        for owner in (album_tuning_state,model_selection_heads,ranking_checkpoints):
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
                model_state.register(self.conn, {key: value}, {})
            return
        self.conn.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) "
                          "DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value",
                          (key, value or ""))
        self.conn.commit()

    @synchronized
    def delete_setting(self, key) -> None:
        for owner in (album_tuning_state,model_selection_heads,ranking_checkpoints):
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
                model_state.register(self.conn, {key: None}, {})
            return
        self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        self.conn.commit()

    @synchronized
    def save_spotify_import_job(self, prepared, reference):
        with self.conn.atomic():
            return spotify_job_state.register(self.conn, prepared, reference)

    @lookup_read
    def has_transfer_receipts(self):
        return workflow_settings.available(self.conn)

    @snapshot_read
    def workflow_documents(self, kind, account, target=None):
        from yt_playlist.repos.setting_workflows import documents
        return documents(self.conn, kind, account, target)

    @snapshot_read
    def workflow_write_facts(self, kind, account, target):
        return workflow_settings.write_facts(self.conn, kind, account, target)

    @synchronized
    def save_workflows(self, plans):
        from yt_playlist.repos.setting_workflows import register
        with self.conn.atomic():
            return register(self.conn, plans)
