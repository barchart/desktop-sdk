"""Writes the API reference: one Markdown page per public name, from the docstrings, into the package's `docs/api/`.

Run it before `uv build`, so the wheel and the sdist ship the pages (`uv run python scripts/docs_api.py`). The pages are
generated, not committed (`.gitignore`). It reads the source with `ast` and imports nothing but the package's `__all__`.
"""

from __future__ import annotations

import ast
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src/barchart_desktop_sdk"
OUT = PACKAGE / "docs/api"

_SECTIONS = {"Args": "Parameters", "Returns": "Returns", "Raises": "Raises", "Example": "Example"}
_SECTION = re.compile(rf"^({'|'.join(_SECTIONS)}):$")
_ENTRY = re.compile(r"^(\w+): (.*)$")


@dataclass
class _Source:
    """The package's top-level definitions, by name, and each documented alias's docstring."""

    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    functions: dict[str, ast.FunctionDef] = field(default_factory=dict)
    aliases: dict[str, tuple[ast.stmt, str]] = field(default_factory=dict)


def _string_after(body: list[ast.stmt], index: int) -> str | None:
    """The docstring under the statement at `index`: an attribute's or an alias's."""
    following = body[index + 1] if index + 1 < len(body) else None
    if isinstance(following, ast.Expr) and isinstance(following.value, ast.Constant) and isinstance(following.value.value, str):
        return _clean(following.value.value)
    return None


def _clean(text: str) -> str:
    """A docstring with its indentation removed, as `ast.get_docstring` gives a function's."""
    lines = text.expandtabs().splitlines()
    indents = [len(line) - len(line.lstrip()) for line in lines[1:] if line.strip()]
    margin = min(indents, default=0)
    return "\n".join([lines[0].strip(), *(line[margin:] for line in lines[1:])]).strip()


