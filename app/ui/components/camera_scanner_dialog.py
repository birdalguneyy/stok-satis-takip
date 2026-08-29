import time
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image, ImageTk

try:
    import cv2

    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False

from app.ui.theme import ACCENT, ERROR, FONT_BODY, FONT_HEADING, FONT_SMALL, SUCCESS
from app.utils.camera import VideoStream, decode_barcode_from_frame, list_available_cameras
from app.utils.sound import play_error_beep, play_success_beep


class CameraScannerDialog(ctk.CTkToplevel):
    """Agresif çoklu ön-işleme motorlu, ham sensör piksellerinden okuyan canlı barkod tarayıcı."""

    def __init__(
        self,
        parent,
        on_barcode_scanned: Callable[[str], None],
        auto_close: bool = True,
        title: str = "📷 Kamera ile Barkod Oku",
    ) -> None:
        super().__init__(parent)
        self.on_barcode_scanned = on_barcode_scanned
        self.auto_close = auto_close

        self.title(title)
        self.geometry("680x670")
        self.resizable(False, False)

        # Make modal & stay on top
        self.transient(parent)
        self.grab_set()

        self.vs: Optional[VideoStream] = None
        self.is_running = False
        self.camera_index = 0
        self.last_scanned_code = ""
        self.last_scan_time = 0.0
        self.cooldown_seconds = 1.5
        self.zoom_factor = 1.0
        self.frame_count = 0

        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        self._build_ui()
        if HAS_OPENCV:
            self.after(50, lambda: self._start_camera(0))
        else:
            self.video_label.configure(
                text="⚠️ opencv-python bulunamadı.\npip install opencv-python"
            )

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(12, 4))

        ctk.CTkLabel(header, text="Kamera ile Canlı Barkod Okuyucu", font=FONT_HEADING).pack(side="left")

        # Camera selection dropdown
        self.cameras = list_available_cameras()
        cam_values = [f"Kamera {i}" for i in self.cameras]
        self.cam_combo = ctk.CTkComboBox(
            header,
            values=cam_values,
            width=120,
            command=self._on_camera_changed,
        )
        self.cam_combo.set(f"Kamera {self.camera_index}")
        self.cam_combo.pack(side="right")

        # Zoom Control Bar (1.0x - 10.0x)
        zoom_bar = ctk.CTkFrame(self, fg_color=("gray90", "gray20"), corner_radius=8)
        zoom_bar.pack(fill="x", padx=16, pady=(4, 6))

        self.zoom_label = ctk.CTkLabel(
            zoom_bar,
            text=f"🔍 Ekran Zoom: {self.zoom_factor:.1f}x",
            font=FONT_SMALL,
        )
        self.zoom_label.pack(side="left", padx=(12, 8), pady=6)

        self.zoom_slider = ctk.CTkSlider(
            zoom_bar,
            from_=1.0,
            to=10.0,
            number_of_steps=90,
            command=self._on_zoom_changed,
            width=180,
        )
        self.zoom_slider.set(self.zoom_factor)
        self.zoom_slider.pack(side="left", padx=4, pady=6)

        # Quick Zoom Buttons
        btn_box = ctk.CTkFrame(zoom_bar, fg_color="transparent")
        btn_box.pack(side="right", padx=(0, 8))
        for z in [1.0, 2.0, 3.0, 5.0, 10.0]:
            btn = ctk.CTkButton(
                btn_box,
                text=f"{z:.0f}x",
                width=34,
                height=26,
                font=(FONT_SMALL[0], 10, "bold"),
                fg_color=ACCENT if z == self.zoom_factor else ("gray75", "gray35"),
                command=lambda val=float(z): self._set_zoom(val),
            )
            btn.pack(side="left", padx=1)

        # Status badge
        self.status_label = ctk.CTkLabel(
            self,
            text="🎯 Barkodu kameraya düz tutun (~20-40 cm mesafe ideal)",
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
        )
        self.status_label.pack(pady=(0, 4))

        # Video Frame Container
        self.video_frame = ctk.CTkFrame(self, fg_color="black", corner_radius=12)
        self.video_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        self.video_label = ctk.CTkLabel(self.video_frame, text="Kamera başlatılıyor...", text_color="white")
        self.video_label.pack(fill="both", expand=True)

        # Controls bar
        ctrl_bar = ctk.CTkFrame(self, fg_color="transparent")
        ctrl_bar.pack(fill="x", padx=16, pady=(0, 10))

        self.mirror_var = ctk.BooleanVar(value=False)
        self.mirror_check = ctk.CTkCheckBox(
            ctrl_bar,
            text="Ayna Görüntüsü (Çevir)",
            variable=self.mirror_var,
            font=FONT_SMALL,
        )
        self.mirror_check.pack(side="left", padx=(0, 12))

        self.continuous_var = ctk.BooleanVar(value=not self.auto_close)
        self.continuous_check = ctk.CTkCheckBox(
            ctrl_bar,
            text="Sürekli Okuma Modu",
            variable=self.continuous_var,
            font=FONT_SMALL,
        )
        self.continuous_check.pack(side="left")

        ctk.CTkButton(
            ctrl_bar,
            text="Kapat",
            font=FONT_SMALL,
            fg_color=("gray75", "gray35"),
            width=100,
            command=self._on_closing,
        ).pack(side="right")

    def _set_zoom(self, val: float) -> None:
        self.zoom_factor = val
        self.zoom_slider.set(val)
        self.zoom_label.configure(text=f"🔍 Ekran Zoom: {self.zoom_factor:.1f}x")

    def _on_zoom_changed(self, value: float) -> None:
        self.zoom_factor = float(value)
        self.zoom_label.configure(text=f"🔍 Ekran Zoom: {self.zoom_factor:.1f}x")

    def _on_camera_changed(self, value: str) -> None:
        if not HAS_OPENCV:
            return
        try:
            idx = int(value.replace("Kamera ", ""))
            self.after(50, lambda: self._start_camera(idx))
        except ValueError:
            pass

    def _start_camera(self, index: int) -> None:
        if not HAS_OPENCV:
            return
        self._stop_camera()
        self.camera_index = index

        try:
            self.vs = VideoStream(src=index, width=1280, height=720).start()
            self.frame_count = 0
            self.is_running = True
            self._update_frame()
        except Exception as exc:
            self.video_label.configure(text=f"⚠️ Kamera başlatılamadı: {exc}")

    def _stop_camera(self) -> None:
        self.is_running = False
        if self.vs:
            try:
                self.vs.stop()
            except Exception:
                pass
            self.vs = None

    def _update_frame(self) -> None:
        if not HAS_OPENCV or not self.is_running or not self.vs:
            return

        try:
            # ── 1. Thread'den HAM sensör karesini çek ──
            raw_frame = self.vs.read()
            if raw_frame is None:
                self.after(25, self._update_frame)
                return

            # ── 2. BARKOD TARAMA (her 2 karede 1, ham kare üzerinde) ──
            self.frame_count += 1
            if self.frame_count % 2 == 0:
                results = decode_barcode_from_frame(raw_frame)

                now = time.time()
                for barcode_text, _ in results:
                    if barcode_text != self.last_scanned_code or (now - self.last_scan_time) > self.cooldown_seconds:
                        self.last_scanned_code = barcode_text
                        self.last_scan_time = now

                        play_success_beep()
                        self.status_label.configure(
                            text=f"✅ OKUNDU: {barcode_text}",
                            text_color=SUCCESS,
                        )

                        self.on_barcode_scanned(barcode_text)

                        if not self.continuous_var.get():
                            self._on_closing()
                            return

            # ── 3. EKRAN GÖRÜNTÜLEMESİ (yalnızca göz için) ──
            disp = raw_frame.copy()

            if self.mirror_var.get():
                disp = cv2.flip(disp, 1)

            h, w = disp.shape[:2]

            # Dijital Zoom (yalnızca ekran önizlemesi)
            if self.zoom_factor > 1.0:
                cw = max(20, int(w / self.zoom_factor))
                ch = max(20, int(h / self.zoom_factor))
                cx, cy = (w - cw) // 2, (h - ch) // 2
                disp = cv2.resize(disp[cy : cy + ch, cx : cx + cw], (w, h), interpolation=cv2.INTER_LINEAR)

            # Siyah-Beyaz
            gray_disp = cv2.cvtColor(disp, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray_disp, (640, 420), interpolation=cv2.INTER_LINEAR)
            color_small = cv2.cvtColor(small, cv2.COLOR_GRAY2RGB)

            # Rehber kutusu
            sh, sw = color_small.shape[:2]
            bw, bh = int(sw * 0.90), int(sh * 0.60)
            bx1, by1 = (sw - bw) // 2, (sh - bh) // 2
            cv2.rectangle(color_small, (bx1, by1), (bx1 + bw, by1 + bh), (0, 255, 0), 2)
            cv2.putText(
                color_small,
                f"Zoom: {self.zoom_factor:.1f}x",
                (bx1 + 10, by1 - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )

            # CTkImage ile HiDPI uyumlu görüntüleme (uyarıyı çözer)
            pil_img = Image.fromarray(color_small)
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(640, 420))
            self.video_label.configure(image=ctk_img, text="")
            self.video_label._ctk_image = ctk_img  # prevent garbage collection

            self.after(25, self._update_frame)
        except Exception:
            if self.is_running:
                self.after(30, self._update_frame)

    def _on_closing(self) -> None:
        self._stop_camera()
        self.grab_release()
        self.destroy()
