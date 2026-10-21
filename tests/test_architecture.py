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
    store_names = {"store", "s", "db", "reopened", "store2", "ctx.store", "self.store", "self.ctx.store"}
    violations = []
    files = [*(SOURCE / "yt_playlist").rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
    for path in files:
        tree = ast.parse(path.read_text())
        for dependency in _imports(tree):
            if dependency in retired:
                violations.append(f"{path.relative_to(ROOT)} imports {dependency}")
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr in methods - {"get"}
                    and ast.unparse(node.value) in store_names):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {ast.unparse(node)}")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"setattr", "object"} and len(node.args) > 1
                    and ast.unparse(node.args[0]) in store_names
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value in methods):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: patch repository directly")
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
