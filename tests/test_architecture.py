"""Keep repository ownership and import direction explicit as the application grows."""
import ast
from collections.abc import Mapping
from functools import cache
import gc
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Static-source and Store checks never start the web sync worker."""


def _source_tree(path):
    return ast.parse(path.read_text(), filename=str(path))


class _ApplicationSources(Mapping):
    """Retain source text, rather than every guard's temporary syntax trees."""
    def __init__(self):
        self.sources = {".".join(path.relative_to(SOURCE).with_suffix("").parts):
                        (path, path.read_text())
                        for path in (SOURCE / "yt_playlist").rglob("*.py")}

    def __getitem__(self, name):
        path, source = self.sources[name]
        return ast.parse(source, filename=str(path))

    def __iter__(self):
        return iter(self.sources)

    def __len__(self):
        return len(self.sources)

    def __contains__(self, name):
        return name in self.sources


@cache
def _modules():
    return _ApplicationSources()


def _import_nodes(tree):
    # Imports are statements. Walking expression operands, names and constants
    # again for this index adds no import edge, even for local imports.
    pending, imports = [tree], []
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        for field in ('body', 'orelse', 'finalbody', 'handlers', 'cases'):
            pending.extend(getattr(node, field, ()))
    return tuple(imports)


def _imports(tree, module=None):
    for node in _import_nodes(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level and module:
                package = module.split(".")[:-node.level]
                base = ".".join([*package, *([base] if base else [])])
            if base:
                yield base
                yield from (f"{base}.{alias.name}" for alias in node.names)


def _flat_repository_references(tree, methods):
    # Known fixture/context names remain useful, but constructor assignments and
    # aliases must also work for reader/writer/repaired and fixture dictionaries.
    stores = {"store", "db", "store2", "ctx.store", "self.store", "self.ctx.store"}
    constructors = {"Store", "yt_playlist.core.store.Store"}
    forwarded_methods = methods - {"get"}
    for node in _import_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "yt_playlist.core.store":
            constructors.update(a.asname or a.name for a in node.names if a.name == "Store")

    def reference(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        return '.'.join([node.id, *reversed(parts)])

    def is_store(node, stores):
        return ((isinstance(node, (ast.Name, ast.Attribute)) and reference(node) in stores)
                or (isinstance(node, ast.Call) and reference(node.func) in constructors)
                or (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "store"))

    def scope_references(scope, inherited):
        assignments, attributes, patches, nested = [], [], [], []
        pending = list(ast.iter_child_nodes(scope))
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nested.append(node)
            else:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    assignments.append(node)
                elif isinstance(node, ast.Attribute) and node.attr in forwarded_methods:
                    attributes.append(node)
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr == 'setattr'):
                    patches.append(node)
                # Names/constants cannot contain a constructor assignment,
                # access or patch. Attribute contexts are also terminal; only
                # its value can contain another relevant expression.
                if isinstance(node, ast.Attribute):
                    pending.append(node.value)
                elif not isinstance(node, (ast.Name, ast.Constant, ast.alias)):
                    pending.extend(ast.iter_child_nodes(node))
        # Resolve the alias graph once. Repeatedly rescanning all assignments
        # makes reversed chains quadratic, although every edge is immutable.
        aliases, edges = set(inherited), {}
        for node in assignments:
            if node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            targets = {reference(t) for t in targets if isinstance(t, (ast.Name, ast.Attribute))}
            if is_store(node.value, aliases):
                aliases.update(targets)
            source = reference(node.value)
            if source is not None:
                edges.setdefault(source, set()).update(targets)
        pending = list(aliases)
        while pending:
            for target in edges.get(pending.pop(), ()):
                if target not in aliases:
                    aliases.add(target)
                    pending.append(target)
        for node in attributes:
            if is_store(node.value, aliases):
                yield node.lineno, ast.unparse(node)
        for node in patches:
            if (len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value in methods):
                target = node.args[0]
                if (isinstance(target, ast.Call) and isinstance(target.func, ast.Name)
                        and target.func.id == "type" and len(target.args) == 1):
                    target = target.args[0]
                if is_store(target, aliases):
                    yield node.lineno, "patch repository directly"
        return nested, aliases

    scopes = [(tree, stores)]
    while scopes:
        scope, inherited = scopes.pop()
        nested, aliases = yield from scope_references(scope, inherited)
        scopes.extend((child, aliases) for child in reversed(nested))


def test_repository_guard_finds_store_aliases_and_fixture_lookups():
    tree = ast.parse('''
from yt_playlist.core.store import Store as CatalogStore
reader = CatalogStore("data.db")
writer = reader
repaired = writer
reader.content_generation()
writer.content_generation()
repaired.queue_remaining()
songs["store"].get_playlist(1)
monkeypatch.setattr(type(writer), "generated_track_keys", lambda: set())
reader.rec_model.content_generation()
''')
    methods = {"content_generation", "queue_remaining", "get_playlist", "generated_track_keys"}
    assert {reference for _, reference in _flat_repository_references(tree, methods)} == {
        "reader.content_generation", "writer.content_generation", "repaired.queue_remaining",
        "songs['store'].get_playlist", "patch repository directly",
    }


def test_repository_guard_distinguishes_short_store_aliases_from_domain_state():
    for source in ('s = Store(":memory:")', 's = store'):
        tree = ast.parse(source + '\ns.pending()')
        assert list(_flat_repository_references(tree, {"pending"})) == [(2, "s.pending")]
    tree = ast.parse('s = Session()\nqueue = s.pending')
    assert not list(_flat_repository_references(tree, {"pending"}))
    tree = ast.parse('''
def fixture(store):
    s = store
    s.pending()
def socket_helper():
    s = socket.socket()
    s.close()
def runtime_helper(store):
    reopened = Runtime(store)
    reopened.load()
''')
    assert [(line, reference) for line, reference in _flat_repository_references(
        tree, {'pending', 'close', 'load'})] == [(4, 's.pending')]


@pytest.mark.parametrize('reverse', [False, True], ids=['forward', 'reversed'])
def test_repository_guard_resolves_long_alias_chains_and_cycles(reverse):
    assignments = [f'alias{index + 1} = alias{index}' for index in range(256)]
    if reverse:
        assignments.reverse()
    source = '\n'.join(['alias0 = Store("library.db")', *assignments,
                        'alias0 = alias256', 'alias256.pending()',
                        'unrelated = Session()', 'unrelated.pending()'])
    assert [reference for _, reference in _flat_repository_references(
        ast.parse(source), {'pending'})] == ['alias256.pending']


def test_application_import_graph_is_acyclic_including_local_imports():
    modules = _modules()
    # Frozen upgrades import the runner's error type; the runner lazily loads
    # those upgrades. Collapse these internals into one component so cycles
    # through application policy or persistence still traverse every edge.
    migration_package = 'yt_playlist.core.migrations'

    def component(name):
        return migration_package if name.startswith(migration_package + '.') else name

    graph = {}
    collecting = gc.isenabled()
    try:
        # ASTs have no parent links and are discarded after each module. Avoid
        # repeatedly tracing these temporary trees for cycles while parsing
        # the complete application; reference counting still frees each tree.
        gc.disable()
        for name, tree in modules.items():
            owner = component(name)
            dependencies = {component(dependency) for dependency in _imports(tree, name)
                            if dependency in modules}
            if owner == migration_package:
                dependencies.discard(owner)
            graph.setdefault(owner, set()).update(dependencies)
    finally:
        if collecting:
            gc.enable()
    finished, path = set(), []

    def visit(name):
        assert name not in path, "Import cycle: " + " / ".join([*path, name])
        if name in finished:
            return
        path.append(name)
        for dependency in sorted(graph[name]):
            visit(dependency)
        path.pop()
        finished.add(name)

    for name in graph:
        visit(name)


@cache
def _repository_methods():
    # This guard needs repository declarations and Store's own API, rather
    # than an eagerly parsed index of every application implementation.
    repositories = [_source_tree(path) for path in (SOURCE / 'yt_playlist' / 'repos').rglob('*.py')]
    store = _source_tree(SOURCE / 'yt_playlist' / 'core' / 'store.py')
    methods = {node.name for tree in repositories
               for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name.endswith('Repo')
               for node in cls.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
               and not any(isinstance(decorator, ast.Name) and decorator.id == 'property'
                           for decorator in node.decorator_list)}
    # Store's own lifetime and connection API is not a forwarded repository
    # method merely because a repository has a method with the same name.
    methods -= {node.name for cls in store.body
                if isinstance(cls, ast.ClassDef) and cls.name == 'Store'
                for node in cls.body if isinstance(node, ast.FunctionDef)}
    methods -= {target.attr for node in ast.walk(store)
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == 'self'}
    return methods


def _caller_batches():
    paths = sorted([*(SOURCE / 'yt_playlist').rglob('*.py'), *(ROOT / 'tests').rglob('*.py')])
    return [paths[offset:offset + 16] for offset in range(0, len(paths), 16)]


@pytest.mark.parametrize('paths', _caller_batches(), ids=lambda paths: str(paths[0].relative_to(ROOT)))
def test_callers_name_repository_owners_and_retired_modules_stay_retired(paths):
    methods = _repository_methods()
    retired = {"yt_playlist.repos.rec", "yt_playlist.rec.rec_dao", "yt_playlist.rec.recommend"}
    violations = []
    for path in paths:
        # Each caller owns its tree only for this check. Keeping thousands of
        # caller ASTs alive adds memory and garbage-collector scans without reuse.
        tree = ast.parse(path.read_text(), filename=str(path))
        for dependency in _imports(tree):
            if dependency in retired:
                violations.append(f"{path.relative_to(ROOT)} imports {dependency}")
        violations.extend(f"{path.relative_to(ROOT)}:{line}: {reference}"
                          for line, reference in _flat_repository_references(tree, methods))
    assert not violations, "\n".join(violations)


def test_persistence_and_playlist_primitives_do_not_import_their_consumers():
    modules = _modules()
    playlist_primitives = {"yt_playlist.library.playlist_metadata", "yt_playlist.library.playlist_writes",
                          "yt_playlist.library.playlist_mutation", "yt_playlist.library.sync"}
    for name in modules:
        if not (name.startswith('yt_playlist.repos.') or name in playlist_primitives
                or name == 'yt_playlist.rec.ordering'):
            continue
        tree = modules[name]
        imports = set(_imports(tree, name))
        if name.startswith("yt_playlist.repos."):
            assert not _persistence_consumers(tree, name), name
        if name in playlist_primitives:
            assert "yt_playlist.library.executor" not in imports, name
        if name == "yt_playlist.rec.ordering":
            assert not imports & {"yt_playlist.rec.journeys", "yt_playlist.rec.surfaces"}


def _persistence_consumers(tree, module):
    forbidden = ("yt_playlist.core.store", "yt_playlist.rec", "yt_playlist.web")
    return {dependency for dependency in _imports(tree, module)
            if any(dependency == prefix or dependency.startswith(prefix + ".") for prefix in forbidden)}


def test_persistence_guard_catches_absolute_local_and_relative_policy_imports():
    for source in (
        'from yt_playlist.rec.progressive_features import recent',
        'import yt_playlist.rec as recommendation_policy',
        'def recent(self):\n    from yt_playlist.rec import progressive_features',
        'from ..rec.progressive_features import recent',
        'from .. import rec',
        'from ..core.store import Store',
        'from ..web import app',
        'if enabled:\n    pass\nelse:\n    from yt_playlist.rec import surfaces',
        'try:\n    pass\nexcept Exception:\n    import yt_playlist.web\nfinally:\n    pass',
        'try:\n    pass\nexcept* Exception:\n    from .. import rec',
        'try:\n    pass\nexcept Exception:\n    pass\nelse:\n    from ..web import app',
        'try:\n    pass\nfinally:\n    from ..core.store import Store',
        'match value:\n    case 1:\n        from .. import rec',
        'async def lookup():\n    async with owner:\n        from ..web import app',
    ):
        assert _persistence_consumers(ast.parse(source), "yt_playlist.repos.rec_model"), source
    assert not _persistence_consumers(ast.parse(
        'from ..core.recommendation_settings import LANES\nfrom .models import Playlist'),
        "yt_playlist.repos.rec_model")


def test_runtime_does_not_import_frozen_numbered_migrations():
    for name, tree in _modules().items():
        if name.startswith("yt_playlist.core.migrations."):
            continue
        assert not any(re.match(r"yt_playlist\.core\.migrations\.v\d", dependency)
                       for dependency in _imports(tree, name)), name


def test_prepared_recommendation_policy_contains_no_connection_access_or_sql():
    for name, tree in _modules().items():
        if not (name.startswith("yt_playlist.rec.progressive")
                or name in {"yt_playlist.rec.candidate_refresh", "yt_playlist.rec.learning_publication",
                            "yt_playlist.rec.model_refresh", "yt_playlist.rec.home_prepared"}):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"conn", "execute", "executemany", "executescript", "commit", "rollback"}, name
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.match(r"\s*(SELECT |INSERT |UPDATE |DELETE |REPLACE )", node.value), name


