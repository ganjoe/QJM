import base64
import logging
from typing import Dict, Any
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter, QColor

from chart_viewer.config import ViewerConfig
from chart_viewer.ui.window import ChartWindow
from chart_viewer.models.envelope import MessageKind, make_envelope

logger = logging.getLogger(__name__)

class ScreenshotCapture:
    def __init__(self, config: ViewerConfig, transport: Any, windows: Dict[str, ChartWindow]):
        self.config = config
        self.transport = transport
        self.windows = windows

    def handle_request(self, payload: dict) -> None:
        request_id = payload.get("request_id", "")
        target_win_id = payload.get("window_id")

        is_hires = bool(
            payload.get("hires")
            or payload.get("resolution") in ("hires", "800x600")
            or payload.get("mode") == "hires"
        )
        default_w = self.config.screenshot_hires_width if is_hires else self.config.screenshot_width
        default_h = self.config.screenshot_hires_height if is_hires else self.config.screenshot_height

        target_width = int(payload.get("width", default_w))
        target_height = int(payload.get("height", default_h))
        mode = payload.get("mode", self.config.screenshot_mode)
        sharpen_amount = float(payload.get("sharpen_amount", self.config.screenshot_sharpen_amount))

        screenshots = []

        # Filter target windows
        if target_win_id and target_win_id in self.windows:
            target_windows = [(target_win_id, self.windows[target_win_id])]
        else:
            target_windows = list(self.windows.items())

        for win_id, win in target_windows:
            try:
                pixmap = win.grab()
                if pixmap.isNull():
                    continue

                # Ensure 1:1 pixel mapping for accurate downsampling
                src_img = pixmap.toImage()
                src_img.setDevicePixelRatio(1.0)

                if mode == "stretch":
                    scaled_img = src_img.scaled(
                        target_width,
                        target_height,
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                elif mode == "fit":
                    scaled_img = src_img.scaled(
                        target_width,
                        target_height,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                else:
                    # Default: "letterbox" on background color to preserve aspect ratio without distortion
                    bg_color = QColor(self.config.default_background_color)
                    final_img = QImage(target_width, target_height, QImage.Format.Format_ARGB32_Premultiplied)
                    final_img.fill(bg_color)

                    scaled_sub = src_img.scaled(
                        target_width,
                        target_height,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    painter = QPainter(final_img)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                    x_offset = (target_width - scaled_sub.width()) // 2
                    y_offset = (target_height - scaled_sub.height()) // 2
                    painter.drawImage(x_offset, y_offset, scaled_sub)
                    painter.end()
                    scaled_img = final_img

                # Apply edge sharpening to eliminate downscaling blur
                if sharpen_amount > 0:
                    scaled_img = self._sharpen_image(scaled_img, factor=sharpen_amount)

                ba = QByteArray()
                buffer = QBuffer(ba)
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                scaled_img.save(buffer, "PNG")
                b64_data = base64.b64encode(bytes(ba.data())).decode("ascii")

                symbol = getattr(win, "symbol", "")
                if not symbol and getattr(win, "canvas", None) and getattr(win.canvas, "window_data", None):
                    symbol = getattr(win.canvas.window_data, "symbol", "")

                screenshots.append({
                    "window_id": win_id,
                    "symbol": symbol,
                    "width": scaled_img.width(),
                    "height": scaled_img.height(),
                    "image_base64": b64_data,
                })
            except Exception as e:
                logger.error(f"Failed to capture screenshot for window {win_id}: {e}")

        resp_env = make_envelope(
            msg_type="screenshot.response",
            payload={
                "request_id": request_id,
                "screenshots": screenshots,
            },
            kind=MessageKind.EVENT,
        )
        try:
            self.transport.send_command(resp_env)
        except Exception as e:
            logger.error(f"Failed to send screenshot.response: {e}")

    @staticmethod
    def _sharpen_image(img: QImage, factor: float = 0.5) -> QImage:
        w, h = img.width(), img.height()
        if w < 10 or h < 10:
            return img

        blurred = img.scaled(
            w // 2, h // 2,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).scaled(
            w, h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        highpass = img.copy()
        p1 = QPainter(highpass)
        p1.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
        p1.drawImage(0, 0, blurred)
        p1.end()

        sharpened = img.copy()
        p2 = QPainter(sharpened)
        p2.setOpacity(min(1.0, max(0.0, factor)))
        p2.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
        p2.drawImage(0, 0, highpass)
        p2.end()
        return sharpened
