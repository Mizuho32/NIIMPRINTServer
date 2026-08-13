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


class FakeTransport:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class FakePrinterClientFixed:
    """Stand-in for niimprint.PrinterClientFixed used to drive the retry loop."""

    fail_times = 0
    densities: ClassVar[list[int]] = []

    def __init__(self, transport):
        self.transport = transport

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


if __name__ == "__main__":
    unittest.main()
