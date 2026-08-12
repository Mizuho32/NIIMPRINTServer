from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PrinterConfig:
    name: str
    model: str
    address: str | None = None


@dataclass(frozen=True)
class TemplateConfig:
    name: str
    content_path: Path | None = None
    engine: str = "handlebars"
    recipe: str = "html"


@dataclass(frozen=True)
class SelectionConfig:
    default_printer: str | None = None
    default_template: str | None = None
    printer_selector: str | None = None
    template_selector: str | None = None


@dataclass(frozen=True)
class AppConfig:
    app_name: str
    config_path: Path
    database_path: Path
    session_secret: str
    jsreport_url: str
    summary_key: str | None
    printers: tuple[PrinterConfig, ...]
    templates: tuple[TemplateConfig, ...]
    transforms: tuple[dict[str, str], ...]
    selection: SelectionConfig

    def printer_names(self) -> tuple[str, ...]:
        return tuple(printer.name for printer in self.printers)

    def template_names(self) -> tuple[str, ...]:
        return tuple(template.name for template in self.templates)

    def printer_by_name(self, name: str) -> PrinterConfig:
        for printer in self.printers:
            if printer.name == name:
                return printer
        raise KeyError(f"Unknown printer: {name}")

    def template_by_name(self, name: str) -> TemplateConfig:
        for template in self.templates:
            if template.name == name:
                return template
        raise KeyError(f"Unknown template: {name}")


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else (base_dir / path).resolve()


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    base_dir = config_path.parent

    printers = tuple(
        PrinterConfig(
            name=item["name"],
            model=item["model"],
            address=item.get("address"),
        )
        for item in raw.get("printers", [])
    )
    templates = tuple(
        TemplateConfig(
            name=item["name"],
            content_path=(
                _resolve_path(base_dir, item["content_path"])
                if item.get("content_path")
                else None
            ),
            engine=item.get("engine", "handlebars"),
            recipe=item.get("recipe", "html"),
        )
        for item in raw.get("templates", [])
    )
    selection_raw = raw.get("selection", {})
    database_path = _resolve_path(
        base_dir,
        raw.get("database_path", "label_print_server.sqlite3"),
    )
    transforms = tuple(
        {"name": item["name"], "template": item["template"]}
        for item in raw.get("transforms", [])
    )

    return AppConfig(
        app_name=raw.get("app_name", "NIIM Label Print Server"),
        config_path=config_path,
        database_path=database_path,
        session_secret=raw.get("session_secret", "change-me"),
        jsreport_url=raw.get("jsreport_url", "http://127.0.0.1:5488"),
        summary_key=raw.get("summary_key", "name"),
        printers=printers,
        templates=templates,
        transforms=transforms,
        selection=SelectionConfig(
            default_printer=selection_raw.get("default_printer"),
            default_template=selection_raw.get("default_template"),
            printer_selector=selection_raw.get("printer_selector"),
            template_selector=selection_raw.get("template_selector"),
        ),
    )


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
