import pytest

from golden import presets

PY = presets.get("python")
NODE = presets.get("node")
GO = presets.get("go")


def test_unknown_preset():
    with pytest.raises(ValueError, match="unknown preset 'rust'"):
        presets.get("rust")


@pytest.mark.parametrize(
    ("path", "source", "test"),
    [
        ("src/pkg/mod.py", True, False),
        ("pkg/mod.py", True, False),
        ("tests/test_mod.py", False, True),
        ("tests/helpers.py", False, True),
        ("shop/tests.py", False, True),
        ("pkg/tests/factories.py", False, True),
        ("lib/test/support.py", False, True),
        ("pkg/testing.py", True, False),
        ("pkg/mod_test.py", False, True),
        ("conftest.py", False, True),
        ("docs/conf.py", False, False),
        ("app/migrations/0001.py", False, False),
        ("pkg/__main__.py", False, False),
        ("README.md", False, False),
        ("mutants/src/pkg/mod.py", False, False),
        (".venv/lib/x.py", False, False),
    ],
)
def test_python_classification(path, source, test):
    assert (PY.is_source(path), PY.is_test(path)) == (source, test)


@pytest.mark.parametrize(
    ("path", "source", "test"),
    [
        ("src/pricing.ts", True, False),
        ("src/ui/Button.tsx", True, False),
        ("src/pricing.test.ts", False, True),
        ("src/pricing.spec.mjs", False, True),
        ("src/__tests__/x.ts", False, True),
        ("test/pricing.ts", False, True),
        ("src/test/setup.ts", False, True),
        ("packages/web/tests/helpers.ts", False, True),
        ("src/testing.ts", True, False),
        ("src/types.d.ts", False, False),
        ("vitest.config.ts", False, False),
        ("dist/index.js", False, False),
        ("node_modules/a/index.js", False, False),
        (".stryker-tmp/sandbox/src/a.ts", False, False),
    ],
)
def test_node_classification(path, source, test):
    assert (NODE.is_source(path), NODE.is_test(path)) == (source, test)


@pytest.mark.parametrize(
    ("path", "source", "test"),
    [
        ("pricing/pricing.go", True, False),
        ("pricing/pricing_test.go", False, True),
        ("api/v1/api.pb.go", False, False),
        ("vendor/x/y.go", False, False),
        ("internal/mock_store.go", False, False),
    ],
)
def test_go_classification(path, source, test):
    assert (GO.is_source(path), GO.is_test(path)) == (source, test)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("src/orders/pricing.py", "orders.pricing"),
        ("orders/pricing.py", "orders.pricing"),
        ("src/orders/__init__.py", "orders"),
        ("tool.py", "tool"),
    ],
)
def test_python_module(source, expected):
    assert presets.PythonPreset.module(source) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("from orders.pricing import quote", True),
        ("import orders.pricing as p", True),
        ("from orders import pricing", True),
        ("from orders.pricing_v2 import quote", False),
        ("from orders.api import app", False),
        ("x = repricing()", False),
    ],
)
def test_python_references(text, expected):
    assert PY.references("src/orders/pricing.py", "tests/test_x.py", text) is expected


def test_python_init_references_package_name():
    assert PY.references("src/orders/__init__.py", "tests/t.py", "import orders")


PY_SOURCE = """\
import functools

RATE = 3


def first(a):
    return a + 1


@functools.cache
def second(a):
    return a * 2


class Cart:
    def add(self, x):
        return x

    async def remove(self, x):
        return -x
"""


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        ([(3, 3)], []),
        ([(7, 7)], ["m.x_first__mutmut_*"]),
        ([(10, 10)], ["m.x_second__mutmut_*"]),
        (
            [(1, 30)],
            [
                "m.x_first__mutmut_*",
                "m.x_second__mutmut_*",
                "m.xǁCartǁadd__mutmut_*",
                "m.xǁCartǁremove__mutmut_*",
            ],
        ),
        ([(19, 20)], ["m.xǁCartǁremove__mutmut_*"]),
    ],
)
def test_python_mutation_targets_follow_changed_functions(ranges, expected):
    assert PY.mutation_targets("src/m.py", ranges, PY_SOURCE) == expected


def test_python_mutation_targets_fall_back_to_module_on_syntax_error():
    assert PY.mutation_targets("src/m.py", [(1, 1)], "def broken(:") == ["m.x*"]


@pytest.mark.parametrize(
    ("source", "test", "text", "expected"),
    [
        ("src/pricing.ts", "src/pricing.test.ts", "", True),
        ("src/pricing.ts", "test/a.test.ts", "import { quote } from '../src/pricing'", True),
        ("src/pricing.ts", "test/a.test.ts", 'import { quote } from "../src/pricing.js"', True),
        ("src/pricing.ts", "test/a.test.ts", "import { quote } from '../src/pricing-v2'", False),
        ("src/cart/index.ts", "test/a.test.ts", "import { cart } from '../src/cart'", True),
        ("src/pricing.ts", "test/a.test.ts", "const pricing = 1", False),
    ],
)
def test_node_references(source, test, text, expected):
    assert NODE.references(source, test, text) is expected


