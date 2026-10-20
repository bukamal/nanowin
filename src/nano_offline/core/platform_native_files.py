from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from flet_native_files import NativeFiles

from nano_offline.core.platform_capabilities import feature_status

MAX_RECEIPT_IMAGE_BYTES = 12 * 1024 * 1024


class PlatformNativeFiles(NativeFiles):
    """Native bridge with explicit Windows fallbacks for camera features."""

    async def capture_receipt(self, *, filename: str = "receipt.jpg") -> dict | None:
        status = feature_status("camera_capture")
        if status == "native":
            return await super().capture_receipt(filename=filename)
        if status != "fallback:file-pick":
            raise RuntimeError("التقاط صورة الإيصال غير مدعوم على هذا النظام")

        selected = await self.pick_file(
            extensions=("jpg", "jpeg", "png", "webp"),
            dialog_title="اختر صورة الإيصال",
        )
        if selected is None:
            return None
        path = Path(str(selected.get("path") or ""))
        if not path.is_file():
            raise RuntimeError("تعذر الوصول إلى الصورة المحددة")
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise RuntimeError("صيغة الصورة غير مدعومة؛ استخدم JPG أو PNG أو WebP")
        if path.stat().st_size > MAX_RECEIPT_IMAGE_BYTES:
            raise RuntimeError("الصورة كبيرة جدًا؛ اختر صورة أصغر من 12 ميغابايت")
        image_bytes = await asyncio.to_thread(path.read_bytes)
        name = str(selected.get("name") or filename)
        extension = str(selected.get("extension") or Path(name).suffix.lstrip(".") or "jpg").lower()
        return {
            "name": name,
            "extension": extension,
            "bytes": base64.b64encode(image_bytes).decode("ascii"),
        }

    async def scan_barcode(self) -> str | None:
        status = feature_status("barcode_camera_scan")
        if status == "native":
            return await super().scan_barcode()
        if status == "fallback:wedge":
            raise RuntimeError(
                "مسح الكاميرا غير متاح على ويندوز. استخدم قارئ باركود USB أو أدخل الرمز يدويًا."
            )
        raise RuntimeError("مسح الباركود غير مدعوم على هذا النظام")
