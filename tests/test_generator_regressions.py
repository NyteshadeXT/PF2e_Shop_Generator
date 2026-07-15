"""Expose the migrated function-style generator checks to unittest discovery."""
from __future__ import annotations

import importlib.util
import inspect
import unittest
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parent
REGRESSION_FILES = sorted(TEST_ROOT.glob("regression_*.py"))


class GeneratorRegressionTests(unittest.TestCase):
    """Populated below so each migrated check is reported independently."""


def _method_for(check):
    def method(self):
        check()

    method.__doc__ = check.__doc__
    return method


for regression_file in REGRESSION_FILES:
    module_name = f"generator_tests_{regression_file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, regression_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {regression_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    group = regression_file.stem.removeprefix("regression_")
    for function_name, check in inspect.getmembers(module, inspect.isfunction):
        if not function_name.startswith("test_"):
            continue
        test_name = f"test_{group}__{function_name.removeprefix('test_')}"
        method = _method_for(check)
        method.__name__ = test_name
        setattr(GeneratorRegressionTests, test_name, method)
