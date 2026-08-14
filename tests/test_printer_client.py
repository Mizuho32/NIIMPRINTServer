from __future__ import annotations

import io
import sys
import types
import unittest
from typing import ClassVar
from unittest import mock

from PIL import Image

from label_print_server.config import PrinterConfig, RetryConfig
from label_print_server.printer_client import NiimprintPrintClient


def _fake_png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeTransport:
    """Mirrors niimprint.BluetoothTransport/SerialTransport just enough for
    NiimprintPrintClient's close-on-discard logic (which reaches into
    `_transport._sock`) to have something real to close."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self._sock = FakeSocket()


class FakePrinterClientFixed:
    """Stand-in for niimprint.PrinterClientFixed used to drive the retry loop."""

    fail_times = 0
    densities: ClassVar[list[int]] = []
    instances: ClassVar[list[FakePrinterClientFixed]] = []

    def __init__(self, transport):
        self._transport = transport
        type(self).instances.append(self)

    def print_image(self, image, density=5):
        type(self).densities.append(density)
        if type(self).fail_times > 0:
            type(self).fail_times -= 1
            # Mirrors the documented Bluetooth response-drop quirk, which
            # surfaces in niimprint as an AttributeError (see
            # mds/ConnectNiimPrint.md).
            raise AttributeError("simulated dropped response packet")


class NiimprintPrintClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fake_module = types.ModuleType("niimprint")
        fake_module.BluetoothTransport = FakeTransport
        fake_module.SerialTransport = FakeTransport
        fake_module.PrinterClientFixed = FakePrinterClientFixed
        self._module_patch = mock.patch.dict(sys.modules, {"niimprint": fake_module})
        self._module_patch.start()
        FakePrinterClientFixed.fail_times = 0
        FakePrinterClientFixed.densities = []
        FakePrinterClientFixed.instances = []
        self.printer = PrinterConfig(
            name="B1", model="b1", address="AA:BB:CC:DD:EE:FF", density=4
        )

    def tearDown(self) -> None:
        self._module_patch.stop()

    def test_succeeds_on_first_attempt(self) -> None:
        client = NiimprintPrintClient(RetryConfig(max_attempts=3, delay_seconds=0))
        client.print_label(self.printer, _fake_png_bytes())
        self.assertEqual(FakePrinterClientFixed.densities, [4])

    def test_retries_and_recovers_from_dropped_response(self) -> None:
        FakePrinterClientFixed.fail_times = 1
        client = NiimprintPrintClient(RetryConfig(max_attempts=3, delay_seconds=0))
        client.print_label(self.printer, _fake_png_bytes())
        self.assertEqual(len(FakePrinterClientFixed.densities), 2)

    def test_raises_after_exhausting_retries(self) -> None:
        FakePrinterClientFixed.fail_times = 99
        client = NiimprintPrintClient(RetryConfig(max_attempts=2, delay_seconds=0))
        with self.assertRaises(RuntimeError):
            client.print_label(self.printer, _fake_png_bytes())
        self.assertEqual(len(FakePrinterClientFixed.densities), 2)

    def test_missing_bluetooth_address_fails_fast_without_retrying(self) -> None:
        printer = PrinterConfig(name="B1", model="b1", address=None)
        client = NiimprintPrintClient(RetryConfig(max_attempts=3, delay_seconds=0))
        with self.assertRaises(ValueError):
            client.print_label(printer, _fake_png_bytes())
        self.assertEqual(FakePrinterClientFixed.densities, [])

    def test_usb_connection_uses_address_as_port(self) -> None:
        printer = PrinterConfig(
            name="USB-B1", model="b1", address="/dev/ttyUSB0", connection="usb"
        )
        client = NiimprintPrintClient(RetryConfig(max_attempts=1, delay_seconds=0))
        client.print_label(printer, _fake_png_bytes())
        self.assertEqual(FakePrinterClientFixed.densities, [5])

    def test_reuses_connection_across_successful_prints(self) -> None:
        client = NiimprintPrintClient(RetryConfig(max_attempts=3, delay_seconds=0))
        client.print_label(self.printer, _fake_png_bytes())
        client.print_label(self.printer, _fake_png_bytes())
        client.print_label(self.printer, _fake_png_bytes())
        # Three prints, but only one underlying connection was ever opened.
        self.assertEqual(len(FakePrinterClientFixed.instances), 1)
        self.assertEqual(len(FakePrinterClientFixed.densities), 3)
        self.assertFalse(FakePrinterClientFixed.instances[0]._transport._sock.closed)

    def test_discards_and_closes_connection_on_failure_then_reconnects(self) -> None:
        FakePrinterClientFixed.fail_times = 1
        client = NiimprintPrintClient(RetryConfig(max_attempts=2, delay_seconds=0))
        client.print_label(self.printer, _fake_png_bytes())
        self.assertEqual(len(FakePrinterClientFixed.instances), 2)
        self.assertTrue(FakePrinterClientFixed.instances[0]._transport._sock.closed)
        self.assertFalse(FakePrinterClientFixed.instances[1]._transport._sock.closed)

    def test_quantity_prints_multiple_copies_on_one_connection(self) -> None:
        client = NiimprintPrintClient(RetryConfig(max_attempts=2, delay_seconds=0))
        client.print_label(self.printer, _fake_png_bytes(), quantity=3)
        self.assertEqual(len(FakePrinterClientFixed.densities), 3)
        # All three copies reused the same connection - no reconnect needed
        # since none of them failed.
        self.assertEqual(len(FakePrinterClientFixed.instances), 1)

    def test_quantity_stops_after_a_copy_exhausts_retries(self) -> None:
        FakePrinterClientFixed.fail_times = 99  # every attempt fails
        client = NiimprintPrintClient(RetryConfig(max_attempts=2, delay_seconds=0))
        with self.assertRaises(RuntimeError) as ctx:
            client.print_label(self.printer, _fake_png_bytes(), quantity=3)
        self.assertIn("copy 1/3", str(ctx.exception))
        # Only the first copy's 2 retry attempts happened; copies 2 and 3
        # were never attempted.
        self.assertEqual(len(FakePrinterClientFixed.densities), 2)

    def test_close_closes_all_held_connections(self) -> None:
        client = NiimprintPrintClient(RetryConfig(max_attempts=1, delay_seconds=0))
        client.print_label(self.printer, _fake_png_bytes())
        client.close()
        self.assertTrue(FakePrinterClientFixed.instances[0]._transport._sock.closed)

    def test_settles_before_reconnecting_after_a_failure(self) -> None:
        FakePrinterClientFixed.fail_times = 1
        client = NiimprintPrintClient(RetryConfig(max_attempts=2, delay_seconds=5))
        sleep_calls: list[float] = []
        with (
            mock.patch(
                "label_print_server.printer_client.time.sleep",
                side_effect=sleep_calls.append,
            ),
            mock.patch.object(NiimprintPrintClient, "_now", return_value=1000.0),
        ):
            client.print_label(self.printer, _fake_png_bytes())
        self.assertEqual(sleep_calls, [5])

    def test_no_settle_delay_on_the_very_first_connect(self) -> None:
        client = NiimprintPrintClient(RetryConfig(max_attempts=1, delay_seconds=5))
        with mock.patch(
            "label_print_server.printer_client.time.sleep"
        ) as sleep_mock:
            client.print_label(self.printer, _fake_png_bytes())
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
