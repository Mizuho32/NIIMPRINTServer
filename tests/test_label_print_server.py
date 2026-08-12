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


class LabelPrintServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        template_path = root / "label.html"
        template_path.write_text("<div>{{name}}</div>", encoding="utf-8")

        config = {
            "app_name": "Test Label Server",
            "database_path": "jobs.sqlite3",
            "session_secret": "test-secret",
            "jsreport_url": "http://127.0.0.1:5488",
            "summary_key": "name",
            "printers": [
                {"name": "printer-a", "model": "b1"},
                {"name": "printer-b", "model": "b21"},
            ],
            "templates": [{"name": "Label"}, {"name": "AltLabel"}],
            "transforms": [{"name": "name_line", "template": "name is {{ name }}"}],
            "selection": {
                "default_printer": "printer-a",
                "default_template": "Label",
            },
        }
        self.config_path = root / "config.json"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.database_path = root / "jobs.sqlite3"
        self.client = TestClient(
            create_app(
                self.config_path,
                self.database_path,
                render_client=FakeRenderer(),
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_api_intake_applies_transforms_and_defaults(self) -> None:
        response = self.client.post(
            "/api/jobs",
            json=[{"id": "000-011", "name": "USB-C Hub"}],
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["jobs"]), 1)

        jobs_response = self.client.get("/api/jobs")
        self.assertEqual(jobs_response.status_code, 200)
        job = jobs_response.json()["jobs"][0]
        self.assertEqual(job["selected_template"], "Label")
        self.assertEqual(job["selected_printer"], "printer-a")
        self.assertEqual(job["draft"]["name_line"], "name is USB-C Hub")

    def test_preview_uses_session_override_data(self) -> None:
        self.client.post(
            "/api/jobs",
            json=[{"id": "000-012", "name": "Original"}],
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

        detail = self.client.get("/jobs/1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(expected, detail.text)

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
        self.assertIn("Render all queued jobs", response.text)
        self.assertIn(
            base64.b64encode(b"Label:First:name is First").decode("ascii"),
            response.text,
        )
        self.assertIn(
            base64.b64encode(b"Label:Second:name is Second").decode("ascii"),
            response.text,
        )

    def test_list_render_uses_selected_dropdown_values(self) -> None:
        self.client.post(
            "/api/jobs",
            json=[{"id": "000-015", "name": "Selectable"}],
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
            base64.b64encode(b"AltLabel:Selectable:name is Selectable").decode("ascii"),
            response.text,
        )
        self.assertIn('value="printer-b" selected', response.text)


if __name__ == "__main__":
    unittest.main()
