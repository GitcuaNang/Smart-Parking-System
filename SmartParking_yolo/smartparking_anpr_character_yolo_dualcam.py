# -*- coding: utf-8 -*-
"""Smart Parking ANPR - two YOLO stages, Vietnamese character-level recognition.

Pipeline inspired by character-box ALPR systems:
  1) YOLO plate detector (existing D:\\SmartParking\\license_plate_detector.pt)
  2) YOLO character detector trained on Vietnamese plate characters (Letters_detection.pt)
  3) order characters by row/column, validate Vietnamese plate layouts,
     require cross-frame agreement, print only: timestamp | plate

Required model files:
  D:\\SmartParking\\license_plate_detector.pt
  D:\\SmartParking\\Letters_detection.pt  (character-level model, 0-9/A-Z)

Install:
  py -3.12 -m pip install opencv-python ultralytics numpy openpyxl torch
Run:
  cd /d D:\\SmartParking
  py -3.12 smartparking_anpr_character_yolo_dualcam.py

Important: a plate detector alone cannot recognize individual characters. This script
requires the second, character-level model; it does not silently fall back to EasyOCR.
"""
from __future__ import annotations

import os
import re
import sys
import time
import logging
import threading
import warnings
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from ultralytics import YOLO
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from firebase_service import upload_vehicle_event

