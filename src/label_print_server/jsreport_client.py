from __future__ import annotations

from typing import Any

import httpx

from label_print_server.config import TemplateConfig


class JsReportClient:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def render_preview(
        self,
        template: TemplateConfig,
        data: dict[str, Any],
    ) -> bytes:
        payload = {
            "template": {
                "name": template.name,
            },
            "data": data,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f"{self.base_url}/api/report", json=payload)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"jsreport request failed: {exc}") from exc

        if response.is_error:
            raise RuntimeError(
                f"jsreport render failed with {response.status_code}: "
                f"{response.text[:500]}"
            )
        return response.content
