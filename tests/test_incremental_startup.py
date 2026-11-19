"""Startup-order regression, without wx, audio hardware or a model.

MainWindow.__init__ enters the event loop through init_UI before returning.
The controller must already exist at that boundary. Existing recording-adapter
unit tests supply their own controller and cannot catch a misplaced startup
assignment. These source-order and sliced-constructor tests cover that gap;
they do not claim a Windows GUI or microphone integration test.
"""
import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

MAIN = Path(__file__).resolve().parents[1] / "client" / "main.py"


def startup_nodes():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    window = next(n for n in tree.body
                  if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    init = next(n for n in window.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    return tree, init


def is_controller_assignment(node):
    return isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Attribute)
        and target.attr == "_incremental_controller"
        for target in node.targets)


def is_ui_boundary(node):
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "self"
            and node.value.func.attr == "init_UI")


class IncrementalStartupTests(unittest.TestCase):
    def test_only_controller_creation_precedes_blocking_ui_constructor(self):
        tree, init = startup_nodes()
        assignments = [n for n in ast.walk(tree) if is_controller_assignment(n)]
        self.assertEqual(len(assignments), 1)
        self.assertIn(assignments[0], init.body)
        boundary = next(n for n in init.body if is_ui_boundary(n))
        self.assertLess(init.body.index(assignments[0]), init.body.index(boundary))
        self.assertIsInstance(assignments[0].targets[0].value, ast.Name)
        self.assertEqual(assignments[0].targets[0].value.id, "self")

    def test_controller_is_available_to_first_ui_callback_and_per_window(self):
        _, init = startup_nodes()
        # Execute startup-relevant statements in their actual source order.
        # All other constructor work belongs to wx/platform integration tests.
        body = [n for n in init.body if is_controller_assignment(n)
                or is_ui_boundary(n)
                or (isinstance(n, ast.ImportFrom)
                    and n.module == "core.incremental_denoise")]
        function = ast.FunctionDef(
            name="startup", args=ast.arguments(posonlyargs=[],
                args=[ast.arg(arg="self")], kwonlyargs=[], kw_defaults=[],
                defaults=[]), body=body, decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        fake_module = types.ModuleType("core.incremental_denoise")
        fake_module.IncrementalDenoiseController = type("Controller", (), {})
        fake_core = types.ModuleType("core")
        namespace = {}
        seen = []
        class Window:
            def init_UI(self):
                seen.append(self._incremental_controller)
        with patch.dict(sys.modules, {"core": fake_core,
                        "core.incremental_denoise": fake_module}):
            exec(compile(module, str(MAIN), "exec"), namespace)
            namespace["startup"](Window())
            namespace["startup"](Window())
        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen[0], seen[1])


if __name__ == "__main__":
    unittest.main()
