"""Canonical ordering helpers for reproducible seeded generation."""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

import pandas as pd


def _scalar_key(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return f"{text.casefold()}\0{text}"


def canonicalize_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Return rows in a stable content-derived order with a fresh index."""
    if frame is None:
        return pd.DataFrame()
    if frame.empty:
        return frame.copy().reset_index(drop=True)
    working = frame.reset_index(drop=True)
    columns = sorted(working.columns, key=lambda value: (str(value).casefold(), str(value)))
    sort_keys = pd.DataFrame(index=working.index)
    key_names: list[str] = []
    for position, column in enumerate(columns):
        key_name = f"key_{position:04d}"
        sort_keys[key_name] = working[column].map(_scalar_key)
        key_names.append(key_name)
    ordered_index = sort_keys.sort_values(
        key_names,
        kind="mergesort",
        na_position="first",
    ).index
    return working.iloc[ordered_index.to_list()].reset_index(drop=True)


def canonicalize_records(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return mapping records ordered by their complete serialized content."""
    copied = [dict(record) for record in records]
    return sorted(
        copied,
        key=lambda record: json.dumps(
            record,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )
