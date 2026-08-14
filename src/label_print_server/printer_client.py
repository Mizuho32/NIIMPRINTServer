from __future__ import annotations

import logging
import threading
import time
from io import BytesIO
from typing import Any, Protocol

from label_print_server.config import PrinterConfig, RetryConfig

logger = logging.getLogger(__name__)


class PrintClient(Protocol):
    def print_label(
        self, printer: PrinterConfig, image_bytes: bytes, quantity: int = 1
    ) -> None: ...


class NiimprintPrintClient:
    """Adapter around the `niimprint` package.

    `niimprint` is kept as an unmodified external dependency (see
    mds/ConnectNiimPrint.md for the investigation behind this).

    Real-hardware testing (see mds/ConnectNiimPrint.md section 5) showed that
    reconnecting for every single print is both slow and actively harmful:
    the very first connect attempt after a print almost always hits
    `ECONNRESET` (the printer/OS needs a moment to tear down the previous
    connection), and printing several labels back-to-back eventually hits
    `EBUSY` because old sockets were never explicitly closed.

    So this client holds one persistent connection per printer name and
    reuses it across prints, instead of reconnecting every call. A
    connection is only dropped and re-established when a print actually
    fails, and even then a settle delay (`AppConfig.retry.delay_seconds`) is
    enforced before reconnecting, tracked per-printer from the moment the
    connection was dropped. Access per printer is serialized with a lock
    since FastAPI's sync route handlers can run on a thread pool.

    `quantity` (multiple copies of the same label) is implemented as a loop
    of independent `print_image()` calls rather than niimprint's native
    copies-count field, so each copy gets its own retry budget. If a copy
    fails after exhausting retries, the whole call raises and any remaining
    copies are not attempted (the caller sees the job as failed and it stays
    queued; printing again reprints from copy 1).
    """

    def __init__(self, retry: RetryConfig):
        self.retry = retry
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._clients: dict[str, Any] = {}
        self._last_disconnect_at: dict[str, float] = {}

    def print_label(
        self, printer: PrinterConfig, image_bytes: bytes, quantity: int = 1
    ) -> None:
        from PIL import Image

        # Config problems (bad connection type, missing address) can't be
        # fixed by retrying, so fail fast instead of burning the retry budget.
        self._validate_printer(printer)

        count = max(1, quantity)
        image = Image.open(BytesIO(image_bytes))

        # One lock acquisition for the whole batch: copies of the same job
        # print back-to-back on this printer without another job's print
        # interleaving in between.
        with self._lock_for(printer.name):
            for copy_index in range(1, count + 1):
                self._print_one(printer, image, copy_index, count)

    def _print_one(self, printer: PrinterConfig, image: Any, copy_index: int, count: int) -> None:
        attempts = max(1, self.retry.max_attempts)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                client = self._get_or_connect(printer)
                client.print_image(image, density=printer.density)
                return
            except Exception as exc:  # noqa: BLE001 - niimprint raises loose/bare exceptions
                last_error = exc
                logger.warning(
                    "print attempt %s/%s failed for printer '%s' (copy %s/%s): %s",
                    attempt,
                    attempts,
                    printer.name,
                    copy_index,
                    count,
                    exc,
                )
                # The connection may be wedged (or the printer/OS may need to
                # finish tearing it down); drop it so the next attempt
                # reconnects instead of reusing a bad socket.
                self._discard_connection(printer.name)

        raise RuntimeError(
            f"Failed to print copy {copy_index}/{count} on '{printer.name}' "
            f"after {attempts} attempt(s): {last_error}"
        ) from last_error

    def close(self) -> None:
        """Close every held connection. Call on server shutdown."""
        with self._locks_guard:
            printer_names = list(self._clients)
        for printer_name in printer_names:
            self._discard_connection(printer_name)

    def _lock_for(self, printer_name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(printer_name)
            if lock is None:
                lock = threading.Lock()
                self._locks[printer_name] = lock
            return lock

    def _get_or_connect(self, printer: PrinterConfig) -> Any:
        client = self._clients.get(printer.name)
        if client is not None:
            return client

        self._sleep_until_settled(printer.name)

        # Imported lazily so importing this module (and running the test
        # suite with a fake PrintClient) never requires `niimprint` to be
        # importable.
        from niimprint import BluetoothTransport, PrinterClientFixed, SerialTransport

        transport = self._open_transport(printer, BluetoothTransport, SerialTransport)
        client = PrinterClientFixed(transport)
        self._clients[printer.name] = client
        return client

    def _discard_connection(self, printer_name: str) -> None:
        client = self._clients.pop(printer_name, None)
        if client is not None:
            self._close_client(client)
        self._last_disconnect_at[printer_name] = self._now()

    def _sleep_until_settled(self, printer_name: str) -> None:
        last_disconnect = self._last_disconnect_at.get(printer_name)
        if last_disconnect is None:
            return
        remaining = self.retry.delay_seconds - (self._now() - last_disconnect)
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _close_client(client: Any) -> None:
        transport = getattr(client, "_transport", None)
        sock = getattr(transport, "_sock", None)  # BluetoothTransport
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            return
        serial_conn = getattr(transport, "_serial", None)  # SerialTransport
        if serial_conn is not None:
            try:
                serial_conn.close()
            except OSError:
                pass

    @staticmethod
    def _now() -> float:
        return time.time()

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