def _read_source() -> _Source:
    source = _Source()
    for path in sorted(PACKAGE.glob("*.py")):
        body = ast.parse(path.read_text()).body
        for index, node in enumerate(body):
            if isinstance(node, ast.ClassDef):
                source.classes[node.name] = node
            elif isinstance(node, ast.FunctionDef):
                source.functions[node.name] = node
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and (doc := _string_after(body, index)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        source.aliases[target.id] = (node, doc)
    return source


def _markdown(docstring: str) -> str:
    """A Google-style docstring as Markdown: the text as is, and each section under a heading of its own."""
    out: list[str] = []
    section: str | None = None
    for line in docstring.splitlines():
        if match := _SECTION.match(line):
            section = match.group(1)
            out.extend(["", f"**{_SECTIONS[section]}**", ""])
        elif section is None:
            out.append(line)
        else:
            text = line.removeprefix("    ")
            entry = _ENTRY.match(text) if section in {"Args", "Raises"} else None
            if entry:
                out.append(f"- `{entry.group(1)}`: {entry.group(2)}")
            elif section in {"Args", "Raises"} and text.startswith("    "):
                out[-1] += " " + text.strip()
            else:
                out.append(text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _signature(function: ast.FunctionDef, name: str) -> str:
    arguments = function.args
    parameters = [*arguments.posonlyargs, *arguments.args]
    defaults = [None] * (len(parameters) - len(arguments.defaults)) + list(arguments.defaults)
    parts = [_parameter(parameter, default) for parameter, default in zip(parameters, defaults, strict=True) if parameter.arg != "self"]
    if arguments.kwonlyargs:
        parts.append("*")
        parts.extend(_parameter(p, d) for p, d in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True))
    returns = f" -> {ast.unparse(function.returns)}" if function.returns else ""
    return f"{name}({', '.join(parts)}){returns}"


def _parameter(parameter: ast.arg, default: ast.expr | None) -> str:
    text = parameter.arg + (f": {ast.unparse(parameter.annotation)}" if parameter.annotation else "")
    return text + (f" = {ast.unparse(default)}" if default is not None else "")


def _is_property(function: ast.FunctionDef) -> bool:
    return any(isinstance(d, ast.Name) and d.id == "property" for d in function.decorator_list)


def _is_overload(function: ast.FunctionDef) -> bool:
    return any(isinstance(d, ast.Name) and d.id == "overload" for d in function.decorator_list)


def _class_page(node: ast.ClassDef, source: _Source) -> str:
    """A class's page: its docstring, then its constructor, fields and properties, and methods, inherited ones included."""
    members = [node]
    members.extend(source.classes[base] for base in (ast.unparse(b).split("[")[0] for b in node.bases) if base in source.classes)
    fields: list[str] = []
    properties: list[str] = []
    methods: list[str] = []
    constructor = ""
    for cls in members:
        for index, item in enumerate(cls.body):
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and not item.target.id.startswith("_"):
                if "ClassVar" in ast.unparse(item.annotation):
                    continue
                doc = _string_after(cls.body, index) or ""
                fields.append(f"### `{item.target.id}: {ast.unparse(item.annotation)}`\n\n{_markdown(doc)}")
            elif isinstance(item, ast.FunctionDef) and not _is_overload(item):
                doc = _markdown(ast.get_docstring(item) or "")
                if item.name == "__init__" and cls is node:
                    constructor = f"```python\n{_signature(item, node.name)}\n```\n\n{doc}"
                    fields.extend(_constructor_attributes(item))
                elif _is_property(item) and not item.name.startswith("_"):
                    returns = ast.unparse(item.returns) if item.returns else ""
                    properties.append(f"### `{item.name}: {returns}`\n\n{doc}")
                elif not item.name.startswith("_"):
                    methods.append(f"### `{_signature(item, item.name)}`\n\n{doc}")
    parts = [f"# {node.name}", _markdown(ast.get_docstring(node) or "")]
    if constructor:
        parts += ["## Constructor", constructor]
    for title, entries in (("Fields", fields), ("Properties", properties), ("Methods", methods)):
        if entries:
            parts += [f"## {title}", *entries]
    return "\n\n".join(part for part in parts if part) + "\n"


def _constructor_attributes(init: ast.FunctionDef) -> list[str]:
    """Attributes the constructor sets (`self.name = …`) and documents."""
    return [
        f"### `{target.attr}`\n\n{_markdown(doc)}"
        for index, statement in enumerate(init.body)
        if isinstance(statement, ast.Assign) and (doc := _string_after(init.body, index))
        for target in statement.targets
        if isinstance(target, ast.Attribute) and not target.attr.startswith("_")
    ]


def _function_page(node: ast.FunctionDef) -> str:
    return f"# {node.name}\n\n```python\n{_signature(node, node.name)}\n```\n\n{_markdown(ast.get_docstring(node) or '')}\n"


def _alias_page(name: str, statement: ast.stmt, doc: str) -> str:
    value = statement.value if isinstance(statement, (ast.Assign, ast.AnnAssign)) else None
    definition = f"{name} = {ast.unparse(value)}" if value is not None else name
    return f"# {name}\n\n```python\n{definition}\n```\n\n{_markdown(doc)}\n"


def _summary(docstring: str) -> str:
    return docstring.split("\n\n", maxsplit=1)[0].replace("\n", " ")


def generate(out: Path, names: list[str]) -> list[str]:
    """Writes a page for each name, and the index; returns the names it found no definition for."""
    source = _read_source()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    groups: dict[str, list[str]] = {"Classes": [], "Functions": [], "Types": []}
    missing: list[str] = []
    for name in names:
        if name in source.classes:
            page, group, doc = _class_page(source.classes[name], source), "Classes", ast.get_docstring(source.classes[name]) or ""
        elif name in source.functions:
            page, group, doc = _function_page(source.functions[name]), "Functions", ast.get_docstring(source.functions[name]) or ""
        elif name in source.aliases:
            statement, doc = source.aliases[name]
            page, group = _alias_page(name, statement, doc), "Types"
        else:
            missing.append(name)
            continue
        (out / f"{name}.md").write_text(page)
        groups[group].append(f"- [`{name}`]({name}.md): {_summary(doc)}")
    index = ["# barchart-desktop-sdk API", "Every public class, function and type, from the docstrings."]
    for title, entries in groups.items():
        if entries:
            index += [f"## {title}", "\n".join(entries)]
    (out / "README.md").write_text("\n\n".join(index) + "\n")
    return missing


def main() -> int:
    sys.path.insert(0, str(PACKAGE.parent))
    import barchart_desktop_sdk  # noqa: PLC0415

    missing = generate(OUT, list(barchart_desktop_sdk.__all__))
    for name in missing:
        print(f"No definition found for {name}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
