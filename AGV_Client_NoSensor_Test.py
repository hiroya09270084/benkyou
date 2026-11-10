# -*- coding: utf-8 -*-
"""AGV専用QRクライアント。

サーバー側の超音波センサーが有効な間だけAGV QRを読み取る。
Camera Module 3 の下側を切り取って拡大表示（デスクトップ環境・OpenCVが必要）。

実機: python3 AGV_QR_クライアント_高解像度_センサー連動.py --server-ip 192.168.1.155
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

SERVER_IP = "192.168.1.155"
SERVER_PORT = 5000
MAX_AGV_COUNT = 11
# 停止中の遠距離QR用。高解像度で読み取り、未検知時はカメラOFF。
CAMERA_SIZE = (4608, 2592)  # Camera Module 3 最大静止画解像度（実機性能により調整）
CAMERA_FPS = 8.0  # 高解像度撮影中の負荷を下げる
MANUAL_LENS_POSITION = 0.5  # 遠距離の初期値。現場で要調整。
# 元映像の中央下部を約2.5倍狭く切り取って大きく表示。QRが枠外なら調整。
CROP_LEFT = 0.38
CROP_RIGHT = 0.62
CROP_TOP = 0.69
CROP_BOTTOM = 0.86
PREVIEW_WIDTH = 960  # 表示幅。元画素が少ないとズームだけでは復元できない。
CAMERA_QR_REARM_ABSENCE_SEC = 0.35
CAMERA_ALWAYS_ON = True  # センサーを使わないカメラ常時ONテスト
STATE_POLL_SEC = 0.15
STATE_TIMEOUT_SEC = 0.6


def normalize_agv_qr(value):
    code = str(value).strip().upper()
    match = re.fullmatch(r"A(\d+)", code)
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
            raise ValueError(f"AGV QRはA1～A{MAX_AGV_COUNT}のみ: {qr_data}")
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
    def __init__(self, sender):
        self.sender = sender
        self.events = queue.Queue(maxsize=2048)
        self.stop_event = threading.Event()
        self.scan_enabled = threading.Event()
        self.send_worker = threading.Thread(target=self._send_loop, daemon=True, name="agv-tcp-sender")
        self.state_worker = threading.Thread(target=self._sensor_state_loop, daemon=True, name="sensor-state-poll")

    def start_workers(self):
        self.send_worker.start()
        if CAMERA_ALWAYS_ON:
            self.scan_enabled.set()
            print("[センサーバイパス] AGVカメラ・QR解析 常時ON", flush=True)
        else:
            self.state_worker.start()

    def enqueue(self, qr):
        code = normalize_agv_qr(qr)
        if code is None:
            return False
        try:
            self.events.put_nowait((uuid.uuid4().hex, code))
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
            camera.set_controls({
                "AfMode": controls.AfModeEnum.Manual,
                "LensPosition": MANUAL_LENS_POSITION,
            })
            self.start_workers()
            print("センサー待機中: カメラ本体OFF。検知でON、解除でOFF。qで終了", flush=True)
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
                height, width = frame.shape[:2]
                x1, x2 = int(width * CROP_LEFT), int(width * CROP_RIGHT)
                y1, y2 = int(height * CROP_TOP), int(height * CROP_BOTTOM)
                roi = frame[y1:y2, x1:x2]
                if roi.size == 0:
                    raise RuntimeError("切り取り範囲が不正です")

                if active and self.scan_enabled.is_set():
                    decoded = decode(roi) or []
                    if not decoded:
                        gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
                        enlarged = cv2.resize(gray, None, fx=3, fy=3,
                                              interpolation=cv2.INTER_CUBIC)
                        decoded = decode(enlarged) or []
                    codes = []
                    for found in decoded:
                        try:
                            raw = found.data.decode("utf-8")
                        except (UnicodeDecodeError, AttributeError):
                            continue
                        qr = normalize_agv_qr(raw)
                        if qr is not None:
                            codes.append(qr)
                    if self.scan_enabled.is_set():
                        for qr in newly_visible_qrs(codes, time.monotonic(), last_visible):
                            self.enqueue(qr)
                else:
                    last_visible.clear()

                preview_height = max(1, int(roi.shape[0] * PREVIEW_WIDTH / roi.shape[1]))
                preview = cv2.resize(roi, (PREVIEW_WIDTH, preview_height))
                preview = cv2.cvtColor(preview, cv2.COLOR_RGB2BGR)
                cv2.putText(preview, "QR SCAN ON" if active else "STOPPING CAMERA",
                            (16, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                            (0, 255, 0) if active else (0, 170, 255), 2)
                cv2.imshow("AGV QR camera - cropped zoom", preview)
                if (cv2.waitKey(1) & 0xFF) == ord("q"):
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
        print("AGVクライアント手入力試験。A1～A11、qで終了")
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
    client = AGVCameraClient(sender)
    try:
        client.run_camera()
    except KeyboardInterrupt:
        print("クライアント終了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