def test_node_mutation_targets_are_line_ranges():
    assert NODE.mutation_targets("src/pricing.ts", [(3, 5), (9, 9)], "") == [
        "src/pricing.ts:3-5",
        "src/pricing.ts:9-9",
    ]


@pytest.mark.parametrize(
    ("source", "test", "expected"),
    [
        ("pricing/pricing.go", "pricing/pricing_test.go", True),
        ("pricing/pricing.go", "pricing/rules_test.go", True),
        ("pricing/pricing.go", "cart/cart_test.go", False),
        ("main.go", "main_test.go", True),
    ],
)
def test_go_references_same_package(source, test, expected):
    assert GO.references(source, test, "") is expected


@pytest.mark.parametrize(
    ("source", "expected"), [("pricing/pricing.go", "./pricing"), ("main.go", "./")]
)
def test_go_mutation_target_is_the_package(source, expected):
    assert GO.mutation_targets(source, [(1, 1)], "") == [expected]


@pytest.mark.parametrize(
    ("preset", "path", "runnable"),
    [
        (PY, "tests/test_mod.py", True),
        (PY, "pkg/mod_test.py", True),
        (PY, "shop/tests.py", True),
        (PY, "tests/conftest.py", False),
        (PY, "tests/helpers.py", False),
        (PY, "pkg/tests/factories.py", False),
        (PY, "src/pkg/test_utils.py", True),
        (NODE, "src/pricing.test.ts", True),
        (NODE, "src/pricing.spec.mjs", True),
        (NODE, "src/__tests__/pricing.ts", False),
        (NODE, "src/__tests__/pricing.test.ts", True),
        (NODE, "src/__tests__/fixture.json", False),
        (NODE, "test/fixtures/order.json", False),
        (NODE, "src/test/setup.ts", False),
        (GO, "pricing/pricing_test.go", True),
        (GO, "pricing/testdata/case.json", False),
    ],
)
def test_runnable_tests(preset, path, runnable):
    assert preset.is_runnable_test(path) is runnable


@pytest.mark.parametrize(
    ("source", "test", "text", "expected"),
    [
        # Relative imports resolve against the test's own package.
        ("shop/models.py", "shop/tests.py", "from .models import total", True),
        ("pkg/core.py", "pkg/tests/test_core.py", "from ..core import f", True),
        ("pkg/core.py", "pkg/tests/test_core.py", "from .. import core", True),
        ("src/pkg/core.py", "src/pkg/tests/test_x.py", "from ..core import f", True),
        ("pkg/sub/core.py", "pkg/tests/test_x.py", "from ..sub.core import f", True),
        ("pkg/core.py", "pkg/tests/__init__.py", "from ..core import f", True),
        ("other/core.py", "pkg/tests/test_core.py", "from ..core import f", False),
        ("pkg/core.py", "pkg/tests/test_core.py", "from .core import f", False),
        ("pkg/core.py", "pkg/tests/test_core.py", "from ....core import f", False),
        # A comment is not a reference; code and strings (mock targets) are.
        ("billing/tax.py", "tests/test_x.py", "# TODO: tax\nimport os", False),
        ("billing/tax.py", "tests/test_x.py", "import os  # billing.tax later", False),
        ("billing/tax.py", "tests/test_x.py", "x = '# not a comment: tax'", True),
        ("billing/tax.py", "tests/test_x.py", "mock.patch('billing.tax.rate')", True),
        # Unparseable test files still get the lenient textual match.
        ("billing/tax.py", "tests/test_x.py", "def broken(:\n    tax", True),
        ("billing/tax.py", "tests/test_x.py", 'x = """\ntax', True),
    ],
)
def test_python_references_imports_and_comments(source, test, text, expected):
    assert PY.references(source, test, text) is expected


@pytest.mark.parametrize(
    ("source", "text", "expected"),
    [
        ("src/order.service.ts", "import { a } from './order.service'", True),
        ("src/order.service.ts", "import { a } from './order.service.js'", True),
        ("src/order.service.ts", "import { a } from './order'", False),
        ("src/cart/index.ts", "import { a } from './cart/index.js'", True),
        ("src/cart/index.ts", "import { a } from './cart/index'", True),
        ("src/cart/index.ts", "import { a } from './cart/indexer'", False),
        ("src/pricing.ts", "import { a } from './pricing/index.js'", False),
    ],
)
def test_node_references_dotted_names_and_index(source, text, expected):
    assert NODE.references(source, "src/app.test.ts", text) is expected


def test_node_test_named_after_dotted_source():
    assert NODE.references("src/order.service.ts", "src/order.service.spec.ts", "")
    assert NODE.references("src/order.service.ts", "src/order.test.ts", "")


@pytest.mark.parametrize(
    ("preset", "path", "runnable"),
    [
        ("python", "tests/test_a.py", True),
        ("python", "pkg/a_test.py", True),
        ("python", "tests/conftest.py", False),
        ("python", "tests/helpers.py", False),
        ("python", "src/test_like_name.py", True),
        ("node", "src/a.spec.tsx", True),
        ("node", "test/setup.ts", False),
        ("node", "src/a.ts", False),
        ("go", "p/p_test.go", True),
        ("go", "p/p.go", False),
    ],
)
def test_runnable_tests_by_preset_name(preset, path, runnable):
    assert presets.get(preset).is_runnable_test(path) is runnable
