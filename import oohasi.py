import RPi.GPIO as GPIO
import time

# GPIOピン番号設定（BCM方式）
TRIG = 4  # 超音波送信ピン
ECHO = 17  # 超音波受信ピン

def setup():
    GPIO.setmode(GPIO.BCM)  # BCM番号で指定
    GPIO.setup(TRIG, GPIO.OUT)
    GPIO.setup(ECHO, GPIO.IN)
    GPIO.output(TRIG, False)
    print("超音波センサー初期化中...")
    time.sleep(2)  # センサー安定化待ち

def measure_distance():
    # 超音波パルス送信
    GPIO.output(TRIG, True)
    time.sleep(0.00001)  # 10μ秒
    GPIO.output(TRIG, False)

    # ECHOがHighになるまで待つ
    start_time = time.time()
    while GPIO.input(ECHO) == 0:
        start_time = time.time()

    # ECHOがLowになるまで待つ
    stop_time = time.time()
    while GPIO.input(ECHO) == 1:
        stop_time = time.time()

    # 時間差から距離計算（音速343m/s）
    elapsed_time = stop_time - start_time
    distance = (elapsed_time * 34300) / 2  # cm単位
    return round(distance, 2)

def main():
    try:
        setup()
        while True:
            dist = measure_distance()
            print(f"距離: {dist} cm")
            time.sleep(1)  # 1秒ごとに測定
    except KeyboardInterrupt:
        print("\n測定終了")
    except Exception as e:
        print(f"エラー: {e}")
    finally:
        GPIO.cleanup()  # GPIO解放

if __name__ == "__main__":
    main()
