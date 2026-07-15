"""Exact Pathfinder 2e currency parsing and arithmetic."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CP_PER_GP = 100
CP_PER_SP = 10
_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(gp|sp|cp)\b", re.IGNORECASE)


def _round_cp(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def gp_to_cp(value: int | float | Decimal | str) -> int:
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid gold-piece value: {value!r}") from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError("Price must be a finite non-negative value.")
    return _round_cp(decimal_value * CP_PER_GP)


def parse_price_to_cp(value: object | None) -> int | None:
    """Parse bare gp values or strings such as '2 gp 5 sp 3 cp'."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            return gp_to_cp(value)
        except ValueError:
            return None

    text = str(value).strip().lower()
    if not text:
        return None
    try:
        return gp_to_cp(text)
    except ValueError:
        pass

    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    matches = list(_TOKEN.finditer(text))
    if not matches:
        return None
    leftover = _TOKEN.sub(" ", text)
    leftover = re.sub(r"\band\b", " ", leftover)
    if leftover.strip(" ,;+/"):
        return None

    total = Decimal(0)
    rates = {"gp": Decimal(CP_PER_GP), "sp": Decimal(CP_PER_SP), "cp": Decimal(1)}
    for match in matches:
        total += Decimal(match.group(1)) * rates[match.group(2).lower()]
    return _round_cp(total)


def cp_to_gp(cp_value: int) -> Decimal:
    return Decimal(int(cp_value)) / Decimal(CP_PER_GP)


def format_cp(cp_value: int | None) -> str:
    if cp_value is None:
        return ""
    total = max(0, int(cp_value))
    gp, remainder = divmod(total, CP_PER_GP)
    sp, cp = divmod(remainder, CP_PER_SP)
    parts = []
    if gp:
        parts.append(f"{gp} gp")
    if sp:
        parts.append(f"{sp} sp")
    if cp:
        parts.append(f"{cp} cp")
    return " ".join(parts) if parts else "0 gp"


def format_gp(value: int | float | Decimal | None) -> str:
    if value is None:
        return ""
    try:
        return format_cp(gp_to_cp(value))
    except ValueError:
        return ""


def multiply_cp(cp_value: int, multiplier: int | float | Decimal) -> int:
    try:
        factor = Decimal(str(multiplier))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid price multiplier: {multiplier!r}") from exc
    if not factor.is_finite() or factor < 0:
        raise ValueError("Price multiplier must be finite and non-negative.")
    return _round_cp(Decimal(int(cp_value)) * factor)
