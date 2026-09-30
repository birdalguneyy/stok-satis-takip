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

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def is_valid_barcode(code: str) -> bool:
    """Barkodun biçimini ve varsa kontrol basamağını (checksum) doğrular."""
    if not code:
        return False
    code = code.strip()
    if len(code) < 3:
        return False

    # EAN-13 kontrol basamağı doğrulaması
    if len(code) == 13 and code.isdigit():
        checksum = sum(int(code[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
        check_digit = (10 - (checksum % 10)) % 10
        return check_digit == int(code[12])

    # EAN-8 kontrol basamağı doğrulaması
    if len(code) == 8 and code.isdigit():
        checksum = sum(int(code[i]) * (3 if i % 2 == 0 else 1) for i in range(7))
        check_digit = (10 - (checksum % 10)) % 10
        return check_digit == int(code[7])

    # UPC-A kontrol basamağı doğrulaması
    if len(code) == 12 and code.isdigit():
        checksum = sum(int(code[i]) * (3 if i % 2 == 0 else 1) for i in range(11))
        check_digit = (10 - (checksum % 10)) % 10
        return check_digit == int(code[11])

    return True


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
    """Ultra-hızlı hibrit C++ zxing-cpp, PIL ve pyzbar barkod çözücü."""
    if frame is None:
        return []

    # Eğer PIL Image nesnesi geldiyse
    if HAS_PIL and isinstance(frame, Image.Image):
        # 1. Doğrudan PIL Image ile zxingcpp dene
        if HAS_ZXING:
            try:
                detected = zxingcpp.read_barcodes(
                    frame,
                    try_rotate=True,
                    try_downscale=True,
                    try_invert=True,
                )
                for item in detected:
                    txt = (item.text or "").strip()
                    is_valid = getattr(item, "is_valid", True)
                    if txt and is_valid and is_valid_barcode(txt):
                        return [(txt, None)]
            except Exception:
                pass

        if HAS_OPENCV:
            np_arr = np.array(frame.convert("RGB"))
            frame = cv2.cvtColor(np_arr, cv2.COLOR_RGB2BGR)
        else:
            return []

    if not HAS_OPENCV or frame is None:
        return []

    # 1. Hızlı gri tonlama
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    h, w = gray.shape[:2]

    # Aşırı büyük fotoğrafları hız için optimize boyuta indir (Max 1600px - barkod çizgilerini koruyarak)
    if max(h, w) > 1600:
        scale = 1600.0 / max(h, w)
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = gray.shape[:2]

    results = []

    # ════════════════════════════════════════════════
    # PASS 1: C++ zxingcpp ile 0ms Gecikmeli Doğrudan Okuma (En Hızlı & Güçlü)
    # ════════════════════════════════════════════════
    if HAS_ZXING:
        try:
            detected = zxingcpp.read_barcodes(
                gray,
                try_rotate=True,
                try_downscale=True,
                try_invert=True,
            )
            for item in detected:
                txt = (item.text or "").strip()
                is_valid = getattr(item, "is_valid", True)
                if txt and is_valid and is_valid_barcode(txt):
                    results.append((txt, None))
            if results:
                return results
        except Exception:
            pass

    # ════════════════════════════════════════════════
    # PASS 2: Orijinal Gri Kareyi pyzbar ile tara
    # ════════════════════════════════════════════════
    found = _pyzbar_scan(gray)
    valid_found = [t for t in found if is_valid_barcode(t)]
    if valid_found:
        return [(t, None) for t in valid_found]

    # ════════════════════════════════════════════════
    # PASS 3: Keskinleştirilmiş kareyi tara (zxingcpp + pyzbar)
    # ════════════════════════════════════════════════
    sharpened = _sharpen_for_barcode(gray)
    if HAS_ZXING:
        try:
            detected = zxingcpp.read_barcodes(sharpened, try_rotate=True, try_downscale=True)
            for item in detected:
                txt = (item.text or "").strip()
                is_valid = getattr(item, "is_valid", True)
                if txt and is_valid and is_valid_barcode(txt):
                    results.append((txt, None))
            if results:
                return results
        except Exception:
            pass

    found = _pyzbar_scan(sharpened)
    valid_found = [t for t in found if is_valid_barcode(t)]
    if valid_found:
        return [(t, None) for t in valid_found]

    # ════════════════════════════════════════════════
    # PASS 4: Rotasyonlu Tarama (90, 180, 270 derece dikey/yan telefon çekimleri)
    # ════════════════════════════════════════════════
    for rot in [cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_180]:
        try:
            rotated = cv2.rotate(gray, rot)
            if HAS_ZXING:
                detected = zxingcpp.read_barcodes(rotated, try_rotate=True, try_downscale=True)
                for item in detected:
                    txt = (item.text or "").strip()
                    is_valid = getattr(item, "is_valid", True)
                    if txt and is_valid and is_valid_barcode(txt):
                        return [(txt, None)]
            found = _pyzbar_scan(rotated)
            valid_found = [t for t in found if is_valid_barcode(t)]
            if valid_found:
                return [(t, None) for t in valid_found]
        except Exception:
            pass

    # ════════════════════════════════════════════════
    # PASS 5: Otsu & Adaptive Binarization
    # ════════════════════════════════════════════════
    try:
        _, otsu = cv2.threshold(sharpened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if HAS_ZXING:
            detected = zxingcpp.read_barcodes(otsu, try_rotate=True, try_downscale=True)
            for item in detected:
                txt = (item.text or "").strip()
                is_valid = getattr(item, "is_valid", True)
                if txt and is_valid and is_valid_barcode(txt):
                    return [(txt, None)]
        found = _pyzbar_scan(otsu)
        valid_found = [t for t in found if is_valid_barcode(t)]
        if valid_found:
            return [(t, None) for t in valid_found]
    except Exception:
        pass

    try:
        adapt = cv2.adaptiveThreshold(
            sharpened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 5
        )
        if HAS_ZXING:
            detected = zxingcpp.read_barcodes(adapt, try_rotate=True, try_downscale=True)
            for item in detected:
                txt = (item.text or "").strip()
                is_valid = getattr(item, "is_valid", True)
                if txt and is_valid and is_valid_barcode(txt):
                    return [(txt, None)]
        found = _pyzbar_scan(adapt)
        valid_found = [t for t in found if is_valid_barcode(t)]
        if valid_found:
            return [(t, None) for t in valid_found]
    except Exception:
        pass

    # ════════════════════════════════════════════════
    # PASS 6: Upscale (Düşük çözünürlüklü küçük barkodlar için)
    # ════════════════════════════════════════════════
    if w < 700 or h < 700:
        try:
            upscaled = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
            if HAS_ZXING:
                detected = zxingcpp.read_barcodes(upscaled, try_rotate=True, try_downscale=True)
                for item in detected:
                    txt = (item.text or "").strip()
                    is_valid = getattr(item, "is_valid", True)
                    if txt and is_valid and is_valid_barcode(txt):
                        return [(txt, None)]

            upscaled_sharp = _sharpen_for_barcode(upscaled)
            found = _pyzbar_scan(upscaled_sharp)
            valid_found = [t for t in found if is_valid_barcode(t)]
            if valid_found:
                return [(t, None) for t in valid_found]
        except Exception:
            pass

    return results

