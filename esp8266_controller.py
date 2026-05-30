#!/usr/bin/env python3
"""
ESP8266 AT 指令控制器 — 华为云 IoTDA
=====================================
通过串口发送 AT 指令控制 ESP8266 连接华为云 IoTDA 并上报模拟传感器数据。

架构：
  Python ──AT指令(串口)──▶ ESP8266 ──MQTT──▶ 华为云 IoTDA

使用方式：
  python esp8266_controller.py                      # 自动检测串口
  python esp8266_controller.py --port COM15         # 指定串口
  python esp8266_controller.py --interval 5          # 每5秒上报一次

依赖：paho-mqtt  pyserial （本脚本只需要 pyserial，MQTT 由 ESP8266 负责）
"""

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timezone

import serial
import serial.tools.list_ports

from common.iot_common import load_config, compute_password, build_client_id


# ============================================================
# 传感器数据生成器
# ============================================================

class SensorGenerator:
    """生成模拟传感器数据"""

    def __init__(self):
        self._ticks = 0
        self._state = {}

    def _init(self, key, val):
        if key not in self._state:
            self._state[key] = val

    def _walk(self, key, initial, step, vmin, vmax):
        self._init(key, initial)
        delta = random.uniform(-step, step)
        val = max(vmin, min(vmax, self._state[key] + delta))
        self._state[key] = round(val, 1)
        return self._state[key]

    def tick(self):
        self._ticks += 1

    def temperature(self):
        return round(26 + 8 * math.sin(2 * math.pi * self._ticks / 40) + random.uniform(-0.5, 0.5), 1)

    def humidity(self):
        return self._walk("hum", 60, 3, 30, 90)

    def soil(self):
        return self._walk("soil", 50, 2, 20, 80)

    def gas(self):
        return round(random.uniform(40, 120), 1)

    def battery_level(self):
        self._init("bat", 88)
        self._state["bat"] = max(0, min(100, self._state["bat"] - 0.02 + (random.uniform(0, 1) if random.random() < 0.05 else 0)))
        return round(self._state["bat"], 1)

    def battery_voltage(self):
        lvl = self.battery_level()
        return round(3.0 + lvl / 100 * 1.2 + random.uniform(-0.05, 0.05), 2)


# ============================================================
# ESP8266 AT 指令控制器
# ============================================================

class ESP8266Controller:
    """通过 AT 指令控制 ESP8266"""

    def __init__(self, ser: serial.Serial, device_id: str, password: str,
                 timestamp: str, hostname: str, mqtt_port: int = 1883):
        self.ser = ser
        self.device_id = device_id
        self.password = password
        self.timestamp = timestamp
        self.hostname = hostname
        self.mqtt_port = mqtt_port
        self.client_id = build_client_id(device_id, timestamp)
        self.report_count = 0

    def _send_cmd(self, cmd: str, wait_ms: float = 1.0, timeout: float = 15) -> str:
        """发送 AT 指令并读取响应"""
        self.ser.reset_input_buffer()
        full_cmd = cmd + "\r\n"
        self.ser.write(full_cmd.encode())
        time.sleep(wait_ms)
        response = ""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.ser.in_waiting > 0:
                chunk = self.ser.read(self.ser.in_waiting).decode("utf-8", errors="replace")
                response += chunk
                if "OK" in response or "ERROR" in response:
                    break
            time.sleep(0.2)
        return response.strip()

    def setup_mqtt(self) -> bool:
        """配置 ESP8266 MQTT 连接参数"""
        print("[1/3] 配置 MQTT 用户...")
        cmd = (
            f'AT+MQTTUSERCFG=0,1,"{self.client_id}",'
            f'"{self.device_id}","{self.password}",0,0,""'
        )
        resp = self._send_cmd(cmd)
        print(f"      响应: {resp[:80]}")
        if "OK" not in resp:
            print("      [X] 配置失败")
            return False
        print("      [OK]")

        print("[2/3] 连接 IoTDA (最长等待15秒)...")
        cmd = f'AT+MQTTCONN=0,"{self.hostname}",{self.mqtt_port},1'
        resp = self._send_cmd(cmd, wait_ms=3.0, timeout=15)
        print(f"      响应: {resp[:120]}")
        if "OK" not in resp:
            print("      [X] 连接失败，检查网络和密码")
            return False
        print("      [OK]")
        time.sleep(1)

        print("[3/3] 订阅响应 Topic...")
        sub_topic = f"$oc/devices/{self.device_id}/sys/properties/report/response"
        cmd = f'AT+MQTTSUB=0,"{sub_topic}",1'
        resp = self._send_cmd(cmd)
        print(f"      响应: {resp[:80]}")
        print("      [OK]")
        return True

    def publish(self, services: list) -> bool:
        """通过 AT+MQTTPUBRAW 上报属性数据"""
        event_time = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for svc in services:
            svc["event_time"] = event_time

        payload = json.dumps({"services": services}, ensure_ascii=False)
        payload_bytes = len(payload.encode("utf-8"))

        pub_topic = f"$oc/devices/{self.device_id}/sys/properties/report"
        cmd = f'AT+MQTTPUBRAW=0,"{pub_topic}",{payload_bytes},0,0'
        resp = self._send_cmd(cmd)
        if ">" not in resp:
            print(f"      [X] 未收到 '>' 提示符: {resp[:60]}")
            return False

        # 收到 '>' 后发送 payload（不加 \r\n，直接发原始内容）
        self.ser.write(payload.encode())
        time.sleep(0.8)
        # 读取发布结果
        if self.ser.in_waiting > 0:
            result = self.ser.read(self.ser.in_waiting).decode("utf-8", errors="replace")
            if "OK" in result:
                self.report_count += 1
                return True
            else:
                print(f"      [X] 发布失败: {result[:60]}")
                return False
        self.report_count += 1
        return True


