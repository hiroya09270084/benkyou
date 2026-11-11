import csv
import argparse
import ast
import copy
import os
import socket
import uuid
from collections import OrderedDict, deque
import statistics
import re
import queue
import threading
import time
import tkinter as tk
import traceback
import sys
#192,168,1,155
try:
    from picamera2 import Picamera2, Preview
    PICAMERA2_AVAILABLE = True
except ImportError:
    Picamera2 = None
    Preview = None
    PICAMERA2_AVAILABLE = False

try:
    from pyzbar.pyzbar import decode
    PYZBAR_AVAILABLE = True
except ImportError:
    decode = None
    PYZBAR_AVAILABLE = False

try:
    from libcamera import controls
    LIBCAMERA_AVAILABLE = True
except ImportError:
    controls = None
    LIBCAMERA_AVAILABLE = False

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO = None
    GPIO_AVAILABLE = False

from pathlib import Path

SERVER_ROI_CONFIG_PATH = Path(__file__).resolve().parent / "server_cart_roi.csv"
SERVER_OVERVIEW_WIDTH = 960
SERVER_CROP_LEFT = 0.05
SERVER_CROP_RIGHT = 0.95
SERVER_CROP_TOP = 0.05
SERVER_CROP_BOTTOM = 0.95

from tkinter import ttk
SIMULATION_MODE = False
# テスト用: 超音波センサーを使わずカメラを起動し続ける
CAMERA_ALWAYS_ON = False
# AGV受信サーバー設定: サーバーPiのLAN内IPにクライアントから接続する。
# host=0.0.0.0 は全LANインターフェースで受け付けるという意味。
SOCKET_BIND_HOST = "0.0.0.0"
SOCKET_PORT = 5000
SOCKET_MAX_MESSAGE_BYTES = 256
SOCKET_ACK_WAIT_SEC = 8.0
SOCKET_EVENT_HISTORY_LIMIT = 5000

# 画面上の枠は上側最大19＋下側最大7（合計26枠）。実際の台車QRは1～24のみ。
UPPER_SIDE_COUNT = 19
LOWER_SIDE_COUNT = 7
CARTS_19_COUNT = UPPER_SIDE_COUNT  # 旧変数名を互換維持
CARTS_5_COUNT = LOWER_SIDE_COUNT   # 旧変数名を互換維持
TOTAL_CART_COUNT = 24             # QRの実台数（表示枠数とは独立）
TOTAL_CART_SLOTS = UPPER_SIDE_COUNT + LOWER_SIDE_COUNT
# 未読番号を点検エリアに表示するのは、この回数の周回が終了してから。
NEVER_READ_INSPECTION_AFTER_LAPS = 3
# 下側の運用台数（1～7）。初期値は5。画面から変更・保存できる。
DEFAULT_ACTIVE_BOTTOM_COUNT = 5
ACTIVE_BOTTOM_FILE = Path(__file__).resolve().parent / "active_bottom_count.txt"

def load_active_bottom_count():
    try:
        count = int(ACTIVE_BOTTOM_FILE.read_text(encoding="utf-8").strip())
        if 1 <= count <= LOWER_SIDE_COUNT:
            return count
    except (OSError, ValueError):
        pass
    return DEFAULT_ACTIVE_BOTTOM_COUNT

CART_QR_ALL_NUMBERS = {str(n) for n in range(1, TOTAL_CART_COUNT + 1)}
# 旧7台側: インパネ2枠 / 間隔 / クォーターウィンドウ1枠 / 間隔 / 名称未設定4枠
BOTTOM_SLOT_CENTERS = [92, 162, 286, 410, 475, 540, 605]
MAX_AGV_COUNT = 11
AGV_COUNT = 5
TOTAL_AGV_COUNT = MAX_AGV_COUNT
AGV_REQUIRED_QR_NUMBERS = tuple(range(1, MAX_AGV_COUNT + 1))

LAYOUT_FILE = Path(__file__).resolve().parent / "cart_layout.csv"
RUNTIME_STATE_FILE = Path(__file__).resolve().parent / "server_runtime_state.csv"
RESTORE_PREVIOUS_STATE = True
DEFAULT_CARTS_19_SIDE = [""] * CARTS_19_COUNT  # 実台数はQRの初回周回で確定
DEFAULT_CARTS_5_SIDE = [""] * CARTS_5_COUNT  # 5台側も番号はQRから学習、枠数だけ固定で5

CARTS_19_SIDE = DEFAULT_CARTS_19_SIDE.copy()
CARTS_5_SIDE = DEFAULT_CARTS_5_SIDE.copy()
CARTS_19_DISPLAY_ORDER = CARTS_19_SIDE.copy()
CARTS_20_SIDE = CARTS_19_SIDE
CARTS_20_DISPLAY_ORDER = CARTS_19_DISPLAY_ORDER


def apply_cart_layout(carts_19, carts_5):
    """19台側・5台側の配置をプログラム内へ反映する。"""
    global CARTS_19_SIDE, CARTS_5_SIDE
    global CARTS_20_SIDE, CARTS_19_DISPLAY_ORDER, CARTS_20_DISPLAY_ORDER

    carts_19 = [str(value).strip() for value in list(carts_19)[:CARTS_19_COUNT]]
    carts_5 = [str(value).strip() for value in list(carts_5)[:CARTS_5_COUNT]]
    carts_19 += [""] * (CARTS_19_COUNT - len(carts_19))
    carts_5 += [""] * (CARTS_5_COUNT - len(carts_5))

    CARTS_19_SIDE = carts_19
    CARTS_5_SIDE = carts_5
    CARTS_19_DISPLAY_ORDER = CARTS_19_SIDE.copy()
    CARTS_20_SIDE = CARTS_19_SIDE
    CARTS_20_DISPLAY_ORDER = CARTS_19_DISPLAY_ORDER


def load_cart_layout():
    """19台側＋最大7台側をCSVから読み込む。旧17＋7/19＋5も読み取り可能。"""
    try:
        if LAYOUT_FILE.exists():
            with LAYOUT_FILE.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))
            loaded = {}
            for row in rows:
                if row and row[0].strip() in ("19台側", "17台側", "7台側", "5台側"):
                    loaded[row[0].strip()] = [str(value).strip() for value in row[1:]]
            # 新しい19＋7を優先し、過去に保存した17＋7や19＋5も使う。
            top = loaded.get("19台側", loaded.get("17台側", []))
            bottom = loaded.get("7台側", loaded.get("5台側", []))
            # 旧設定に含まれる台車番号を失わず、新しい上側19枠へ優先配置。
            top = list(dict.fromkeys(value for value in top if value))
            bottom = list(dict.fromkeys(value for value in bottom if value and value not in top))
            while len(top) < CARTS_19_COUNT and bottom:
                top.append(bottom.pop(0))
            apply_cart_layout(top, bottom)
            return
    except (OSError, csv.Error, ValueError, TypeError) as error:
        print("台車配置設定の読込エラー:", error)
    apply_cart_layout(DEFAULT_CARTS_19_SIDE, DEFAULT_CARTS_5_SIDE)


load_cart_layout()
AGV_CYCLE_SEC = 60.0
AGV_LOAD_RATIO = 0.20
AGV_TO_20_RATIO = 0.55
AGV_UNLOAD_RATIO = 0.65
AGV_DISPLAY_UPDATE_MS = 500
AGV_UNLOAD_HOLD_SEC = 5.0
AGV_INSPECTION_MOVE_SEC = 3.0  # AGVが黄色い点検エリアに入る表示時間
AGV_PASS_RETURN_SEC = 80.0  # 通常AGVが19台側を通過し、5台側に戻るまでの時間
TRIG_PIN = 24  # BCM24 / 物理18番ピン
ECHO_PIN = 23  # BCM23 / 物理16番ピン
DETECT_DISTANCE_CM = 60.0  # 台車検知距離（cm）
# 1023cmなどの範囲外値は採用しない
SENSOR_MIN_CM = 2.0
SENSOR_MAX_CM = 350.0
SENSOR_FILTER_COUNT = 5
SENSOR_ON_COUNT = 3
SENSOR_OFF_COUNT = 3
# 新しい有効測定が1秒間ない場合は履歴を破棄し、停止方向に判定する
SENSOR_VALID_TIMEOUT_SEC = 1.0
CAMERA_SIZE = (1280, 720)
CAMERA_FPS = 20.0
MANUAL_LENS_POSITION = 1.0

CAMERA_DUPLICATE_IGNORE_SEC = 1.0
CAMERA_QR_REARM_ABSENCE_SEC = 0.35


def newly_visible_qrs(frame_codes, now, presence_state):
    """フレームから消えた後に再登場したQRだけを通知する。

    presence_state[qr] は (最後に見た時刻, 消失開始時刻またはNone)。
    カメラが一時停止しても、消失したフレームがなければ再通過とはしない。
    """
    accepted = []
    unique_codes = list(dict.fromkeys(code for code in frame_codes if code))
    visible = set(unique_codes)

    for code in list(presence_state):
        last_seen, missing_since = presence_state[code]
        if code not in visible:
            if missing_since is None:
                missing_since = now
            if now - last_seen > 5.0:
                del presence_state[code]
            else:
                presence_state[code] = (last_seen, missing_since)

    for code in unique_codes:
        past = presence_state.get(code)
        if past is None:
            accepted.append(code)
        else:
            _, missing_since = past
            if (missing_since is not None
                    and now - missing_since >= CAMERA_QR_REARM_ABSENCE_SEC):
                accepted.append(code)
        presence_state[code] = (now, None)
    return accepted
LED1_PIN = 17
LED2_PIN = 27
LED2_DELAY_SEC = 5.0

DISPLAY_STEP_MS = 150
CAMERA_11_OCLOCK_POSITION = (130, 28)

ONE_CAMERA_POSITIONS = [(150, 42), (250, 42), (350, 42), (450, 42), (550, 42), (650, 42), (750, 42), (850, 42)]
AGV_ROUTE_POSITIONS = [
    (130, 24), (450, 24), (800, 24), (1060, 24),
    (1150, 60), (1215, 120), (1230, 190), (1195, 260),
    (1110, 310), (990, 340), (860, 338), (740, 332),
    (620, 326), (500, 320), (380, 312), (280, 307),
    (190, 304), (70, 285), (18, 235), (12, 160),
    (20, 80), (48, 38), (90, 24), (130, 24)
]
AGV_5SIDE_POSITIONS = [(x, 377) for x in BOTTOM_SLOT_CENTERS]
AGV_19SIDE_POSITIONS = [(1090, 42), (1040, 42), (990, 42), (940, 42), (890, 42),
                         (840, 42), (790, 42), (740, 42), (690, 42), (640, 42),
                         (590, 42), (540, 42), (490, 42), (440, 42), (390, 42),
                         (340, 42), (290, 42), (240, 42), (190, 42)]

DISPLAY_ROUTE_POSITIONS = AGV_ROUTE_POSITIONS[:]

class CsvLogger:

    def __init__(self, directory=None):
        log_directory = Path(directory) if directory is not None else Path(__file__).resolve().parent
        self.qr_path = log_directory / "qr_read_log.csv"
        self.error_path = log_directory / "error_log.csv"
        print(self.qr_path)
        self.lock = threading.Lock()
        self.qr_header = ["日時", "動作モード", "カメラ", "台車番号", "処理結果"]
        self.error_header = ["日時", "場所", "カメラ", "対象データ", "エラー種類", "内容", "詳細"]
        self.append_csv(self.qr_path, self.qr_header)
        self.append_csv(self.error_path, self.error_header)
        print(f"QR:{self.qr_path}")
        print(f"error:{self.error_path}")

    def append_csv(self, path, header, row=None):
        try:
            with self.lock:
                empty = not path.exists() or path.stat().st_size == 0
                encoding = "utf-8-sig" if empty else "utf-8"

                with path.open("a", encoding=encoding, newline="") as csv_file:
                    writer = csv.writer(csv_file)

                    if empty:
                        writer.writerow(header)

                    if row is not None:
                        writer.writerow([self.excel_text(value) for value in row])

        except (OSError, csv.Error) as error:
            print(f"CSV保存エラー ({path.name}): {error}")
            if path != self.error_path:
                self.log_error("CSV保存", "-", str(path), error)

    def excel_text(self, value):
        print(value)
        result = str(value).replace("\r", " ").replace("\n", " | ")
        
        if result.lstrip().startswith(("=", "+", "-", "@")):
            result = "'" + result

        print(result)
        return result

    def log_qr(self, timestamp, run_mode, camera, cart, result):
        self.append_csv(self.qr_path, self.qr_header, [timestamp,"+"+run_mode, camera, cart, result])

    def log_error(self, place, camera, data, error):
        error_type = type(error).__name__
        detail = "".join(traceback.format_exception(type(error), error, error.__traceback__)) if error.__traceback__ else ""
        timestamp = time.strftime("%Y/%m/%d %H:%M:%S")
        self.append_csv(self.error_path, self.error_header, [timestamp, place, camera, data, error_type, str(error), detail])


class AgvCsvLogger:

    def __init__(self, directory=None):
        log_directory = Path(directory) if directory is not None else Path(__file__).resolve().parent
        self.path = log_directory / "agv_read_log.csv"
        self.lock = threading.Lock()
        self.header = ["日時", "AGV QR", "牽引台車"]
        self.append_csv(self.header)
        print(f"AGV CSV:{self.path}")

    def excel_text(self, value):
        result = str(value).replace("\r", " ").replace("\n", " | ")
        if result.lstrip().startswith(("=", "+", "-", "@")):
            result = "'" + result
        return result

    def append_csv(self, row=None):
        try:
            with self.lock:
                empty = not self.path.exists() or self.path.stat().st_size == 0
                encoding = "utf-8-sig" if empty else "utf-8"
                with self.path.open("a", encoding=encoding, newline="") as csv_file:
                    writer = csv.writer(csv_file)
                    if empty:
                        writer.writerow(self.header)
                    if row is not None:
                        writer.writerow([self.excel_text(value) for value in row])
        except (OSError, csv.Error) as error:
            print(f"AGV CSV保存エラー: {error}")

    def log(self, agv_qr, carts):
        timestamp = time.strftime("%Y/%m/%d %H:%M:%S")
        self.append_csv([timestamp, agv_qr, ",".join(carts) if carts else "なし"])


AGV_JUDGMENT_VERSION = "2026-10-30・実通過を境界から分離・周回点検修正版"

