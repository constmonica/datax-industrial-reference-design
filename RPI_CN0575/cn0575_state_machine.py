#!/usr/bin/env python3
"""
CN0575 Command Server — runs on the EVAL-CN0575-RPIZ Raspberry Pi.
Accepts commands over TCP and reads the ADT75 temperature sensor.

"""

import socket
import glob

HOST = "0.0.0.0"
PORT = 10000


def read_adt75_temperature():
    """Read temperature from ADT75 via IIO sysfs interface."""
    # ADT75 exposed as IIO device on Kuiper Linux with rpi-cn0575 overlay
    # Try IIO first, then hwmon fallback
    try:
        # IIO: /sys/bus/iio/devices/iio:deviceX/in_temp_raw + in_temp_scale
        iio_devices = glob.glob("/sys/bus/iio/devices/iio:device*/name")
        for name_path in iio_devices:
            with open(name_path) as f:
                if "adt75" in f.read().strip().lower():
                    base = name_path.rsplit("/", 1)[0]
                    with open(base + "/in_temp_raw") as fr:
                        raw = int(fr.read().strip())
                    with open(base + "/in_temp_scale") as fs:
                        scale = float(fs.read().strip())
                    return raw * scale / 1000.0
    except (IOError, ValueError):
        pass

    try:
        # hwmon fallback: /sys/class/hwmon/hwmonX/temp1_input (millidegrees)
        hwmon_paths = glob.glob("/sys/class/hwmon/hwmon*/temp1_input")
        for path in hwmon_paths:
            with open(path) as f:
                return int(f.read().strip()) / 1000.0
    except (IOError, ValueError):
        pass

    # Direct I2C fallback using smbus2
    try:
        import smbus2
        bus = smbus2.SMBus(1)
        data = bus.read_i2c_block_data(0x48, 0x00, 2)
        bus.close()
        raw = (data[0] << 8) | data[1]
        if raw & 0x8000:
            raw -= 65536
        return (raw >> 4) * 0.0625
    except Exception:
        pass

    return None


def handle_command(cmd):
    """Process a command and return the response string."""
    cmd = cmd.strip().upper()

    if cmd == "READ_TEMP":
        temp = read_adt75_temperature()
        if temp is not None:
            return f"TEMP:{temp:.1f}\n"
        return "ERR:SENSOR_FAIL\n"

    return "ERR:UNKNOWN_CMD\n"


def main():
    print(f"CN0575 Command Server starting on port {PORT}...")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(1)
        print(f"Listening on {HOST}:{PORT}")

        while True:
            try:
                conn, addr = server.accept()
                with conn:
                    data = conn.recv(1024)
                    if not data:
                        continue
                    cmd = data.decode("ascii").strip()
                    print(f"[{addr[0]}] CMD: {cmd}")
                    response = handle_command(cmd)
                    conn.sendall(response.encode("ascii"))
                    print(f"[{addr[0]}] RSP: {response.strip()}")
            except KeyboardInterrupt:
                print("\nShutting down...")
                break
            except Exception as e:
                print(f"Error: {e}")


if __name__ == "__main__":
    main()