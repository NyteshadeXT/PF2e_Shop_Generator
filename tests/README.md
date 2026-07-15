# Test suite

Run all tests from the project root with:

```text
python -m unittest discover -s tests -v
```

The `regression_*.py` modules contain the migrated rune, scroll, prerequisite, and weighting checks. `test_generator_regressions.py` exposes each function as a separately reported standard-library test, so no additional test framework is required.

`test_generation.py` verifies framework-neutral generation validation and snapshot orchestration with controlled selector results. `test_magic_builder.py` covers successful weapon, armor, and shield builds plus deterministic rerolls. `production_smoke.py` exercises the primary authenticated workflow against the two-worker Gunicorn server used by GitHub Actions.