def test_embedding_calculations_stay_detached_from_storage_and_serving():
    modules = _modules()
    forbidden = {"yt_playlist.rec.embedding_vectors", "yt_playlist.rec.model_refresh",
                 "yt_playlist.rec.rec_worker", "yt_playlist.rec.embed"}
    for owner in ("embedding_training", "content_model", "vector_math"):
        name = f"yt_playlist.rec.{owner}"
        tree = modules[name]
        imports = set(_imports(tree, name))
        assert not imports & forbidden, name
        assert not any(dependency.startswith(("yt_playlist.repos", "yt_playlist.web",
                                              "yt_playlist.core.store")) for dependency in imports), name
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                assert node.arg not in {"store", "reader", "session"}, name
    graph = modules["yt_playlist.rec.embed"]
    assert not set(_imports(graph)) & {"yt_playlist.rec.embedding_training",
                                      "yt_playlist.rec.content_model"}
    expansion = next(node for node in graph.body
                     if isinstance(node, ast.FunctionDef) and node.name == "expand_spaces")
    assert not any(isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                   and node.value.id in {"vectors", "rec_params"} for node in ast.walk(expansion))


def test_refresh_notifications_do_not_write_model_or_serving_state():
    modules = _modules()
    for name in ("yt_playlist.repos.models", "yt_playlist.core.recommendation_settings"):
        assert not _persistence_consumers(modules[name], name), name
    ledger = modules["yt_playlist.repos.rec_refresh"]
    owned = {"rec_change_queue"}
    for node in ast.walk(ledger):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = re.match(r"\s*(?:INSERT (?:OR IGNORE )?INTO|DELETE FROM|UPDATE) (\w+)", node.value)
            if match:
                assert match[1] in owned, match[1]


