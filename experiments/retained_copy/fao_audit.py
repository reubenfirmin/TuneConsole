"""Inventory production file-body operations and their explicit FAO boundaries."""
import ast
import json
from pathlib import Path
import re
from unicodedata import normalize

ROOT = Path(__file__).resolve().parents[2]/'src'/'yt_playlist'
OPERATIONS = {'open','read_text','read_bytes','write_text','write_bytes','unlink'}
CALL_NAMES = OPERATIONS | {'load','dump','replace','rename','copy','copy2','copyfile','rmtree'}
CALL_IDENTIFIER = re.compile(r'\b(?:' + '|'.join(sorted(CALL_NAMES)) + r')\b')

# A boundary names the actual non-asset operation and its narrow owner. Adding
# another raw file operation elsewhere requires an explicit inventory decision.
BOUNDARIES = {
 'offline whole-root custody, with application handles drained': {
  'core/db_rebuild/files.py': ('_move',),
 },
 'native SQLite custody': {
  'core/db_rebuild/build.py': ('_source_identity','construct','build'),
  'core/db_rebuild/install.py': ('digest','sync','_rollback','replace','owner'),
  'core/db_rebuild/job.py': ('run',),
  'core/repair.py': ('_publish_backup',),
 },
 'TOML configuration or credentials': {
  'core/config.py': ('load_accounts',), 'core/features.py': ('audio_vibes_enabled',),
  'core/runtime.py': ('Runtime.load','Runtime.apply_setup'),
  'core/setup.py': ('prepared_config','write_config','restore_config'),
  'providers/credentials.py': ('resolve',),
  'rec/audio_vibe_cruise.py': ('_configured',), 'rec/audio_vibe_model.py': ('configured_path',),
  'rec/audio_vibe_runtime.py': ('configured',),
 },
 'SQL/code definition, never runtime JSON': {
  'core/db_rebuild/destination.py': ('signature',),
  'core/genre_contract.py': ('installed_definitions',),
  # Bundled schema sources, including the frozen installed-format JSON
  # contract. These exact readers never open account-owned runtime assets.
  'core/schema_admission.py': ('reference',),
  'core/destination_schema.py': ('definition',),
  'core/schema_definition.py': ('_statements','_read_statements','assembled_statements'),
  'core/schema_transforms.py': ('saved_output_definitions',),
  'core/preparation/choice_learning.py': ('_contract',),
  'core/preparation/tv_state.py': ('_frozen',),
 },
 'lock or fixed coordination counter': {
  'core/db_handles.py': ('lease',),
  'core/evidence_ledger.py': ('EvidenceLedger._budget','EvidenceLedger.generation',
      'EvidenceLedger._advance_generation','EvidenceLedger._maintain'),
  'core/playback_inbox.py': ('Inbox.admission',),
  'core/album_feedback_outbox.py': ('fence',),
  'library/action_claim.py': ('_claim',),
  'rec/group_roots.py': ('_pending','_pending.close','reap_pending'),
 },
 'temporary SQLite recovery workspace and human report': {
  'core/store.py': ('Store.__init__',),
 },
 'repository-owned SQL reader lifetime, no raw file body': {
  'rec/stored_groups.py': ('selection_reader',),
  'rec/stored_home.py': ('preparation_inputs','_assemble'),
 },
 'external network, zip, package metadata, OS or developer log': {
  'core/updatecheck.py': ('_is_editable_install',),
  'core/work_diagnostics.py': ('CpuDiagnostics._native_cpu',),
  'egress.py': ('EgressGuard.recent',),
  'library/spotify_data.py': ('_load',), 'library/takeout.py': ('maybe_unzip','_unzip_html_member'),
  'rec/tv_visual.py': ('_read_image',),
  'web/app.py': ('create_app.lifespan',),
 },
}


def boundary(row):
    path,owner,call = row['path'],row['owner'],row['call']
    if path=='core/fao.py':
        return 'FAO implementation, including leased temporary materialization'
    if row['operation']=='os.open' and owner=='_sync_directory':
        return 'directory fsync'
    if re.fullmatch(r"\(.* / '\.[a-z]+\.lock'\)\.open\('a\+b'\)",call):
        return 'domain reader/publication lock'
    if path.startswith('core/migrations/') and row['operation'].endswith('.read_text'):
        return 'frozen SQL schema source'
    if path.startswith('integrations/') and call=='json.load(response)':
        return 'external network JSON response'
    for reason,files in BOUNDARIES.items():
        if owner in files.get(path,()):
            return reason
    return None


def audit(root=ROOT, *, paths=None):
    return [{**row,'boundary':boundary(row)} for row in inventory(root, paths=paths)]


def inventory(root=ROOT, *, paths=None):
    results = []
    for path in sorted(root.rglob('*.py') if paths is None else paths):
        source = path.read_text()
        # Any candidate's terminal identifier must occur in its source. Python
        # normalizes identifiers, so normalize before this conservative filter.
        # Comments and strings may keep an unnecessary parse, never hide a call.
        normalized = normalize('NFKC', source)
        if not CALL_IDENTIFIER.search(normalized):
            continue
        tree = ast.parse(source)
        class Visitor(ast.NodeVisitor):
            scopes = []
            def visit_Name(self, node):
                pass  # Identifiers and literals cannot contain nested calls.
            visit_Constant = visit_Name
            def visit_Attribute(self, node):
                self.visit(node.value)
            def visit_FunctionDef(self,node):
                self.scopes.append(node.name)
                self.generic_visit(node)
                self.scopes.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            visit_ClassDef = visit_FunctionDef
            def visit_Call(self,node):
                # Most production calls cannot be file operations. Inspect the
                # actual terminal identifier before unparsing nested receivers.
                function = node.func
                terminal = function.attr if isinstance(function, ast.Attribute) else (
                    function.id if isinstance(function, ast.Name) else None)
                if terminal not in CALL_NAMES:
                    self.generic_visit(node)
                    return
                name = ast.unparse(node.func)
                operation = name.rsplit('.',1)[-1]
                if ((operation in OPERATIONS and not name.startswith('fao.') and name not in
                        ('webbrowser.open','self.open','app.work.open'))
                        or name in ('json.load','json.dump','pickle.load','pickle.dump','joblib.load','joblib.dump','np.load','numpy.load')
                        or name in ('os.replace','os.rename','shutil.copy','shutil.copy2','shutil.copyfile','shutil.rmtree')):
                    results.append({'path':path.relative_to(root).as_posix(),'owner':'.'.join(self.scopes),
                        'line':node.lineno,'operation':name,'call':ast.unparse(node)})
                self.generic_visit(node)
        Visitor().visit(tree)
    return results


if __name__=='__main__':
    rows = audit()
    print(json.dumps(rows,indent=2))
    raise SystemExit(any(row['boundary'] is None for row in rows))
