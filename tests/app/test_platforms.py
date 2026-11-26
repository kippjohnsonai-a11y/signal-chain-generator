"""Pieces that differ by platform: the bundled libusb and Windows folders."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stompxl import config


def test_pyusb_uses_the_bundled_libusb() -> None:
    import libusb_package
    import usb.backend.libusb1 as libusb1

    from stompxl.helixgen.device import usb as device_usb

    device_usb._require_pyusb()
    assert libusb1._bundled is True
    assert libusb_package.find_library("usb-1.0")  # the wheel ships one for this platform
    device_usb._require_pyusb()  # a second call doesn't wrap it again
    assert libusb1.get_backend.__name__ == "get_backend"


def test_windows_keeps_data_in_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.base_dir() == tmp_path / "Signal Chain Generator"
