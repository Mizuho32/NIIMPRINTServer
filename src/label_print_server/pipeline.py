from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from importlib import import_module
from typing import Any

from jinja2 import Environment, StrictUndefined

from label_print_server.config import AppConfig

Selector = Callable[..., str | None]

_ENVIRONMENT = Environment(undefined=StrictUndefined, autoescape=False)


def apply_transforms(
    payload: dict[str, Any],
    transforms: tuple[dict[str, str], ...],
) -> dict[str, Any]:
    draft = deepcopy(payload)
    for rule in transforms:
        rendered = _ENVIRONMENT.from_string(rule["template"]).render(**draft)
        draft[rule["name"]] = rendered
    return draft


def _load_selector(spec: str | None) -> Selector | None:
    if spec is None:
        return None
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"Invalid selector spec: {spec}")
    module = import_module(module_name)
    selector = getattr(module, attribute, None)
    if selector is None or not callable(selector):
        raise ValueError(f"Selector is not callable: {spec}")
    return selector


def _resolve_default(
    configured_default: str | None,
    options: tuple[str, ...],
    kind: str,
) -> str:
    if configured_default is not None:
        if configured_default not in options:
            raise ValueError(f"Configured {kind} '{configured_default}' is missing")
        return configured_default
    if not options:
        raise ValueError(f"No {kind}s configured")
    return options[0]


def _select_name(
    selector_spec: str | None,
    payload: dict[str, Any],
    draft: dict[str, Any],
    options: tuple[str, ...],
    configured_default: str | None,
    kind: str,
) -> str:
    default_name = _resolve_default(configured_default, options, kind)
    selector = _load_selector(selector_spec)
    if selector is None:
        return default_name
    selected_name = selector(
        payload=payload,
        draft=draft,
        options=options,
        default_name=default_name,
    )
    if selected_name not in options:
        raise ValueError(f"Selector returned unknown {kind}: {selected_name}")
    return selected_name


def resolve_initial_job(
    payload: dict[str, Any],
    config: AppConfig,
) -> tuple[dict[str, Any], str, str]:
    draft = apply_transforms(payload, config.transforms)
    template_name = _select_name(
        config.selection.template_selector,
        payload,
        draft,
        config.template_names(),
        config.selection.default_template,
        "template",
    )
    printer_name = _select_name(
        config.selection.printer_selector,
        payload,
        draft,
        config.printer_names(),
        config.selection.default_printer,
        "printer",
    )
    return draft, template_name, printer_name
