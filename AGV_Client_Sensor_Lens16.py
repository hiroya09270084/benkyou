# -*- coding: utf-8 -*-
"""AGV専用QRクライアント。

サーバー側の超音波センサーが有効な間だけカメラ本体を起動しAGV QRを読み取る。
Camera Module 3 の下側を切り取って拡大表示（デスクトップ環境・OpenCVが必要）。

実機: python3 AGV_Client_Sensor_Lens16.py --server-ip 192.168.1.156
試験: python3 AGV_QR_クライアント_高解像度_センサー連動.py --simulate --server-ip 127.0.0.1
単発: python3 AGV_QR_クライアント_高解像度_センサー連動.py --send A1 --server-ip 127.0.0.1
"""
import argparse
import queue
import re
import socket
import sys
import threading
import time
import uuid
import csv
from pathlib import Path

SERVER_IP = "192.168.1.156"
SERVER_PORT = 5000
MAX_AGV_COUNT = 11
# 停止中の遠距離QR用。高解像度で読み取り、未検知時はカメラOFF。
CAMERA_SIZE = (4608, 2592)  # Camera Module 3 最大静止画解像度（実機性能により調整）
CAMERA_FPS = 8.0  # 高解像度撮影中の負荷を下げる
MANUAL_LENS_POSITION = 1.6  # --autofocusを指定しない場合に使用
# 元映像の中央下部を約2.5倍狭く切り取って大きく表示。QRが枠外なら調整。
CROP_LEFT = 0.15
CROP_RIGHT = 0.85
CROP_TOP = 0.35
CROP_BOTTOM = 1.00
ROI_CONFIG_PATH = Path(__file__).resolve().parent / "agv_qr_roi.csv"
OVERVIEW_WIDTH = 960
PREVIEW_WIDTH = 960  # 表示幅。元画素が少ないとズームだけでは復元できない。
CAMERA_QR_REARM_ABSENCE_SEC = 0.35
CAMERA_ALWAYS_ON = False  # サーバー側の超音波センサーに連動し撮影ON/OFF
STATE_POLL_SEC = 0.15
STATE_TIMEOUT_SEC = 0.6


def normalize_agv_qr(value):
    """A1 / AGV1 ～ A11 / AGV11 を受理して A形式に統一する。"""
    code = str(value).strip().upper()
    match = re.fullmatch(r"(?:A|AGV)(\d+)", code)
    if match and 1 <= int(match.group(1)) <= MAX_AGV_COUNT:
        return "A" + str(int(match.group(1)))
    return None


def newly_visible_qrs(frame_codes, now, presence_state):
    unique_codes = list(dict.fromkeys(code for code in frame_codes if code))
    visible = set(unique_codes)
    accepted = []
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
        previous = presence_state.get(code)
        if previous is None or (previous[1] is not None and now - previous[1] >= CAMERA_QR_REARM_ABSENCE_SEC):
            accepted.append(code)
        presence_state[code] = (now, None)
    return accepted


class QRRegionEditor:
    """Full-frame overview with mouse-editable QR search region.

    Left drag inside region: move. Drag bottom-right handle: resize.
    Drag outside region: draw new region. Right click: reset to default.
    """

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.window_name = "AGV camera - select QR area"
        self.scale = min(1.0, OVERVIEW_WIDTH / width)
        self.display_width = max(1, int(round(width * self.scale)))
        self.display_height = max(1, int(round(height * self.scale)))
        self.default = (
            int(width * CROP_LEFT), int(height * CROP_TOP),
            int(width * CROP_RIGHT), int(height * CROP_BOTTOM)
        )
        self.rect = list(self.default)
        self.mode = None
        self.start = None
        self.before = None
        self._load()

    def _load(self):
        try:
            with ROI_CONFIG_PATH.open("r", encoding="utf-8-sig", newline="") as f:
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
        with ROI_CONFIG_PATH.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["left", "top", "right", "bottom"])
            writer.writerow([f"{v:.7f}" for v in coords])
        print(f"[QR枠] 設定保存: {ROI_CONFIG_PATH} 画素={self.rect}", flush=True)

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


