"""Language presets: which files are sources, which are tests, and how they relate.

The presets encode each ecosystem's conventions so the language-agnostic gates
(test presence, flake re-runs, mutation scoping) can reason about a change.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from golden.globmatch import match_any

JS_EXTS = ("ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs")
JS_EXT = "{" + ",".join(JS_EXTS) + "}"
Range = tuple[int, int]


@dataclass(frozen=True)
class Preset:
    name: str
    sources: tuple[str, ...]
    # Test code: test modules plus their helpers, fixtures and configuration.
    tests: tuple[str, ...]
    ignore: tuple[str, ...] = field(default=())
    # Test files the runner collects by default. Other test files (conftest.py,
    # fixtures, helpers) count for test presence but cannot be re-run on their
    # own: pytest exits 5 and Vitest exits 1 when handed only those.
    runnable: tuple[str, ...] = field(default=())

    def is_test(self, path: str) -> bool:
        return match_any(path, self.tests)

    def is_runnable_test(self, path: str) -> bool:
        return self.is_test(path) and match_any(path, self.runnable)

    def is_source(self, path: str) -> bool:
        return (
            match_any(path, self.sources)
            and not self.is_test(path)
            and not match_any(path, self.ignore)
        )

    def references(self, source: str, test: str, test_text: str) -> bool:
        raise NotImplementedError

    def mutation_targets(self, source: str, ranges: list[Range], text: str) -> list[str]:
        """Mutation-tool selectors covering only the changed lines of `source`."""
        raise NotImplementedError


def _overlaps(node: ast.stmt, ranges: list[Range]) -> bool:
    start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
    end = node.end_lineno or node.lineno
    return any(lo <= end and hi >= start for lo, hi in ranges)


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.]){re.escape(name)}(?!\w)")


def _without_comments(text: str) -> str:
    """Python source minus comments: mentioning a module in a comment does not test it."""
    lines = text.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return text
    for token in tokens:
        if token.type == tokenize.COMMENT:
            (row, start), (_, end) = token.start, token.end
            lines[row - 1] = lines[row - 1][:start] + lines[row - 1][end:]
    return "".join(lines)


class PythonPreset(Preset):
    @staticmethod
    def module(source: str) -> str:
        parts = list(PurePosixPath(source).with_suffix("").parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def relative_imports(self, test: str, test_text: str) -> set[str]:
        """Modules a test reaches with `from .x import y` / `from .. import x`."""
        try:
            tree = ast.parse(test_text)
        except SyntaxError:
            return set()
        package = self.module(test).split(".")
        if PurePosixPath(test).name != "__init__.py":
            package = package[:-1]
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and 0 < node.level <= len(package) + 1:
                anchor = package[: len(package) - (node.level - 1)]
                target = ".".join([*anchor, *([node.module] if node.module else [])])
                found.add(target)
                found.update(f"{target}.{alias.name}" for alias in node.names)
        return found

    def references(self, source: str, test: str, test_text: str) -> bool:
        dotted = self.module(source)
        if dotted in self.relative_imports(test, test_text):
            return True
        code = _without_comments(test_text)
        if dotted and re.search(rf"\b{re.escape(dotted)}\b", code):
            return True
        stem = dotted.rsplit(".", 1)[-1] if dotted else PurePosixPath(source).stem
        return bool(stem) and _word(stem).search(code) is not None

    def mutation_targets(self, source: str, ranges: list[Range], text: str) -> list[str]:
        # mutmut names mutants `<module>.x_<func>__mutmut_N` and
        # `<module>.xǁ<Class>ǁ<method>__mutmut_N`; select the functions the diff touches.
        module = self.module(source)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [f"{module}.x*"]
        functions = (ast.FunctionDef, ast.AsyncFunctionDef)
        targets = []
        for node in tree.body:
            if isinstance(node, functions) and _overlaps(node, ranges):
                targets.append(f"{module}.x_{node.name}__mutmut_*")
            elif isinstance(node, ast.ClassDef):
                targets.extend(
                    f"{module}.xǁ{node.name}ǁ{item.name}__mutmut_*"
                    for item in node.body
                    if isinstance(item, functions) and _overlaps(item, ranges)
                )
        return targets


class NodePreset(Preset):
    def references(self, source: str, test: str, test_text: str) -> bool:
        path = PurePosixPath(source)
        name = path.name.rsplit(".", 1)[0]  # order.service.ts -> order.service
        index = name == "index"
        if index:
            name = path.parent.name
        if PurePosixPath(test).name.split(".", 1)[0] == name.split(".", 1)[0]:
            return True
        ext = "|".join(JS_EXTS)
        tail = "(?:/index)?" if index else ""
        import_of = re.compile(rf"""["'][^"'\n]*/{re.escape(name)}{tail}(?:\.(?:{ext}))?["']""")
        return import_of.search(test_text) is not None

    def mutation_targets(self, source: str, ranges: list[Range], text: str) -> list[str]:
        # StrykerJS `--mutate` accepts `path:start-end` line ranges.
        return [f"{source}:{lo}-{hi}" for lo, hi in ranges]


class GoPreset(Preset):
    def references(self, source: str, test: str, test_text: str) -> bool:
        # Go tests live in the package directory they exercise.
        return PurePosixPath(source).parent == PurePosixPath(test).parent

    def mutation_targets(self, source: str, ranges: list[Range], text: str) -> list[str]:
        # gremlins scopes itself with `--diff`; report the package for visibility.
        parent = PurePosixPath(source).parent
        return ["./" if str(parent) == "." else f"./{parent}"]


PRESETS: dict[str, Preset] = {
    "python": PythonPreset(
        name="python",
        sources=("*.py",),
        tests=(
            "test_*.py",
            "*_test.py",
            "tests.py",  # Django's default
            "conftest.py",
            "**/tests/**/*.py",
            "**/test/**/*.py",
        ),
        runnable=("test_*.py", "*_test.py", "tests.py"),
        ignore=(
            "**/migrations/**",
            "docs/**",
            "setup.py",
            "noxfile.py",
            "**/__main__.py",
            "mutants/",
            ".venv/",
        ),
    ),
    "node": NodePreset(
        name="node",
        sources=(f"*.{JS_EXT}",),
        tests=(
            f"*.test.{JS_EXT}",
            f"*.spec.{JS_EXT}",
            "**/__tests__/**",
            "**/test/**",
            "**/tests/**",
        ),
        # Vitest's default test-file patterns. Files under __tests__/ without the
        # suffix are often helpers, which both Vitest and Jest refuse to run alone.
        runnable=(f"*.test.{JS_EXT}", f"*.spec.{JS_EXT}"),
        ignore=(
            "*.d.ts",
            "*.config.*",
            ".*rc.*",
            "node_modules/",
            "dist/",
            "build/",
            "coverage/",
            "reports/",
            ".stryker-tmp/",
        ),
    ),
    "go": GoPreset(
        name="go",
        sources=("*.go",),
        tests=("*_test.go",),
        runnable=("*_test.go",),
        ignore=("vendor/", "*.pb.go", "*_gen.go", "zz_generated*.go", "mock_*.go", "testdata/"),
    ),
}


def get(name: str) -> Preset:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(f"unknown preset {name!r}; choose from {', '.join(PRESETS)}") from None
