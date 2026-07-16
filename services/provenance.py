"""Stable fingerprints for the inputs that determine generated inventory."""
from __future__ import annotations

import hashlib
import sys
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .settings import CONFIG, DEFAULT_CONFIG_PATH, PROJECT_ROOT


_GENERATION_CODE = (
    PROJECT_ROOT / "services" / "db.py",
    PROJECT_ROOT / "services" / "catalog_order.py",
    PROJECT_ROOT / "services" / "generation.py",
    PROJECT_ROOT / "services" / "inventory_sections.py",
    PROJECT_ROOT / "services" / "logic.py",
    PROJECT_ROOT / "services" / "money.py",
    PROJECT_ROOT / "services" / "randomness.py",
    PROJECT_ROOT / "services" / "spellbooks.py",
    PROJECT_ROOT / "services" / "spell_items.py",
    PROJECT_ROOT / "services" / "utils.py",
)


def _source_paths() -> tuple[Path, ...]:
    paths = [DEFAULT_CONFIG_PATH, *_GENERATION_CODE]
    catalog = CONFIG.get("sqlite_db_path")
    if catalog:
        paths.append(Path(str(catalog)))
    return tuple(path.resolve() for path in paths)


def _file_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return str(path), int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return str(path), -1, -1


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "missing"


@lru_cache(maxsize=8)
def _fingerprint_for_signature(
    signature: tuple[tuple[str, int, int], ...],
    runtime: tuple[str, str, str],
) -> str:
    digest = hashlib.sha256()
    digest.update(("runtime:" + "|".join(runtime)).encode("utf-8"))
    for raw_path, _size, _mtime in signature:
        path = Path(raw_path)
        digest.update(("\nfile:" + path.name + "\n").encode("utf-8"))
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()[:16]


def generation_fingerprint() -> str:
    """Identify the code, configuration, catalog, and runtime used for a shop."""
    paths = _source_paths()
    signature = tuple(_file_signature(path) for path in paths)
    runtime = (
        f"python-{sys.version_info.major}.{sys.version_info.minor}",
        f"pandas-{_package_version('pandas')}",
        f"numpy-{_package_version('numpy')}",
    )
    return _fingerprint_for_signature(signature, runtime)