class AGVSender:
    def __init__(self, host, port, timeout=3.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    @staticmethod
    def read_line(conn):
        response = bytearray()
        while b"\n" not in response:
            part = conn.recv(128)
            if not part:
                raise ConnectionError("応答前に接続が切れました")
            response.extend(part)
            if len(response) > 256:
                raise ConnectionError("サーバー応答が長すぎます")
        return bytes(response).split(b"\n", 1)[0].decode("ascii")

    def get_sensor_state(self):
        """状態問い合わせ。応答不正やタイムアウトは例外とし、スキャンを停止する。"""
        with socket.create_connection((self.host, self.port), timeout=STATE_TIMEOUT_SEC) as conn:
            conn.settimeout(STATE_TIMEOUT_SEC)
            conn.sendall(b"STATE\n")
            response = self.read_line(conn)
        if response not in {"STATE|0", "STATE|1"}:
            raise ConnectionError(f"未対応のサーバー応答: {response!r}")
        return response == "STATE|1"

    def send_once(self, qr_data, event_id=None):
        qr = normalize_agv_qr(qr_data)
        if qr is None:
            raise ValueError(f"AGV QRはA1～A{MAX_AGV_COUNT} または AGV1～AGV{MAX_AGV_COUNT}のみ: {qr_data}")
        if event_id is None:
            event_id = uuid.uuid4().hex
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
            conn.settimeout(max(10.0, self.timeout))
            conn.sendall(f"AGV|{event_id}|{qr}\n".encode("ascii"))
            answer = self.read_line(conn).split("|")
        if len(answer) < 2 or answer[1] != event_id:
            raise ConnectionError(f"ACKのイベントID不一致: {answer}")
        if answer[0] != "OK":
            raise ConnectionError(f"サーバー処理未完了: {answer}")
        return event_id

    def send_with_retry(self, qr_data, event_id=None, stop_event=None, max_attempts=None):
        if event_id is None:
            event_id = uuid.uuid4().hex
        attempts = 0
        while stop_event is None or not stop_event.is_set():
            attempts += 1
            try:
                self.send_once(qr_data, event_id)
                print(f"[ACK] {qr_data} → {self.host}:{self.port}", flush=True)
                return True
            except (OSError, ConnectionError, ValueError) as error:
                print(f"[送信失敗] {qr_data} ({attempts}回目): {error}", flush=True)
                if max_attempts is not None and attempts >= max_attempts:
                    return False
                if stop_event is not None:
                    if stop_event.wait(1.0):
                        break
                else:
                    time.sleep(1.0)
        return False


class AGVCameraClient:
    def __init__(self, sender, autofocus=False):
        self.sender = sender
        self.autofocus = autofocus
        self.events = queue.Queue(maxsize=2048)
        self.stop_event = threading.Event()
        self.scan_enabled = threading.Event()
        self.send_worker = threading.Thread(target=self._send_loop, daemon=True, name="agv-tcp-sender")
        self.state_worker = threading.Thread(target=self._sensor_state_loop, daemon=True, name="sensor-state-poll")
        self.connection_worker = threading.Thread(target=self._connection_status_loop, daemon=True, name="agv-connection-status")
        self.last_enqueued_qr = None  # 同じAGVの連続登録を防止

    def start_workers(self):
        self.send_worker.start()
        self.connection_worker.start()
        if CAMERA_ALWAYS_ON:
            self.scan_enabled.set()
            print("[センサーバイパス] AGVカメラ・QR解析 常時ON", flush=True)
        else:
            self.state_worker.start()

    def _connection_status_loop(self):
        """STATEプロトコルの正常応答で、サーバー接続状態を起動中も確認する。"""
        last_connected = None
        while not self.stop_event.is_set():
            try:
                self.sender.get_sensor_state()
                connected = True
                error_message = ""
            except (OSError, ConnectionError, UnicodeError, ValueError) as error:
                connected = False
                error_message = str(error)

            if connected != last_connected:
                if connected:
                    print(f"【接続完了】サーバー {self.sender.host}:{self.sender.port} / STATE応答正常", flush=True)
                else:
                    print(f"【接続失敗・切断】{self.sender.host}:{self.sender.port} : {error_message}", file=sys.stderr, flush=True)
                last_connected = connected
            self.stop_event.wait(2.0)

    def enqueue(self, qr):
        code = normalize_agv_qr(qr)
        if code is None:
            return False
        if code == self.last_enqueued_qr:
            return False  # A7 → A7 を連続で送信しない
        try:
            self.events.put_nowait((uuid.uuid4().hex, code))
            self.last_enqueued_qr = code
            print(f"[AGV QR] {code} 読取 / 送信待ち {self.events.qsize()}件", flush=True)
            return True
        except queue.Full:
            print("[警告] 送信待ちキュー満杯", file=sys.stderr, flush=True)
            return False

    def _send_loop(self):
        while not self.stop_event.is_set():
            try:
                event_id, qr = self.events.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if not self.sender.send_with_retry(qr, event_id, stop_event=self.stop_event):
                    print(f"[未確認] {qr}: ACK未確認。終了時キュー内データは永続保存されません", file=sys.stderr)
            finally:
                self.events.task_done()

    def _sensor_state_loop(self):
        prior = None
        while not self.stop_event.is_set():
            try:
                active = self.sender.get_sensor_state()
            except (OSError, ConnectionError, UnicodeError) as error:
                active = False  # 通信断の場合は誤読防止のため停止
                if prior is not False:
                    print(f"[通信断/応答異常] QR解析停止: {error}", flush=True)
            if active:
                self.scan_enabled.set()
            else:
                self.scan_enabled.clear()
            if active != prior:
                print("[センサー同期] QR解析ON" if active else "[センサー同期] QR解析OFF / 待機", flush=True)
                prior = active
            self.stop_event.wait(STATE_POLL_SEC)

    def run_camera(self):
        """センサー検知時のみカメラ本体を起動し、未検知時は停止する。"""
        try:
            import cv2
            import numpy as np
            from picamera2 import Picamera2
            from pyzbar.pyzbar import decode
            from libcamera import controls
        except ImportError as error:
            raise SystemExit(f"実機用ライブラリ不足: {error}。python3-opencv も必要です")

        # カメラのインスタンスと設定は一度だけ生成し、start/stopを繰り返す。
        camera = Picamera2()
        camera_started = False
        last_visible = {}
        last_diagnostic_at = 0.0
        frame_index = 0
        editor = None
        # 検知の短い途切れでは頻繁にstart/stopをしない（0.5秒保持）。
        camera_on_hold_sec = 0.5
        last_active_at = 0.0
        # OFFの間は撮影せず、固定の待機画像だけ表示する。
        standby = np.zeros((360, 640, 3), dtype=np.uint8)
        cv2.putText(standby, "CAMERA OFF / WAIT SENSOR", (28, 175),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 170, 255), 2)
        try:
            config = camera.create_video_configuration(
                main={"size": CAMERA_SIZE, "format": "RGB888"},
                controls={"FrameRate": CAMERA_FPS},
            )
            camera.configure(config)
            if self.autofocus:
                camera.set_controls({"AfMode": controls.AfModeEnum.Continuous})
                print("[ピント] オートフォーカス連続調整", flush=True)
            else:
                camera.set_controls({
                    "AfMode": controls.AfModeEnum.Manual,
                    "LensPosition": MANUAL_LENS_POSITION,
                })
                print(f"[ピント] 手動 {MANUAL_LENS_POSITION}", flush=True)
            self.start_workers()
            print("QR探索枠: 全体画面でドラッグ移動・右下角で拡縮・外側で新規枠。w=設定保存、r=初期化、s=画像保存、q=終了", flush=True)
            while not self.stop_event.is_set():
                active = self.scan_enabled.is_set()
                if active:
                    last_active_at = time.monotonic()
                should_run = active or (
                    camera_started and (time.monotonic() - last_active_at < camera_on_hold_sec)
                )

                if should_run and not camera_started:
                    try:
                        camera.start()
                        camera_started = True
                        last_visible.clear()
                        print("[AGVカメラ] ON", flush=True)
                    except Exception as error:
                        print(f"[AGVカメラ] 起動失敗: {error}", file=sys.stderr, flush=True)
                        # ポーリングやソケット送信は継続。次の周期で再試行。
                        self.stop_event.wait(1.0)
                        continue
                elif not should_run and camera_started:
                    try:
                        camera.stop()
                    finally:
                        camera_started = False
                        last_visible.clear()
                        print("[AGVカメラ] OFF", flush=True)

                if not camera_started:
                    cv2.imshow("AGV QR camera - cropped zoom", standby)
                    if (cv2.waitKey(30) & 0xFF) == ord("q"):
                        break
                    self.stop_event.wait(0.08)
                    continue

                # 短い停止猶予中も撮影は行うが、センサーOFFなら解析・送信しない。
                frame = camera.capture_array()
                frame_index += 1
                height, width = frame.shape[:2]
                if editor is None or editor.width != width or editor.height != height:
                    editor = QRRegionEditor(width, height)
                    cv2.namedWindow(editor.window_name, cv2.WINDOW_NORMAL)
                    cv2.resizeWindow(editor.window_name, editor.display_width, editor.display_height)
                    cv2.setMouseCallback(editor.window_name, editor.on_mouse)
                x1, y1, x2, y2 = editor.rect
                roi = frame[y1:y2, x1:x2]
                if roi.size == 0:
                    raise RuntimeError("切り取り範囲が不正です")

                if active and self.scan_enabled.is_set():
                    # pyzbarとOpenCVの両方で試す。表示ズームと検出対象は別。
                    gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
                    decoded = decode(gray) or []
                    detected_texts = []
                    if not decoded:
                        enhanced = cv2.createCLAHE(
                            clipLimit=2.0, tileGridSize=(8, 8)
                        ).apply(gray)
                        decoded = decode(enhanced) or []
                    if not decoded:
                        bigger = cv2.resize(
                            gray, None, fx=2, fy=2,
                            interpolation=cv2.INTER_CUBIC
                        )
                        decoded = decode(bigger) or []

                    detector = cv2.QRCodeDetector()
                    if not decoded:
                        # QRCodeDetectorは複数QRも解析できる。
                        try:
                            ok, values, _, _ = detector.detectAndDecodeMulti(gray)
                            if ok:
                                detected_texts.extend(v for v in values if v)
                        except cv2.error as error:
                            print(f"[OpenCV解析失敗] {error}", flush=True)
                        if not detected_texts:
                            value, _, _ = detector.detectAndDecode(gray)
                            if value:
                                detected_texts.append(value)
                    # ROI外や切り取り境界にQRがある場合、フル画像も低頻度で解析。
                    if not decoded and not detected_texts and frame_index % 5 == 0:
                        full_gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                        decoded = decode(full_gray) or []
                        if not decoded:
                            full_small = cv2.resize(full_gray, (2304, 1296))
                            try:
                                ok, values, _, _ = detector.detectAndDecodeMulti(full_small)
                                if ok:
                                    detected_texts.extend(v for v in values if v)
                            except cv2.error:
                                pass
                    codes = []
                    for found in decoded:
                        try:
                            raw = found.data.decode("utf-8").strip().upper()
                        except (UnicodeDecodeError, AttributeError):
                            continue
                        qr = normalize_agv_qr(raw)
                        if qr is not None:
                            codes.append(qr)
                        else:
                            pass  # 対象外QRは毎フレームのログを出さない
                    for raw_value in detected_texts:
                        raw = raw_value.strip().upper()
                        qr = normalize_agv_qr(raw)
                        if qr is not None:
                            codes.append(qr)
                        else:
                            pass  # 対象外QRは毎フレームのログを出さない
                    if self.scan_enabled.is_set():
                        for qr in newly_visible_qrs(codes, time.monotonic(), last_visible):
                            if qr != self.last_enqueued_qr:
                                print(f"[QRデコード成功] {qr}", flush=True)
                            self.enqueue(qr)
                    if time.monotonic() - last_diagnostic_at >= 3.0:
                        last_diagnostic_at = time.monotonic()
                        print(f"[読取診断] カメラON / QR解析ON / "
                              f"撮影={width}x{height}, 解析範囲={roi.shape[1]}x{roi.shape[0]}, "
                              f"QR検出数={len(decoded)+len(detected_texts)}", flush=True)
                else:
                    last_visible.clear()

                preview_height = max(1, int(roi.shape[0] * PREVIEW_WIDTH / roi.shape[1]))
                preview = cv2.resize(roi, (PREVIEW_WIDTH, preview_height))
                preview = cv2.cvtColor(preview, cv2.COLOR_RGB2BGR)
                cv2.putText(preview, "QR SCAN ON" if active else "STOPPING CAMERA",
                            (16, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                            (0, 255, 0) if active else (0, 170, 255), 2)
                cv2.imshow("AGV QR camera - cropped zoom", preview)
                cv2.imshow(editor.window_name, editor.render(frame))
                pressed = cv2.waitKey(1) & 0xFF
                if pressed == ord("w"):
                    try:
                        editor.save()
                    except OSError as error:
                        print(f"[QR枠] 保存失敗: {error}", file=sys.stderr, flush=True)
                if pressed == ord("r"):
                    editor.reset()
                if pressed == ord("s"):
                    from pathlib import Path
                    filename = Path.cwd() / "agv_camera_debug.png"
                    cv2.imwrite(str(filename), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                    roi_filename = Path.cwd() / "agv_camera_crop_debug.png"
                    cv2.imwrite(str(roi_filename), cv2.cvtColor(roi, cv2.COLOR_RGB2BGR))
                    print(f"[画像保存] フル画像={filename} / 切り取り画像={roi_filename}", flush=True)
                if pressed == ord("q"):
                    break
                self.stop_event.wait(0.02)
        finally:
            self.stop_event.set()
            if camera_started:
                try:
                    camera.stop()
                except Exception:
                    pass
            camera.close()
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="AGV専用QRクライアント / センサー同期")
    parser.add_argument("--server-ip", default=SERVER_IP)
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    parser.add_argument("--simulate", action="store_true", help="PC手入力試験（センサー状態に関係なく送信）")
    parser.add_argument("--send", metavar="AGV_QR", action="append", help="単発送信試験（センサー状態に関係なく送信）")
    parser.add_argument("--autofocus", action="store_true", help="手動ピントではなく連続AFを使用")
    args = parser.parse_args()
    sender = AGVSender(args.server_ip, args.port)
    if args.send:
        ok = True
        for raw in args.send:
            qr = normalize_agv_qr(raw)
            if qr is None:
                print(f"AGV QRが不正: {raw}", file=sys.stderr)
                return 2
            ok = sender.send_with_retry(qr, max_attempts=3) and ok
        return 0 if ok else 1
    if args.simulate:
        print("AGVクライアント手入力試験。A1～A11 / AGV1～AGV11、qで終了")
        while True:
            try:
                raw = input("AGV QR > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if raw.lower() in {"q", "quit", "exit"}:
                break
            qr = normalize_agv_qr(raw)
            if qr is None:
                print("A1～A11を入力してください")
                continue
            sender.send_with_retry(qr, max_attempts=3)
        return 0
    client = AGVCameraClient(sender, autofocus=args.autofocus)
    try:
        client.run_camera()
    except KeyboardInterrupt:
        print("クライアント終了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
