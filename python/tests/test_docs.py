"""What the docs for AI assistants promise about the repo."""

import ast
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
LLMS = ROOT / "llms.txt"


def test_the_copy_of_llms_txt_in_the_package_matches() -> None:
    assert (ROOT / "src/barchart_desktop_sdk/llms.txt").read_text() == LLMS.read_text()


def test_llms_txt_links_to_files_this_repo_has() -> None:
    links = re.findall(r"barchart/desktop-sdk/main/python/([\w./-]+)", LLMS.read_text())
    assert links
    assert [link for link in links if not (ROOT / link).is_file()] == []


def test_llms_txt_and_the_examples_readme_list_every_example() -> None:
    examples = {path.name for path in (ROOT / "examples").glob("*.py")}
    assert set(re.findall(r"examples/(\w+\.py)", LLMS.read_text())) == examples
    assert set(re.findall(r"\((\w+\.py)\)", (ROOT / "examples/README.md").read_text())) == examples


# ---- every public item has a docstring, and the API reference has a page for it

PACKAGE = ROOT / "src/barchart_desktop_sdk"


def _has_docstring_after(body: list[ast.stmt], index: int) -> bool:
    """Whether the statement at `index` is followed by a string: an attribute's docstring."""
    following = body[index + 1] if index + 1 < len(body) else None
    return isinstance(following, ast.Expr) and isinstance(following.value, ast.Constant) and isinstance(following.value.value, str)


def _is_overload(function: ast.FunctionDef) -> bool:
    return any(isinstance(decorator, ast.Name) and decorator.id == "overload" for decorator in function.decorator_list)


_PUBLIC_DUNDERS = {"__init__", "__enter__", "__exit__"}


def _missing_constructor_attributes(class_name: str, init: ast.FunctionDef) -> list[str]:
    """Attributes the constructor sets (`self.name = …`) without a docstring under them."""
    return [
        f"{class_name}.{target.attr}"
        for position, statement in enumerate(init.body)
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Attribute) and not target.attr.startswith("_") and not _has_docstring_after(init.body, position)
    ]


def _is_public_field(item: ast.AnnAssign) -> bool:
    if not isinstance(item.target, ast.Name):
        return False
    name = item.target.id
    return not name.startswith("_") and not name.isupper() and "ClassVar" not in ast.unparse(item.annotation)


def _missing_in_class(node: ast.ClassDef) -> list[str]:
    missing = [] if ast.get_docstring(node) else [node.name]
    for index, item in enumerate(node.body):
        if isinstance(item, ast.FunctionDef) and not _is_overload(item):
            public = not item.name.startswith("_") or item.name in _PUBLIC_DUNDERS
            if public and not ast.get_docstring(item):
                missing.append(f"{node.name}.{item.name}")
            if item.name == "__init__":
                missing.extend(_missing_constructor_attributes(node.name, item))
        elif isinstance(item, ast.AnnAssign) and _is_public_field(item) and not _has_docstring_after(node.body, index):
            missing.append(f"{node.name}.{ast.unparse(item.target)}")
    return missing


def test_every_public_item_has_a_docstring() -> None:
    import barchart_desktop_sdk  # noqa: PLC0415

    classes: dict[str, ast.ClassDef] = {}
    documented_names: set[str] = set()
    functions: dict[str, ast.FunctionDef] = {}
    for path in PACKAGE.glob("*.py"):
        body = ast.parse(path.read_text()).body
        for index, node in enumerate(body):
            if isinstance(node, ast.ClassDef):
                classes[node.name] = node
            elif isinstance(node, ast.FunctionDef):
                functions[node.name] = node
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and _has_docstring_after(body, index):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                documented_names.update(target.id for target in targets if isinstance(target, ast.Name))

    missing: list[str] = []
    checked: set[str] = set()
    pending = list(barchart_desktop_sdk.__all__)
    while pending:
        name = pending.pop()
        if name in checked:
            continue
        checked.add(name)
        if name in classes:
            missing.extend(_missing_in_class(classes[name]))
            # members a public class inherits from one of the package's own classes are public too
            pending.extend(ast.unparse(base).split("[")[0] for base in classes[name].bases if ast.unparse(base).split("[")[0] in classes)
        elif name in functions:
            if not ast.get_docstring(functions[name]):
                missing.append(name)
        elif name not in documented_names:
            missing.append(name)
    assert sorted(missing) == []


def test_the_api_reference_has_a_page_for_every_public_name(tmp_path: Path) -> None:
    import barchart_desktop_sdk  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("docs_api", ROOT / "scripts/docs_api.py")
    assert spec is not None
    assert spec.loader is not None
    docs_api = importlib.util.module_from_spec(spec)
    # a dataclass looks its module up while it's created
    sys.modules["docs_api"] = docs_api
    spec.loader.exec_module(docs_api)

    assert docs_api.generate(tmp_path, list(barchart_desktop_sdk.__all__)) == []
    pages = {path.stem for path in tmp_path.glob("*.md")}
    assert pages == {*barchart_desktop_sdk.__all__, "README"}
    # every Google-style section became Markdown
    assert [path.name for path in tmp_path.glob("*.md") if re.search(r"^(Args|Returns|Raises|Example):$", path.read_text(), re.MULTILINE)] == []
