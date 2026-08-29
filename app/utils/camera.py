import os
import threading
import time
from typing import List, Optional, Tuple

# Suppress OpenCV internal log warnings in console
os.environ["OPENCV_LOG_LEVEL"] = "SILENT"

try:
    import cv2
    import numpy as np

    try:
        cv2.setLogLevel(0)
    except Exception:
        pass

    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False

try:
    from pyzbar.pyzbar import decode as pyzbar_decode, ZBarSymbol

    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False

try:
    import zxingcpp

    HAS_ZXING = True
except ImportError:
    HAS_ZXING = False


class VideoStream:
    """Kamera I/O işlemini ana arayüzden ayıran arka plan Daemon Thread sınıfı."""

    def __init__(self, src: int = 0, width: int = 1280, height: int = 720) -> None:
        self.src = src
        self.width = width
        self.height = height
        self.stream = None
        self.grabbed = False
        self.frame = None
        self.stopped = True
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "VideoStream":
        if not HAS_OPENCV:
            return self

        try:
            self.stream = cv2.VideoCapture(self.src)
            if self.stream and self.stream.isOpened():
                try:
                    self.stream.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                    self.stream.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                    # Autofocus ON
                    self.stream.set(cv2.CAP_PROP_AUTOFOCUS, 1)
                except Exception:
                    pass

                self.grabbed, self.frame = self.stream.read()
                self.stopped = False
                self._thread = threading.Thread(target=self._update, args=(), daemon=True)
                self._thread.start()
        except Exception:
            self.stopped = True

        return self

    def _update(self) -> None:
        """Bu fonksiyon ana programdan tamamen bağımsız arka planda çalışır (Daemon Thread)."""
        while not self.stopped and self.stream and self.stream.isOpened():
            try:
                grabbed, frame = self.stream.read()
                if not grabbed or frame is None:
                    time.sleep(0.01)
                    continue
                self.grabbed = grabbed
                self.frame = frame
            except Exception:
                time.sleep(0.01)

    def read(self):
        """Ana döngünün kullanması için en taze kareyi 0ms gecikmeyle döndürür."""
        return self.frame

    def stop(self) -> None:
        """Thread ve kamera kaynaklarını güvenle kapatır."""
        self.stopped = True
        if self._thread and self._thread.is_alive():
            try:
                self._thread.join(timeout=0.5)
            except Exception:
                pass
        if self.stream:
            try:
                self.stream.release()
            except Exception:
                pass
            self.stream = None


def list_available_cameras() -> List[int]:
    """Kamera listesini arayüzü dondurmadan (0ms gecikmeyle) döndürür."""
    return [0, 1]


# ─── Yalnızca 1D barkod türlerini tara (PDF417 assertion hatasını engeller) ───
PYZBAR_1D_SYMBOLS = [
    ZBarSymbol.EAN13,
    ZBarSymbol.EAN8,
    ZBarSymbol.UPCA,
    ZBarSymbol.UPCE,
    ZBarSymbol.CODE128,
    ZBarSymbol.CODE39,
    ZBarSymbol.CODE93,
    ZBarSymbol.I25,
    ZBarSymbol.QRCODE,
] if HAS_PYZBAR else []


def _pyzbar_scan(image) -> List[str]:
    """pyzbar ile sadece 1D barkod + QR türlerini tarar (PDF417 hatalarını tamamen engeller)."""
    if not HAS_PYZBAR:
        return []
    try:
        barcodes = pyzbar_decode(image, symbols=PYZBAR_1D_SYMBOLS)
        found = []
        for b in barcodes:
            text = b.data.decode("utf-8", errors="ignore").strip()
            if text:
                found.append(text)
        return found
    except Exception:
        return []


def _sharpen_for_barcode(gray):
    """Bulanık barkod çizgilerini keskinleştiren agresif filtre zinciri."""
    # 1. CLAHE ile kontrast artırma
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # 2. Gaussian Blur ile gürültü temizleme
    blurred = cv2.GaussianBlur(enhanced, (3, 3), 0)

    # 3. Unsharp Mask ile keskinleştirme
    sharpened = cv2.addWeighted(enhanced, 1.8, blurred, -0.8, 0)

    return sharpened


def decode_barcode_from_frame(frame) -> List[Tuple[str, Optional[list]]]:
    """Agresif çoklu ön-işleme ile bulanık kamera barkodlarını çözen hibrit motor."""
    if not HAS_OPENCV or frame is None:
        return []

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    h, w = gray.shape[:2]

    results = []

    # ════════════════════════════════════════════════
    # PASS 1: Orijinal Gri Kareyi pyzbar ile tara
    # ════════════════════════════════════════════════
    found = _pyzbar_scan(gray)
    if found:
        return [(t, None) for t in found]

    # ════════════════════════════════════════════════
    # PASS 2: Keskinleştirilmiş kareyi tara
    # ════════════════════════════════════════════════
    sharpened = _sharpen_for_barcode(gray)
    found = _pyzbar_scan(sharpened)
    if found:
        return [(t, None) for t in found]

    # ════════════════════════════════════════════════
    # PASS 3: Otsu Binarization
    # ════════════════════════════════════════════════
    try:
        _, otsu = cv2.threshold(sharpened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        found = _pyzbar_scan(otsu)
        if found:
            return [(t, None) for t in found]
    except Exception:
        pass

    # ════════════════════════════════════════════════
    # PASS 4: Adaptive Thresholding
    # ════════════════════════════════════════════════
    try:
        adapt = cv2.adaptiveThreshold(
            sharpened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 5
        )
        found = _pyzbar_scan(adapt)
        if found:
            return [(t, None) for t in found]
    except Exception:
        pass

    # ════════════════════════════════════════════════
    # PASS 5: 2x Upscale + keskinleştirme (düşük çözünürlüklü kameralar için)
    # ════════════════════════════════════════════════
    try:
        upscaled = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        upscaled_sharp = _sharpen_for_barcode(upscaled)
        found = _pyzbar_scan(upscaled_sharp)
        if found:
            return [(t, None) for t in found]

        _, up_otsu = cv2.threshold(upscaled_sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        found = _pyzbar_scan(up_otsu)
        if found:
            return [(t, None) for t in found]
    except Exception:
        pass

    # ════════════════════════════════════════════════
    # PASS 6: Sabit Threshold Seviyeleri (80, 100, 120, 140, 160)
    # ════════════════════════════════════════════════
    for thresh_val in [80, 100, 120, 140, 160]:
        try:
            _, fixed_bin = cv2.threshold(sharpened, thresh_val, 255, cv2.THRESH_BINARY)
            found = _pyzbar_scan(fixed_bin)
            if found:
                return [(t, None) for t in found]
        except Exception:
            pass

    # ════════════════════════════════════════════════
    # PASS 7: zxing-cpp Fallback
    # ════════════════════════════════════════════════
    if HAS_ZXING:
        for img in [gray, sharpened]:
            try:
                detected = zxingcpp.read_barcodes(
                    img,
                    try_rotate=True,
                    try_downscale=True,
                    try_invert=True,
                )
                for item in detected:
                    if item.text and item.text.strip():
                        results.append((item.text.strip(), None))
                if results:
                    return results
            except Exception:
                pass

    return results