def test_model_publication_keeps_settings_and_modes_in_their_repositories():
    tree = _modules()['yt_playlist.repos.embeddings']
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == 'publish')
    for node in ast.walk(method):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {'_store', 'settings', 'modes'}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not re.search(r'\b(?:INTO|FROM|UPDATE) (?:settings|taste_modes)\b', node.value)


def test_store_exposes_only_named_repositories_and_preserves_them_on_reinit(store):
    settings = store.settings
    settings.set_setting("architecture-check", "saved")
    store.init_schema()
    assert store.settings is settings
    assert store.settings.get_setting("architecture-check") == "saved"
    assert not hasattr(store, "get_setting")
    assert not hasattr(store, "rec")
    assert "__getattr__" not in type(store).__dict__


def _unguarded_connection_access(method):
    if {ast.unparse(node) for node in method.decorator_list} & {
            "synchronized", "snapshot_read", "lookup_read"}:
        return False

    def unguarded(node, locked=False):
        if isinstance(node, ast.FunctionDef):
            previous = None
            for statement in node.body:
                admitted = (isinstance(previous, ast.If) and not previous.orelse
                    and ast.unparse(previous.test) == 'not self._lock.acquire(blocking=False)'
                    and len(previous.body) == 1 and isinstance(previous.body[0], ast.Return)
                    and isinstance(statement, ast.Try)
                    and len(statement.finalbody) == 1
                    and ast.unparse(statement.finalbody[0]) == 'self._lock.release()')
                if unguarded(statement, locked or admitted):
                    return True
                previous = statement
            return False
        if isinstance(node, ast.With):
            # A lock elsewhere in the method does not protect SQL outside its body.
            for item in node.items:
                if unguarded(item.context_expr, locked):
                    return True
                locked = locked or ast.unparse(item.context_expr) == "self._lock" or (
                    isinstance(item.context_expr, ast.Call)
                    and ast.unparse(item.context_expr.func) in {'atomic_write', 'reserved_write'}
                    and len(item.context_expr.args) == 1
                    and ast.unparse(item.context_expr.args[0]) == 'self._store')
            return any(unguarded(statement, locked) for statement in node.body)
        if isinstance(node, ast.Attribute) and ast.unparse(node) == "self.conn":
            return not locked
        return any(unguarded(child, locked) for child in ast.iter_child_nodes(node))

    return unguarded(method)


