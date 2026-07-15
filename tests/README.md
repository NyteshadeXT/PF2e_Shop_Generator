# Test suite

Run all tests from the project root with:

```text
python -m unittest discover -s tests -v
```

The `regression_*.py` modules contain the migrated rune, scroll, prerequisite, and weighting checks. `test_generator_regressions.py` exposes each function as a separately reported standard-library test, so no additional test framework is required.