# ============================================================
# 串口辅助
# ============================================================

def list_ports():
    print("可用串口:")
    for p in serial.tools.list_ports.comports():
        print(f"  {p.device} — {p.description}")


def auto_detect():
    for p in serial.tools.list_ports.comports():
        desc = p.description.lower()
        if any(k in desc for k in ["ch340", "cp210", "silicon", "esp", "uart"]):
            return p.device
    ports = list(serial.tools.list_ports.comports())
    return ports[0].device if ports else None


# ============================================================
# 主程序
# ============================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="ESP8266 AT 指令控制器 — 华为云 IoTDA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python esp8266_controller.py                     自动检测串口
  python esp8266_controller.py --port COM15         指定串口
  python esp8266_controller.py --interval 5         每5秒上报
  python esp8266_controller.py --list               列出可用串口
        """,
    )
    p.add_argument("--config", "-c", default=None, help="配置文件路径")
    p.add_argument("--port", "-p", default=None, help="串口名")
    p.add_argument("--baud", "-b", type=int, default=115200, help="波特率")
    p.add_argument("--interval", "-i", type=float, default=5.0, help="上报间隔（秒）")
    p.add_argument("--list", "-l", action="store_true", help="列出串口后退出")
    return p.parse_args()


def main():
    args = parse_args()

    if args.list:
        list_ports()
        return

    # 串口
    port = args.port or auto_detect()
    if not port:
        print("[X] 未检测到串口。用 --port 指定或 --list 查看")
        sys.exit(1)

    # 配置
    cfg = load_config(args.config)
    if not cfg.get("device_id") or not cfg.get("hostname"):
        print("[X] 请先配置 config.json（device_id / hostname / password）")
        sys.exit(1)

    device_id = cfg["device_id"]
    hostname = cfg["hostname"]
    # AT 固件用 1883（无 TLS），不管 config.json 里写的是什么
    mqtt_port = 1883

    # 密码处理
    device_secret = cfg.get("device_secret", "").strip()
    timestamp = cfg.get("timestamp", "").strip()
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    if device_secret:
        password = compute_password(device_secret, timestamp)
    else:
        password = cfg["password"]
    client_id = build_client_id(device_id, timestamp)

    print("=" * 56)
    print("  ESP8266 AT 指令控制器")
    print("=" * 56)
    print(f"  设备 ID:   {device_id}")
    print(f"  Client ID: {client_id}")
    print(f"  串口:      {port} @ {args.baud}")
    print(f"  MQTT:      {hostname}:{mqtt_port}")
    print(f"  上报间隔:  {args.interval}s")
    print("=" * 56)

    # 打开串口
    print(f"\n[*] 打开串口 {port}...")
    try:
        ser = serial.Serial(port, args.baud, timeout=1)
        ser.reset_input_buffer()
        print("[+] 串口已打开")
    except Exception as e:
        print(f"[X] 打开串口失败: {e}")
        sys.exit(1)

    controller = ESP8266Controller(
        ser, device_id, password, timestamp, hostname, mqtt_port
    )

    # 配置 MQTT
    print("\n[*] 配置 ESP8266 MQTT...")
    if not controller.setup_mqtt():
        print("[X] MQTT 配置失败，请检查 AT 命令响应")
        ser.close()
        sys.exit(1)

    # 数据生成器
    gen = SensorGenerator()

    # 主循环
    print(f"\n[*] 开始上报模拟数据（间隔 {args.interval}s）...")
    print("   按 Ctrl+C 停止\n")

    try:
        while True:
            gen.tick()

            services = [
                {
                    "service_id": "Battery",
                    "properties": {
                        "batteryLevel": int(gen.battery_level()),
                        "batteryVoltage": gen.battery_voltage(),
                    },
                },
            ]

            now = datetime.now().strftime("%H:%M:%S")
            success = controller.publish(services)
            status = "OK" if success else "FAIL"
            props = services[0]["properties"]
            print(f"  [{now}] #{controller.report_count} {status}  "
                  f"batteryLevel={props['batteryLevel']}  "
                  f"batteryVoltage={props['batteryVoltage']}")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n[*] 停止")
    finally:
        ser.close()
        print(f"[−] 共上报 {controller.report_count} 次")


if __name__ == "__main__":
    main()
