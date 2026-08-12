from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from label_print_server.config import AppConfig, dump_json, load_config
from label_print_server.jsreport_client import JsReportClient
from label_print_server.pipeline import resolve_initial_job
from label_print_server.storage import JobStore

TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)


def _ensure_session_id(request: Request) -> str:
    session_id = request.session.get("session_id")
    if session_id is None:
        session_id = uuid4().hex
        request.session["session_id"] = session_id
    return session_id


def _parse_payload_list(raw_text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, list):
        raise TypeError("Expected a JSON array")
    if not all(isinstance(item, dict) for item in payload):
        raise TypeError("Expected an array of JSON objects")
    return payload


def _parse_payload_object(raw_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise TypeError("Expected a JSON object")
    return payload


def _job_summary(job_data: dict[str, Any], summary_key: str | None) -> str:
    if summary_key:
        value = job_data.get(summary_key)
        if value:
            return str(value)
    for key in ("name", "id", "title", "label"):
        value = job_data.get(key)
        if value:
            return str(value)
    return dump_json(job_data).replace("\n", " ")[:60]


def _fallback_job_summary(job: dict[str, Any]) -> str:
    payload = job["payload"]
    for key in ("name", "id", "title", "label"):
        value = payload.get(key)
        if value:
            return str(value)
    return dump_json(payload).replace("\n", " ")[:60]


def _resolve_editor_state(
    job: dict[str, Any],
    override: dict[str, Any] | None,
) -> tuple[dict[str, Any], str, str]:
    if override is None:
        return (
            job["draft"],
            job["selected_template"],
            job["selected_printer"],
        )
    return (
        override["data"],
        override["selected_template"],
        override["selected_printer"],
    )


def _preview_data_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _render_jobs_page(
    request: Request,
    *,
    session_id: str,
    config: AppConfig,
    store: JobStore,
    payload_text: str = "[]",
    error: str | None = None,
) -> HTMLResponse:
    jobs = store.list_jobs()
    overrides = store.list_session_overrides(session_id)
    job_cards = []
    for job in jobs:
        override = overrides.get(job["id"])
        data, selected_template, selected_printer = _resolve_editor_state(job, override)
        summary_text = _job_summary(data, config.summary_key)
        if not summary_text:
            summary_text = _fallback_job_summary(job)
        job_cards.append(
            {
                **job,
                "data": data,
                "summary_text": summary_text,
                "selected_template": selected_template,
                "selected_printer": selected_printer,
                "preview_data_url": (
                    None if override is None else override["preview_data_url"]
                ),
                "render_error": None if override is None else override["render_error"],
            }
        )

    return TEMPLATES.TemplateResponse(
        request,
        "jobs.html",
        {
            "app_name": config.app_name,
            "jobs": job_cards,
            "payload_text": payload_text,
            "error": error,
            "templates": config.template_names(),
            "printers": config.printer_names(),
        },
        status_code=status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK,
    )


def _render_job_page(
    request: Request,
    *,
    config: AppConfig,
    job: dict[str, Any],
    override: dict[str, Any] | None,
    error: str | None = None,
    response_status: int | None = None,
) -> HTMLResponse:
    data, selected_template, selected_printer = _resolve_editor_state(job, override)
    status_code = response_status
    if status_code is None:
        status_code = status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK

    return TEMPLATES.TemplateResponse(
        request,
        "job_detail.html",
        {
            "app_name": config.app_name,
            "job": job,
            "raw_json": dump_json(job["payload"]),
            "data_json": dump_json(data),
            "selected_template": selected_template,
            "selected_printer": selected_printer,
            "templates": config.template_names(),
            "printers": config.printer_names(),
            "preview_data_url": (
                None if override is None else override["preview_data_url"]
            ),
            "render_error": error
            or (None if override is None else override["render_error"]),
        },
        status_code=status_code,
    )


def _validate_selection(name: str, options: tuple[str, ...], kind: str) -> str:
    if name not in options:
        raise ValueError(f"Unknown {kind}: {name}")
    return name


async def _read_editor_submission(
    request: Request,
    *,
    config: AppConfig,
    job: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    form = await request.form()
    data = _parse_payload_object(str(form.get("data_json", "")))
    selected_template = _validate_selection(
        str(form.get("selected_template", job["selected_template"])),
        config.template_names(),
        "template",
    )
    selected_printer = _validate_selection(
        str(form.get("selected_printer", job["selected_printer"])),
        config.printer_names(),
        "printer",
    )
    return data, selected_template, selected_printer


def _enqueue_jobs(
    store: JobStore,
    config: AppConfig,
    payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    batch_id = uuid4().hex[:12]
    jobs = []
    for index, payload in enumerate(payloads, start=1):
        draft, selected_template, selected_printer = resolve_initial_job(
            payload,
            config,
        )
        job_id = store.create_job(
            batch_id=batch_id,
            batch_index=index,
            payload=payload,
            draft=draft,
            selected_template=selected_template,
            selected_printer=selected_printer,
        )
        jobs.append(
            {
                "id": job_id,
                "selected_template": selected_template,
                "selected_printer": selected_printer,
            }
        )
    return {"batch_id": batch_id, "jobs": jobs}


def _render_and_store_preview(
    *,
    config: AppConfig,
    store: JobStore,
    report_client: JsReportClient,
    session_id: str,
    job_id: int,
    data: dict[str, Any],
    selected_template: str,
    selected_printer: str,
) -> None:
    image_bytes = report_client.render_preview(
        config.template_by_name(selected_template),
        data,
    )
    store.save_session_override(
        session_id=session_id,
        job_id=job_id,
        data=data,
        selected_template=selected_template,
        selected_printer=selected_printer,
        preview_data_url=_preview_data_url(image_bytes),
        render_error=None,
    )


def create_app(
    config_path: str | Path,
    database_path: str | Path | None = None,
    *,
    render_client: JsReportClient | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    config = load_config(config_path)
    effective_database = (
        Path(database_path) if database_path is not None else config.database_path
    )
    job_store = store or JobStore(effective_database)
    report_client = render_client or JsReportClient(config.jsreport_url)

    app = FastAPI(title=config.app_name)
    app.add_middleware(SessionMiddleware, secret_key=config.session_secret)
    app.state.config = config
    app.state.store = job_store
    app.state.render_client = report_client

    @app.get("/", response_class=HTMLResponse)
    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        return _render_jobs_page(
            request,
            session_id=session_id,
            config=config,
            store=job_store,
        )

    @app.post("/jobs/intake", response_class=HTMLResponse)
    async def intake_jobs_form(request: Request) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        form = await request.form()
        payload_text = str(form.get("payload_json", ""))
        try:
            payloads = _parse_payload_list(payload_text)
        except (TypeError, ValueError) as exc:
            return _render_jobs_page(
                request,
                session_id=session_id,
                config=config,
                store=job_store,
                payload_text=payload_text,
                error=str(exc),
            )
        _enqueue_jobs(job_store, config, payloads)
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/api/jobs")
    async def intake_jobs_api(payloads: list[dict[str, Any]]) -> JSONResponse:
        result = _enqueue_jobs(job_store, config, payloads)
        return JSONResponse(result)

    @app.get("/api/jobs")
    async def jobs_api() -> JSONResponse:
        return JSONResponse({"jobs": job_store.list_jobs()})

    @app.get("/api/jobs/{job_id}")
    async def job_api(job_id: int) -> JSONResponse:
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return JSONResponse(job)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: int) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        override = job_store.get_session_override(session_id, job_id)
        return _render_job_page(request, config=config, job=job, override=override)

    @app.post("/jobs/render-all")
    async def render_all_jobs(request: Request) -> RedirectResponse:
        session_id = _ensure_session_id(request)
        jobs = job_store.list_jobs()
        overrides = job_store.list_session_overrides(session_id)
        for job in jobs:
            override = overrides.get(job["id"])
            data, selected_template, selected_printer = _resolve_editor_state(
                job,
                override,
            )
            try:
                _render_and_store_preview(
                    config=config,
                    store=job_store,
                    report_client=report_client,
                    session_id=session_id,
                    job_id=job["id"],
                    data=data,
                    selected_template=selected_template,
                    selected_printer=selected_printer,
                )
            except RuntimeError as exc:
                job_store.save_session_override(
                    session_id=session_id,
                    job_id=job["id"],
                    data=data,
                    selected_template=selected_template,
                    selected_printer=selected_printer,
                    preview_data_url=None,
                    render_error=str(exc),
                )
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/{job_id}/preview-from-list")
    async def render_preview_from_list(
        request: Request,
        job_id: int,
    ) -> RedirectResponse:
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        override = job_store.get_session_override(session_id, job_id)
        data, selected_template, selected_printer = _resolve_editor_state(job, override)
        form = await request.form()
        try:
            selected_template = _validate_selection(
                str(form.get("selected_template", selected_template)),
                config.template_names(),
                "template",
            )
            selected_printer = _validate_selection(
                str(form.get("selected_printer", selected_printer)),
                config.printer_names(),
                "printer",
            )
        except ValueError as exc:
            job_store.save_session_override(
                session_id=session_id,
                job_id=job_id,
                data=data,
                selected_template=selected_template,
                selected_printer=selected_printer,
                preview_data_url=None,
                render_error=str(exc),
            )
            return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)
        try:
            _render_and_store_preview(
                config=config,
                store=job_store,
                report_client=report_client,
                session_id=session_id,
                job_id=job_id,
                data=data,
                selected_template=selected_template,
                selected_printer=selected_printer,
            )
        except RuntimeError as exc:
            job_store.save_session_override(
                session_id=session_id,
                job_id=job_id,
                data=data,
                selected_template=selected_template,
                selected_printer=selected_printer,
                preview_data_url=None,
                render_error=str(exc),
            )
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/{job_id}/draft", response_class=HTMLResponse)
    async def save_session_draft(request: Request, job_id: int) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        try:
            data, selected_template, selected_printer = await _read_editor_submission(
                request,
                config=config,
                job=job,
            )
        except (TypeError, ValueError) as exc:
            override = job_store.get_session_override(session_id, job_id)
            return _render_job_page(
                request,
                config=config,
                job=job,
                override=override,
                error=str(exc),
            )

        job_store.save_session_override(
            session_id=session_id,
            job_id=job_id,
            data=data,
            selected_template=selected_template,
            selected_printer=selected_printer,
        )
        return RedirectResponse(
            f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/preview", response_class=HTMLResponse)
    async def render_preview(request: Request, job_id: int) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        try:
            data, selected_template, selected_printer = await _read_editor_submission(
                request,
                config=config,
                job=job,
            )
        except (TypeError, ValueError) as exc:
            override = job_store.get_session_override(session_id, job_id)
            return _render_job_page(
                request,
                config=config,
                job=job,
                override=override,
                error=str(exc),
            )

        try:
            _render_and_store_preview(
                config=config,
                store=job_store,
                report_client=report_client,
                session_id=session_id,
                job_id=job_id,
                data=data,
                selected_template=selected_template,
                selected_printer=selected_printer,
            )
        except RuntimeError as exc:
            job_store.save_session_override(
                session_id=session_id,
                job_id=job_id,
                data=data,
                selected_template=selected_template,
                selected_printer=selected_printer,
                preview_data_url=None,
                render_error=str(exc),
            )
            override = job_store.get_session_override(session_id, job_id)
            return _render_job_page(
                request,
                config=config,
                job=job,
                override=override,
                response_status=status.HTTP_502_BAD_GATEWAY,
            )

        override = job_store.get_session_override(session_id, job_id)
        return _render_job_page(request, config=config, job=job, override=override)

    @app.post("/jobs/{job_id}/clear-session")
    async def clear_session_draft(job_id: int, request: Request) -> RedirectResponse:
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        job_store.clear_session_override(session_id, job_id)
        return RedirectResponse(
            f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return app
