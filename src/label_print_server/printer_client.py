from __future__ import annotations

import logging
import time
from io import BytesIO
from typing import Any, Protocol

from label_print_server.config import PrinterConfig, RetryConfig

logger = logging.getLogger(__name__)


class PrintClient(Protocol):
    def print_label(self, printer: PrinterConfig, image_bytes: bytes) -> None: ...


class NiimprintPrintClient:
    """Adapter around the `niimprint` package.

    `niimprint` is kept as an unmodified external dependency (see
    mds/ConnectNiimPrint.md for the investigation behind this). The known
    Bluetooth response-drop quirk around PrintStart/PrintEnd is not patched
    inside niimprint; instead it is absorbed here with a bounded
    connect-and-print retry loop, driven by `AppConfig.retry`.

    A fresh transport/connection is opened for each attempt (and each call):
    niimprint's transports have no `close()`, so on failure the simplest safe
    recovery is to drop the object and reconnect rather than try to reuse a
    possibly-wedged socket.
    """

    def __init__(self, retry: RetryConfig):
        self.retry = retry

    def print_label(self, printer: PrinterConfig, image_bytes: bytes) -> None:
        # Imported lazily so importing this module (and running the test
        # suite with a fake PrintClient) never requires `niimprint` to be
        # importable.
        from PIL import Image

        from niimprint import BluetoothTransport, PrinterClientFixed, SerialTransport

        # Config problems (bad connection type, missing address) can't be
        # fixed by retrying, so fail fast instead of burning the retry budget.
        self._validate_printer(printer)

        image = Image.open(BytesIO(image_bytes))
        attempts = max(1, self.retry.max_attempts)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                transport = self._open_transport(printer, BluetoothTransport, SerialTransport)
                client = PrinterClientFixed(transport)
                client.print_image(image, density=printer.density)
                return
            except Exception as exc:  # noqa: BLE001 - niimprint raises loose/bare exceptions
                last_error = exc
                logger.warning(
                    "print attempt %s/%s failed for printer '%s': %s",
                    attempt,
                    attempts,
                    printer.name,
                    exc,
                )
                if attempt < attempts:
                    time.sleep(self.retry.delay_seconds)

        raise RuntimeError(
            f"Failed to print on '{printer.name}' after {attempts} attempt(s): {last_error}"
        ) from last_error

    @staticmethod
    def _validate_printer(printer: PrinterConfig) -> None:
        if printer.connection not in ("bluetooth", "usb"):
            raise ValueError(
                f"Printer '{printer.name}' has unknown connection type: {printer.connection}"
            )
        if printer.connection == "bluetooth" and not printer.address:
            raise ValueError(
                f"Printer '{printer.name}' has no bluetooth address configured"
            )

    @staticmethod
    def _open_transport(
        printer: PrinterConfig,
        bluetooth_transport_cls: Any,
        serial_transport_cls: Any,
    ) -> Any:
        if printer.connection == "usb":
            return serial_transport_cls(port=printer.address or "auto")
        return bluetooth_transport_cls(printer.address)