class QRRegionEditor:
    """Full-frame overview with mouse-editable QR search region.

    Left drag inside region: move. Drag bottom-right handle: resize.
    Drag outside region: draw new region. Right click: reset to default.
    """

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.window_name = "SERVER camera - select cart QR area"
        self.scale = min(1.0, SERVER_OVERVIEW_WIDTH / width)
        self.display_width = max(1, int(round(width * self.scale)))
        self.display_height = max(1, int(round(height * self.scale)))
        self.default = (
            int(width * SERVER_CROP_LEFT), int(height * SERVER_CROP_TOP),
            int(width * SERVER_CROP_RIGHT), int(height * SERVER_CROP_BOTTOM)
        )
        self.rect = list(self.default)
        self.mode = None
        self.start = None
        self.before = None
        self._load()

    def _load(self):
        try:
            with SERVER_ROI_CONFIG_PATH.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            if len(rows) >= 2 and rows[0] == ["left", "top", "right", "bottom"]:
                nums = [float(v) for v in rows[1]]
                self.rect = [
                    round(nums[0] * self.width), round(nums[1] * self.height),
                    round(nums[2] * self.width), round(nums[3] * self.height)
                ]
                self._clamp()
                print(f"[QR枠] 保存設定を読み込み: {self.rect}", flush=True)
        except FileNotFoundError:
            pass
        except (ValueError, OSError, csv.Error) as error:
            print(f"[QR枠] 設定読込失敗、初期値を使用: {error}", flush=True)
            self.rect = list(self.default)

    def save(self):
        coords = [
            self.rect[0] / self.width, self.rect[1] / self.height,
            self.rect[2] / self.width, self.rect[3] / self.height
        ]
        with SERVER_ROI_CONFIG_PATH.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["left", "top", "right", "bottom"])
            writer.writerow([f"{v:.7f}" for v in coords])
        print(f"[QR枠] 設定保存: {SERVER_ROI_CONFIG_PATH} 画素={self.rect}", flush=True)

    def reset(self):
        self.rect = list(self.default)
        print("[QR枠] 初期値に戻しました (wで保存)", flush=True)

    def _clamp(self):
        x1, y1, x2, y2 = self.rect
        x1, x2 = sorted((max(0, min(self.width, x1)), max(0, min(self.width, x2))))
        y1, y2 = sorted((max(0, min(self.height, y1)), max(0, min(self.height, y2))))
        if x2 - x1 < 30:
            x2 = min(self.width, x1 + 30)
            x1 = max(0, x2 - 30)
        if y2 - y1 < 30:
            y2 = min(self.height, y1 + 30)
            y1 = max(0, y2 - 30)
        self.rect = [int(x1), int(y1), int(x2), int(y2)]

    def on_mouse(self, event, dx, dy, flags, userdata):
        # Callback runs on the same thread as cv2.waitKey(), so no locks needed.
        import cv2
        px = max(0, min(self.width-1, round(dx / self.scale)))
        py = max(0, min(self.height-1, round(dy / self.scale)))
        x1,y1,x2,y2 = self.rect
        handle = max(20, int(18 / self.scale))
        if event == cv2.EVENT_RBUTTONDOWN:
            self.reset()
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            self.start = (px, py)
            self.before = self.rect[:]
            if abs(px-x2) <= handle and abs(py-y2) <= handle:
                self.mode = "resize"
            elif x1 <= px <= x2 and y1 <= py <= y2:
                self.mode = "move"
            else:
                self.mode = "new"
                self.rect = [px, py, px+30, py+30]
        elif event == cv2.EVENT_MOUSEMOVE and self.mode is not None and (flags & cv2.EVENT_FLAG_LBUTTON):
            sx, sy = self.start
            if self.mode == "move":
                oldx1, oldy1, oldx2, oldy2 = self.before
                shift_x = max(-oldx1, min(self.width-oldx2, px-sx))
                shift_y = max(-oldy1, min(self.height-oldy2, py-sy))
                self.rect = [oldx1+shift_x, oldy1+shift_y, oldx2+shift_x, oldy2+shift_y]
            elif self.mode == "resize":
                self.rect = [self.before[0], self.before[1], px, py]
                self._clamp()
            elif self.mode == "new":
                self.rect = [sx, sy, px, py]
                self._clamp()
        elif event == cv2.EVENT_LBUTTONUP:
            self.mode = None
            self._clamp()

    def render(self, frame):
        import cv2
        image = cv2.resize(frame, (self.display_width, self.display_height), interpolation=cv2.INTER_AREA)
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        x1,y1,x2,y2 = (int(v*self.scale) for v in self.rect)
        cv2.rectangle(image, (x1,y1), (x2,y2), (0,255,0), 2)
        cv2.rectangle(image, (x2-7,y2-7), (x2+7,y2+7), (0,255,255), -1)
        cv2.rectangle(image, (0,0), (self.display_width,38), (15,15,15), -1)
        cv2.putText(image, "Drag: MOVE / CORNER: RESIZE / OUTSIDE: NEW", (8,16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.43, (255,255,255), 1)
        cv2.putText(image, "w: SAVE ROI  r/right-click: RESET  s: IMAGE  q: QUIT", (8,32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.43, (255,255,255), 1)
        return image



# 停止・再起動後も画面の台車配置、AGV状態、学習順・点検を引き継ぐ。
# カメラ/GUIオブジェクトは保存しない。CSVは操作データのみ。
RUNTIME_SAVED_FIELDS = ('current_cycle', 'initial_seen_carts', 'first_cycle', 'cart_configured_top_order', 'cart_configured_bottom_order', 'cart_configured_order', 'cart_configured_expected_ids', 'cart_expected_ids', 'cart_first_cycle_order', 'cart_previous_cycle_order', 'cart_current_cycle_order', 'cart_lap_count', 'cart_last_normal_read', 'cart_19_actual_count', 'cart_inspection', 'run_cart', 'inspection_cart', 'hikadouyoyaku', 'cart_last_time', 'cart_last_camera', 'cart_status', 'camera_cart_queue', 'agv_states', 'last_agv_qr', 'agv_active', 'agv_qr', 'agv_carts', 'active_cart_count', 'agv_qr_alias', 'a_qr_active', 'a_qr_started', 'a_qr_name', 'a_qr_returned', 'last_a_number', 'agv_first_cycle_order', 'agv_previous_cycle_order', 'agv_current_cycle_order', 'agv_tracking_order', 'agv_recovery_order_dirty', 'agv_recently_restored', 'agv_unstable_order_this_lap', 'agv_cycle_boundary_marker', 'agv_recovery_guard_until', 'agv_lap_count', 'agv_cycle_wrapped', 'agv_cycle_forward_steps', 'agv_wrap_skipped', 'agv_required_numbers', 'agv_expected_numbers', 'agv_inspection', 'agv_inspection_motion', 'agv_pending_passages', 'agv_last_forward_number', 'agv_deferred_recovery', 'agv_restored_this_lap', 'agv_actual_passage_history', 'agv_actual_passage_count', 'agv_last_physical_boundary_at', 'cart_moves', 'next_19_side_slot', 'five_side_agv', 'next_five_side_slot', 'cart_side', 'saigo_qr_read', 'last_accepted_cart_qr', 'last_accepted_agv_qr', 'skip_jump_carts', 'cart_display_position')

def _encode_runtime(value):
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, set):
        return {"__runtime_type__": "set", "items": [_encode_runtime(v) for v in value]}
    if isinstance(value, tuple):
        return {"__runtime_type__": "tuple", "items": [_encode_runtime(v) for v in value]}
    if isinstance(value, list):
        return [_encode_runtime(v) for v in value]
    if isinstance(value, dict):
        return {"__runtime_type__": "dict", "items": [
            [_encode_runtime(k), _encode_runtime(v)] for k, v in value.items()]}
    raise TypeError(f"保存できない型: {type(value).__name__}")

def _decode_runtime(value):
    if isinstance(value, list):
        return [_decode_runtime(v) for v in value]
    if isinstance(value, dict):
        tag = value.get("__runtime_type__")
        if tag == "set":
            return set(_decode_runtime(v) for v in value["items"])
        if tag == "tuple":
            return tuple(_decode_runtime(v) for v in value["items"])
        if tag == "dict":
            return {_decode_runtime(k): _decode_runtime(v) for k, v in value["items"]}
    return value

class daisyakannri:

    def __init__(self, root, run_mode, simulation_mode=None):
        self.root = root
        self.run_mode = "MANUAL_CAMERA"

        self.simulation_mode_runtime = SIMULATION_MODE if simulation_mode is None else bool(simulation_mode)
        self.test_mode = self.simulation_mode_runtime
        self.simulation_mode = "Q1" if self.test_mode else None

        self.active_bottom_count = load_active_bottom_count()
        self.event_queue = queue.Queue()
        # 受信スレッドはTkinterを直接触らず、QR処理キューを経由する。
        self.socket_bind_host = SOCKET_BIND_HOST
        self.socket_port = SOCKET_PORT
        self.socket_stop_event = threading.Event()
        # サーバーの超音波センサー検知状態。TCP STATEでクライアントへ共有。
        self.sensor_active_event = threading.Event()
        self.socket_listener = None
        self.socket_thread = None
        self.socket_workers = []
        # クライアント再送時も同じAGVを二重に処理しない (稼働中のみ)。
        self.socket_processed_events = OrderedDict()
        self.csv_logger = CsvLogger()
        self.agv_logger = AgvCsvLogger()
        self.root.report_callback_exception = self.log_tk_error
        self.history_window = None
        self.history_tree = None

        self.current_cycle = []
        self.initial_seen_carts = set()
        self.first_cycle = False
        self.cart_configured_top_order = [cart for cart in CARTS_19_SIDE if cart]
        self.cart_configured_bottom_order = [cart for cart in CARTS_5_SIDE if cart]
        self.cart_configured_order = self.cart_configured_top_order + self.cart_configured_bottom_order
        self.cart_configured_expected_ids = set(self.cart_configured_order)
        self.cart_expected_ids = set(self.cart_configured_expected_ids)
        self.cart_first_cycle_order = []
        self.cart_previous_cycle_order = []
        self.cart_current_cycle_order = []
        self.cart_lap_count = 0
        self.cart_last_normal_read = None
        self.cart_19_actual_count = None
        self.cart_inspection = set()

        self.run_cart = []
        self.inspection_cart = []
        self.hikadouyoyaku = set()
        self.cart_last_time = {}
        self.cart_last_camera = {}
        self.cart_status = {}


        self.camera_cart_queue = []

        self.agv_states = {}
        self.last_agv_qr = None
        self.agv_active = False
        self.agv_qr = None
        self.agv_carts = []
        self.active_cart_count = 0

        self.agv_qr_alias = {}
        self.a_qr_active = {}
        self.a_qr_started = {}
        self.a_qr_name = {}
        self.a_qr_returned = {}
        self.last_a_number = None
        self.agv_first_cycle_order = []
        self.agv_previous_cycle_order = []
        self.agv_current_cycle_order = []
        # 点検復帰で走行順が変わった場合の、最新の周回判定用順序。
        # 1周目の学習履歴は保存し、復帰後の実際の順序だけこちらに反映する。
        self.agv_tracking_order = []
        self.agv_recovery_order_dirty = False
        # 復帰後に順番が変わっても、当該AGVは周回の終わりまで即時読み飛ばし扱いしない。
        self.agv_recently_restored = set()
        self.agv_unstable_order_this_lap = False
        # 周回の目印QRを固定し、点検復帰時に区切りが勝手にずれることを防ぐ。
        self.agv_cycle_boundary_marker = None
        # 復帰QRの読取中に不要な読み飛ばしが出ないよう周回進行を分離する。
        self.agv_recovery_guard_until = {}
        self.agv_lap_count = 0
        self.agv_cycle_wrapped = False
        self.agv_cycle_forward_steps = 0  # 学習済みの走行順で進んだ数（実際の秒数ではない）
        self.agv_wrap_skipped = set()
        self.agv_required_numbers = {
            int(number) for number in AGV_REQUIRED_QR_NUMBERS
            if 1 <= int(number) <= MAX_AGV_COUNT
        }
        self.agv_expected_numbers = set(self.agv_required_numbers)
        self.agv_inspection = set()
        self.agv_inspection_motion = {}
        # 参考コードのカメラ間通過待ちリストに対応する、AGVの次回通過待ちキュー。
        self.agv_pending_passages = []
        self.agv_last_forward_number = None
        # 数台先で復帰したAGVは次のQRが順方向に続くまで進行を保留。
        self.agv_deferred_recovery = None
        # 点検復帰QRは通常通過の進行位置を動かさない。
        # 順序は2周目以降、各周回終了時の実際の読取順で更新する。
        self.agv_restored_this_lap = set()
        # 周回の判定起点が変化しても、実際の同一QR再通過を見失わない記録。
        # CSV履歴を読み直すのではなく、現在の実行中のQRだけを記録する。
        self.agv_actual_passage_history = []
        # 受理したAGV QRの通算読取位置。以前の周回と重なった
        # 同一QRの再登場で周回を二重確定しないための基準。
        self.agv_actual_passage_count = 0
        self.agv_last_physical_boundary_at = None

        self.cart_moves = {}
        self.next_19_side_slot = 0

        self.five_side_agv = {cart: None for cart in CARTS_5_SIDE if cart}

        self._init_default_agvs()

        self.next_five_side_slot = 0

        self.cart_side = {}
        for cart in CARTS_19_SIDE:
            if cart:
                self.cart_side[cart] = "20SIDE"
        for cart in CARTS_5_SIDE:
            if cart:
                self.cart_side[cart] = "5SIDE"

        self.saigo_qr_read = {}
        self.last_accepted_cart_qr = None
        self.last_accepted_agv_qr = None

        self.running_objects = {}
        self.inspection_objects = {}
        self.cart_display_position = {}
        self.cart_animation_after_ids = {}
        self.skip_jump_carts = set()
        self.camera = None
        self.camera_running = False
        self.camera_lock = threading.Lock()
        self.hardware_stop_event = threading.Event()
        self.sensor_thread = None
        self.camera_thread = None

        if not self.test_mode:
            self._init_hardware()

        self.create_widgets()
        # 運行状態だけの初期値を保持。保存した前回状態を読み込む前に撮る。
        self._fresh_runtime_state = {
            name: copy.deepcopy(getattr(self, name)) for name in RUNTIME_SAVED_FIELDS
        }
        self.root.protocol("WM_DELETE_WINDOW", self.close_program)

    def _init_hardware(self):
        """Raspberry Pi実機用のカメラ・GPIOを初期化する。"""
        missing = []
        if not PICAMERA2_AVAILABLE:
            missing.append("picamera2")
        if not PYZBAR_AVAILABLE:
            missing.append("pyzbar")
        if not LIBCAMERA_AVAILABLE:
            missing.append("libcamera")
        if not GPIO_AVAILABLE and not CAMERA_ALWAYS_ON:
            missing.append("RPi.GPIO")
        if missing:
            raise RuntimeError(
                "実機モードですが必要なライブラリがありません: " + ", ".join(missing)
                + "。PCで試す場合はSIMULATION_MODE=Trueにしてください。"
            )

        self.camera = Picamera2()

        # センサーを使用しないテスト中はGPIOの初期化もしない。
        if not CAMERA_ALWAYS_ON:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(TRIG_PIN, GPIO.OUT)
            GPIO.setup(ECHO_PIN, GPIO.IN)
            GPIO.setup(LED1_PIN, GPIO.OUT)
            GPIO.setup(LED2_PIN, GPIO.OUT)
            GPIO.output(TRIG_PIN, GPIO.LOW)
            GPIO.output(LED1_PIN, GPIO.LOW)
            GPIO.output(LED2_PIN, GPIO.LOW)

        # Camera Module 3 の全画角に近いセンサーモードを明示的に選択する。
        # 出力1280x720だけを指定すると自動選択で中央クロップの
        # センサーモードになり、ズームしたように見える場合がある。
        self.camera_config = self.camera.create_video_configuration(
            main={"size": CAMERA_SIZE, "format": "RGB888"},
            raw={"size": (2304, 1296)},
            controls={"FrameRate": CAMERA_FPS}
        )
        self.camera.configure(self.camera_config)
        full_crop = self.camera.camera_properties["ScalerCropMaximum"]
        self.camera.set_controls({
            "AfMode": controls.AfModeEnum.Manual,
            "LensPosition": MANUAL_LENS_POSITION,
            "ScalerCrop": full_crop,
        })
        print(f"サーバーカメラ: 全画角設定 ScalerCrop={full_crop}")

        print("実機モード: Camera Module 3 / Manual Focus 初期化完了")

    def log_tk_error(self, error_type, error, error_traceback):
        self.csv_logger.log_error("画面処理", "-", "", error)
        traceback.print_exception(error_type, error, error_traceback)

    def create_widgets(self):
        self.root.title(f"台車管理 / AGV判定 {AGV_JUDGMENT_VERSION}")
        self.root.geometry("980x640" if self.test_mode else "980x610")

        self.clock_label = tk.Label(self.root, text="", font=("Arial", 9))
        self.clock_label.pack()

        # 画面の行数を減らすため、上側/下側の台数サマリーは非表示。
        self.status_label = tk.Label(self.root, text="", font=("Arial", 9))

        self.qr_time_label = tk.Label(self.root, text="", font=("Arial", 9))
        self.qr_time_label.pack()

        self.yoyaku_label = tk.Label(self.root, text="", font=("Arial", 9))
        self.yoyaku_label.pack()

        if self.test_mode:
            self.open_input_button = tk.Button(self.root, text="入力画面を開く", font=("Arial", 10, "bold"), command=self.simu_akeru)
            self.open_input_button.pack(pady=3)

        button_frame = tk.Frame(self.root)
        button_frame.pack(pady=2)

        self.open_history_button = tk.Button(button_frame, text="QR履歴", font=("Arial", 10, "bold"), command=self.open_qr_history)
        self.open_history_button.pack(side="left", padx=4)

        self.open_layout_button = tk.Button(
            button_frame,
            text="台車配置番号を設定",
            font=("Arial", 10, "bold"),
            command=self.open_cart_layout_editor
        )
        self.open_layout_button.pack(side="left", padx=4)

        # 下側運用台数の操作欄は非表示。設定は active_bottom_count.txt から読み込む。

        # 実機・シミュレーション共通。前回保存状態も初期化する。
        self.reset_runtime_button = tk.Button(
            button_frame, text="運行状態をリセット", font=("Arial", 10, "bold"),
            fg="white", bg="#a43232", command=self.reset_runtime_with_confirmation
        )
        self.reset_runtime_button.pack(side="left", padx=4)
        if self.test_mode:
            self.reset_test_button = tk.Button(
                button_frame, text="テスト初期化", font=("Arial", 9, "bold"),
                command=self.reset_learning_for_test
            )
            self.reset_test_button.pack(side="left", padx=4)

        self.canvas = tk.Canvas(self.root, width=920, height=430, bg="black", highlightthickness=0)
        self.canvas.pack(pady=6)

        if self.test_mode:
            self.simu_gamen()

    def reset_runtime_with_confirmation(self):
        """確認後、運行中の台車・AGV判定と再開用状態を初期化する。"""
        from tkinter import messagebox
        message = (
            "運行状態を初期化しますか？\n\n"
            "台車・AGVの周回、点検、画面上の移動、前回の再開状態をリセットします。\n"
            "保存済みの台車番号配置・QR履歴・エラーログ・緑枠設定は残します。\n\n"
            "実際の台車・AGVが停止していることを確認してから実行してください。"
        )
        if not messagebox.askyesno("運行状態をリセット", message, parent=self.root):
            return
        try:
            self.reset_runtime_state()
            messagebox.showinfo("リセット完了", "初期画面に戻しました。次のQRから1周目として学習します。", parent=self.root)
        except Exception as error:
            self.csv_logger.log_error("運行リセット", "-", "", error)
            messagebox.showerror("リセット失敗", str(error), parent=self.root)

    def reset_runtime_state(self):
        """運行情報だけ初期化。配置・ログ・カメラ設定は維持。"""
        # QR処理はTkinterスレッドのみで行われるため、ここで処理キューを整理できる。
        # 未ACKのネットワークイベントはRETRYを返し、通信スレッドを待たせない。
        while True:
            try:
                event_type, data = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if event_type == "NETWORK_AGV":
                event_id, qr, complete, response = data
                response["status"] = "RETRY"
                complete.set()

        self.cancel_cart_animations()
        self.socket_processed_events.clear()
        # 配置番号は直前に保存された最新の値を維持する。
        top = list(CARTS_19_SIDE)
        bottom = list(CARTS_5_SIDE)
        for field, value in self._fresh_runtime_state.items():
            setattr(self, field, copy.deepcopy(value))

        self.cart_configured_top_order = [v for v in top if v]
        self.cart_configured_bottom_order = [v for v in bottom if v]
        self.cart_configured_order = (
            self.cart_configured_top_order + self.cart_configured_bottom_order
        )
        self.cart_configured_expected_ids = set(self.cart_configured_order)
        self.cart_expected_ids = set(self.cart_configured_expected_ids)
        self.cart_status = {v: "未確認" for v in self.cart_configured_order}
        self.cart_side = {
            **{v: "20SIDE" for v in top if v},
            **{v: "5SIDE" for v in bottom if v},
        }
        self.five_side_agv = {v: None for v in bottom if v}
        self._init_default_agvs()
        self.running_objects.clear()
        self.inspection_objects.clear()
        self.cart_animation_after_ids.clear()
        if hasattr(self, "agv_last_gap_judgment"):
            self.agv_last_gap_judgment = ""
        self.qr_time_label.config(text="")
        self.yoyaku_label.config(text="運行状態を初期化しました。QRの順番を1周目から学習します")
        self.redraw()
        # 直ちに新しい初期状態を保存するので、再起動しても古い状態に戻らない。
        self.save_runtime_state()
        print("[運行リセット完了] 台車・AGV・周回・点検を初期化しました", flush=True)

    def reset_learning_for_test(self):
        """シミュレーションで次の試行を独立させる。CSVログと設定済み番号は残す。"""
        if not self.test_mode:
            return
        while True:
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                break
        self.current_cycle.clear()
        self.initial_seen_carts.clear()
        self.first_cycle = False
        self.cart_first_cycle_order.clear()
        self.cart_previous_cycle_order.clear()
        self.cart_current_cycle_order.clear()
        self.cart_lap_count = 0
        self.cart_last_normal_read = None
        self.cart_19_actual_count = None
        self.cart_expected_ids = set(self.cart_configured_expected_ids)
        self.cart_inspection.clear()
        self.inspection_cart.clear()
        self.run_cart.clear()
        self.hikadouyoyaku.clear()
        self.cart_last_time.clear()
        self.cart_last_camera.clear()
        self.cart_status = {value: "未確認" for value in self.cart_configured_order}
        self.camera_cart_queue.clear()
        self.active_cart_count = 0
        self.cancel_cart_animations()
        self.cart_display_position.clear()
        self.skip_jump_carts.clear()
        self.cart_moves.clear()
        self.next_19_side_slot = 0
        self.next_five_side_slot = 0

        self.agv_first_cycle_order.clear()
        self.agv_previous_cycle_order.clear()
        self.agv_current_cycle_order.clear()
        self.agv_tracking_order.clear()
        self.agv_recovery_order_dirty = False
        self.agv_recently_restored.clear()
        self.agv_unstable_order_this_lap = False
        self.agv_cycle_boundary_marker = None
        self.agv_recovery_guard_until.clear()
        self.agv_lap_count = 0
        self.agv_cycle_wrapped = False
        self.agv_cycle_forward_steps = 0
        self.agv_wrap_skipped.clear()
        self.agv_expected_numbers = set(self.agv_required_numbers)
        self.agv_inspection.clear()
        self.agv_inspection_motion.clear()
        self.agv_pending_passages.clear()
        self.agv_last_forward_number = None
        self.agv_deferred_recovery = None
        self.agv_restored_this_lap.clear()
        self.agv_actual_passage_history.clear()
        self.agv_actual_passage_count = 0
        self.agv_last_physical_boundary_at = None
        self.last_a_number = None
        self.last_agv_qr = None
        self.agv_active = False
        self.agv_qr = None
        self.agv_carts.clear()
        self.agv_qr_alias.clear()
        self.a_qr_active.clear()
        self.a_qr_started.clear()
        self.a_qr_name.clear()
        self.a_qr_returned.clear()
        self.saigo_qr_read.clear()
        self.last_accepted_cart_qr = None
        self.last_accepted_agv_qr = None
        apply_cart_layout(self.cart_configured_top_order, self.cart_configured_bottom_order)
        self.cart_side = {}
        for value in CARTS_19_SIDE:
            if value:
                self.cart_side[value] = "20SIDE"
        for value in CARTS_5_SIDE:
            if value:
                self.cart_side[value] = "5SIDE"
        self.five_side_agv = {value: None for value in CARTS_5_SIDE if value}
        self._init_default_agvs()
        self.qr_time_label.config(text="")
        self.yoyaku_label.config(text="テスト初期化完了：QRの順番を1周目から学習します")
        self.redraw()

    def open_qr_history(self):
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            self.refresh_qr_history()
            return

        self.history_window = tk.Toplevel(self.root)
        self.history_window.title("QR読取履歴")
        self.history_window.geometry("850x450")

        frame = ttk.Frame(self.history_window)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        scrollbar = ttk.Scrollbar(frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        columns = self.csv_logger.qr_header
        self.history_tree = ttk.Treeview(frame, columns=columns, show="headings", yscrollcommand=scrollbar.set)

        for column in columns:
            self.history_tree.heading(column, text=column)
            self.history_tree.column(column, width=155)

        self.history_tree.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.history_tree.yview)
        self.refresh_qr_history()

    def refresh_qr_history(self):
        if self.history_window is None or not self.history_window.winfo_exists():
            return

        top, bottom = self.history_tree.yview()
        was_at_bottom = bottom >= 0.99

        try:
            with self.csv_logger.qr_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
                reader = csv.reader(csv_file)
                next(reader, None)  # 見出しを飛ばす
                rows = list(reader)
        except (OSError, csv.Error) as error:
            print("CSV読込エラー:", error)
            self.csv_logger.log_error("QR履歴CSV読込", "-", str(self.csv_logger.qr_path), error)
            return

        for item in self.history_tree.get_children():
            self.history_tree.delete(item)

        for row in rows:
            self.history_tree.insert("", "end", values=row)

        if was_at_bottom:
            self.history_tree.yview_moveto(1)
        else:
            self.history_tree.yview_moveto(top)

    def open_cart_layout_editor(self):
        """19台側19枠・7台側最大7枠の台車番号をあとから設定する画面。"""
        if hasattr(self, "layout_window") and self.layout_window is not None:
            try:
                if self.layout_window.winfo_exists():
                    self.layout_window.lift()
                    return
            except tk.TclError:
                pass

        self.layout_window = tk.Toplevel(self.root)
        self.layout_window.title("台車配置番号設定（上19枠／下最大7枠）")
        self.layout_window.geometry("1320x360")
        self.layout_window.resizable(False, False)

        tk.Label(
            self.layout_window,
            text="現場の並び順に合わせて番号を入力してください。番号は連番でなくてOKです。",
            font=("Arial", 12, "bold")
        ).pack(pady=(12, 4))

        tk.Label(
            self.layout_window,
            text="空欄の場所は未設定として表示します。保存すると次回起動時もこの配置を使います。",
            font=("Arial", 10)
        ).pack(pady=(0, 10))

        self.layout_19_entries = []
        self.layout_20_entries = self.layout_19_entries
        self.layout_5_entries = []

        tk.Label(self.layout_window, text="上段：台車配置", font=("Arial", 11, "bold")).pack()
        row19 = tk.Frame(self.layout_window)
        row19.pack(pady=5)

        for index in range(CARTS_19_COUNT):
            frame = tk.Frame(row19)
            frame.pack(side="left", padx=2)
            tk.Label(frame, text=str(index + 1), font=("Arial", 8)).pack()
            entry = tk.Entry(frame, width=4, justify="center", font=("Arial", 11))
            current = CARTS_19_SIDE[index] if index < len(CARTS_19_SIDE) else ""
            entry.insert(0, current)
            entry.pack()
            self.layout_19_entries.append(entry)

        tk.Label(self.layout_window, text="下段：設備エリア", font=("Arial", 11, "bold")).pack(pady=(12, 0))
        row5 = tk.Frame(self.layout_window)
        row5.pack(pady=5)

        for index in range(CARTS_5_COUNT):
            frame = tk.Frame(row5)
            frame.pack(side="left", padx=8)
            tk.Label(frame, text=str(index + 1), font=("Arial", 8)).pack()
            entry = tk.Entry(frame, width=8, justify="center", font=("Arial", 11))
            current = CARTS_5_SIDE[index] if index < len(CARTS_5_SIDE) else ""
            entry.insert(0, current)
            entry.pack()
            self.layout_5_entries.append(entry)

        button_frame = tk.Frame(self.layout_window)
        button_frame.pack(pady=12)

        tk.Button(
            button_frame,
            text="保存",
            width=12,
            font=("Arial", 11, "bold"),
            command=self.save_cart_layout
        ).pack(side="left", padx=6)

        tk.Button(button_frame,text="閉じる",width=12,font=("Arial", 11),command=self.layout_window.destroy).pack(side="left", padx=6)

    def save_cart_layout(self):

        carts_19 = [entry.get().strip().upper() for entry in self.layout_19_entries]
        carts_5 = [entry.get().strip().upper() for entry in self.layout_5_entries]
        all_values = [value for value in carts_19 + carts_5 if value]

        duplicates = sorted({value for value in all_values if all_values.count(value) > 1})
        if duplicates:
            return

        apply_cart_layout(carts_19, carts_5)
        self.cart_configured_top_order = [cart for cart in CARTS_19_SIDE if cart]
        self.cart_configured_bottom_order = [cart for cart in CARTS_5_SIDE if cart]
        self.cart_configured_order = self.cart_configured_top_order + self.cart_configured_bottom_order
        self.cart_configured_expected_ids = set(self.cart_configured_order)
        self.cart_expected_ids = set(self.cart_configured_expected_ids)
        self.cart_first_cycle_order = []
        self.cart_previous_cycle_order = []
        self.cart_current_cycle_order = []
        self.cart_lap_count = 0
        self.cart_last_normal_read = None
        self.cart_19_actual_count = None
        self.cart_inspection.clear()
        self.inspection_cart.clear()
        self.run_cart.clear()
        self.hikadouyoyaku.clear()
        self.camera_cart_queue.clear()
        self.cart_display_position.clear()
        self.cart_last_time.clear()
        self.cart_last_camera.clear()
        self.cart_status = {cart: "未確認" for cart in self.cart_configured_order}
        self.active_cart_count = 0

        try:
            with LAYOUT_FILE.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["19台側"] + CARTS_19_SIDE)
                writer.writerow(["7台側"] + CARTS_5_SIDE)
        except (OSError, csv.Error) as error:
            self.csv_logger.log_error("台車配置設定保存", "-", str(LAYOUT_FILE), error)
            from tkinter import messagebox
            messagebox.showerror("保存エラー", str(error), parent=self.layout_window)
            return
        old_agv_carts = {cart   for state in self.agv_states.values()   for cart in state.get("carts", [])}
        self.five_side_agv = {cart: self.five_side_agv.get(cart) for cart in CARTS_5_SIDE if cart}
        known_carts = self._known_cart_ids() | old_agv_carts | set(self.cart_expected_ids)
        self.cart_side = {}
        for cart in known_carts:
            if cart in old_agv_carts:
                self.cart_side[cart] = "AGV"
            elif cart in CARTS_20_SIDE:
                self.cart_side[cart] = "20SIDE"
            elif cart in CARTS_5_SIDE:
                self.cart_side[cart] = "5SIDE"
            else:
                self.cart_side[cart] = "UNSET"

        self.run_cart[:] = [cart for cart in self.run_cart if cart in known_carts]
        self.inspection_cart[:] = [cart for cart in self.inspection_cart if cart in known_carts]
        for cart in known_carts:
            self.cart_status.setdefault(cart, "未確認")

        self.active_cart_count = len(self.run_cart)
        self.yoyaku_label.config(text="台車配置番号を保存しました")
        self.redraw()

        try:
            self.layout_window.destroy()
        except tk.TclError:
            pass
        self.layout_window = None

    def simu_gamen(self):
        self.input_window = tk.Toplevel(self.root)
        self.input_window.title("1カメラ シミュレーション入力")
        self.input_window.geometry("650x300")
        self.input_window.resizable(False, False)
        self.input_window.protocol("WM_DELETE_WINDOW", self.simu_ireru)

        title_label = tk.Label(self.input_window, text="QR入力（1カメラ・シミュレーション）", font=("Arial", 16, "bold"))
        title_label.pack(pady=(15, 8))

        self.input_mode_label = tk.Label(self.input_window, text="入力元: Camera Module 3", font=("Arial", 14, "bold"))
        self.input_mode_label.pack(pady=3)

        self.entry_guide_label = tk.Label(
            self.input_window,
            text="サーバー入力は台車QRだけ / AGVは別クライアントの --simulate から送信 / 実機: --hardware",
            font=("Arial", 11)
        )
        self.entry_guide_label.pack(pady=(10, 5))

        entry_frame = tk.Frame(self.input_window)
        entry_frame.pack(pady=4)

        self.input_entry = tk.Entry(entry_frame, width=14, justify="center", font=("Arial", 18))
        self.input_entry.pack(side="left", padx=8)
        self.input_entry.bind("<Return>", self.entry)

        self.send_button = tk.Button(entry_frame, text="送信", width=10, font=("Arial", 11, "bold"), command=self.entry)
        self.send_button.pack(side="left", padx=8)

        self.input_result_label = tk.Label(self.input_window, text="", fg="blue", font=("Arial", 11))
        self.input_result_label.pack(pady=5)

    def simu_akeru(self):
        if not self.test_mode:
            return

        self.input_window.deiconify()
        self.input_window.lift()
        self.input_entry.focus_set()

    def simu_ireru(self):
        self.input_window.withdraw()

    def start_agv_socket_server(self):
        """AGV専用TCP受信。QRの判定・GUI更新はTkinterメインスレッドで行う。"""
        if self.socket_listener is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.socket_bind_host, self.socket_port))
            sock.listen(8)
            sock.settimeout(0.4)
        except Exception:
            sock.close()
            raise
        self.socket_listener = sock
        self.socket_port = sock.getsockname()[1]
        self.socket_thread = threading.Thread(
            target=self._agv_socket_accept_loop, name="agv-tcp-listener", daemon=True
        )
        self.socket_thread.start()
        print(f"AGV受信サーバー起動: {self.socket_bind_host}:{self.socket_port}")

    def _agv_socket_accept_loop(self):
        while not self.socket_stop_event.is_set():
            try:
                client, address = self.socket_listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(
                target=self._agv_socket_handle_client,
                args=(client, address), daemon=True,
                name="agv-tcp-client"
            )
            worker.start()

    def _agv_socket_handle_client(self, client, address):
        """AGV|イベントID|A1\n → OK|イベントID\n。応答はGUI処理完了後。"""
        with client:
            try:
                client.settimeout(SOCKET_ACK_WAIT_SEC + 2)
                incoming = bytearray()
                while b"\n" not in incoming:
                    chunk = client.recv(128)
                    if not chunk:
                        return
                    incoming.extend(chunk)
                    if len(incoming) > SOCKET_MAX_MESSAGE_BYTES:
                        client.sendall(b"ERR|INVALID|TOO_LONG\n")
                        return
                line = bytes(incoming).split(b"\n", 1)[0].decode("ascii")
                if line == "STATE":
                    # ポーリング要求: クライアントのカメラ解析ON/OFFを同期する。
                    active = int(self.sensor_active_event.is_set())
                    client.sendall(f"STATE|{active}\n".encode("ascii"))
                    return
                parts = line.split("|")
                if (len(parts) != 3 or parts[0] != "AGV"
                        or not re.fullmatch(r"[0-9a-f]{32}", parts[1])
                        or not re.fullmatch(r"A(?:[1-9]|10|11)", parts[2])):
                    client.sendall(b"ERR|INVALID|BAD_FRAME_OR_QR\n")
                    return
                event_id, qr = parts[1], parts[2]
                complete = threading.Event()
                response = {}
                self.event_queue.put(("NETWORK_AGV", (event_id, qr, complete, response)))
                if not complete.wait(SOCKET_ACK_WAIT_SEC):
                    client.sendall((f"RETRY|{event_id}|GUI_TIMEOUT\n").encode("ascii"))
                    return
                outcome = response.get("status", "RETRY")
                client.sendall((f"{outcome}|{event_id}\n").encode("ascii"))
            except (OSError, UnicodeError, ValueError) as error:
                # 切断済みソケットへの応答などはQR処理を中断させない。
                print(f"AGV TCP接続終了 {address}: {error}")

    def _receive_remote_agv_in_gui(self, item):
        """必ずTkinterメインスレッドで呼ぶ。再送IDを保持して二重登録を防止。"""
        event_id, qr_data, complete, response = item
        try:
            if event_id in self.socket_processed_events:
                response["status"] = self.socket_processed_events[event_id]
                return
            # 既存のAGV点検・周回・移動・CSV処理をそのまま使用する。
            self.receive_agv(qr_data, camera_name="AGVクライアント(TCP)")
            response["status"] = "OK"
            self.socket_processed_events[event_id] = "OK"
            if len(self.socket_processed_events) > SOCKET_EVENT_HISTORY_LIMIT:
                self.socket_processed_events.popitem(last=False)
        except Exception as error:
            response["status"] = "RETRY"
            self.csv_logger.log_error("AGVネットワーク受信処理", "AGVクライアント", qr_data, error)
            traceback.print_exc()
        finally:
            complete.set()

    def save_runtime_state(self):
        """最新の画面・学習・点検をCSVへ原子的に保存する。"""
        if not RESTORE_PREVIOUS_STATE:
            return
        temp = RUNTIME_STATE_FILE.with_suffix(".csv.tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["field", "value"])
                writer.writerow(["__version__", repr(1)])
                writer.writerow(["__saved_monotonic__", repr(time.monotonic())])
                writer.writerow(["__layout_top__", repr(_encode_runtime(CARTS_19_SIDE))])
                writer.writerow(["__layout_bottom__", repr(_encode_runtime(CARTS_5_SIDE))])
                for field in RUNTIME_SAVED_FIELDS:
                    writer.writerow([field, repr(_encode_runtime(getattr(self, field)))])
            os.replace(temp, RUNTIME_STATE_FILE)
        except (OSError, TypeError, ValueError) as error:
            print(f"[状態保存エラー] {error}", flush=True)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def load_runtime_state(self):
        if not RESTORE_PREVIOUS_STATE or not RUNTIME_STATE_FILE.exists():
            print("[運行状態] 新規開始", flush=True)
            return
        try:
            with RUNTIME_STATE_FILE.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))
            if not rows or rows[0] != ["field", "value"]:
                raise ValueError("運行状態CSVの形式が違います")
            raw = {row[0]: ast.literal_eval(row[1]) for row in rows[1:] if len(row) == 2}
            if raw.get("__version__") != 1:
                raise ValueError("運行状態のバージョンが違います")
            # 保存が完全な場合だけ復元する（中途半端な復元を防ぐ）。
            missing = [field for field in RUNTIME_SAVED_FIELDS if field not in raw]
            if missing:
                raise ValueError(f"運行状態が不完全です: {missing[:3]}")
            layout_top = _decode_runtime(raw["__layout_top__"])
            layout_bottom = _decode_runtime(raw["__layout_bottom__"])
            restored = {field: _decode_runtime(raw[field]) for field in RUNTIME_SAVED_FIELDS}
            # monotonic()は再起動で基準が変わるため、時刻を移し替える。
            delta = time.monotonic() - float(raw["__saved_monotonic__"])
            time_keys = {"cycle_started", "unload_started", "agv_read_started", "loop_started",
                         "a_qr_started", "a_qr_returned_at", "started", "last_seen"}
            for state in restored["agv_states"].values():
                for key in time_keys:
                    if isinstance(state.get(key), (int, float)) and not isinstance(state[key], bool):
                        state[key] += delta
            for motion in restored["agv_inspection_motion"].values():
                if isinstance(motion.get("started"), (int, float)):
                    motion["started"] += delta
            for field in ("a_qr_started", "agv_recovery_guard_until"):
                restored[field] = {k: (v + delta if type(v) in (int, float) else v)
                                   for k, v in restored[field].items()}
            # 次の読み取りに使う時間キャッシュは古い値を引き継がない。
            restored["saigo_qr_read"] = {}
            apply_cart_layout(layout_top, layout_bottom)
            for field, value in restored.items():
                setattr(self, field, value)
            print(f"[運行状態を復元] 台車{len(self.cart_expected_ids)}台・AGV周回{self.agv_lap_count}", flush=True)
            self.yoyaku_label.config(text="前回停止時の台車・AGV状態から再開")
        except (OSError, csv.Error, SyntaxError, ValueError, TypeError, KeyError, AttributeError) as error:
            print(f"[運行状態の復元失敗] {error} / 初期画面から開始", flush=True)

    def autosave_runtime_state(self):
        if not self.hardware_stop_event.is_set():
            self.save_runtime_state()
            self.root.after(1000, self.autosave_runtime_state)

    def start(self):
        self.load_runtime_state()
        self.start_agv_socket_server()
        self.update_clock()
        self.qr_yomi()
        self.update_agv_cycles()
        if self.test_mode:
            print("シミュレーションモード: Picamera2 / GPIOは使用しません")
        else:
            self.start_hardware()
        self.redraw()
        self.root.after(1000, self.autosave_runtime_state)

    def get_distance(self):
        if self.test_mode or not GPIO_AVAILABLE:
            return None

        GPIO.output(TRIG_PIN, GPIO.LOW)
        time.sleep(0.000002)
        GPIO.output(TRIG_PIN, GPIO.HIGH)
        time.sleep(0.00001)
        GPIO.output(TRIG_PIN, GPIO.LOW)

        timeout = time.monotonic() + 0.1
        while GPIO.input(ECHO_PIN) == GPIO.LOW:
            if time.monotonic() > timeout:
                return None

        start_time = time.monotonic()
        timeout = time.monotonic() + 0.1
        while GPIO.input(ECHO_PIN) == GPIO.HIGH:
            if time.monotonic() > timeout:
                return None

        end_time = time.monotonic()
        return (end_time - start_time) * 34300 / 2

    def start_hardware(self):
        """センサー監視と、起動・停止を繰り返せるカメラ監視スレッドを開始する。"""
        self.camera_thread = threading.Thread(target=self.camera_loop, daemon=True)
        self.camera_thread.start()
        if CAMERA_ALWAYS_ON:
            # STATEへの返信もONにして、AGVクライアントの読み取りも許可する。
            self.sensor_active_event.set()
            self.start_camera()
            print("[センサーバイパス] サーバーカメラON / AGV読取許可ON")
        else:
            self.sensor_thread = threading.Thread(target=self.sensor_loop, daemon=True)
            self.sensor_thread.start()

    def set_leds(self, led1_on, led2_on):
        """実機モードでLED1・LED2を操作する。PCシミュレーションでは何もしない。"""
        if self.test_mode or CAMERA_ALWAYS_ON or not GPIO_AVAILABLE:
            return
        GPIO.output(LED1_PIN, GPIO.HIGH if led1_on else GPIO.LOW)
        GPIO.output(LED2_PIN, GPIO.HIGH if led2_on else GPIO.LOW)

    def sensor_loop(self):
        """距離を中央値で安定化。連続検知で両カメラを起動、未検知で停止。"""
        history = deque(maxlen=SENSOR_FILTER_COUNT)
        detected = False
        on_count = 0
        off_count = 0
        led1_started_at = None
        led2_is_on = False
        last_valid_time = None

        def turn_off():
            # 通信先のカメラも確実に停止要求する
            self.sensor_active_event.clear()
            self.stop_camera()
            self.set_leds(False, False)

        self.sensor_active_event.clear()
        while not self.hardware_stop_event.is_set():
            try:
                now = time.monotonic()
                raw_distance = self.get_distance()
                valid = (
                    raw_distance is not None
                    and SENSOR_MIN_CM <= raw_distance <= SENSOR_MAX_CM
                )
                if valid:
                    history.append(raw_distance)
                    last_valid_time = now
                elif (last_valid_time is None
                      or now - last_valid_time >= SENSOR_VALID_TIMEOUT_SEC):
                    history.clear()

                # 新しい測定値が有効でないときは「検知開始」の票にしない。
                # 履歴だけで古い検知が続かないようにする。
                fresh = (last_valid_time is not None
                         and now - last_valid_time < SENSOR_VALID_TIMEOUT_SEC)
                distance = statistics.median(history) if fresh and len(history) >= 3 else None

                if valid and distance is not None and distance <= DETECT_DISTANCE_CM:
                    on_count += 1
                    off_count = 0
                elif valid and distance is not None:
                    on_count = 0
                    off_count += 1
                else:
                    # 無効値はONのために利用しない。連続測定不能ならOFFへ。
                    on_count = 0
                    off_count += 1

                if not detected and on_count >= SENSOR_ON_COUNT:
                    detected = True
                    led1_started_at = now
                    led2_is_on = False
                    self.sensor_active_event.set()
                    self.set_leds(True, False)
                    self.start_camera()
                    print("センサー検知: サーバーON / クライアントON要求", flush=True)

                if detected and off_count >= SENSOR_OFF_COUNT:
                    detected = False
                    led1_started_at = None
                    led2_is_on = False
                    turn_off()
                    print("センサー未検知・測定異常: 両カメラOFF要求", flush=True)

                if (detected and not led2_is_on and led1_started_at is not None
                        and now - led1_started_at >= LED2_DELAY_SEC):
                    self.set_leds(True, True)
                    led2_is_on = True
                if detected and not self.camera_running:
                    self.start_camera()
                self.hardware_stop_event.wait(0.1)
            except Exception as error:
                self.csv_logger.log_error("超音波センサー/LED", "-", "", error)
                detected = False
                on_count = 0
                off_count = 0
                history.clear()
                last_valid_time = None
                led1_started_at = None
                led2_is_on = False
                try:
                    turn_off()
                except Exception:
                    self.sensor_active_event.clear()
                self.hardware_stop_event.wait(0.5)
        try:
            turn_off()
        except Exception:
            self.sensor_active_event.clear()

    def start_camera(self):
        """カメラワーカーへ起動を依頼する。"""
        if self.test_mode or self.camera is None or self.hardware_stop_event.is_set():
            if self.test_mode:
                print("シミュレーション: カメラ起動を省略")
            return

        with self.camera_lock:
            if self.camera_running:
                return
            self.camera_running = True
        print("カメラON要求（Camera Module 3 / Manual Focus）")

    def stop_camera(self):
        """カメラワーカーへ停止を依頼する。機器のstop()はワーカー側で行う。"""
        with self.camera_lock:
            was_running = self.camera_running
            self.camera_running = False
        if was_running:
            print("カメラOFF要求")

    def camera_loop(self):
        """センサーが検知している間だけ撮影し、1フレーム内のQRをすべて処理する。"""
        if self.test_mode or self.camera is None:
            return

        camera_started = False
        last_visible_at = {}
        editor = None
        preview_ready = False
        frame_index = 0

        try:
            while not self.hardware_stop_event.is_set():
                with self.camera_lock:
                    should_run = self.camera_running

                if not should_run:
                    if camera_started:
                        try:
                            import cv2
                            cv2.destroyWindow(editor.window_name) if preview_ready and editor is not None else None
                            preview_ready = False
                        except Exception:
                            pass
                        try:
                            self.camera.stop()
                        except Exception:
                            pass
                        camera_started = False
                        last_visible_at.clear()
                        print("カメラOFF")
                    time.sleep(0.05)
                    continue

                if not camera_started:
                    try:
                        self.camera.start()
                        camera_started = True
                        print("カメラON（Camera Module 3 / Manual Focus）")
                    except Exception as error:
                        print("カメラ起動エラー:", error)
                        self.csv_logger.log_error("カメラ起動", "カメラ1", "", error)
                        try:
                            import cv2
                            cv2.destroyWindow(editor.window_name) if preview_ready and editor is not None else None
                            preview_ready = False
                        except Exception:
                            pass
                        try:
                            self.camera.stop()
                        except Exception:
                            pass
                        with self.camera_lock:
                            self.camera_running = False
                        time.sleep(0.5)
                        continue

                try:
                    with self.camera_lock:
                        should_capture = self.camera_running
                    if not should_capture:
                        continue

                    frame = self.camera.capture_array()
                    with self.camera_lock:
                        should_process_frame = self.camera_running
                    if not should_process_frame:
                        continue

                    import cv2
                    now = time.monotonic()
                    frame_index += 1
                    height, width = frame.shape[:2]
                    if editor is None or (editor.width, editor.height) != (width, height):
                        editor = QRRegionEditor(width, height)
                        preview_ready = False
                    if not preview_ready:
                        cv2.namedWindow(editor.window_name, cv2.WINDOW_NORMAL)
                        cv2.resizeWindow(editor.window_name, editor.display_width, editor.display_height)
                        cv2.setMouseCallback(editor.window_name, editor.on_mouse)
                        preview_ready = True

                    x1, y1, x2, y2 = editor.rect
                    roi = frame[y1:y2, x1:x2]
                    if roi.size == 0:
                        raise RuntimeError("QR枠が空です。rキーで初期化してください")

                    # 緑枠内を最優先で解析。画素を増やさない元画像を使用する。
                    gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
                    qr_codes = decode(gray) or []
                    if not qr_codes:
                        qr_codes = decode(cv2.equalizeHist(gray)) or []
                    if not qr_codes and frame_index % 5 == 0:
                        # 枠の設定違いを発見できるよう全画面も低頻度で確認。
                        full_gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                        qr_codes = decode(full_gray) or []

                    frame_codes = []
                    for qr_code in qr_codes:
                        try:
                            data = qr_code.data.decode("utf-8").strip().upper()
                        except (AttributeError, UnicodeDecodeError) as error:
                            self.csv_logger.log_error("QRデコード", "カメラ1", "", error)
                            continue
                        if data not in frame_codes:
                            frame_codes.append(data)

                    # 無関係なQRを判定履歴に入れない
                    valid_cart_codes = [
                        code for code in frame_codes
                        if re.fullmatch(r"(?:[1-9]|1[0-9]|2[0-4])", code)
                    ]
                    for data in newly_visible_qrs(valid_cart_codes, now, last_visible_at):
                        self.event_queue.put(("QR1", data))

                    cv2.imshow(editor.window_name, editor.render(frame))
                    key = cv2.waitKey(1) & 0xff
                    if key == ord('w'):
                        editor.save()
                    elif key == ord('r'):
                        editor.reset()
                    elif key == ord('s'):
                        path = Path.cwd() / 'server_camera_debug.png'
                        cv2.imwrite(str(path), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                        print(f"[サーバー画像保存] {path}", flush=True)
                    elif key == ord('q'):
                        self.stop_camera()
                    self.hardware_stop_event.wait(0.02)
                except Exception as error:
                    with self.camera_lock:
                        still_requested = self.camera_running
                    if still_requested and not self.hardware_stop_event.is_set():
                        print("カメラエラー:", error)
                        self.csv_logger.log_error("QR読み取り", "カメラ1", "", error)
                    try:
                        self.camera.stop_preview()
                    except Exception:
                        pass
                    try:
                        self.camera.stop()
                    except Exception:
                        pass
                    camera_started = False
                    with self.camera_lock:
                        self.camera_running = False
                    time.sleep(0.2)
        finally:
            with self.camera_lock:
                self.camera_running = False
            if camera_started:
                try:
                    self.camera.stop_preview()
                except Exception:
                    pass
                try:
                    self.camera.stop()
                except Exception:
                    pass
            try:
                import cv2
                cv2.destroyAllWindows()
            except Exception:
                pass
            print("カメラワーカー終了")

    def update_clock(self):
        now = time.strftime("%H:%M:%S")
        self.clock_label.config(text=f"現在時刻:{now}")
        self.root.after(1000, self.update_clock)

    def botan(self, mode):
        self.simulation_mode = "Q1"
        if self.test_mode and hasattr(self, "input_mode_label"):
            self.input_mode_label.config(text="入力元: Camera Module 3", fg="blue")
            self.input_entry.focus_set()

    def entry(self, event=None):
        data = self.input_entry.get().strip().upper()

        if not data:
            self.input_result_label.config(text="入力してください", fg="red")
            self.input_entry.focus_set()
            return "break"

        if data.startswith("A"):
            self.input_result_label.config(text=f"AGV {data} はクライアント側から送信してください", fg="red")
        else:

            self.event_queue.put(("QR1", data))
            self.input_result_label.config(text=f"台車QR {data} を送信しました", fg="blue")

        self.input_entry.delete(0, tk.END)
        self.input_entry.focus_set()
        return "break"

    def qr_yomi(self):
        while True:
            try:
                event_type, data = self.event_queue.get_nowait()
            except queue.Empty:
                break

            try:
                if event_type == "QR1":
                    self.receive_camera1(data)
                elif event_type == "NETWORK_AGV":
                    self._receive_remote_agv_in_gui(data)
            except Exception as error:
                print("イベント処理エラー:", error)
                self.csv_logger.log_error("イベント処理", event_type, data, error)

        self.root.after(50, self.qr_yomi)

    def receive_camera1(self, qr_data):
        self.receive_camera(qr_data)

    def receive_camera(self, qr_data):
        qr_data = str(qr_data).strip()
        # 台車は半角数字1～24だけ。A1、AGV1、0、25、文字列などは無視。
        if not re.fullmatch(r"(?:[1-9]|1[0-9]|2[0-4])", qr_data):
            return

        camera_name = "Camera Module 3"
        read_key = (camera_name, qr_data)
        now_monotonic = time.monotonic()
        previous_read = self.saigo_qr_read.get(read_key)
        read_time = time.strftime("%Y/%m/%d %H:%M:%S")
        # 同じ台車QRが続く場合は時間・一時的な画像消失に関係なく無視する。
        # 別の台車QRが受理されると、前のQRを再び受け付けられる。
        if self.last_accepted_cart_qr == qr_data:
            return
        if (self.last_accepted_cart_qr == qr_data
                and previous_read is not None
                and now_monotonic - previous_read < CAMERA_DUPLICATE_IGNORE_SEC):
            self.csv_logger.log_qr(read_time, self.run_mode, camera_name, qr_data, "重複無視")
            self.refresh_qr_history()
            return
        self.saigo_qr_read[read_key] = now_monotonic
        if not self._register_cart_identifier(qr_data):
            self.csv_logger.log_qr(read_time, self.run_mode, camera_name, qr_data, "台数上限/未登録QR")
            self.yoyaku_label.config(text=f"台車QR {qr_data}：登録上限（上側{CARTS_19_COUNT}＋下側{CARTS_5_COUNT}）")
            self.refresh_qr_history()
            return

        self.last_accepted_cart_qr = qr_data
        print(f"[台車QR 読取] {qr_data}", flush=True)
        self.csv_logger.log_qr(read_time, self.run_mode, camera_name, qr_data, "読み込み完了")
        self.refresh_qr_history()
        was_learning_initial_lap = self.cart_lap_count == 0
        self._record_cart_cycle_read(qr_data)
        self.cart_inspection.discard(qr_data)

        self.cart_status.setdefault(qr_data, "未確認")
        self.cart_last_time[qr_data] = read_time
        self.cart_last_camera[qr_data] = camera_name
        self.kadouireru(qr_data)
        self.cameranoaida(qr_data)
        self._update_cart_19_actual_count()

        moved = False if was_learning_initial_lap else self.move_cart_by_qr(qr_data)
        self._rebalance_cart_layout_excluding_inspection()

        self.qr_time_label.config(
            text=f"最後の読取: {camera_name}  台車{qr_data}  {read_time[-8:]}")
        if moved:
            self.yoyaku_label.config(text=f"台車{qr_data}を読み取り → 19台側へ移動開始")
        else:
            self.yoyaku_label.config(text=f"台車{qr_data}をQR読み取りしました")

        self.redraw()

    def _update_cart_19_actual_count(self):
        """点検中の台車を除いた、現在の19台側の実数を更新する。"""
        if self.cart_lap_count <= 0:
            return

        known_ids = set(self.cart_expected_ids) | set(self.cart_configured_expected_ids)
        inspection_ids = set(self.cart_inspection) | set(self.inspection_cart)
        normal_ids = known_ids - inspection_ids
        current_bottom_ids = {
            value for value in CARTS_5_SIDE
            if value and value not in inspection_ids
        }
        known_top_ids = normal_ids - current_bottom_ids
        configured_top_ids = {
            value for value in self.cart_configured_top_order
            if value and value not in current_bottom_ids and value not in inspection_ids
        }
        inferred_top_count = max(len(configured_top_ids), len(known_top_ids))
        self.cart_19_actual_count = max(0, min(CARTS_19_COUNT, inferred_top_count))

    def _rebalance_cart_layout_excluding_inspection(self):
        """19台側を優先。点検台車を外し、空きがあれば7台側から17台側へ移す。"""
        global CARTS_19_SIDE, CARTS_5_SIDE
        global CARTS_20_SIDE, CARTS_19_DISPLAY_ORDER, CARTS_20_DISPLAY_ORDER

        inspection_ids = set(self.cart_inspection) | set(self.inspection_cart)
        top = self._unique_cart_order(
            value for value in CARTS_19_SIDE if value and value not in inspection_ids
        )
        bottom = self._unique_cart_order(
            value for value in CARTS_5_SIDE
            if value and value not in inspection_ids and value not in top
        )

        # 旧仕様と逆：7台側を優先的に埋めず、19台側の空きを先に埋める。
        while len(top) < CARTS_19_COUNT and bottom:
            top.append(bottom.pop(0))

        top = top[:CARTS_19_COUNT]
        bottom = bottom[:CARTS_5_COUNT]
        apply_cart_layout(top, bottom)
        CARTS_20_SIDE = CARTS_19_SIDE
        CARTS_19_DISPLAY_ORDER = CARTS_19_SIDE.copy()
        CARTS_20_DISPLAY_ORDER = CARTS_19_DISPLAY_ORDER

        self.cart_side = {}
        for value in CARTS_19_SIDE:
            if value:
                self.cart_side[value] = "20SIDE"
        for value in CARTS_5_SIDE:
            if value:
                self.cart_side[value] = "5SIDE"
        for cart in inspection_ids:
            self.cart_status[cart] = "点検"
        self._update_cart_19_actual_count()

    def _register_cart_identifier(self, cart):
        """新規台車は17台側の空き枠を優先し、満車時だけ7台側へ登録。"""
        global CARTS_19_SIDE, CARTS_5_SIDE
        global CARTS_20_SIDE, CARTS_19_DISPLAY_ORDER, CARTS_20_DISPLAY_ORDER

        if cart in self.cart_expected_ids or cart in CARTS_19_SIDE or cart in CARTS_5_SIDE:
            self.cart_expected_ids.add(cart)
            self._update_cart_19_actual_count()
            return True

        empty_top = next((i for i, value in enumerate(CARTS_19_SIDE) if not value), None)
        empty_bottom = next((i for i, value in enumerate(CARTS_5_SIDE) if not value), None)
        if empty_top is not None:
            CARTS_19_SIDE[empty_top] = cart
            self.cart_side[cart] = "20SIDE"
        elif empty_bottom is not None:
            CARTS_5_SIDE[empty_bottom] = cart
            self.cart_side[cart] = "5SIDE"
        else:
            return False

        CARTS_20_SIDE = CARTS_19_SIDE
        CARTS_19_DISPLAY_ORDER = CARTS_19_SIDE.copy()
        CARTS_20_DISPLAY_ORDER = CARTS_19_DISPLAY_ORDER
        self.cart_expected_ids.add(cart)
        self._update_cart_19_actual_count()
        return True

    def _known_cart_order(self):
        """現在の配置・設定・周回情報から台車IDの順序を作る。全台車リストを固定で持たずに管理する。"""
        values = []
        values.extend(value for value in CARTS_19_SIDE if value)
        values.extend(value for value in CARTS_5_SIDE if value)
        values.extend(getattr(self, "cart_configured_order", []))
        values.extend(getattr(self, "cart_first_cycle_order", []))
        values.extend(getattr(self, "cart_previous_cycle_order", []))
        values.extend(getattr(self, "cart_current_cycle_order", []))
        values.extend(getattr(self, "cart_expected_ids", set()))
        return self._unique_cart_order(values)

    def _known_cart_ids(self):
        return set(self._known_cart_order())

    @staticmethod
    def _unique_cart_order(values):
        result = []
        seen = set()
        for value in values:
            if value and value not in seen:
                result.append(value)
                seen.add(value)
        return result

    def _complete_cart_lap(self, next_cart):
        """周回終了時、登録済み・以前に確認した台車の未読を点検へ入れる。"""
        global CARTS_19_SIDE, CARTS_5_SIDE
        global CARTS_20_SIDE, CARTS_19_DISPLAY_ORDER, CARTS_20_DISPLAY_ORDER

        completed_order = self._unique_cart_order(self.cart_current_cycle_order)
        if not completed_order:
            self.cart_current_cycle_order = [next_cart]
            return
        is_first_cart_lap = self.cart_lap_count == 0
        observed = set(completed_order)
        expected = set(self.cart_expected_ids) | observed | set(self.cart_configured_expected_ids)
        # 3周終了時点で、1～24のうち一度も読まれなかった番号も点検対象にする。
        # 第1・2周では未登録番号を推測して点検に入れない。
        completed_laps = self.cart_lap_count + 1
        never_read = set()
        if completed_laps >= NEVER_READ_INSPECTION_AFTER_LAPS:
            ever_read = set(self.cart_last_time) | set(self.cart_first_cycle_order) | set(completed_order)
            never_read = CART_QR_ALL_NUMBERS - ever_read
            expected.update(CART_QR_ALL_NUMBERS)
        missing = expected - observed

        if self.cart_lap_count == 0:
            self.cart_first_cycle_order = completed_order.copy()
            self.cart_previous_cycle_order = completed_order.copy()
            self.cart_lap_count = 1
            print("台車QR 初回周回確定:", completed_order)
        else:
            self.cart_previous_cycle_order = completed_order.copy()
            self.cart_lap_count += 1
        self.cart_inspection.difference_update(observed)
        self.inspection_cart[:] = [value for value in self.inspection_cart if value not in observed]
        self.cart_inspection.update(missing)
        if never_read:
            print(f"[未読台車] {completed_laps}周終了・一度も読まれていない番号: "
                  f"{sorted(never_read, key=int)} → 点検エリア", flush=True)
        for read_cart in observed:
            self.cart_status[read_cart] = "稼働"
            if read_cart not in self.run_cart:
                self.run_cart.append(read_cart)
        for missing_cart in missing:
            self.tennkennireru(missing_cart)
        print(
            f"台車QR 第{self.cart_lap_count}周: 今周読取={completed_order} "
            f"/ 未読点検={sorted(missing)} / 次周先頭={next_cart}"
        )

        self.cart_expected_ids = expected
        configured_top = self._unique_cart_order(self.cart_configured_top_order)
        configured_bottom = self._unique_cart_order(self.cart_configured_bottom_order)
        current_bottom = self._unique_cart_order(CARTS_5_SIDE)
        if is_first_cart_lap and not configured_bottom and len(completed_order) >= CARTS_5_COUNT:
            if next_cart == completed_order[0] and len(completed_order) > CARTS_5_COUNT:
                current_bottom = completed_order[1:CARTS_5_COUNT + 1]
            elif not current_bottom:
                current_bottom = completed_order[:CARTS_5_COUNT]
            print("台車 初回周回からカメラ基準で5台側を学習:", current_bottom)

        bottom_ids = set(current_bottom)
        physically_seen = set(self.cart_last_time) | set(completed_order) | set(self.cart_first_cycle_order)
        known_ids = self._unique_cart_order(
            configured_top
            + [value for value in completed_order if value not in bottom_ids]
            + [value for value in CARTS_19_SIDE if value and value not in bottom_ids]
            + [value for value in sorted(expected) if value in physically_seen and value not in bottom_ids]
        )
        configured_top_ids = {value for value in configured_top if value and value not in bottom_ids}
        known_top_ids = set(known_ids) - bottom_ids
        inferred_top_count = max(len(configured_top_ids), len(known_top_ids))
        self.cart_19_actual_count = max(0, min(CARTS_19_COUNT, inferred_top_count))
        top_candidates = self._unique_cart_order(
            configured_top
            + [value for value in completed_order if value not in bottom_ids]
            + [value for value in CARTS_19_SIDE if value and value not in bottom_ids]
            + [value for value in sorted(expected) if value in physically_seen and value not in bottom_ids]
        )
        top_values = top_candidates[:self.cart_19_actual_count]
        if (is_first_cart_lap and not configured_top and not configured_bottom
                and completed_order and next_cart == completed_order[0] and len(top_values) >= 2):
            top_values = [top_values[0]] + list(reversed(top_values[1:]))
        bottom_values = current_bottom[:CARTS_5_COUNT]
        top_values += [""] * (CARTS_19_COUNT - len(top_values))
        bottom_values += [""] * (CARTS_5_COUNT - len(bottom_values))
        apply_cart_layout(top_values, bottom_values)
        self.cart_expected_ids.update(expected)
        self.cart_side = {}
        for value in CARTS_19_SIDE:
            if value:
                self.cart_side[value] = "20SIDE"
        for value in CARTS_5_SIDE:
            if value:
                self.cart_side[value] = "5SIDE"
        for value in expected:
            self.cart_status.setdefault(value, "未確認")
        for cart in self.cart_inspection:
            self.cart_status[cart] = "点検"

        self.redraw()
        if self.cart_19_actual_count is not None:
            self.yoyaku_label.config(
                text=f"台車の周回を確認：上側 {self.cart_19_actual_count}台 / 下側 {CARTS_5_COUNT}台")
        self.cart_current_cycle_order = [next_cart]
        self.cart_last_normal_read = next_cart

    @staticmethod
    def _circular_skipped(master, prev, cur):
        """masterは時計回りの固定順。prevの次からcurの手前までを返す。
        wrapped=Trueは、curがprevより前(または同じ位置)に戻った
        = 周回の境界を越えたことを表す。"""
        pi, ci = master.index(prev), master.index(cur)
        n = len(master)
        gap = (ci - pi - 1) % n
        skipped = [master[(pi + 1 + k) % n] for k in range(gap)]
        return skipped, ci <= pi

    def _record_cart_cycle_read(self, cart):
        """読み飛ばしを検出し、同一周回で読取済みなら点検へ戻さない。"""
        self.cart_expected_ids.add(cart)
        current = self.cart_current_cycle_order
        if cart in self.cart_inspection or cart in self.inspection_cart:
            self.cart_inspection.discard(cart)
            self.inspection_cart[:] = [v for v in self.inspection_cart if v != cart]
            self.cart_status[cart] = "稼働"
            if cart not in current:
                current.append(cart)
            print("点検解除 → 通常読取:", cart)
            return

        if not current:
            current.append(cart)
            self.cart_last_normal_read = cart
            return
        if current[-1] == cart:
            return

        if self.cart_lap_count == 0:
            if cart == current[0] and len(current) > 1:
                self._complete_cart_lap(cart)
            elif cart not in current:
                current.append(cart)
                self.cart_last_normal_read = cart
            return

        master = self.cart_first_cycle_order
        if (master and cart == master[0] and len(current) > 1
                and current[-1] != cart):
            self._complete_cart_lap(cart)
            return
        prev = self.cart_last_normal_read
        if prev in master and cart in master and cart != prev:
            prev_index = master.index(prev)
            now_index = master.index(cart)
            forward = now_index > prev_index or (prev_index == len(master) - 1 and now_index == 0)
            if forward and now_index > prev_index + 1:
                skipped = master[prev_index + 1:now_index]
                for skipped_cart in skipped:
                    if skipped_cart in current:
                        if skipped_cart in self.cart_inspection or skipped_cart in self.inspection_cart:
                            self.cart_inspection.discard(skipped_cart)
                            self.inspection_cart[:] = [
                                value for value in self.inspection_cart if value != skipped_cart
                            ]
                            self.cart_status[skipped_cart] = "稼働"
                            if skipped_cart not in self.run_cart:
                                self.run_cart.append(skipped_cart)
                            print("台車QR 今周既読につき古い点検状態を解除:", skipped_cart)
                        continue
                    if skipped_cart not in self.cart_inspection and skipped_cart not in self.inspection_cart:
                        self.cart_inspection.add(skipped_cart)
                        self.tennkennireru(skipped_cart)
                        print(
                            f"台車QR 読み飛ばし→点検: {skipped_cart} "
                            f"(第{self.cart_lap_count + 1}周, 読取済み={list(current)}, "
                            f"直前={prev}, 今回={cart})"
                        )

        if cart not in current:
            current.append(cart)
        self.cart_last_normal_read = cart
        self.cart_inspection.discard(cart)
        self.inspection_cart[:] = [v for v in self.inspection_cart if v != cart]
        self.cart_status[cart] = "稼働"

    def _init_default_agvs(self):
        self.agv_states.clear()
        now = time.monotonic()
        for index in range(MAX_AGV_COUNT):
            agv_id = f"AGV{index + 1}"
            # 下側7台すべてにAGVを割り当てない（初期稼働の5台分だけ）。
            cart = CARTS_5_SIDE[index] if index < AGV_COUNT and index < len(CARTS_5_SIDE) else None
            base_slot = index if index < AGV_COUNT and index < len(AGV_5SIDE_POSITIONS) else None
            self.agv_states[agv_id] = {
                "phase": "5SIDE_WAIT",
                "carts": [],
                "towing_cart": None,
                "cycle_started": None,
                "cycle_count": 0,
                "base_slot": base_slot,
                "start_slot": base_slot,
                "unload_slot": None,
                "unload_started": None,
                "agv_read_started": None,
                "qr_name": agv_id,
                "display_qr": agv_id,
                "assigned_cart": cart,
                "a_qr_active": False,
                "a_qr_name": None,
                "a_qr_started": None,
                "a_qr_returned": False,
                "a_qr_returned_at": None,
                "a_qr_stage": None,
                "a_qr_target": None,
                "a_qr_return_xy": None,
                "a_qr_path": None,
            }
            if cart:
                self.five_side_agv[cart] = agv_id

    def _agv_loop_position(self, state, now=None):
        if now is None:
            now = time.monotonic()
        route = AGV_ROUTE_POSITIONS[:-1] if len(AGV_ROUTE_POSITIONS) > 1 else list(AGV_ROUTE_POSITIONS)
        if not route:
            return CAMERA_11_OCLOCK_POSITION

        elapsed = max(0.0, now - (state.get("loop_started") or now))
        progress = (elapsed / max(AGV_CYCLE_SEC, 0.1)) % 1.0
        scaled = progress * len(route)
        i0 = int(scaled) % len(route)
        i1 = (i0 + 1) % len(route)
        frac = scaled - int(scaled)
        x0, y0 = route[i0]
        x1, y1 = route[i1]
        return x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac

    @staticmethod
    def _merge_agv_tracking_order(previous_order, actual_order):
        """点検から戻ったAGVを実際の通過順に組み込み、未読AGVの席も残す。

        復帰でA4がA6の後になった例:
          旧 [1,2,3,4,5,6,7,8]
          実 [1,2,3,5,6,4,7,8]
          新 [1,2,3,5,6,4,7,8]
        今周読めなかった番号は前回の並びを手がかりに挿入して残し、
        一時的な未読で学習対象から消えるのを防ぐ。
        """
        result = list(dict.fromkeys(actual_order))
        previous = list(dict.fromkeys(previous_order))
        if not result:
            return previous
        for number in previous:
            if number in result:
                continue
            i = previous.index(number)
            # 前回順で次に観測されたAGVの手前へ置く。
            successor = next(
                (previous[(i + offset) % len(previous)]
                 for offset in range(1, len(previous))
                 if previous[(i + offset) % len(previous)] in result),
                None,
            )
            if successor is None:
                result.append(number)
            elif result.index(successor) == 0:
                # 周回先頭の手前は、循環順では末尾に相当する。
                result.append(number)
            else:
                result.insert(result.index(successor), number)
        return result

    def _agv_observed_since_previous_same_qr(self, next_number):
        """Collect one actual camera-to-camera return, independent of lap cursor.

        When the same AGV is seen after a sufficiently complete circuit, use
        that segment to prevent a stale lap boundary from calling already
        scanned AGVs missing. The history is runtime data, not old CSV data.
        """
        history = self.agv_actual_passage_history
        last_index = next(
            (i for i in range(len(history) - 1, -1, -1)
             if history[i] == next_number), None
        )
        if last_index is None:
            return None
        segment = list(dict.fromkeys(history[last_index:]))
        learned = self.agv_tracking_order or self.agv_first_cycle_order
        # A short backwards movement is not evidence of a completed lap.
        threshold = max(3, len(learned) - 2)
        known_passes = set(segment) & set(learned)
        if len(known_passes) < threshold:
            return None
        return segment

    def _complete_agv_lap(self, next_number, fallback_boundary=False, completed_override=None):
        """今周で既読のAGVが再登場したら周回確定。

        agv_current_cycle_order[0] は前周から引き継ぐ境界QR。
        周回終了判定では既読扱いするが、物理通過の証拠には含めない。
        今回の再登場QRは実通過であり、漏れ判定に含める。
        """
        completed_order = list(dict.fromkeys(
            self.agv_current_cycle_order if completed_override is None else completed_override
        ))
        if not completed_order:
            self.agv_current_cycle_order = [next_number]
            return

        expected = (set(self.agv_required_numbers) | set(self.agv_expected_numbers)
                    | set(self.agv_first_cycle_order) | set(self.agv_tracking_order)
                    | set(completed_order))
        first = not self.agv_first_cycle_order
        # 周回境界のQRは「次の周回の基準点」だが、その周の実通過ではない。
        # 終了を告げた今回のQRだけが実際の再通過として数えられる。
        physical_order = (completed_order if first else completed_order[1:])
        physical_order = list(dict.fromkeys([*physical_order, next_number]))
        observed = set(physical_order)

        if first:
            self.agv_first_cycle_order = completed_order.copy()
            # 初回で途中の番号へ戻った場合、復帰QRより前の番号は
            # 一巡した区間で未通過として扱うという既存ルールを保つ。
            earlier = set(completed_order[:completed_order.index(next_number)])
            missing = (expected - observed) | earlier
            missing.discard(next_number)
            learned = completed_order.copy()
        else:
            # 周回中に一度読んだ番号は二重点検しない。ただし一巡の
            # 継ぎ目で実際に飛ばされた番号は agv_wrap_skipped に保持する。
            missing = (expected - observed) | (set(self.agv_wrap_skipped) - observed)
            previous = list(self.agv_tracking_order or self.agv_first_cycle_order)
            # 順序の学習は論理上の周回境界も含める。
            # 既読判定だけは物理的な再通過を使う（両者を混ぜない）。
            for item in completed_order:
                if item not in previous:
                    previous.append(item)
            learned = (self._merge_agv_tracking_order(previous, completed_order)
                       if len(completed_order) >= 2 else previous)
            if learned != previous:
                print('AGV周回ごとの実通過順へ更新:', learned)

        self.agv_inspection.difference_update(observed)
        self.agv_inspection.update(missing)
        self.agv_expected_numbers = expected
        self.agv_previous_cycle_order = completed_order.copy()
        self.agv_lap_count += 1
        # この番号を次周の開始QRにするが、終了条件はこの番号に固定しない。
        self.agv_cycle_boundary_marker = next_number
        if next_number in learned:
            pivot = learned.index(next_number)
            learned = learned[pivot:] + learned[:pivot]
        self.agv_tracking_order = learned
        self.agv_current_cycle_order = [next_number]
        self.agv_last_forward_number = next_number
        self.agv_pending_passages = (learned[learned.index(next_number) + 1:]
                                     if next_number in learned else [])
        self.last_a_number = next_number
        self.agv_cycle_wrapped = False
        self.agv_cycle_forward_steps = 0
        self.agv_wrap_skipped.clear()
        self.agv_recently_restored.clear()
        self.agv_deferred_recovery = None
        self.agv_recovery_order_dirty = False
        self.agv_unstable_order_this_lap = False
        self.agv_recovery_guard_until.clear()
        self.agv_restored_this_lap.clear()
        print(f'AGV第{self.agv_lap_count}周: 既読={completed_order}; '
              f'未読点検={sorted(missing)}; 次周先頭=A{next_number}; '
              f'待ち={self.agv_pending_passages}')

    @staticmethod
    def _is_agv_first_lap_return(number, current):
        """最初は別QRを挟んで再登場したときだけ一周とする。"""
        return bool(current and number in current
                    and current[-1] != number and len(set(current)) >= 2)

    @staticmethod
    def _agv_is_wrap_transition(learned, previous, current):
        """学習順の終端→先頭をまたいだ「確実性の高い」周回だけ認定。

        単なる逆順 (A7→A4) を周回と勘違いすると、A1～A3まで
        関係ない大量の点検が発生する。前の実際の通常通過位置のみを使い、
        周回境界をまたいだ未読が最大3台の遷移だけを確定する。
        それ以上の大量未読は1カメラでは逆順と区別できず保留する。
        """
        if previous not in learned or current not in learned:
            return False
        n = len(learned)
        i_prev = learned.index(previous)
        i_cur = learned.index(current)
        if i_cur >= i_prev or n < 2:
            return False
        skipped_count = n - i_prev - 1 + i_cur
        near_end = i_prev >= max(1, n - 4)
        near_start = i_cur <= min(2, n - 1) or (i_prev >= n - 2 and i_cur == 3)
        if near_end and near_start and skipped_count <= 3:
            return True

        # 旧判定の「最大3台」制限では、例えば9台中の
        # A5→A1（A6～A9が未読）を逆順とみなして周回を確定できない。
        # 周回の先頭側に戻り、かつ前周の並びを半分以上進んでいる場合に
        # 限り、4台以上の読み逃しも周回境界として認める。
        # A9→A4など途中の番号への逆戻りは認めない。
        sufficiently_advanced = i_prev >= n // 2
        near_anchor = i_cur == 0
        return bool(
            sufficiently_advanced and near_anchor
            and skipped_count <= max(3, n // 2)
        )

    def _check_agv_actual_full_return(self, number, allow_new_inspections=True):
        """AGVの実通過履歴から、現在周回における見逃しを二重確認する。

        周回基準QRが以前の点検・復帰でずれてしまうと、同じA1が
        一周後に再登場しても従来の周回判定が受け付けないことがある。
        最後に同じQRを読んでから、学習済みAGVの大半が通過した
        場合に限って、実際には一度も読めなかったAGVを点検対象へ追加する。
        この補助判定は周回の確定や現在の並びの更新は行わず、
        本来検出すべき読み飛ばしの点検漏れのみを防ぐ。
        """
        history = self.agv_actual_passage_history
        learned = list(dict.fromkeys(self.agv_tracking_order or self.agv_first_cycle_order))
        before = None
        for i in range(len(history) - 1, -1, -1):
            if history[i] == number:
                before = i
                break
        history.append(number)
        passage = history[before + 1:-1] if before is not None else []
        # 記録は直近の最大33件だけ。位置計算後に古い履歴を削除する。
        if len(history) > 3 * MAX_AGV_COUNT:
            del history[:len(history) - 3 * MAX_AGV_COUNT]
        if before is None or len(learned) < 4 or number not in learned or not allow_new_inspections:
            return

        unique_seen = set(passage) & set(learned)
        # 例えば9台中、A1の前後に7台を見たなら一周の再通過と認定。
        # 少数のQRを往復して再読しただけでは判定しない。
        threshold = max(3, len(learned) - 2)
        if len(unique_seen) < threshold:
            return

        missing = set(learned) - unique_seen - {number}
        # すでに点検中だったAGVには二重の通知を行わない。
        newly_missing = missing - self.agv_inspection
        if not newly_missing:
            return
        self.agv_inspection.update(newly_missing)
        self.agv_last_gap_judgment += (
            '; 実通過再確認=' + ','.join(f'A{n}' for n in sorted(newly_missing))
        )
        print('AGV実通過の再確認→点検:', [f'A{n}' for n in sorted(newly_missing)])

    def _record_agv_cycle_read(self, number):
        """AGVの点検と周回判定を1か所で管理する。

        1周終了条件: 今周に既に記録した番号がもう一度来たとき。
        どの番号でもよい。初回の番号や固定の起点には依存しない。
        復帰時は復帰AGVだけ解除し、他のAGVを読み飛ばしにしない。
        """
        current = self.agv_current_cycle_order
        self.agv_expected_numbers.add(number)
        self.agv_last_gap_judgment = f'A{number}: 通常読取'

        # 実際の読取履歴は診断用。過去の履歴で周回を再判定しない。
        history = self.agv_actual_passage_history
        history.append(number)
        if len(history) > 3 * MAX_AGV_COUNT:
            del history[:-3 * MAX_AGV_COUNT]
        self.agv_actual_passage_count += 1

        # 受信側の1秒以内の重複抑制を通ったQRは、連続同番号でも
        # 明示された「今周2回目なら終了」ルールに従う。

        # この関数にはカメラで新たに出現したQRイベントだけを渡す。
        # 文字列が同じでも新たな出現なら、指定どおり今周2回目で一周終了。
        # 【唯一の周回終了条件】今周で読んだ番号の再登場。
        # 旧「固定起点」「同一QRの十分な間隔」「末尾→先頭」の判定は使用しない。
        if number in current:
            learned = self.agv_tracking_order or self.agv_first_cycle_order
            previous = self.last_a_number
            physical_seen = set(current if self.agv_lap_count == 0 else current[1:])
            if learned and previous in learned and number in learned:
                prev_idx = learned.index(previous)
                next_idx = learned.index(number)
                # 先頭側へ周回をまたいだ時だけ、末尾と先頭の間にある
                # 未読を点検対象にする。すでに今周で読んだAGVは除外。
                if prev_idx > next_idx and prev_idx >= len(learned) // 2:
                    crossed = learned[prev_idx + 1:] + learned[:next_idx]
                    for candidate in crossed:
                        if candidate not in physical_seen and candidate != number:
                            self.agv_wrap_skipped.add(candidate)
            self._complete_agv_lap(number)
            self.agv_last_gap_judgment = f'A{number}: 今周2回目の読取で1周終了'
            return True

        if not self.agv_first_cycle_order:
            current.append(number)
            self.agv_inspection.discard(number)
            self.agv_last_forward_number = number
            self.last_a_number = number
            return True

        # 前周の終了QRを今周の通過済みに含めない。
        physically_seen = set(current[1:])

        # 以前「読み飛ばし」と仮判定したQRが今周に実際に来たら、
        # 周回終了時の点検候補からも解除する。
        self.agv_wrap_skipped.discard(number)

        # 点検復帰したQR単独では通過判定の進行位置を動かさない。
        # ただし直後にそのAGVの次の番号が読まれた場合だけ、復帰した
        # AGVを通過基準として採用する（例 A6復帰→A9正常）。
        deferred = self.agv_deferred_recovery
        self.agv_deferred_recovery = None
        if deferred is not None and number not in self.agv_inspection:
            recovered_number, old_forward = deferred
            learned_for_check = self.agv_tracking_order or self.agv_first_cycle_order
            if (recovered_number in learned_for_check
                    and number in learned_for_check
                    and learned_for_check.index(number)
                    == learned_for_check.index(recovered_number) + 1):
                self.agv_last_forward_number = recovered_number
                self.agv_pending_passages = learned_for_check[learned_for_check.index(recovered_number) + 1:]

        # 点検AGVの復帰だけで他のAGVを点検にしない。
        restored = number in self.agv_inspection
        if restored:
            self.agv_inspection.discard(number)
            self.agv_restored_this_lap.add(number)

        learned = self.agv_tracking_order or self.agv_first_cycle_order
        forward = self.agv_last_forward_number
        if forward not in learned or number not in learned:
            current.append(number)
            if not restored:
                self.agv_last_forward_number = number
            self.last_a_number = number
            self.agv_last_gap_judgment = f'A{number}: 点検復帰' if restored else f'A{number}: 新規読取'
            return True

        prev_idx, idx = learned.index(forward), learned.index(number)
        if idx > prev_idx:
            crossed = learned[prev_idx + 1:idx]
            if not restored:
                skipped = [n for n in crossed if n not in physically_seen and n not in self.agv_inspection]
                self.agv_inspection.update(skipped)
                if skipped:
                    print('AGV読み飛ばし即時点検:', [f'A{n}' for n in skipped])
                self.agv_last_gap_judgment = (
                    f'A{forward}→A{number}: '
                    '新規点検=' + (','.join(f'A{n}' for n in skipped) or 'なし'))
            else:
                self.agv_last_gap_judgment = f'A{number}: 点検復帰（別AGVの点検なし）'
            # 復帰QRを比較の起点にすると、例 A1→A4(復帰)→A6(復帰)→A2
            # で、本来未読のA9を見逃す。通常読取の時だけ起点を進める。
            if not restored:
                self.agv_last_forward_number = number
                self.agv_pending_passages = learned[idx + 1:]
            else:
                self.agv_deferred_recovery = (number, forward)
        elif self._agv_is_wrap_transition(learned, forward, number):
            # 先頭側に戻るが、今周2回目のQRでない限りまだ周回終了しない。
            crossed = learned[prev_idx + 1:] + learned[:idx]
            if not restored:
                skipped = [n for n in crossed if n not in physically_seen and n not in self.agv_inspection]
                self.agv_inspection.update(skipped)
                self.agv_wrap_skipped.update(skipped)
                self.agv_last_gap_judgment = (
                    f'A{forward}→A{number}: 周回またぎ通過・点検='
                    + (','.join(f'A{n}' for n in skipped) or 'なし'))
            else:
                # 点検復帰そのものでは他のAGVを点検にしない。
                # ただし学習済みの順番で「末尾→先頭」の周回境界を越え、
                # 前周の終了QRが物理的に今周まだ通過していなければ、
                # その境界QRだけは点検対象とする（例 A9→A2、未通過A1）。
                boundary = self.agv_cycle_boundary_marker
                boundary_missed = (boundary in crossed and boundary not in physically_seen
                                   and boundary != number and boundary not in self.agv_inspection)
                if boundary_missed:
                    self.agv_inspection.add(boundary)
                    self.agv_wrap_skipped.add(boundary)
                    self.agv_last_gap_judgment = (
                        f'A{number}: 点検復帰・周回境界A{boundary}を読み飛ばし即時点検')
            if not restored:
                self.agv_last_forward_number = number
                self.agv_pending_passages = learned[idx + 1:]
            else:
                self.agv_deferred_recovery = (number, forward)
        else:
            # 逆順・入れ替わりは周回境界と誤認しない。
            self.agv_last_gap_judgment = (
                f'A{number}: 点検復帰（位置維持）' if restored
                else f'A{number}: 通過順入れ替わり（位置維持）')

        if restored:
            self.agv_deferred_recovery = (number, forward)
        current.append(number)
        self.last_a_number = number
        return True

    def _log_agv_judgment(self, qr_data, previous_lap, previous_inspection):
        """現場で誤判定を再現した時にQR順と点検理由を追跡できるCSV。"""
        try:
            log_path = Path(__file__).resolve().parent / 'agv_judgment_log.csv'
            empty = not log_path.exists() or log_path.stat().st_size == 0
            with log_path.open('a', encoding='utf-8-sig' if empty else 'utf-8', newline='') as file:
                writer = csv.writer(file)
                if empty:
                    writer.writerow(['日時', 'QR', '周回前', '周回後', '点検追加', '点検解除',
                                     '今周の読取順', '判定基準順', '点検中'])
                writer.writerow([
                    time.strftime('%Y/%m/%d %H:%M:%S'), qr_data, previous_lap,
                    self.agv_lap_count,
                    ','.join('A'+str(n) for n in sorted(self.agv_inspection - previous_inspection)),
                    ','.join('A'+str(n) for n in sorted(previous_inspection - self.agv_inspection)),
                    ','.join('A'+str(n) for n in self.agv_current_cycle_order),
                    ','.join('A'+str(n) for n in self.agv_tracking_order),
                    ','.join('A'+str(n) for n in sorted(self.agv_inspection)),
                ])
        except (OSError, csv.Error) as error:
            # 記録失敗でメインの点検・周回判定を止めない。
            print('AGV判定履歴CSV保存エラー:', error)
        # 旧 agv_judgment_log.csv の列構成を変えず、診断ログを別ファイルに保存する。
        # 何周も使った後の誤判定でも、A5→A7の候補A6を確認できる。
        try:
            debug_path = Path(__file__).resolve().parent / 'agv_gap_debug_v4.csv'
            empty_debug = not debug_path.exists() or debug_path.stat().st_size == 0
            with debug_path.open('a', encoding='utf-8-sig' if empty_debug else 'utf-8', newline='') as f:
                writer = csv.writer(f)
                if empty_debug:
                    writer.writerow(['日時', 'QR', '周回前', '周回後', '点検追加', '点検解除',
                                     '読み飛ばし判定詳細', '今周既読', '点検中',
                                     '固定周回基準QR', '最後に読んだQR', '学習した循環順'])
                writer.writerow([
                    time.strftime('%Y/%m/%d %H:%M:%S'), qr_data,
                    previous_lap, self.agv_lap_count,
                    ','.join('A'+str(n) for n in sorted(self.agv_inspection - previous_inspection)),
                    ','.join('A'+str(n) for n in sorted(previous_inspection - self.agv_inspection)),
                    getattr(self, 'agv_last_gap_judgment', ''),
                    ','.join('A'+str(n) for n in self.agv_current_cycle_order),
                    ','.join('A'+str(n) for n in sorted(self.agv_inspection)),
                    'A' + str(self.agv_cycle_boundary_marker),
                    'A' + str(self.last_a_number),
                    ','.join('A'+str(n) for n in self.agv_tracking_order),
                ])
        except (OSError, csv.Error) as error:
            print('AGV読み飛ばし診断ログ保存エラー:', error)

    def receive_agv(self, qr_data, camera_name="AGVクライアント(TCP)"):
        qr_data = str(qr_data).strip().upper()
        read_key = (camera_name, qr_data)
        now_monotonic = time.monotonic()
        previous_read = self.saigo_qr_read.get(read_key)
        read_time = time.strftime("%Y/%m/%d %H:%M:%S")
        match = re.fullmatch(r"A(\d+)", qr_data, flags=re.IGNORECASE)
        if not match:
            self.yoyaku_label.config(text=f"AGV QRは A1～A{MAX_AGV_COUNT} の形式です")
            return

        number = int(match.group(1))
        if not (1 <= number <= MAX_AGV_COUNT):
            self.yoyaku_label.config(text=f"AGVは最大{MAX_AGV_COUNT}台です")
            return
        # 同じAGVの連続受信は周回として計上しない（別のAGVが通過すれば解除）。
        if self.last_accepted_agv_qr == f"A{number}":
            return
        # AGVは1秒の時間制限を使わない。連続した同一QRのみ
        # _record_agv_cycle_readで除外し、QRが変われば即座に判定する。
        lap_before = self.agv_lap_count
        inspection_before = set(self.agv_inspection)
        if not self._record_agv_cycle_read(number):
            return
        self.saigo_qr_read[read_key] = now_monotonic
        self.last_accepted_agv_qr = qr_data
        newly_inspected = self.agv_inspection - inspection_before
        newly_restored = inspection_before - self.agv_inspection
        self._log_agv_judgment(qr_data, lap_before, inspection_before)
        if newly_inspected:
            names = ", ".join(f"A{n}" for n in sorted(newly_inspected))
            self.yoyaku_label.config(text=f"{qr_data} 読取 → {names} を点検へ移動")
        elif newly_restored:
            names = ", ".join(f"A{n}" for n in sorted(newly_restored))
            self.yoyaku_label.config(text=f"{qr_data} 読取 → {names} の点検解除")

        agv_id = f"AGV{number}"
        if agv_id not in self.agv_states:
            self.yoyaku_label.config(text=f"{agv_id}は現在の設定（{AGV_COUNT}台）では存在しません")
            self.redraw()
            return

        state = self.agv_states[agv_id]
        if state.get("a_qr_active"):
            self.redraw()
            return

        slot_w = 42
        gap = 5
        start_x = 30
        y19 = 70
        slot_h = 38
        a_path = [CAMERA_11_OCLOCK_POSITION]
        a_path.extend(
            (start_x + i * (slot_w + gap) + slot_w / 2, y19 + slot_h / 2)
            for i in range(CARTS_19_COUNT)
        )
        target_xy = a_path[-1] if a_path else CAMERA_11_OCLOCK_POSITION
        slot = state.get("base_slot")
        if not isinstance(slot, int) or slot < 0:
            slot = number - 1
        if AGV_5SIDE_POSITIONS:
            slot = min(slot, len(AGV_5SIDE_POSITIONS) - 1)
            return_xy = AGV_5SIDE_POSITIONS[slot]
        else:
            return_xy = (350, 310)

        state["a_qr_active"] = True
        state["a_qr_name"] = qr_data
        state["a_qr_started"] = now_monotonic
        state["a_qr_returned"] = False
        state["a_qr_returned_at"] = None
        state["a_qr_stage"] = "TO_19"
        state["a_qr_target"] = target_xy
        state["a_qr_return_xy"] = return_xy
        state["a_qr_path"] = a_path

        self.a_qr_active[qr_data] = True
        self.a_qr_started[qr_data] = now_monotonic
        self.a_qr_name[agv_id] = qr_data
        self.a_qr_returned[agv_id] = False

        self.last_agv_qr = agv_id
        self.agv_qr = agv_id
        self.agv_carts = list(state.get("carts", []))
        self.agv_logger.log(qr_data, state.get("carts", []))
        self.csv_logger.log_qr(read_time, self.run_mode, camera_name, qr_data, "AGV登録")
        self.refresh_qr_history()
        self.qr_time_label.config(text=f"最後の読取: {camera_name}  AGV {qr_data}  {read_time[-8:]}")
        if not newly_inspected and not newly_restored:
            self.yoyaku_label.config(text=f"AGV {qr_data}：上側を通過 → {AGV_PASS_RETURN_SEC:g}秒後に7台側（満車なら19台側の通行ルート終端に表示）")
        self.redraw()

    def _reorder_after_camera_read(self, cart):
        """QRを読んだ台車を19台側の先頭へ移す。

        7台側のQRなら19台側の先頭へ移す。19台側が満車の場合に限り
        末尾の台車を7台側へ押し出す。19台側を優先する。
        """
        global CARTS_19_SIDE, CARTS_5_SIDE
        global CARTS_20_SIDE, CARTS_19_DISPLAY_ORDER, CARTS_20_DISPLAY_ORDER

        top = list(CARTS_19_SIDE[:CARTS_19_COUNT])
        bottom = list(CARTS_5_SIDE[:CARTS_5_COUNT])
        top += [""] * (CARTS_19_COUNT - len(top))
        bottom += [""] * (CARTS_5_COUNT - len(bottom))

        if self.cart_19_actual_count is None:
            active_top_count = max(
                (i + 1 for i, value in enumerate(top) if value), default=0
            )
        else:
            active_top_count = max(0, min(CARTS_19_COUNT, int(self.cart_19_actual_count)))

        active_top = [value for value in top[:active_top_count] if value]
        if cart in bottom:
            remaining_bottom = [value for value in bottom if value and value != cart]
            # 19台側に空きがある間は押し出さず、読んだ台車を先頭に追加。
            displaced_top = active_top[-1] if len(active_top) >= CARTS_19_COUNT else ""

            if displaced_top:
                new_top_active = [cart] + [value for value in active_top if value != cart][:-1]
            else:
                new_top_active = [cart] + [value for value in active_top if value != cart]

            if displaced_top and displaced_top != cart and displaced_top not in remaining_bottom:
                remaining_bottom.append(displaced_top)

            top = new_top_active[:CARTS_19_COUNT]
            top += [""] * (CARTS_19_COUNT - len(top))
            bottom = remaining_bottom[:CARTS_5_COUNT]
            bottom += [""] * (CARTS_5_COUNT - len(bottom))
            if self.cart_19_actual_count is None or active_top_count == 0:
                self.cart_19_actual_count = min(CARTS_19_COUNT, len(new_top_active))
        elif cart in top:
            top_active = [value for value in top[:active_top_count] if value]
            if cart in top_active:
                top_active = [cart] + [value for value in top_active if value != cart]
                top = top_active[:CARTS_19_COUNT]
                top += [""] * (CARTS_19_COUNT - len(top))
        else:
            if cart not in self.cart_expected_ids and cart not in self.cart_configured_expected_ids:
                return
            top_active = [value for value in top[:active_top_count] if value and value not in (self.cart_inspection | set(self.inspection_cart))]
            new_top_active = [cart] + [value for value in top_active if value != cart]
            if len(new_top_active) > CARTS_19_COUNT:
                displaced = new_top_active.pop()
                bottom_active = [value for value in bottom if value and value not in (self.cart_inspection | set(self.inspection_cart))]
                if displaced and displaced not in bottom_active:
                    bottom_active.append(displaced)
                bottom = bottom_active[:CARTS_5_COUNT]
                bottom += [""] * (CARTS_5_COUNT - len(bottom))
            top = new_top_active[:CARTS_19_COUNT]
            top += [""] * (CARTS_19_COUNT - len(top))

        apply_cart_layout(top, bottom)
        CARTS_20_SIDE = CARTS_19_SIDE
        CARTS_19_DISPLAY_ORDER = CARTS_19_SIDE.copy()
        CARTS_20_DISPLAY_ORDER = CARTS_19_DISPLAY_ORDER
        self.cart_side = {}
        for value in CARTS_19_SIDE:
            if value:
                self.cart_side[value] = "20SIDE"
        for value in CARTS_5_SIDE:
            if value:
                self.cart_side[value] = "5SIDE"
        self._update_cart_19_actual_count()

    def move_cart_by_qr(self, cart):
        start_slot = CARTS_5_SIDE.index(cart) if cart in CARTS_5_SIDE else 0
        agv_qr = self.five_side_agv.get(cart)

        self._reorder_after_camera_read(cart)

        if agv_qr and agv_qr in self.agv_states:
            state = self.agv_states[agv_qr]
            state["phase"] = "5SIDE_WAIT"
            state["towing_cart"] = None
            state["carts"] = []
            state["cycle_started"] = None
            state["start_slot"] = start_slot
            state["unload_slot"] = None
            state["unload_started"] = None
            self.last_agv_qr = agv_qr
            self.agv_qr = agv_qr
            self.agv_carts = []

        print(f"台車{cart}: QR読取 → 19台側の並びを更新")
        return True

    def update_agv_cycles(self):
        now = time.monotonic()
        changed = False

        for agv_id, state in self.agv_states.items():
            if state.get("a_qr_active"):
                a_qr = state.get("a_qr_name") or agv_id
                started = state.get("a_qr_started")
                if started is not None and now - started >= AGV_PASS_RETURN_SEC:
                    state["a_qr_active"] = False
                    state["a_qr_started"] = None
                    state["a_qr_returned"] = True
                    state["a_qr_returned_at"] = now
                    state["a_qr_stage"] = "RETURNED"
                    self.a_qr_active[a_qr] = False
                    self.a_qr_returned[agv_id] = True
                    changed = True
                    print(f"AGV {a_qr}: {AGV_PASS_RETURN_SEC:g}秒経過 → 待機位置を更新")

            phase = state.get("phase")
            agv_qr = state.get("display_qr") or state.get("qr_name") or agv_id

            if phase != "5SIDE_WAIT":
                state["phase"] = "5SIDE_WAIT"
                state["cycle_started"] = None
                state["carts"] = []
                state["towing_cart"] = None
                state["unload_started"] = None
                changed = True

        if self.last_agv_qr in self.agv_states:
            current = self.agv_states[self.last_agv_qr]
            self.agv_qr = self.last_agv_qr
            self.agv_carts = list(current.get("carts", []))

        active_motion = any(state.get("a_qr_active") for state in self.agv_states.values())
        inspection_moving = any(
            number in self.agv_inspection
            and now - motion["started"] < AGV_INSPECTION_MOVE_SEC
            for number, motion in self.agv_inspection_motion.items()
        )

        if changed or active_motion or inspection_moving:
            self.redraw()

        self.root.after(AGV_DISPLAY_UPDATE_MS, self.update_agv_cycles)

    def issyumenodaisya(self, camera_name, qr_data):
        if qr_data not in self.current_cycle:
            self.current_cycle.append(qr_data)

    def issyuume(self, reason):
        saisyononarabi = []
        for cart in self.current_cycle:
            if cart not in saisyononarabi:
                saisyononarabi.append(cart)

        self.run_cart = saisyononarabi.copy()
        known_carts = self._known_cart_ids() | set(self.cart_expected_ids)
        self.inspection_cart = [cart for cart in known_carts if cart not in self.run_cart]
        self.active_cart_count = len(self.run_cart)
        self.first_cycle = False

        for cart in self.run_cart:
            self.cart_status[cart] = "稼働"
        for cart in self.inspection_cart:
            self.cart_status[cart] = "点検"

        print("初回確認終了:", reason)
        print("稼働:", self.run_cart)
        print("点検:", self.inspection_cart)
        self.yoyaku_label.config(text=f"初回確認終了：{reason}")

    def cameranoaida(self, cart):
        if cart in self.camera_cart_queue:
            self.camera_cart_queue.remove(cart)
        self.camera_cart_queue.append(cart)

    def kadouireru(self, cart):
        if cart in self.inspection_cart:
            self.inspection_cart.remove(cart)
            print("点検 → 稼働:", cart)

        if cart not in self.run_cart:
            self.run_cart.append(cart)

        self.cart_status[cart] = "稼働"
        self.active_cart_count = len(self.run_cart)

    def tennkennireru(self, cart):
        if cart in self.run_cart:
            self.run_cart.remove(cart)

        if cart in self.camera_cart_queue:
            self.camera_cart_queue.remove(cart)

        if cart not in self.inspection_cart:
            self.inspection_cart.append(cart)
            known_order = self._known_cart_order()
            rank = {value: index for index, value in enumerate(known_order)}
            self.inspection_cart.sort(key=lambda value: rank.get(value, len(rank)))

        self.hikadouyoyaku.discard(cart)
        self.cart_status[cart] = "点検"
        self.active_cart_count = len(self.run_cart)
        self.cart_display_position.pop(cart, None)

    def kadoukadouka(self, cart):
        self.tennkennireru(cart)
        print("点検へ移動:", cart)
        self.yoyaku_label.config(text=f"台車{cart}が点検に入りました")
        self.redraw()

    def kodou_osu(self, cart):
        if cart not in self.run_cart:
            return

        if cart in self.hikadouyoyaku:
            self.hikadouyoyaku.discard(cart)
            print(f"台車{cart}の点検予約をキャンセルしました")
            self.yoyaku_label.config(text=f"台車{cart}の点検予約をキャンセルしました")
        else:
            self.hikadouyoyaku.add(cart)
            print(f"台車{cart}を点検予約しました")
            self.yoyaku_label.config(text=f"台車{cart}が点検予約されました")

        self.redraw()

    def run_to_botan(self, cart, body, text):
        cart_tag = f"running_cart_{cart}"
        self.canvas.itemconfig(body, tags=(cart_tag, "running_cart_button"))
        self.canvas.itemconfig(text, tags=(cart_tag, "running_cart_button"))
        self.canvas.tag_bind(cart_tag, "<Button-1>", lambda event, selected=cart: self.kodou_osu(selected))
        self.canvas.tag_bind(cart_tag, "<Enter>", lambda event: self.canvas.config(cursor="hand2"))
        self.canvas.tag_bind(cart_tag, "<Leave>", lambda event: self.canvas.config(cursor=""))

    def draw_running_cart(self, cart, x, y, color):
        yoyaku = cart in self.hikadouyoyaku
        body = self.canvas.create_rectangle(x - 28, y - 18, x + 28, y + 18, fill=color, outline="orange" if yoyaku else "black", width=4 if yoyaku else 1)
        text = self.canvas.create_text(x, y, text=cart, font=("Arial", 12))
        self.running_objects[cart] = {"body": body, "text": text}
        self.run_to_botan(cart, body, text)

    def cancel_cart_animations(self):
        for after_id in self.cart_animation_after_ids.values():
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass

        self.cart_animation_after_ids.clear()

    def make_display_steps(self, start_position, target_position):
        if start_position == target_position:
            return []

        if start_position not in DISPLAY_ROUTE_POSITIONS or target_position not in DISPLAY_ROUTE_POSITIONS:
            return [target_position]

        start_index = DISPLAY_ROUTE_POSITIONS.index(start_position)
        target_index = DISPLAY_ROUTE_POSITIONS.index(target_position)
        route_count = len(DISPLAY_ROUTE_POSITIONS)

        direction = 1
        step_count = (target_index - start_index) % route_count

        steps = []

        for step in range(1, step_count + 1):
            position_index = (start_index + direction * step) % route_count
            steps.append(DISPLAY_ROUTE_POSITIONS[position_index])

        return steps

    def animate_cart(self, cart, steps, step_index=0):
        if cart not in self.running_objects or step_index >= len(steps):
            self.cart_animation_after_ids.pop(cart, None)
            return

        x, y = steps[step_index]
        obj = self.running_objects[cart]

        self.canvas.coords(obj["body"], x - 28, y - 18, x + 28, y + 18)
        self.canvas.coords(obj["text"], x, y)
        self.cart_display_position[cart] = (x, y)

        if step_index + 1 < len(steps):
            after_id = self.root.after(DISPLAY_STEP_MS, self.animate_cart, cart, steps, step_index + 1)
            self.cart_animation_after_ids[cart] = after_id
        else:
            self.cart_animation_after_ids.pop(cart, None)

    def clear_cart_objects(self):
        self.cancel_cart_animations()
        self.canvas.delete("cart_info")

        for obj in self.running_objects.values():
            self.canvas.delete(obj["body"])
            self.canvas.delete(obj["text"])

        for obj in self.inspection_objects.values():
            self.canvas.delete(obj["body"])
            self.canvas.delete(obj["text"])

        self.running_objects.clear()
        self.inspection_objects.clear()

    def line(self):
        """以前の画面サイズに収まる周回ルートと点検エリアを描画する。"""
        route = [
            (18, 18), (38, 12), (65, 10), (100, 10), (160, 10),
            (230, 10), (300, 10), (370, 10), (440, 10), (510, 10),
            (580, 10), (650, 10), (710, 10), (755, 12),
            (790, 25), (815, 48), (830, 78), (834, 115),
            (830, 150), (816, 182), (795, 210), (765, 229),
            (735, 239), (680, 239), (620, 239), (560, 239),
            (500, 239), (440, 239), (380, 239), (320, 239),
            (260, 239), (200, 239), (140, 239), (95, 239),
            (60, 230), (32, 211), (16, 184), (10, 151),
            (10, 112), (12, 77), (16, 45), (18, 18)
        ]

    def _perimeter_s(self, x, y):
        left, top, right, bottom = 320.0, 140.0, 640.0, 290.0
        segments = [(left, bottom, left, top),(left, top, right, top),(right, top, right, bottom),(right, bottom, left, bottom),]
        best = None
        offset = 0.0
        for x1, y1, x2, y2 in segments:
            if x1 == x2:
                qy = max(top, min(bottom, y))
                qx = x1
                dist = abs(x - qx) + abs(y - qy)
                seg_dist = abs(qy - y1)
            else:
                qx = max(left, min(right, x))
                qy = y1
                dist = abs(x - qx) + abs(y - qy)
                seg_dist = abs(qx - x1)
            if best is None or dist < best[0]:
                best = (dist, offset + seg_dist)
            offset += abs(x2 - x1) + abs(y2 - y1)
        return best[1] if best is not None else 0.0

    def _point_from_perimeter_s(self, s_value):
        left, top, right, bottom = 320.0, 140.0, 640.0, 290.0
        segments = [((left, bottom), (left, top)),((left, top), (right, top)),((right, top), (right, bottom)),((right, bottom), (left, bottom)),]
        lengths = [abs(y2-y1) + abs(x2-x1) for (x1,y1),(x2,y2) in segments]
        perimeter = sum(lengths)
        s_value %= perimeter
        walked = 0.0
        for (x1,y1),(x2,y2),length in zip([a for a,b in segments],[b for a,b in segments],lengths):
            if s_value <= walked + length:
                local = (s_value - walked) / max(length, 1.0)
                return x1 + (x2-x1)*local, y1 + (y2-y1)*local
            walked += length
        return segments[-1][1]

    def _clockwise_position_between(self, start_xy, end_xy, progress):
        start_s = self._perimeter_s(*start_xy)
        end_s = self._perimeter_s(*end_xy)
        perimeter = 2 * ((640-320) + (290-140))
        distance = (end_s - start_s) % perimeter
        current_s = start_s + distance * max(0.0, min(1.0, progress))
        return self._point_from_perimeter_s(current_s)

    def _route_position(self, start_index, end_index, progress):
        start_slot = max(0, min(start_index, len(AGV_5SIDE_POSITIONS) - 1)) if AGV_5SIDE_POSITIONS else 0
        end_slot = max(0, min(end_index, len(AGV_19SIDE_POSITIONS) - 1)) if AGV_19SIDE_POSITIONS else 0
        start_xy = AGV_5SIDE_POSITIONS[start_slot] if AGV_5SIDE_POSITIONS else (315, 315)
        end_xy = AGV_19SIDE_POSITIONS[end_slot] if AGV_19SIDE_POSITIONS else (500, 115)
        return self._clockwise_position_between(start_xy, end_xy, progress)

    def _find_unload_index(self, cart, fallback_index=10):
        route_count = len(AGV_ROUTE_POSITIONS)
        if route_count <= 0:
            return 0

        if cart and cart in CARTS_19_SIDE:
            idx = CARTS_19_SIDE.index(cart)
            return max(0, min(int(idx), route_count - 1))
        try:
            fallback = int(fallback_index)
        except (TypeError, ValueError):
            fallback = 0
        return max(0, min(fallback, route_count - 1))

    def _sync_agv_inspection_motion(self):
        """点検判定への追加・解除を追跡。新しく点検になったAGVだけ移動開始。"""
        now = time.monotonic()
        inspection_ids = set(self.agv_inspection)
        for number in list(self.agv_inspection_motion):
            if number not in inspection_ids:
                del self.agv_inspection_motion[number]
        for number in sorted(inspection_ids):
            if number not in self.agv_inspection_motion:
                ordinal = sum(
                    1 for motion in self.agv_inspection_motion.values()
                    if now - motion["started"] < AGV_INSPECTION_MOVE_SEC
                )
                self.agv_inspection_motion[number] = {
                    "started": now + min(ordinal, 7) * 0.27
                }
                agv_id = f"AGV{number}"
                state = self.agv_states.get(agv_id)
                if state is not None:
                    qr_name = state.get("a_qr_name") or f"A{number}"
                    state["a_qr_active"] = False
                    state["a_qr_started"] = None
                    state["a_qr_returned"] = False
                    state["a_qr_stage"] = "INSPECTION"
                    self.a_qr_active[qr_name] = False
                    self.a_qr_returned[agv_id] = False
                print(f"AGV A{number}: 点検エリアへ移動開始")

    @staticmethod
    def _position_on_inspection_path(points, progress):
        """点検までの折れ線ルートを一定速度で移動する表示座標。"""
        import math
        progress = max(0.0, min(1.0, progress))
        if not points:
            return (0, 0)
        if len(points) == 1:
            return points[0]
        lengths = [math.hypot(x2 - x1, y2 - y1)
                   for (x1, y1), (x2, y2) in zip(points, points[1:])]
        total = sum(lengths)
        if total == 0:
            return points[-1]
        remaining = total * progress
        for (x1, y1), (x2, y2), length in zip(points, points[1:], lengths):
            if remaining <= length:
                t = remaining / max(length, 0.00001)
                return (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            remaining -= length
        return points[-1]

    def draw_agv(self):
        self.canvas.delete("agv_info")
        canvas_w = int(self.canvas.cget("width"))
        canvas_h = int(self.canvas.cget("height"))
        route_left = 70
        route_top = 48
        route_right = canvas_w - 70
        route_bottom = canvas_h - 4

        self.canvas.create_oval(
            route_left, route_top, route_right, route_bottom,
            outline="#777777", width=5, tag="agv_info"
        )
        cart_inspection_ids = set(self.cart_inspection) | set(self.inspection_cart)
        display_positions = list(CARTS_19_DISPLAY_ORDER) + list(CARTS_5_SIDE)
        display_positions = (display_positions + [""] * TOTAL_CART_SLOTS)[:TOTAL_CART_SLOTS]
        if (self.cart_lap_count == 0
                and not self.cart_configured_top_order
                and not self.cart_configured_bottom_order):
            seen_top = self._unique_cart_order(display_positions[:CARTS_19_COUNT])
            display_positions[:CARTS_19_COUNT] = (
                list(reversed(seen_top))
                + [""] * (CARTS_19_COUNT - len(seen_top))
            )
        visible_top = self._unique_cart_order(
            cart for cart in display_positions[:CARTS_19_COUNT]
            if cart and cart not in cart_inspection_ids
        )
        visible_count = len(visible_top)
        top_x0 = 62
        top_right = canvas_w - 62
        top_gap = 4 if visible_count >= 12 else 6
        usable_top_width = top_right - top_x0 - max(0, visible_count - 1) * top_gap
        top_w = min(128, int(usable_top_width / max(1, visible_count)))
        top_w = max(34, top_w)
        if visible_count <= 4:
            top_h = 72
            top_font = 24
        elif visible_count <= 8:
            top_h = 66
            top_font = 20
        elif visible_count <= 12:
            top_h = 58
            top_font = 16
        elif visible_count <= 16:
            top_h = 52
            top_font = 13
        else:
            top_h = 46
            top_font = 11
        top_y = 114  # 工程名を台車番号のすぐ上に表示するため少し下げる

        self.top_positions = []
        for index, cart in enumerate(visible_top):
            x = top_x0 + index * (top_w + top_gap)
            self.top_positions.append((x, top_y))
            # 工程名は台車番号ではなく上側の位置（5台目以降）に対応。
            if index >= 4:
                self.canvas.create_text(
                    x + top_w / 2, top_y - 13,
                    text=f"I/P{index - 3:02d}",
                    fill="#ffd55c", font=("Arial", 9, "bold"),
                    tag="agv_info"
                )
            self.canvas.create_rectangle(
                x, top_y, x + top_w, top_y + top_h,
                fill="black", outline="white", width=2, tag="agv_info"
            )
            self.canvas.create_text(
                x + top_w / 2, top_y + top_h / 2,
                text=cart,
                fill="white", font=("Arial", top_font, "bold"),
                width=max(1, top_w - 6),
                tag="agv_info"
            )

        # 旧7台側の設備配置。枠の位置は固定し、台車番号だけ既存の配置に従う。
        bottom_w = 60
        bottom_h = 48
        bottom_centers = BOTTOM_SLOT_CENTERS
        bottom_y = 300
        bottom_positions = []
        for index in range(self.active_bottom_count):
            cart = display_positions[CARTS_19_COUNT + index]
            x = bottom_centers[index] - bottom_w / 2
            bottom_positions.append((x, bottom_y))
            display_cart = cart if cart and cart not in cart_inspection_ids else ""
            self.canvas.create_rectangle(
                x, bottom_y, x + bottom_w, bottom_y + bottom_h,
                fill="black", outline="white" if display_cart else "#777777",
                width=2, tag="agv_info"
            )
            if display_cart:
                self.canvas.create_text(
                    x + bottom_w / 2, bottom_y + bottom_h / 2,
                    text=display_cart, fill="white",
                    font=("Arial", 14, "bold"), tag="agv_info"
                )

        self.bottom_positions = bottom_positions
        self.bottom_w = bottom_w
        self.bottom_h = bottom_h

        if self.active_bottom_count >= 1:
            self.canvas.create_text(
                (bottom_centers[0] + bottom_centers[1]) / 2, bottom_y - 22,
                text="インパネ搭載機", fill="#ffdf87", font=("Arial", 10, "bold"),
                tag="agv_info"
            )
        if self.active_bottom_count >= 3:
            self.canvas.create_text(
                bottom_centers[2], bottom_y - 22,
                text="クォーターウィンドウ", fill="#ffdf87", font=("Arial", 9, "bold"),
                tag="agv_info"
            )
        # 後ろ4枠は工程名をまだ設定しない（番号は通常どおり表示）。
        arrow_points = [
            (route_right - 35, route_top + 5),
            (route_right - 18, route_top + 12),
            (route_right - 35, route_top + 19),
        ]
        self.canvas.create_polygon(
            arrow_points, fill="white", outline="white", tag="agv_info"
        )
        cart_order = self.cart_first_cycle_order or self.cart_previous_cycle_order
        cart_rank = {value: index for index, value in enumerate(cart_order)}
        known_order = self._known_cart_order()
        known_rank = {value: index for index, value in enumerate(known_order)}
        cart_inspection_list = sorted(
            cart_inspection_ids,
            key=lambda value: (cart_rank.get(value, 999), known_rank.get(value, 999), str(value))
        )

        agv_order = self.agv_first_cycle_order or self.agv_previous_cycle_order
        agv_rank = {number: index for index, number in enumerate(agv_order)}
        agv_inspection_list = sorted(
            self.agv_inspection,
            key=lambda value: (agv_rank.get(value, 999), value)
        )
        for index, cart in enumerate(cart_inspection_list[:TOTAL_CART_COUNT]):
            col, row = index % 6, index // 6
            x = 28 + col * 44
            y = 10 + row * 22
            self.canvas.create_rectangle(
                x, y, x + 44, y + 22,
                fill="#2b1a0d", outline="#ff9f1a", width=2,
                tag="agv_info"
            )
            self.canvas.create_text(
                x + 22, y + 11, text=str(cart),
                fill="#ffd38a", font=("Arial", 12, "bold"),
                tag="agv_info"
            )
        self._sync_agv_inspection_motion()
        agv_box_x1 = canvas_w - 270
        agv_box_y1 = canvas_h - 110
        agv_box_x2 = canvas_w - 18
        agv_box_y2 = canvas_h - 16
        branch_start = (route_right - 18, route_top + 150)
        branch_mid1 = (route_right - 35, route_top + 205)
        branch_mid2 = (agv_box_x1 + 125, agv_box_y1 - 16)
        branch_entry = (agv_box_x1 + 125, agv_box_y1)
        inspection_branch = [branch_start, branch_mid1, branch_mid2, branch_entry]
        self.canvas.create_rectangle(   agv_box_x1, agv_box_y1, agv_box_x2, agv_box_y2, outline="#ffd400", width=3, fill="", tag="agv_info")
        moving_inspection = []
        inspection_now = time.monotonic()
        for index, number in enumerate(agv_inspection_list[:MAX_AGV_COUNT]):
            col, row = index % 4, index // 4
            x = agv_box_x1 + 8 + col * 59
            y = agv_box_y1 + 7 + row * 28
            target = (x + 26, y + 12)
            motion = self.agv_inspection_motion.get(number)
            progress = 1.0 if motion is None else max(0.0, min( 1.0, (inspection_now - motion["started"]) / AGV_INSPECTION_MOVE_SEC))
            if progress < 1.0:
                points = inspection_branch + [target]
                moving_inspection.append((number, self._position_on_inspection_path(points, progress)))
            else:
                self.canvas.create_rectangle(x, y, x + 52, y + 24,fill="#2b1a0d", outline="#ffb300", width=2,tag="agv_info")
                self.canvas.create_text(x + 26, y + 12, text=f"A{number}",fill="#ffd770", font=("Arial", 13, "bold"),tag="agv_info")
        now = time.monotonic()
        active_a = []
        for index in range(MAX_AGV_COUNT):
            agv_id = f"AGV{index + 1}"
            state = self.agv_states.get(agv_id)
            if state is None or not state.get("a_qr_active"):
                continue
            if index + 1 in self.agv_inspection:
                continue
            a_qr = state.get("a_qr_name") or f"A{index + 1}"
            started = state.get("a_qr_started") or now
            progress = max(0.0, min(1.0, (now - started) / AGV_PASS_RETURN_SEC))
            start_x = top_x0 + 20
            end_x = top_right - 20
            ax = start_x + (end_x - start_x) * progress
            ay = top_y + top_h + 16 
            active_a.append((progress, a_qr, ax, ay))
        for _, a_qr, ax, ay in active_a:
            self.canvas.create_rectangle(ax - 23, ay - 13, ax + 23, ay + 13,fill="#281608", outline="#ff9c25", width=2,tag="agv_info")
            self.canvas.create_text(ax, ay, text=a_qr, fill="#ffd39b",font=("Arial", 13, "bold"), tag="agv_info")
        returned_active = []
        for index in range(MAX_AGV_COUNT):
            if index + 1 in self.agv_inspection:
                continue
            agv_id = f"AGV{index + 1}"
            state = self.agv_states.get(agv_id)
            if state is None or not state.get("a_qr_returned"):
                continue
            returned_active.append((agv_id, state))
        # 帰還順に「運用台数」分だけ使用。満車なら19台側で待機。
        # 7台側に空きができたら、19台側の待機AGVが帰還順で繰り上がる。
        returned_active.sort(key=lambda item: item[1].get("a_qr_returned_at") or float("inf"))
        # 7台側が満車なら、19台側の通行中のAGVと同じ走行線上の右端で停止表示。
        # A8以降は右から左へ並べる。色や形は通常AGVと同じで文字を追加しない。
        # 走行表示の終点と停止表示の座標を一致させ、下にはずらさない。
        upper_wait_y = top_y + top_h + 16
        upper_wait_gap = max(52, top_w + top_gap)
        upper_wait_start_x = top_right - 20
        for slot_index, (agv_id, state) in enumerate(returned_active[:MAX_AGV_COUNT]):
            if slot_index < self.active_bottom_count:
                state["a_qr_stage"] = "7SIDE_WAIT"
                x, y = bottom_positions[slot_index]
                cx = x + bottom_w / 2
                cy = y + bottom_h + 13
                outline = "#ff9c25"
            else:
                state["a_qr_stage"] = "19SIDE_WAIT"
                extra_index = slot_index - self.active_bottom_count
                cx = upper_wait_start_x - extra_index * upper_wait_gap
                cy = upper_wait_y
                outline = "#ff9c25"
            self.canvas.create_rectangle(
                cx - 23, cy - 12, cx + 23, cy + 12,
                fill="#281608", outline=outline, width=2, tag="agv_info"
            )
            self.canvas.create_text(
                cx, cy, text=state.get("a_qr_name") or agv_id.replace("AGV", "A"),
                fill="#ffd39b", font=("Arial", 13, "bold"), tag="agv_info"
            )
        for number, (x, y) in moving_inspection:
            move_tag = f"agv_inspection_moving_{number}"
            self.canvas.create_rectangle(x - 19, y - 12, x + 19, y + 12,fill="#332700", outline="#ffd400", width=2,tags=("agv_info", move_tag))
            self.canvas.create_text(x, y, text=f"A{number}", fill="#ffeb88",font=("Arial", 12, "bold"),tags=("agv_info", move_tag))
    def daisyajyouhoun(self):
        pass
    def redraw(self):
        self.clear_cart_objects()
        self.line()
        self.draw_agv()
    def close_program(self):
        self.save_runtime_state()
        self.sensor_active_event.clear()
        self.socket_stop_event.set()
        if self.socket_listener is not None:
            try:
                self.socket_listener.close()
            except OSError:
                pass
            self.socket_listener = None
        self.hardware_stop_event.set()
        self.stop_camera()
        try:
            self.set_leds(False, False)
        except Exception:
            pass
        if self.socket_thread is not None and self.socket_thread.is_alive():
            self.socket_thread.join(timeout=1.0)
        for worker in (self.sensor_thread, self.camera_thread):
            if worker is not None and worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=2.0)
        if self.camera is not None:
            try:
                self.camera.close()
            except Exception:
                pass
        if not self.test_mode and GPIO_AVAILABLE and not CAMERA_ALWAYS_ON:
            try:
                GPIO.cleanup()
            except Exception:
                pass
        self.root.destroy()
def main():
    parser = argparse.ArgumentParser(description="台車管理サーバー（台車カメラ＋AGV TCP受信）")
    parser.add_argument("--simulate", action="store_true", help="PC用シミュレーションモードで起動")
    parser.add_argument("--host", default=SOCKET_BIND_HOST, help="TCP待受IP（既定 0.0.0.0）")
    parser.add_argument("--port", type=int, default=SOCKET_PORT, help="TCP待受ポート（既定 5000）")
    args = parser.parse_args()
    root = tk.Tk()
    app = daisyakannri(root, "MANUAL_CAMERA", simulation_mode=args.simulate)
    app.socket_bind_host = args.host
    app.socket_port = args.port
    try:
        app.start()
        root.mainloop()
    except Exception as error:
        app.csv_logger.log_error("起動・メインループ", "-", "", error)
        raise
    finally:
        if root.winfo_exists():
            app.close_program()

if __name__ == "__main__":
    main()
