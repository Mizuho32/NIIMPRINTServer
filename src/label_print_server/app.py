from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from label_print_server.config import AppConfig, dump_json, load_config
from label_print_server.jsreport_client import JsReportClient
from label_print_server.pipeline import (
    load_summary_hook,
    load_transform_hook,
    parse_transform_input,
    resolve_initial_jobs,
)
from label_print_server.printer_client import NiimprintPrintClient, PrintClient
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


def _parse_payload_input(raw_text: str) -> dict[str, Any] | list[Any]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg}") from exc
    return parse_transform_input(payload)


def _job_summary(
    job_data: dict[str, Any],
    summary_key: str | None,
    summary_hook: Any,
) -> str:
    if summary_hook is not None:
        summary_value = summary_hook(job_data)
        if not isinstance(summary_value, str):
            raise TypeError("summary_text must return a string")
        if summary_value:
            return summary_value
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
    if isinstance(payload, dict):
        for key in ("name", "id", "title", "label"):
            value = payload.get(key)
            if value:
                return str(value)
    return dump_json(payload).replace("\n", " ")[:60]


def _resolve_editor_state(
    job: dict[str, Any],
    override: dict[str, Any] | None,
) -> tuple[dict[str, Any], str, str, int]:
    if override is None:
        return (
            job["draft"],
            job["selected_template"],
            job["selected_printer"],
            1,
        )
    return (
        override["data"],
        override["selected_template"],
        override["selected_printer"],
        override["quantity"],
    )


_MAX_PRINT_QUANTITY = 100


def _validate_quantity(raw: Any) -> int:
    try:
        quantity = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid quantity: {raw!r}") from exc
    if not (1 <= quantity <= _MAX_PRINT_QUANTITY):
        raise ValueError(f"Quantity must be between 1 and {_MAX_PRINT_QUANTITY}")
    return quantity


def _preview_data_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _preview_image_bytes(override: dict[str, Any] | None) -> bytes | None:
    if override is None:
        return None
    data_url = override.get("preview_data_url")
    if not data_url:
        return None
    _, _, encoded = data_url.partition(",")
    return base64.b64decode(encoded)


