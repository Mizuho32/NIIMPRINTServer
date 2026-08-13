from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from label_print_server.config import AppConfig

Selector = Callable[..., str | None]
TransformInput = dict[str, Any] | list[Any]
TransformHook = Callable[[TransformInput], list[dict[str, Any]]]
SummaryHook = Callable[[dict[str, Any]], str]


def _load_python_file(path: Path) -> Any:
    module_name = f"label_print_server_user_hooks_{abs(hash(path))}"
    spec = spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load hooks file: {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_transform_hook(path: Path | None) -> TransformHook | None:
    if path is None or not path.exists():
        return None
    module = _load_python_file(path)
    transform = getattr(module, "transform", None)
    if transform is None:
        return None
    if not callable(transform):
        raise TypeError(f"transform is not callable: {path}")
    return transform


def load_summary_hook(path: Path | None) -> SummaryHook | None:
    if path is None or not path.exists():
        return None
    module = _load_python_file(path)
    summary_text = getattr(module, "summary_text", None)
    if summary_text is None:
        return None
    if not callable(summary_text):
        raise TypeError(f"summary_text is not callable: {path}")
    return summary_text


def parse_transform_input(payload: Any) -> TransformInput:
    if isinstance(payload, dict):
        return deepcopy(payload)
    if isinstance(payload, list):
        return deepcopy(payload)
    raise TypeError("Expected a JSON object or JSON array")


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


def _default_transform(payload: TransformInput) -> list[Any]:
    if isinstance(payload, dict):
        return [payload]
    return list(payload)


def apply_transforms(
    payload: TransformInput,
    transform_hook: TransformHook | None,
) -> list[dict[str, Any]]:
    transform = transform_hook or _default_transform
    transformed = transform(deepcopy(payload))
    if not isinstance(transformed, list):
        raise TypeError("transform must return a list")
    if not all(isinstance(item, dict) for item in transformed):
        raise TypeError("transform must return a list of dicts")
    return transformed


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


def _payloads_for_jobs(
    payload: TransformInput,
    drafts: list[dict[str, Any]],
) -> list[Any]:
    if isinstance(payload, dict):
        return [deepcopy(payload) for _ in drafts]
    if len(payload) == len(drafts) and all(isinstance(item, dict) for item in payload):
        return deepcopy(payload)
    return [deepcopy(payload) for _ in drafts]


def resolve_initial_jobs(
    payload: TransformInput,
    config: AppConfig,
    transform_hook: TransformHook | None = None,
) -> list[dict[str, Any]]:
    drafts = apply_transforms(payload, transform_hook)
    source_payloads = _payloads_for_jobs(payload, drafts)
    jobs = []
    for source_payload, draft in zip(source_payloads, drafts, strict=True):
        selector_payload = source_payload if isinstance(source_payload, dict) else draft
        template_name = _select_name(
            config.selection.template_selector,
            selector_payload,
            draft,
            config.template_names(),
            config.selection.default_template,
            "template",
        )
        printer_name = _select_name(
            config.selection.printer_selector,
            selector_payload,
            draft,
            config.printer_names(),
            config.selection.default_printer,
            "printer",
        )
        jobs.append(
            {
                "payload": source_payload,
                "draft": draft,
                "selected_template": template_name,
                "selected_printer": printer_name,
            }
        )
    return jobs
