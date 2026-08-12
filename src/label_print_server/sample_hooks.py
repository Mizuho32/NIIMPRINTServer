from __future__ import annotations

from typing import Any


def select_template(
    *,
    payload: dict[str, Any],
    draft: dict[str, Any],
    options: tuple[str, ...],
    default_name: str,
) -> str:
    requested = draft.get("template_name") or payload.get("template_name")
    if requested in options:
        return requested
    return default_name


def select_printer(
    *,
    payload: dict[str, Any],
    draft: dict[str, Any],
    options: tuple[str, ...],
    default_name: str,
) -> str:
    requested = draft.get("printer_name") or payload.get("printer_name")
    if requested in options:
        return requested
    return default_name
