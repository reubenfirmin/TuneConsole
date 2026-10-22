"""Keep repository ownership and import direction explicit as the application grows."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


def _modules():
    return {".".join(p.relative_to(SOURCE).with_suffix("").parts): ast.parse(p.read_text())
            for p in (SOURCE / "yt_playlist").rglob("*.py")}


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def _flat_repository_references(tree, methods):
    # Known fixture/context names remain useful, but constructor assignments and
    # aliases must also work for reader/writer/repaired and fixture dictionaries.
    stores = {"store", "s", "db", "reopened", "store2", "ctx.store", "self.store", "self.ctx.store"}
    constructors = {"Store", "yt_playlist.core.store.Store"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "yt_playlist.core.store":
            constructors.update(a.asname or a.name for a in node.names if a.name == "Store")

    def is_store(node):
        return (ast.unparse(node) in stores
                or (isinstance(node, ast.Call) and ast.unparse(node.func) in constructors)
                or (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                    and node.slice.value == "store"))

    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    while True:
        before = len(stores)
        for node in assignments:
            if node.value is not None and is_store(node.value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                stores.update(ast.unparse(t) for t in targets if isinstance(t, (ast.Name, ast.Attribute)))
        if len(stores) == before:
            break

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in methods - {"get"} and is_store(node.value):
            yield node.lineno, ast.unparse(node)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setattr" and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in methods):
            target = node.args[0]
            if (isinstance(target, ast.Call) and isinstance(target.func, ast.Name)
                    and target.func.id == "type" and len(target.args) == 1):
                target = target.args[0]
            if is_store(target):
                yield node.lineno, "patch repository directly"


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


def test_application_import_graph_is_acyclic_including_local_imports():
    modules = _modules()
    graph = {name: set(_imports(tree)) & modules.keys() for name, tree in modules.items()}
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


def test_callers_name_repository_owners_and_retired_modules_stay_retired():
    modules = _modules()
    methods = {node.name for name, tree in modules.items() if name.startswith("yt_playlist.repos.")
               for cls in tree.body if isinstance(cls, ast.ClassDef)
               for node in cls.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")}
    retired = {"yt_playlist.repos.rec", "yt_playlist.rec.rec_dao", "yt_playlist.rec.recommend"}
    violations = []
    files = [*(SOURCE / "yt_playlist").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
    for path in files:
        tree = ast.parse(path.read_text())
        for dependency in _imports(tree):
            if dependency in retired:
                violations.append(f"{path.relative_to(ROOT)} imports {dependency}")
        violations.extend(f"{path.relative_to(ROOT)}:{line}: {reference}"
                          for line, reference in _flat_repository_references(tree, methods))
    assert not violations, "\n".join(violations)


def test_persistence_and_playlist_primitives_do_not_import_their_consumers():
    modules = _modules()
    for name, tree in modules.items():
        imports = set(_imports(tree))
        if name.startswith("yt_playlist.repos."):
            assert "yt_playlist.core.store" not in imports, name
            assert not any(i.startswith("yt_playlist.web.") for i in imports), name
        if name in {"yt_playlist.library.playlist_metadata", "yt_playlist.library.playlist_writes",
                    "yt_playlist.library.playlist_mutation", "yt_playlist.library.sync"}:
            assert "yt_playlist.library.executor" not in imports, name
        if name == "yt_playlist.rec.ordering":
            assert not imports & {"yt_playlist.rec.journeys", "yt_playlist.rec.surfaces"}


def test_store_exposes_only_named_repositories_and_preserves_them_on_reinit(store):
    settings = store.settings
    settings.set_setting("architecture-check", "saved")
    store.init_schema()
    assert store.settings is settings
    assert store.settings.get_setting("architecture-check") == "saved"
    assert not hasattr(store, "get_setting")
    assert not hasattr(store, "rec")
    assert "__getattr__" not in type(store).__dict__
