import ast
from pathlib import Path


def test_backend_runtime_facades_are_not_imported_at_module_top_level():
    backend_dir = Path(__file__).resolve().parents[1] / "bias_ext_search" / "backend"
    offenders = []

    for source_path in sorted(backend_dir.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level or node.module != "bias_core.extensions.runtime":
                continue
            imported_names = ", ".join(alias.name for alias in node.names)
            offenders.append(f"{source_path.relative_to(backend_dir)}: {imported_names}")

    assert offenders == []
