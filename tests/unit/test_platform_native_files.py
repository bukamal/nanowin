from __future__ import annotations

import asyncio
import base64
import importlib
import sys
import types

import pytest


@pytest.fixture()
def bridge_module(monkeypatch):
    stub = types.ModuleType("flet_native_files")

    class NativeFiles:
        async def pick_file(self, **_kwargs):
            raise AssertionError("pick_file must be provided by the test")

        async def capture_receipt(self, *, filename="receipt.jpg"):
            raise AssertionError(f"unexpected native camera call: {filename}")

        async def scan_barcode(self):
            raise AssertionError("unsupported camera scan must not call native bridge")

    stub.NativeFiles = NativeFiles
    existing = sys.modules.pop("nano_offline.core.platform_native_files", None)
    monkeypatch.setitem(sys.modules, "flet_native_files", stub)
    module = importlib.import_module("nano_offline.core.platform_native_files")
    try:
        yield module
    finally:
        sys.modules.pop("nano_offline.core.platform_native_files", None)
        if existing is not None:
            sys.modules["nano_offline.core.platform_native_files"] = existing


def test_windows_receipt_capture_uses_file_picker(tmp_path, monkeypatch, bridge_module):
    image = tmp_path / "receipt.png"
    image.write_bytes(b"image-bytes")
    monkeypatch.setattr(bridge_module, "feature_status", lambda _: "fallback:file-pick")
    native_files = bridge_module.PlatformNativeFiles()

    async def pick_file(**_kwargs):
        return {"path": str(image), "name": image.name, "extension": "png"}

    monkeypatch.setattr(native_files, "pick_file", pick_file)
    result = asyncio.run(native_files.capture_receipt())

    assert result == {
        "name": "receipt.png",
        "extension": "png",
        "bytes": base64.b64encode(b"image-bytes").decode("ascii"),
    }


def test_windows_camera_scan_explains_usb_wedge_fallback(monkeypatch, bridge_module):
    monkeypatch.setattr(bridge_module, "feature_status", lambda _: "fallback:wedge")
    native_files = bridge_module.PlatformNativeFiles()

    with pytest.raises(RuntimeError, match="قارئ باركود USB"):
        asyncio.run(native_files.scan_barcode())


def test_receipt_picker_rejects_oversized_image(tmp_path, monkeypatch, bridge_module):
    image = tmp_path / "receipt.jpg"
    image.write_bytes(b"x" * (bridge_module.MAX_RECEIPT_IMAGE_BYTES + 1))
    monkeypatch.setattr(bridge_module, "feature_status", lambda _: "fallback:file-pick")
    native_files = bridge_module.PlatformNativeFiles()

    async def pick_file(**_kwargs):
        return {"path": str(image), "name": image.name, "extension": "jpg"}

    monkeypatch.setattr(native_files, "pick_file", pick_file)
    with pytest.raises(RuntimeError, match="12 ميغابايت"):
        asyncio.run(native_files.capture_receipt())