def _render_jobs_page(
    request: Request,
    *,
    session_id: str,
    config: AppConfig,
    store: JobStore,
    summary_hook: Any,
    payload_text: str = "[]",
    error: str | None = None,
) -> HTMLResponse:
    jobs = store.list_jobs()
    overrides = store.list_session_overrides(session_id)
    job_cards = []
    for job in jobs:
        override = overrides.get(job["id"])
        data, selected_template, selected_printer, quantity = _resolve_editor_state(
            job, override
        )
        try:
            summary_text = _job_summary(data, config.summary_key, summary_hook)
        except TypeError:
            summary_text = _fallback_job_summary(job)
        if not summary_text:
            summary_text = _fallback_job_summary(job)
        job_cards.append(
            {
                **job,
                "data": data,
                "summary_text": summary_text,
                "selected_template": selected_template,
                "selected_printer": selected_printer,
                "quantity": quantity,
                "preview_data_url": (
                    None if override is None else override["preview_data_url"]
                ),
                "render_error": None if override is None else override["render_error"],
                "print_error": None if override is None else override["print_error"],
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
            "debug": config.debug,
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
    data, selected_template, selected_printer, quantity = _resolve_editor_state(
        job, override
    )
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
            "quantity": quantity,
            "templates": config.template_names(),
            "printers": config.printer_names(),
            "preview_data_url": (
                None if override is None else override["preview_data_url"]
            ),
            "render_error": error
            or (None if override is None else override["render_error"]),
            "print_error": None if override is None else override["print_error"],
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
    data = json.loads(str(form.get("data_json", "")))
    if not isinstance(data, dict):
        raise TypeError("Expected a JSON object")
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
    payload_input: dict[str, Any] | list[Any],
    transform_hook: Any,
) -> dict[str, Any]:
    batch_id = uuid4().hex[:12]
    jobs = []
    resolved_jobs = resolve_initial_jobs(payload_input, config, transform_hook)
    for index, job_spec in enumerate(resolved_jobs, start=1):
        job_id = store.create_job(
            batch_id=batch_id,
            batch_index=index,
            payload=job_spec["payload"],
            draft=job_spec["draft"],
            selected_template=job_spec["selected_template"],
            selected_printer=job_spec["selected_printer"],
        )
        jobs.append(
            {
                "id": job_id,
                "selected_template": job_spec["selected_template"],
                "selected_printer": job_spec["selected_printer"],
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
    quantity: int,
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
        quantity=quantity,
        preview_data_url=_preview_data_url(image_bytes),
        render_error=None,
        print_error=None,
    )


def _print_and_dequeue(
    *,
    config: AppConfig,
    store: JobStore,
    print_client: PrintClient,
    session_id: str,
    job_id: int,
    data: dict[str, Any],
    selected_template: str,
    selected_printer: str,
    quantity: int,
    override: dict[str, Any] | None,
) -> str | None:
    """Print the currently-rendered preview and dequeue on success.

    Returns an error message on failure (and persists it on the session
    override so it survives the redirect); returns None on success, after
    the job has been deleted from the queue.
    """
    image_bytes = _preview_image_bytes(override)
    if image_bytes is None:
        error = "Render a preview before printing."
    else:
        try:
            printer_config = config.printer_by_name(selected_printer)
            print_client.print_label(printer_config, image_bytes, quantity=quantity)
        except (KeyError, ValueError, RuntimeError) as exc:
            error = str(exc)
        else:
            store.delete_job(job_id)
            return None

    store.save_session_override(
        session_id=session_id,
        job_id=job_id,
        data=data,
        selected_template=selected_template,
        selected_printer=selected_printer,
        quantity=quantity,
        preview_data_url=None if override is None else override["preview_data_url"],
        render_error=None if override is None else override["render_error"],
        print_error=error,
    )
    return error


def create_app(
    config_path: str | Path,
    database_path: str | Path | None = None,
    *,
    render_client: JsReportClient | None = None,
    print_client: PrintClient | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    config = load_config(config_path)
    transform_hook = load_transform_hook(config.user_hooks_path)
    summary_hook = load_summary_hook(config.user_hooks_path)
    effective_database = (
        Path(database_path) if database_path is not None else config.database_path
    )
    job_store = store or JobStore(effective_database)
    report_client = render_client or JsReportClient(config.jsreport_url)
    printer_client = print_client or NiimprintPrintClient(config.retry)

    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        yield
        # NiimprintPrintClient holds one persistent connection per printer
        # (see mds/ConnectNiimPrint.md); close them on shutdown instead of
        # leaving sockets to the GC. Fake print clients used in tests don't
        # need this, hence the duck-typed close().
        close = getattr(printer_client, "close", None)
        if callable(close):
            close()

    app = FastAPI(title=config.app_name, lifespan=_lifespan)
    app.add_middleware(SessionMiddleware, secret_key=config.session_secret)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        return _render_jobs_page(
            request,
            session_id=session_id,
            config=config,
            store=job_store,
            summary_hook=summary_hook,
        )

    @app.post("/jobs/intake", response_class=HTMLResponse)
    async def intake_jobs_form(request: Request) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        if not config.debug:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        form = await request.form()
        payload_text = str(form.get("payload_json", ""))
        try:
            payload_input = _parse_payload_input(payload_text)
            _enqueue_jobs(job_store, config, payload_input, transform_hook)
        except (TypeError, ValueError) as exc:
            return _render_jobs_page(
                request,
                session_id=session_id,
                config=config,
                store=job_store,
                summary_hook=summary_hook,
                payload_text=payload_text,
                error=str(exc),
            )
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/api/jobs")
    async def intake_jobs_api(request: Request) -> JSONResponse:
        try:
            payload_input = parse_transform_input(await request.json())
            result = _enqueue_jobs(job_store, config, payload_input, transform_hook)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON: {exc.msg}",
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
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

    @app.post("/jobs/dequeue-all")
    async def dequeue_all_jobs() -> RedirectResponse:
        job_store.delete_all_jobs()
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/{job_id}/dequeue")
    async def dequeue_job(job_id: int) -> RedirectResponse:
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        job_store.delete_job(job_id)
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/render-all")
    async def render_all_jobs(request: Request) -> RedirectResponse:
        session_id = _ensure_session_id(request)
        jobs = job_store.list_jobs()
        overrides = job_store.list_session_overrides(session_id)
        for job in jobs:
            override = overrides.get(job["id"])
            data, selected_template, selected_printer, quantity = _resolve_editor_state(
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
                    quantity=quantity,
                )
            except RuntimeError as exc:
                job_store.save_session_override(
                    session_id=session_id,
                    job_id=job["id"],
                    data=data,
                    selected_template=selected_template,
                    selected_printer=selected_printer,
                    quantity=quantity,
                    preview_data_url=None,
                    render_error=str(exc),
                )
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/print-all")
    async def print_all_jobs(request: Request) -> RedirectResponse:
        session_id = _ensure_session_id(request)
        jobs = job_store.list_jobs()
        overrides = job_store.list_session_overrides(session_id)
        for job in jobs:
            override = overrides.get(job["id"])
            data, selected_template, selected_printer, quantity = _resolve_editor_state(
                job,
                override,
            )
            _print_and_dequeue(
                config=config,
                store=job_store,
                print_client=printer_client,
                session_id=session_id,
                job_id=job["id"],
                data=data,
                selected_template=selected_template,
                selected_printer=selected_printer,
                quantity=quantity,
                override=override,
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
        data, selected_template, selected_printer, quantity = _resolve_editor_state(
            job, override
        )
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
                quantity=quantity,
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
                quantity=quantity,
            )
        except RuntimeError as exc:
            job_store.save_session_override(
                session_id=session_id,
                job_id=job_id,
                data=data,
                selected_template=selected_template,
                selected_printer=selected_printer,
                quantity=quantity,
                preview_data_url=None,
                render_error=str(exc),
            )
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/{job_id}/quantity")
    async def save_quantity(request: Request, job_id: int) -> JSONResponse:
        """Persist a print quantity as soon as the operator changes it.

        Called by the small autosave script in base.html on the input's
        `input` event, instead of only saving quantity as a side effect of
        clicking Print. Without this, an edited-but-not-yet-printed
        quantity is invisible to /jobs/print-all (which only ever looks at
        the persisted value) and is lost if the page is closed before
        printing - the same "survive an accidental close" guarantee the
        other session-stored fields already have.
        """
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        override = job_store.get_session_override(session_id, job_id)
        data, selected_template, selected_printer, _ = _resolve_editor_state(
            job, override
        )
        form = await request.form()
        try:
            quantity = _validate_quantity(form.get("quantity"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        job_store.save_session_override(
            session_id=session_id,
            job_id=job_id,
            data=data,
            selected_template=selected_template,
            selected_printer=selected_printer,
            quantity=quantity,
            preview_data_url=None if override is None else override["preview_data_url"],
            render_error=None if override is None else override["render_error"],
            print_error=None if override is None else override["print_error"],
        )
        return JSONResponse({"quantity": quantity})

    @app.post("/jobs/{job_id}/print-from-list")
    async def print_job_from_list(request: Request, job_id: int) -> RedirectResponse:
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        override = job_store.get_session_override(session_id, job_id)
        data, selected_template, selected_printer, quantity = _resolve_editor_state(
            job, override
        )
        _print_and_dequeue(
            config=config,
            store=job_store,
            print_client=printer_client,
            session_id=session_id,
            job_id=job_id,
            data=data,
            selected_template=selected_template,
            selected_printer=selected_printer,
            quantity=quantity,
            override=override,
        )
        return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/{job_id}/draft", response_class=HTMLResponse)
    async def save_session_draft(request: Request, job_id: int) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        override = job_store.get_session_override(session_id, job_id)
        try:
            data, selected_template, selected_printer = await _read_editor_submission(
                request,
                config=config,
                job=job,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return _render_job_page(
                request,
                config=config,
                job=job,
                override=override,
                error=str(exc),
            )

        # This form doesn't carry quantity (that's only on the Print form),
        # so keep whatever was already set instead of resetting it to 1.
        quantity = 1 if override is None else override["quantity"]
        job_store.save_session_override(
            session_id=session_id,
            job_id=job_id,
            data=data,
            selected_template=selected_template,
            selected_printer=selected_printer,
            quantity=quantity,
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
        override = job_store.get_session_override(session_id, job_id)
        try:
            data, selected_template, selected_printer = await _read_editor_submission(
                request,
                config=config,
                job=job,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return _render_job_page(
                request,
                config=config,
                job=job,
                override=override,
                error=str(exc),
            )

        # Preserve quantity across a render; it's only set from the Print form.
        quantity = 1 if override is None else override["quantity"]
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
                quantity=quantity,
            )
        except RuntimeError as exc:
            job_store.save_session_override(
                session_id=session_id,
                job_id=job_id,
                data=data,
                selected_template=selected_template,
                selected_printer=selected_printer,
                quantity=quantity,
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

    @app.post("/jobs/{job_id}/print", response_class=HTMLResponse)
    async def print_job(request: Request, job_id: int) -> HTMLResponse:
        session_id = _ensure_session_id(request)
        job = job_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        override = job_store.get_session_override(session_id, job_id)
        data, selected_template, selected_printer, quantity = _resolve_editor_state(
            job, override
        )
        error = _print_and_dequeue(
            config=config,
            store=job_store,
            print_client=printer_client,
            session_id=session_id,
            job_id=job_id,
            data=data,
            selected_template=selected_template,
            selected_printer=selected_printer,
            quantity=quantity,
            override=override,
        )
        if error is None:
            return RedirectResponse("/jobs", status_code=status.HTTP_303_SEE_OTHER)
        override = job_store.get_session_override(session_id, job_id)
        return _render_job_page(
            request,
            config=config,
            job=job,
            override=override,
            response_status=status.HTTP_502_BAD_GATEWAY,
        )

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