def test_connection_guard_recognizes_only_known_repository_decorators():
    for decorator in ('synchronized', 'snapshot_read', 'lookup_read'):
        method = ast.parse(f'@{decorator}\ndef query(self):\n    self.conn.execute("SELECT 1")').body[0]
        assert not _unguarded_connection_access(method), decorator
    for decorator in ('wraps', 'unrelated_read', 'lookup_read()'):
        method = ast.parse(f'@{decorator}\ndef query(self):\n    self.conn.execute("SELECT 1")').body[0]
        assert _unguarded_connection_access(method), decorator


def test_connection_guard_recognizes_explicit_lock_scope():
    protected = ast.parse('''
@contextmanager
def reconciled(self):
    with self._lock:
        self.conn.execute("SAVEPOINT setup_identities")
        try:
            yield self.conn.execute("SELECT * FROM accounts").fetchall()
        finally:
            self.conn.execute("RELEASE setup_identities")
''').body[0]
    assert not _unguarded_connection_access(protected)
    combined = ast.parse('''
@contextmanager
def transaction(self):
    with self._lock, self.conn.atomic():
        yield
''').body[0]
    assert not _unguarded_connection_access(combined)
    for guard in ('atomic_write', 'reserved_write'):
        method = ast.parse(f'def query(self):\n    with {guard}(self._store, wait_seconds=0):\n        self.conn.execute("SELECT 1")').body[0]
        assert not _unguarded_connection_access(method), guard
    nonblocking = ast.parse('''
def query(self):
    if not self._lock.acquire(blocking=False):
        return []
    try:
        return self.conn.execute("SELECT 1").fetchall()
    finally:
        self._lock.release()
''').body[0]
    assert not _unguarded_connection_access(nonblocking)
    for source in (
        'def query(self):\n    self.conn.execute("SELECT 1")',
        'def query(self):\n    with self._lock:\n        pass\n    self.conn.execute("SELECT 1")',
        'def query(self):\n    self.conn.execute("SELECT 1")\n    with self._lock:\n        pass',
        'def query(self):\n    with self.other_lock:\n        self.conn.execute("SELECT 1")',
        'def query(self):\n    with self.conn.atomic(), self._lock:\n        yield',
        'def query(self):\n    with atomic_write(other):\n        self.conn.execute("SELECT 1")',
        'def query(self):\n    with unrelated_write(self._store):\n        self.conn.execute("SELECT 1")',
        'def query(self):\n    if not self._lock.acquire(blocking=False):\n        pass\n    self.conn.execute("SELECT 1")',
    ):
        assert _unguarded_connection_access(ast.parse(source).body[0]), source