warnings.filterwarnings("ignore")
logging.getLogger("ultralytics").setLevel(logging.ERROR)
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import torch
    try:
        torch.set_num_threads(max(1, min(4, (os.cpu_count() or 4) // 2)))
        torch.set_num_interop_threads(1)
    except Exception:
        pass
    CUDA_AVAILABLE = bool(torch.cuda.is_available())
except Exception:
    torch = None
    CUDA_AVAILABLE = False

try:
    cv2.setNumThreads(1)
except Exception:
    pass

# ------------------------------ CONFIG ------------------------------
ROOT = Path(os.getenv("SMARTPARKING_DIR", r"D:\SmartParking"))
ROOT.mkdir(parents=True, exist_ok=True)
PLATE_MODEL_PATH = Path(os.getenv("ANPR_PLATE_MODEL", str(ROOT / "license_plate_detector.pt")))
CHAR_MODEL_PATH = Path(os.getenv("ANPR_CHAR_MODEL", str(ROOT / "Letters_detection.pt")))
EXCEL_PATH = ROOT / "smartparking.xlsx"

CAMERAS = {
    "CONG VAO": int(os.getenv("ANPR_CAM_IN", "1")),
    "CONG RA": int(os.getenv("ANPR_CAM_OUT", "2")),
}
CAM_WIDTH = int(os.getenv("ANPR_CAM_WIDTH", "640"))
CAM_HEIGHT = int(os.getenv("ANPR_CAM_HEIGHT", "480"))
CAM_FPS = int(os.getenv("ANPR_CAM_FPS", "30"))
SHOW_WINDOW = os.getenv("ANPR_SHOW_WINDOW", "1").strip().lower() not in {"0", "false", "no", "off"}

PLATE_IMGSZ_IN = int(os.getenv("ANPR_PLATE_IMGSZ_IN", "384"))
PLATE_IMGSZ_OUT = int(os.getenv("ANPR_PLATE_IMGSZ_OUT", "512"))
CHAR_IMGSZ_IN = int(os.getenv("ANPR_CHAR_IMGSZ_IN", "416"))
CHAR_IMGSZ_OUT = int(os.getenv("ANPR_CHAR_IMGSZ_OUT", "448"))
PLATE_CONF_IN = float(os.getenv("ANPR_PLATE_CONF_IN", "0.30"))
PLATE_CONF_OUT = float(os.getenv("ANPR_PLATE_CONF_OUT", "0.24"))
CHAR_CONF_MIN = float(os.getenv("ANPR_CHAR_CONF", "0.25"))
CHAR_CONFIRM_MEAN = float(os.getenv("ANPR_CHAR_CONFIRM_MEAN", "0.56"))
CHAR_CONFIRM_MIN = float(os.getenv("ANPR_CHAR_CONFIRM_MIN", "0.28"))
FAST_MEAN_CONF = float(os.getenv("ANPR_FAST_MEAN_CONF", "0.86"))
FAST_MIN_CHAR_CONF = float(os.getenv("ANPR_FAST_MIN_CHAR_CONF", "0.62"))
FAST_PLATE_CONF = float(os.getenv("ANPR_FAST_PLATE_CONF", "0.70"))
VOTE_WINDOW_S = float(os.getenv("ANPR_VOTE_WINDOW", "0.72"))
MAX_EVENT_LATENCY_S = float(os.getenv("ANPR_MAX_EVENT_LATENCY", "0.95"))
DEDUP_S = float(os.getenv("ANPR_DEDUP_SECONDS", "3.0"))
FALLBACK_INTERVAL_S = float(os.getenv("ANPR_FALLBACK_INTERVAL", "0.75"))
MAX_PLATES_PER_FRAME = int(os.getenv("ANPR_MAX_PLATES_PER_FRAME", "1"))
DISPLAY_WIDTH = int(os.getenv("ANPR_DISPLAY_WIDTH", "640"))

EXCEL_HEADERS = ["Thời gian", "Biển số xe", "Trạng thái xe"]
stdout_lock = threading.Lock()
excel_lock = threading.Lock()
plate_model_lock = threading.Lock()
char_model_lock = threading.Lock()
stop_event = threading.Event()

# Standard YOLO char class order used as a fallback when model.names is not character-like.
DEFAULT_CHAR_MAP = {i: str(i) for i in range(10)}
DEFAULT_CHAR_MAP.update({10 + i: chr(ord("A") + i) for i in range(26)})


def error(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def emit_result(plate: str) -> None:
    """Only successful recognition lines go to stdout."""
    with stdout_lock:
        print(f"{datetime.now().strftime('%d/%m/%Y %H:%M:%S')} | {plate}", flush=True)


def normalize_plate(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def plate_to_display(rows: list[str], aspect: float) -> Optional[str]:
    """Apply VN plate layouts only; never substitute guessed characters."""
    rows = [re.sub(r"[^A-Z0-9]", "", row.upper()) for row in rows if row]
    if len(rows) >= 2:
        # Two-line Vietnamese motorcycle format: top 99F1, bottom 7777/77777.
        top, bottom = rows[0], rows[1]
        if re.fullmatch(r"\d{2}[A-Z]\d", top) and re.fullmatch(r"\d{4,5}", bottom):
            return f"{top[:2]}-{top[2:]} {bottom}"
        return None

    compact = rows[0] if rows else ""
    # If a two-line plate is detected as a single horizontal string due to perspective,
    # choose its motorcycle pattern only when the crop geometry supports two rows.
    if aspect < 1.95:
        match = re.fullmatch(r"(\d{2})([A-Z]\d)(\d{4,5})", compact)
        if match:
            return f"{match.group(1)}-{match.group(2)} {match.group(3)}"
    # Single-line Vietnamese car plate: 30A12345 / 30AB12345.
    match = re.fullmatch(r"(\d{2})([A-Z]{1,2})(\d{4,5})", compact)
    if match:
        return f"{match.group(1)}{match.group(2)}-{match.group(3)}"
    return None


def class_character(class_id: int, names) -> Optional[str]:
    label = None
    try:
        label = names.get(class_id) if isinstance(names, dict) else names[class_id]
    except Exception:
        pass
    if label is not None:
        label = str(label).strip().upper()
        if len(label) == 1 and label.isalnum():
            return label
    fallback = DEFAULT_CHAR_MAP.get(class_id)
    return fallback if fallback and len(fallback) == 1 else None


def bbox_iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = max(0, ax2 - ax1) * max(0, ay2 - ay1) + max(0, bx2 - bx1) * max(0, by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def improve_crop_lighting(crop: np.ndarray) -> tuple[np.ndarray, dict]:
    """Lightweight, conditional recovery for dark/glary crops; no fake detail synthesis."""
    if crop is None or crop.size == 0:
        return crop, {"sharpness": 0.0, "mean": 0.0, "glare": 0.0}
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mean_luma = float(np.mean(gray))
    glare_ratio = float(np.mean(gray >= 245))
    contrast = float(np.std(gray))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    quality = {"sharpness": sharpness, "mean": mean_luma, "glare": glare_ratio, "contrast": contrast}
    # For ordinary crops, use the original image: excessive enhancement can hurt character models.
    if mean_luma >= 82 and glare_ratio < 0.035 and contrast >= 28:
        return crop, quality
    ycrcb = cv2.cvtColor(crop, cv2.COLOR_BGR2YCrCb)
    y = ycrcb[:, :, 0]
    if mean_luma < 82:
        gamma = 0.68 if mean_luma < 48 else 0.80
        lut = np.array([min(255, int((i / 255.0) ** gamma * 255.0)) for i in range(256)], dtype=np.uint8)
        y = cv2.LUT(y, lut)
    if glare_ratio >= 0.035:
        yf = y.astype(np.float32)
        mask = yf > 190
        yf[mask] = 190 + (yf[mask] - 190) * 0.45
        y = np.clip(yf, 0, 255).astype(np.uint8)
    if contrast < 34 or glare_ratio >= 0.035 or mean_luma < 82:
        y = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(y)
    ycrcb[:, :, 0] = y
    enhanced = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)
    # Only mild sharpening on mildly soft images; severe blur is rejected later.
    if 10 < sharpness < 95:
        blur = cv2.GaussianBlur(enhanced, (0, 0), 0.7)
        enhanced = cv2.addWeighted(enhanced, 1.20, blur, -0.20, 0)
    return enhanced, quality


@dataclass
class PlateChar:
    text: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self): return (self.x1 + self.x2) / 2.0

    @property
    def cy(self): return (self.y1 + self.y2) / 2.0

    @property
    def height(self): return max(1.0, self.y2 - self.y1)


def de_duplicate_characters(chars: list[PlateChar]) -> list[PlateChar]:
    """Class-agnostic duplicate suppression helps remove overlapping 1/7 alternatives."""
    kept: list[PlateChar] = []
    for char in sorted(chars, key=lambda c: c.conf, reverse=True):
        box = (char.x1, char.y1, char.x2, char.y2)
        if any(bbox_iou(box, (k.x1, k.y1, k.x2, k.y2)) >= 0.58 for k in kept):
            continue
        kept.append(char)
    return kept


def split_character_rows(chars: list[PlateChar], crop_shape) -> list[list[PlateChar]]:
    """Split into one/two rows using the largest vertical gap between detected character centers."""
    if len(chars) < 6:
        return [sorted(chars, key=lambda c: c.cx)]
    ordered = sorted(chars, key=lambda c: c.cy)
    centers = [c.cy for c in ordered]
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
    if not gaps:
        return [sorted(chars, key=lambda c: c.cx)]
    split_idx = int(np.argmax(gaps))
    h = int(crop_shape[0])
    median_h = float(np.median([c.height for c in chars]))
    # A genuine two-row plate has a clear vertical separation between character centers.
    threshold = max(median_h * 0.70, h * 0.13)
    aspect = crop_shape[1] / max(1.0, h)
    if aspect < 2.75 and gaps[split_idx] >= threshold:
        upper = ordered[:split_idx + 1]
        lower = ordered[split_idx + 1:]
        if len(upper) >= 3 and len(lower) >= 3:
            return [sorted(upper, key=lambda c: c.cx), sorted(lower, key=lambda c: c.cx)]
    return [sorted(chars, key=lambda c: c.cx)]


def decode_character_boxes(result, crop: np.ndarray, names) -> Optional[dict]:
    if result is None or result.boxes is None or len(result.boxes) == 0:
        return None
    crop_h, crop_w = crop.shape[:2]
    chars: list[PlateChar] = []
    for box in result.boxes:
        try:
            conf = float(box.conf[0].item())
            if conf < CHAR_CONF_MIN:
                continue
            cls_id = int(box.cls[0].item())
            char = class_character(cls_id, names)
            if char is None:
                continue
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0].tolist()]
            x1, y1 = max(0.0, x1), max(0.0, y1)
            x2, y2 = min(float(crop_w), x2), min(float(crop_h), y2)
            if x2 - x1 < 2 or y2 - y1 < 4:
                continue
            chars.append(PlateChar(char, conf, x1, y1, x2, y2))
        except Exception:
            continue
    chars = de_duplicate_characters(chars)
    if len(chars) < 7 or len(chars) > 10:
        return None

    rows = split_character_rows(chars, crop.shape)
    texts = ["".join(c.text for c in row) for row in rows]
    aspect = crop_w / max(1.0, crop_h)
    plate = plate_to_display(texts, aspect)
    # If geometric row split was imperfect, try a conservative compact-string interpretation.
    # Do not apply character substitution; it must already fit a VN plate pattern.
    if plate is None and len(texts) > 1:
        compact = "".join(texts)
        plate = plate_to_display([compact], aspect)
    if plate is None:
        return None

    confidences = [c.conf for c in chars]
    mean_conf = float(np.mean(confidences))
    min_conf = float(np.min(confidences))
    # Character coordinates for overlay are preserved so the UI can show each model prediction.
    return {
        "plate": plate,
        "mean_char_conf": mean_conf,
        "min_char_conf": min_conf,
        "chars": chars,
        "rows": texts,
        "char_count": len(chars),
        "quality": {},
    }


# ------------------------------ MODELS ------------------------------
def resolve_char_model_path() -> Path:
    env_path = os.getenv("ANPR_CHAR_MODEL", "").strip()
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([
        ROOT / "Letters_detection.pt",
        ROOT / "models" / "Letters_detection.pt",
        ROOT / "final_char_yolo.pt",
        ROOT / "models" / "final_char_yolo.pt",
        Path(__file__).resolve().parent / "Letters_detection.pt",
        Path(__file__).resolve().parent / "models" / "Letters_detection.pt",
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


if not PLATE_MODEL_PATH.is_file():
    raise SystemExit(f"Không tìm thấy model phát hiện biển số: {PLATE_MODEL_PATH}")
CHAR_MODEL_PATH = resolve_char_model_path()
if not CHAR_MODEL_PATH.is_file():
    raise SystemExit(
        "Thiếu model NHẬN DIỆN TỪNG KÝ TỰ: " + str(CHAR_MODEL_PATH) + "\n"
        "Bạn cần thêm Letters_detection.pt được huấn luyện cho biển số Việt Nam vào D:\\SmartParking.\n"
        "Repo tham khảo kiến trúc hai YOLO: https://github.com/phatnomenal/Vietnamese-License-Plate-Detecttion\n"
        "Chỉ license_plate_detector.pt không thể thay thế model nhận diện ký tự."
    )

# Sharing the weights avoids loading four network copies on a CPU-only PC. Each network
# has a separate lock; the two gate threads always process the newest available frame.
plate_model = YOLO(str(PLATE_MODEL_PATH))
char_model = YOLO(str(CHAR_MODEL_PATH))


def model_predict(model, lock, source, imgsz, conf, max_det):
    kwargs = dict(source=source, imgsz=imgsz, conf=conf, iou=0.45,
                  max_det=max_det, verbose=False, agnostic_nms=True)
    if CUDA_AVAILABLE:
        kwargs["device"] = 0
        kwargs["half"] = True
    else:
        kwargs["device"] = "cpu"
    with lock:
        return model.predict(**kwargs)[0]


def find_plate_regions(frame: np.ndarray, gate: str, fallback: bool = False):
    imgsz = (PLATE_IMGSZ_OUT if gate == "CONG RA" else PLATE_IMGSZ_IN)
    conf = (PLATE_CONF_OUT if gate == "CONG RA" else PLATE_CONF_IN)
    if fallback:
        imgsz = max(512, imgsz)
        enhanced, _ = improve_crop_lighting(frame)
        frame_in = enhanced
    else:
        frame_in = frame
    result = model_predict(plate_model, plate_model_lock, frame_in, imgsz, conf, 3)
    h, w = frame.shape[:2]
    regions = []
    if result.boxes is None:
        return regions
    boxes = sorted(result.boxes, key=lambda b: float(b.conf[0]), reverse=True)
    for box in boxes[:max(1, MAX_PLATES_PER_FRAME)]:
        try:
            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 - x1 < 24 or y2 - y1 < 12:
                continue
            pad_x = max(3, int((x2 - x1) * 0.06))
            pad_y = max(3, int((y2 - y1) * 0.10))
            cx1, cy1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
            cx2, cy2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
            crop = frame[cy1:cy2, cx1:cx2]
            if crop.size:
                regions.append((crop, (x1, y1, x2, y2), float(box.conf[0]), (cx1, cy1)))
        except Exception:
            continue
    return regions


def recognize_plate_crop(crop: np.ndarray, gate: str):
    enhanced, quality = improve_crop_lighting(crop)
    char_imgsz = CHAR_IMGSZ_OUT if gate == "CONG RA" else CHAR_IMGSZ_IN
    result = model_predict(char_model, char_model_lock, enhanced, char_imgsz, CHAR_CONF_MIN, 16)
    decoded = decode_character_boxes(result, enhanced, char_model.names)
    if decoded is None:
        return None
    decoded["quality"] = quality
    # Very severe blur or underexposure cannot be restored by preprocessing; fail closed.
    if quality["sharpness"] < 4.0 or (quality["mean"] < 12 and quality["contrast"] < 8):
        return None
    if decoded["mean_char_conf"] < CHAR_CONFIRM_MEAN or decoded["min_char_conf"] < CHAR_CONFIRM_MIN:
        return None
    return decoded


# ------------------------------ EXCEL STATE ------------------------------
def ensure_excel():
    if EXCEL_PATH.exists():
        try:
            wb = load_workbook(EXCEL_PATH)
            ws = wb["Nhat ky"] if "Nhat ky" in wb.sheetnames else wb.active
            ws.title = "Nhat ky"
            if ws.max_row == 1 and all(ws.cell(1, c).value is None for c in range(1, 4)):
                ws.append(EXCEL_HEADERS)
            else:
                current = [ws.cell(1, i).value for i in range(1, 4)]
                if current != EXCEL_HEADERS:
                    wb.close()
                    raise ValueError("Header smartparking.xlsx phải là Thời gian | Biển số xe | Trạng thái xe")
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = f"A1:C{max(1, ws.max_row)}"
            for col, width in zip("ABC", [22, 24, 20]):
                ws.column_dimensions[col].width = width
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F4E78")
                cell.alignment = Alignment(horizontal="center")
            wb.save(EXCEL_PATH)
            wb.close()
            return
        except PermissionError as exc:
            raise SystemExit(f"Hãy đóng smartparking.xlsx trong Excel rồi chạy lại: {EXCEL_PATH}") from exc
    wb = Workbook()
    ws = wb.active
    ws.title = "Nhat ky"
    ws.append(EXCEL_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"
    for col, width in zip("ABC", [22, 24, 20]):
        ws.column_dimensions[col].width = width
    wb.save(EXCEL_PATH)
    wb.close()


def load_active_plates() -> dict[str, str]:
    active = {}
    if not EXCEL_PATH.exists():
        return active
    with excel_lock:
        wb = load_workbook(EXCEL_PATH, read_only=True, data_only=True)
        try:
            ws = wb["Nhat ky"] if "Nhat ky" in wb.sheetnames else wb.active
            for plate, status in ws.iter_rows(min_row=2, min_col=2, max_col=3, values_only=True):
                if not plate or not status:
                    continue
                key = normalize_plate(str(plate))
                if str(status).upper().strip() == "VÀO":
                    active[key] = str(plate)
                elif str(status).upper().strip() == "RA":
                    active.pop(key, None)
        finally:
            wb.close()
    return active



def excel_transition(gate: str, plate: str, active: dict[str, str], active_lock: threading.Lock):
    key = normalize_plate(plate)
    is_entry = gate == "CONG VAO"

    # Kiem tra va cap nhat trang thai xe
    with active_lock:
        if is_entry:
            # Xe da trong bai thi khong ghi them su kien vao
            if key in active:
                return

            status = "VÀO"
            active[key] = plate

        else:
            # Khong ghi nhan xe ra neu xe khong co trong bai
            if key not in active:
                return

            # Dung bien so da luu truoc do
            plate = active.pop(key)
            status = "RA"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Ghi Excel
        with excel_lock:
            wb = load_workbook(EXCEL_PATH)
            try:
                ws = wb["Nhat ky"] if "Nhat ky" in wb.sheetnames else wb.active
                ws.append([timestamp, plate, status])
                ws.auto_filter.ref = f"A1:C{ws.max_row}"
                wb.save(EXCEL_PATH)
            finally:
                wb.close()

    # CHI GUI FIREBASE KHI DA XAC NHAN LUOT XE HOP LE
    try:
        firebase_gate = "CONG_VAO" if is_entry else "CONG_RA"

        upload_vehicle_event(
            plate=plate,
            gate=firebase_gate,
            status=status
        )

    except Exception as exc:
        error(f"[FIREBASE ERROR] {firebase_gate}: {exc}")

    with active_lock:
        if is_entry:
            if key in active:
                return
            status = "VÀO"
            active[key] = plate
        else:
            if key not in active:
                # recognition is still printed; don't invent an exit record if no entry exists.
                return
            plate = active.pop(key)
            status = "RA"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with excel_lock:
            wb = load_workbook(EXCEL_PATH)
            try:
                ws = wb["Nhat ky"] if "Nhat ky" in wb.sheetnames else wb.active
                ws.append([timestamp, plate, status])
                ws.auto_filter.ref = f"A1:C{ws.max_row}"
                wb.save(EXCEL_PATH)
            finally:
                wb.close()


# ------------------------------ CAMERA + VOTING ------------------------------
class CameraStream:
    def __init__(self, name: str, index: int):
        self.name = name
        self.index = index
        self.frame = None
        self.frame_id = 0
        self.captured_at = 0.0
        self.fps = 0.0
        self.error = ""
        self.lock = threading.Lock()
        self.stopped = False
        self.cap = None
        self.thread = threading.Thread(target=self._capture_loop, daemon=True, name=f"capture-{name}")

    def start(self):
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(self.index, backend)
        if not self.cap.isOpened():
            self.cap.release()
            self.cap = cv2.VideoCapture(self.index)
        if not self.cap.isOpened():
            self.error = f"Cannot open camera index={self.index}"
            error(f"[CAMERA ERROR] {self.name}: {self.error}")
            return self
        try:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
            self.cap.set(cv2.CAP_PROP_FPS, CAM_FPS)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if hasattr(cv2, "CAP_PROP_AUTOFOCUS"):
                # Best effort only; fixed-focus cameras may ignore this property.
                self.cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        except Exception:
            pass
        self.thread.start()
        return self

    def _capture_loop(self):
        count = 0
        t0 = time.monotonic()
        failed = 0
        while not self.stopped:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                failed += 1
                time.sleep(0.01)
                if failed >= 50:
                    self.error = "Camera frame read failed"
                    failed = 0
                continue
            now = time.monotonic()
            with self.lock:
                self.frame = frame
                self.frame_id += 1
                self.captured_at = now
            count += 1
            elapsed = now - t0
            if elapsed >= 1.0:
                self.fps = count / elapsed
                count = 0
                t0 = now

    def latest(self):
        with self.lock:
            if self.frame is None:
                return None, self.frame_id, self.captured_at
            return self.frame.copy(), self.frame_id, self.captured_at

    def stop(self):
        self.stopped = True
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.cap is not None:
            self.cap.release()


class Consensus:
    def __init__(self):
        self.votes = {gate: deque(maxlen=8) for gate in CAMERAS}
        self.last_emitted = {}
        self.lock = threading.Lock()

    def add(self, gate, plate, mean_conf, min_conf, plate_conf, bbox, frame_id, captured_at):
        now = time.monotonic()
        normalized = normalize_plate(plate)
        with self.lock:
            votes = self.votes[gate]
            while votes and now - votes[0]["seen"] > VOTE_WINDOW_S:
                votes.popleft()
            # A different plate in the same physical location invalidates stale candidates.
            for old in list(votes):
                if bbox_iou(old["bbox"], bbox) >= 0.15 and old["norm"] != normalized:
                    votes.remove(old)
            if votes and votes[-1]["frame_id"] == frame_id:
                return False
            votes.append({"norm": normalized, "plate": plate, "mean": mean_conf,
                          "min": min_conf, "plate_conf": plate_conf, "bbox": bbox,
                          "frame_id": frame_id, "capture": captured_at, "seen": now})
            current = [v for v in votes if v["norm"] == normalized and bbox_iou(v["bbox"], bbox) >= 0.15]
            distinct = {v["frame_id"] for v in current}
            high_conf_single = mean_conf >= FAST_MEAN_CONF and min_conf >= FAST_MIN_CHAR_CONF and plate_conf >= FAST_PLATE_CONF
            if len(distinct) < 2 and not high_conf_single:
                return False
            # Enforce true capture-to-decision latency, including model inference time.
            # Comparing capture timestamps alone would under-report latency on a slow CPU.
            first = min(current, key=lambda v: v["capture"]) if len(distinct) >= 2 else current[-1]
            end_to_end_s = now - float(first["capture"])
            if end_to_end_s > MAX_EVENT_LATENCY_S:
                votes.clear()
                return False
            key = (gate, normalized)
            last = self.last_emitted.get(key, 0.0)
            if now - last < DEDUP_S:
                return False
            self.last_emitted[key] = now
            votes.clear()
            return True


def draw_overlay(frame: np.ndarray, gate: str, items: list[dict]) -> np.ndarray:
    out = frame.copy()
    # Panel header.
    cv2.rectangle(out, (0, 0), (out.shape[1], 34), (28, 28, 28), -1)
    cv2.putText(out, f"{gate} | {out.shape[1]}x{out.shape[0]}", (10, 23),
                cv2.FONT_HERSHEY_SIMPLEX, 0.60, (255, 255, 255), 1, cv2.LINE_AA)
    for item in items:
        x1, y1, x2, y2 = item["bbox"]
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 210, 0), 2)
        text = item.get("plate") or "PLATE"
        cv2.putText(out, text, (x1, max(54, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.70, (0, 255, 0), 2, cv2.LINE_AA)
        crop_x1, crop_y1 = item.get("crop_origin", (x1, y1))
        for ch in item.get("chars", []):
            cx1 = int(crop_x1 + ch.x1)
            cy1 = int(crop_y1 + ch.y1)
            cx2 = int(crop_x1 + ch.x2)
            cy2 = int(crop_y1 + ch.y2)
            cv2.rectangle(out, (cx1, cy1), (cx2, cy2), (255, 100, 30), 1)
            cv2.putText(out, ch.text, (cx1, max(38, cy1 - 2)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.38, (255, 255, 0), 1, cv2.LINE_AA)
    return out


consensus = Consensus()
latest_display = {gate: [] for gate in CAMERAS}
display_lock = threading.Lock()


def gate_worker(gate: str, camera: CameraStream, active: dict[str, str], active_lock: threading.Lock):
    last_frame_id = -1
    last_fallback = 0.0
    while not stop_event.is_set():
        frame, frame_id, captured_at = camera.latest()
        if frame is None or frame_id == last_frame_id:
            time.sleep(0.003)
            continue
        last_frame_id = frame_id
        try:
            started = time.perf_counter()
            regions = find_plate_regions(frame, gate, fallback=False)
            if not regions and time.monotonic() - last_fallback >= FALLBACK_INTERVAL_S:
                last_fallback = time.monotonic()
                regions = find_plate_regions(frame, gate, fallback=True)
            display_items = []
            for crop, bbox, detector_conf, crop_origin in regions:
                result = recognize_plate_crop(crop, gate)
                if result is None:
                    display_items.append({"bbox": bbox, "plate": "", "chars": [], "crop_origin": crop_origin})
                    continue
                item = {"bbox": bbox, "plate": result["plate"], "chars": result["chars"],
                        "crop_origin": crop_origin, "confidence": result["mean_char_conf"]}
                display_items.append(item)
                confirmed = consensus.add(
                    gate=gate, plate=result["plate"], mean_conf=result["mean_char_conf"],
                    min_conf=result["min_char_conf"], plate_conf=detector_conf, bbox=bbox,
                    frame_id=frame_id, captured_at=captured_at,
                )
                if confirmed:
                    # Emit immediately after recognition confirmation, before disk I/O.
                    emit_result(result["plate"])
                    try:
                        excel_transition(gate, result["plate"], active, active_lock)
                    except Exception as exc:
                        error(f"[EXCEL ERROR] {exc}")
            with display_lock:
                latest_display[gate] = display_items
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            # No routine performance diagnostics are printed; output stays clean.
            _ = elapsed_ms
        except Exception as exc:
            error(f"[ANPR ERROR] {gate}: {exc}")
            time.sleep(0.01)


def main():
    if not PLATE_MODEL_PATH.is_file():
        raise SystemExit(f"Không tìm thấy model biển số: {PLATE_MODEL_PATH}")
    if not CHAR_MODEL_PATH.is_file():
        raise SystemExit(f"Không tìm thấy model ký tự: {CHAR_MODEL_PATH}")
    ensure_excel()
    active = load_active_plates()
    active_lock = threading.Lock()

    # Warm-up once at start so the first car does not pay model initialization cost.
    try:
        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        model_predict(plate_model, plate_model_lock, blank, PLATE_IMGSZ_IN, PLATE_CONF_IN, 1)
        blank_crop = np.zeros((64, 240, 3), dtype=np.uint8)
        model_predict(char_model, char_model_lock, blank_crop, CHAR_IMGSZ_IN, CHAR_CONF_MIN, 10)
    except Exception as exc:
        error(f"[WARMUP WARNING] {exc}")

    streams = {gate: CameraStream(gate, idx).start() for gate, idx in CAMERAS.items()}
    workers = []
    for gate, stream in streams.items():
        if stream.cap is not None and stream.cap.isOpened():
            t = threading.Thread(target=gate_worker, args=(gate, stream, active, active_lock),
                                 daemon=True, name=f"recognize-{gate}")
            t.start()
            workers.append(t)

    if not workers:
        for stream in streams.values():
            stream.stop()
        raise SystemExit("Không mở được camera nào. Kiểm tra chỉ số camera ANPR_CAM_IN/ANPR_CAM_OUT.")

    window = "SMART PARKING - CAMERA VAO / CAMERA RA"
    if SHOW_WINDOW:
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window, 1280, 420)
    try:
        while True:
            if SHOW_WINDOW:
                panels = []
                for gate, stream in streams.items():
                    frame, _, _ = stream.latest()
                    if frame is None:
                        frame = np.zeros((CAM_HEIGHT, CAM_WIDTH, 3), dtype=np.uint8)
                    with display_lock:
                        items = list(latest_display[gate])
                    panel = draw_overlay(frame, gate, items)
                    target_w = DISPLAY_WIDTH
                    target_h = max(1, int(panel.shape[0] * target_w / max(1, panel.shape[1])))
                    panel = cv2.resize(panel, (target_w, target_h), interpolation=cv2.INTER_AREA)
                    panels.append(panel)
                if len(panels) == 2:
                    cv2.imshow(window, np.hstack(panels))
                else:
                    cv2.imshow(window, panels[0])
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27):
                    break
            else:
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=2)
        for stream in streams.values():
            stream.stop()
        if SHOW_WINDOW:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
