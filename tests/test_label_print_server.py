from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from label_print_server.app import create_app


class FakeRenderer:
    def render_preview(self, template, data):
        return f"{template.name}:{data['name']}:{data['name_line']}".encode()


class FakePrintClient:
    def __init__(self, fail_times: int = 0, error_message: str = "printer offline"):
        self.calls: list[tuple[str, bytes, int]] = []
        self.fail_times = fail_times
        self.error_message = error_message

    def print_label(self, printer, image_bytes, quantity=1):
        self.calls.append((printer.name, image_bytes, quantity))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(self.error_message)


class LabelPrintServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        hooks_path = root / "hooks.py"
        hooks_path.write_text(
            """def transform(data):
    items = [data] if isinstance(data, dict) else list(data)
    return [
        {**item, "name_line": f"hooked {item['name']}"}
        for item in items
    ]

def summary_text(data):
    return f"SUMMARY:{data['name']}"
""",
            encoding="utf-8",
        )

        config = {
            "app_name": "Test Label Server",
            "database_path": "jobs.sqlite3",
            "session_secret": "test-secret",
            "jsreport_url": "http://127.0.0.1:5488",
            "debug": True,
            "summary_key": "ignored-by-hook",
            "user_hooks_path": str(hooks_path),
            "printers": [
                {"name": "printer-a", "model": "b1"},
                {"name": "printer-b", "model": "b21"},
            ],
            "templates": [{"name": "Label"}, {"name": "AltLabel"}],
            "selection": {
                "default_printer": "printer-a",
                "default_template": "Label",
            },
        }
        self.config_path = root / "config.json"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.database_path = root / "jobs.sqlite3"
        self.print_client = FakePrintClient()
        self.client = TestClient(
            create_app(
                self.config_path,
                self.database_path,
                render_client=FakeRenderer(),
                print_client=self.print_client,
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_api_accepts_object_and_applies_hook_transform(self) -> None:
        response = self.client.post(
            "/api/jobs",
            json={"id": "000-011", "name": "USB-C Hub"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["jobs"]), 1)

        jobs_response = self.client.get("/api/jobs")
        self.assertEqual(jobs_response.status_code, 200)
        job = jobs_response.json()["jobs"][0]
        self.assertEqual(job["selected_template"], "Label")
        self.assertEqual(job["selected_printer"], "printer-a")
        self.assertEqual(job["draft"]["name_line"], "hooked USB-C Hub")

    def test_api_accepts_list_and_splits_into_jobs(self) -> None:
        response = self.client.post(
            "/api/jobs",
            json=[
                {"id": "000-013", "name": "First"},
                {"id": "000-014", "name": "Second"},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["jobs"]), 2)

        jobs_response = self.client.get("/api/jobs")
        self.assertEqual(len(jobs_response.json()["jobs"]), 2)

    def test_preview_uses_session_override_data(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-012", "name": "Original"},
        )

        response = self.client.post(
            "/jobs/1/preview",
            data={
                "data_json": json.dumps(
                    {"id": "000-012", "name": "Updated", "name_line": "name is Updated"}
                ),
                "selected_template": "Label",
                "selected_printer": "printer-a",
            },
        )
        self.assertEqual(response.status_code, 200)
        expected = base64.b64encode(b"Label:Updated:name is Updated").decode("ascii")
        self.assertIn(expected, response.text)

    def test_jobs_page_supports_bulk_render(self) -> None:
        self.client.post(
            "/api/jobs",
            json=[
                {"id": "000-013", "name": "First"},
                {"id": "000-014", "name": "Second"},
            ],
        )

        response = self.client.post("/jobs/render-all", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            base64.b64encode(b"Label:First:hooked First").decode("ascii"),
            response.text,
        )
        self.assertIn(
            base64.b64encode(b"Label:Second:hooked Second").decode("ascii"),
            response.text,
        )
        self.assertIn("SUMMARY:First", response.text)

    def test_list_render_uses_selected_dropdown_values(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-015", "name": "Selectable"},
        )

        response = self.client.post(
            "/jobs/1/preview-from-list",
            data={
                "selected_template": "AltLabel",
                "selected_printer": "printer-b",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            base64.b64encode(b"AltLabel:Selectable:hooked Selectable").decode("ascii"),
            response.text,
        )
        self.assertIn('value="printer-b" selected', response.text)

    def test_dequeue_actions_remove_jobs(self) -> None:
        self.client.post(
            "/api/jobs",
            json=[
                {"id": "000-016", "name": "First"},
                {"id": "000-017", "name": "Second"},
            ],
        )

        response = self.client.post("/jobs/1/dequeue", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('href="/jobs/1"', response.text)
        self.assertIn('href="/jobs/2"', response.text)

        response = self.client.post("/jobs/dequeue-all", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("No queued jobs yet.", response.text)

    def test_print_dequeues_job_on_success(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-018", "name": "PrintMe"},
        )
        self.client.post(
            "/jobs/1/preview-from-list",
            data={"selected_template": "Label", "selected_printer": "printer-a"},
            follow_redirects=True,
        )

        response = self.client.post("/jobs/1/print", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("No queued jobs yet.", response.text)
        self.assertEqual(len(self.print_client.calls), 1)
        self.assertEqual(self.print_client.calls[0][0], "printer-a")

    def test_print_all_dequeues_rendered_jobs_and_leaves_others_queued(self) -> None:
        self.client.post(
            "/api/jobs",
            json=[
                {"id": "000-021", "name": "First"},
                {"id": "000-022", "name": "Second"},
            ],
        )
        self.client.post("/jobs/render-all", follow_redirects=True)
        self.print_client.fail_times = 1

        response = self.client.post("/jobs/print-all", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.print_client.calls), 2)

        jobs_response = self.client.get("/api/jobs")
        remaining = jobs_response.json()["jobs"]
        self.assertEqual(len(remaining), 1)
        self.assertIn("printer offline", response.text)

    def test_print_without_preview_shows_error(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-019", "name": "NoPreview"},
        )

        response = self.client.post("/jobs/1/print")
        self.assertEqual(response.status_code, 502)
        self.assertIn("Render a preview before printing.", response.text)
        self.assertEqual(self.print_client.calls, [])

        jobs_response = self.client.get("/api/jobs")
        self.assertEqual(len(jobs_response.json()["jobs"]), 1)

    def test_print_failure_keeps_job_queued_with_error(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-020", "name": "Flaky"},
        )
        self.client.post(
            "/jobs/1/preview-from-list",
            data={"selected_template": "Label", "selected_printer": "printer-a"},
            follow_redirects=True,
        )
        self.print_client.fail_times = 1

        response = self.client.post("/jobs/1/print-from-list", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("printer offline", response.text)
        jobs_response = self.client.get("/api/jobs")
        self.assertEqual(len(jobs_response.json()["jobs"]), 1)

        response = self.client.post("/jobs/1/print-from-list", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("No queued jobs yet.", response.text)

    def test_print_from_list_uses_submitted_quantity(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-023", "name": "Multi"},
        )
        self.client.post(
            "/jobs/1/preview-from-list",
            data={"selected_template": "Label", "selected_printer": "printer-a"},
            follow_redirects=True,
        )

        response = self.client.post(
            "/jobs/1/print-from-list",
            data={"quantity": "3"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.print_client.calls), 1)
        self.assertEqual(self.print_client.calls[0][2], 3)

    def test_print_from_list_rejects_invalid_quantity(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-024", "name": "BadQty"},
        )
        self.client.post(
            "/jobs/1/preview-from-list",
            data={"selected_template": "Label", "selected_printer": "printer-a"},
            follow_redirects=True,
        )

        response = self.client.post(
            "/jobs/1/print-from-list",
            data={"quantity": "0"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Quantity must be between 1 and 100", response.text)
        self.assertEqual(self.print_client.calls, [])

        jobs_response = self.client.get("/api/jobs")
        self.assertEqual(len(jobs_response.json()["jobs"]), 1)

    def test_detail_print_uses_submitted_quantity(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-025", "name": "DetailQty"},
        )
        self.client.post(
            "/jobs/1/preview",
            data={
                "data_json": json.dumps(
                    {"id": "000-025", "name": "DetailQty", "name_line": "x"}
                ),
                "selected_template": "Label",
                "selected_printer": "printer-a",
            },
        )

        response = self.client.post(
            "/jobs/1/print",
            data={"quantity": "5"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.print_client.calls), 1)
        self.assertEqual(self.print_client.calls[0][2], 5)

    def test_print_defaults_to_quantity_one_when_omitted(self) -> None:
        self.client.post(
            "/api/jobs",
            json={"id": "000-026", "name": "DefaultQty"},
        )
        self.client.post(
            "/jobs/1/preview-from-list",
            data={"selected_template": "Label", "selected_printer": "printer-a"},
            follow_redirects=True,
        )

        response = self.client.post("/jobs/1/print", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.print_client.calls[0][2], 1)

    def test_intake_form_hidden_when_debug_disabled(self) -> None:
        root = Path(self.temp_dir.name)
        config = {
            "app_name": "Prod Label Server",
            "database_path": "prod.sqlite3",
            "session_secret": "test-secret",
            "jsreport_url": "http://127.0.0.1:5488",
            "debug": False,
            "printers": [{"name": "printer-a", "model": "b1"}],
            "templates": [{"name": "Label"}],
            "selection": {
                "default_printer": "printer-a",
                "default_template": "Label",
            },
        }
        config_path = root / "prod-config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        prod_client = TestClient(
            create_app(
                config_path,
                root / "prod.sqlite3",
                render_client=FakeRenderer(),
            )
        )
        try:
            response = prod_client.get("/jobs")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Queue intake", response.text)

            response = prod_client.post(
                "/jobs/intake",
                data={"payload_json": '{"id":"1","name":"X"}'},
            )
            self.assertEqual(response.status_code, 404)

            response = prod_client.post(
                "/api/jobs",
                json={"id": "1", "name": "X"},
            )
            self.assertEqual(response.status_code, 200)
        finally:
            prod_client.close()