def test_public_repository_sql_methods_use_a_connection_guard():
    violations = []
    for name, tree in _modules().items():
        if not name.startswith("yt_playlist.repos."):
            continue
        for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
            for method in [node for node in cls.body if isinstance(node, ast.FunctionDef)]:
                if method.name.startswith("_"):
                    continue
                if _unguarded_connection_access(method):
                    violations.append(f"{name}.{cls.name}.{method.name}")
    assert not violations, "Unguarded shared connection access: " + ", ".join(violations)


def test_recommendation_services_keep_http_and_acquisition_out_of_policy():
    modules = _modules()
    for name in ("yt_playlist.rec.home_cards", "yt_playlist.rec.road_trip",
                 "yt_playlist.rec.road_trip_candidates", "yt_playlist.rec.road_trip_mix"):
        imports = set(_imports(modules[name]))
        assert not any(i.startswith(("yt_playlist.web", "fastapi", "starlette")) for i in imports), name
        if name.endswith("road_trip_mix"):
            assert not any(i.startswith(("yt_playlist.providers", "yt_playlist.repos",
                                         "yt_playlist.core", "yt_playlist.library")) for i in imports), name
            assert not imports & {"yt_playlist.rec.road_trip", "yt_playlist.rec.road_trip_candidates"}
        if name.endswith("road_trip_candidates"):
            assert not imports & {"yt_playlist.rec.road_trip", "yt_playlist.rec.road_trip_mix"}


def test_cluster_presentation_consumes_detached_facts_without_database_access():
    modules = _modules()
    for name in ('yt_playlist.rec.cluster_explain', 'yt_playlist.web.cluster_projection'):
        for node in ast.walk(modules[name]):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {'conn', 'execute', 'store', '_store'}, name
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.match(r'\s*(SELECT |INSERT |UPDATE |DELETE )', node.value), name
    repository = modules['yt_playlist.repos.rec_query']
    names = {node.name for node in ast.walk(repository) if isinstance(node, ast.FunctionDef)}
    assert not names & {'connection_facts', 'cluster_search', '_join_names', '_fam_label'}
