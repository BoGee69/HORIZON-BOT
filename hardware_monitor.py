import os
import time
import subprocess

def get_temp():
    try:
        # Mencoba membaca suhu CPU di Linux Mint
        temp = subprocess.check_output(["cat", "/sys/class/thermal/thermal_zone0/temp"]).decode("utf-8")
        return float(temp) / 1000
    except:
        return 0

def monitor():
    while True:
        temp = get_temp()
        if temp > 80:
            print(f"⚠️ HIGH TEMP DETECTED: {temp}C. Stopping bot service...")
            os.system("sudo systemctl stop horizon")
            # Tunggu dingin
            while get_temp() > 60:
                time.sleep(60)
            print("🌡️ Temp normalized. Starting bot service...")
            os.system("sudo systemctl start horizon")
        time.sleep(30)

if __name__ == "__main__":
    monitor()
