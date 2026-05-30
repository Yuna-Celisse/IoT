#!/usr/bin/env python3
"""
ESP8266 串口桥接 — 华为云 IoTDA
================================
读取 ESP8266 通过串口发送的传感器数据，转发到华为云 IoTDA 平台。

架构：
  ESP8266 ──串口(UART)──▶ 本脚本 ──MQTT──▶ 华为云 IoTDA

使用方式：
  python esp8266_serial_bridge.py                          # 自动检测串口
  python esp8266_serial_bridge.py --port COM3               # 指定串口
  python esp8266_serial_bridge.py --port COM3 --baud 9600   # 指定波特率

支持的 ESP8266 数据格式：
  格式1: {"DHT11_T": 25.5, "DHT11_H": 60, ...}
  格式2: {"service": "stm32", "data": {"DHT11_T": 25.5, ...}}
  格式3: {"services": [{"service_id": "Battery", "properties": {...}}]}

依赖：
  pip install paho-mqtt pyserial
"""

import argparse
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from threading import Event

import paho.mqtt.client as mqtt
import serial
import serial.tools.list_ports


# ============================================================
# 配置
# ============================================================

def load_config(config_path: str = None) -> dict:
    config = {
        "device_id": os.environ.get("IOT_DEVICE_ID", ""),
        "password": os.environ.get("IOT_PASSWORD", ""),
        "device_secret": os.environ.get("IOT_DEVICE_SECRET", ""),
        "hostname": os.environ.get("IOT_MQTT_HOST", ""),
        "port": int(os.environ.get("IOT_MQTT_PORT", "8883")),
        "timestamp": os.environ.get("IOT_TIMESTAMP", ""),
    }

    search_paths = []
    if config_path:
        search_paths.append(config_path)
    search_paths.append(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    )
    search_paths.append("config.json")

    for path in search_paths:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    file_config = json.load(f)
                    for key in config:
                        if key in file_config and file_config[key]:
                            config[key] = file_config[key]
                    break
            except (json.JSONDecodeError, IOError) as e:
                print(f"[!] 配置文件读取失败 ({path}): {e}")
    return config


def compute_password(device_secret: str, timestamp: str = None) -> str:
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return hmac.new(
        device_secret.encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_client_id(device_id: str, timestamp: str = None) -> str:
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return f"{device_id}_0_0_{timestamp}"


# ============================================================
# 数据解析
# ============================================================

def parse_esp8266_data(raw: str) -> list:
    """
    解析 ESP8266 串口发来的数据，转换为 IoTDA services 格式。

    支持的输入格式：
      格式1（键值对）:
        {"DHT11_T": 25.5, "DHT11_H": 60.2, "SOIL_H": 45}
        → 根据键名自动归类到对应 service

      格式2（带 service 名）:
        {"service": "stm32", "data": {"DHT11_T": 25.5}}
        → 直接使用指定的 service_id

      格式3（完整 IoTDA 格式）:
        {"services": [{"service_id": "Battery", "properties": {...}}]}
        → 直接转发

    Returns:
        [{"service_id": "xxx", "properties": {...}}, ...]
    """
    data = json.loads(raw) if isinstance(raw, str) else raw

    # 格式3：已经是完整格式，直接返回
    if "services" in data:
        return data["services"]

    # 格式2：带 service 名
    if "service" in data and "data" in data:
        return [{
            "service_id": data["service"],
            "properties": data["data"],
        }]

    # 格式1：键值对，根据属性名自动归类
    # 已知的属性→服务映射
    PROPERTY_SERVICE_MAP = {
        # Battery 服务
        "batteryLevel": "Battery",
        "batteryVoltage": "Battery",
        "battery": "Battery",
        # STM32 环境传感器
        "DHT11_T": "stm32",
        "temperature": "stm32",
        "temp": "stm32",
        "DHT11_H": "stm32",
        "humidity": "stm32",
        "SOIL_H": "stm32",
        "soil": "stm32",
        "MQ135": "stm32",
        "gas": "stm32",
        "FLAME": "stm32",
        "flame": "stm32",
        "motor": "stm32",
        "GPS": "stm32",
        "lon": "stm32",
        "lat": "stm32",
    }

    # 去掉 event_time / timestamp 等元数据键，按 service 分组
    services = {}
    meta_keys = {"event_time", "timestamp", "time", "service", "data"}

    for key, value in data.items():
        if key in meta_keys:
            continue
        svc_id = PROPERTY_SERVICE_MAP.get(key, "default")
        if svc_id not in services:
            services[svc_id] = {}
        services[svc_id][key] = value

    return [
        {"service_id": svc_id, "properties": props}
        for svc_id, props in services.items()
    ]


# ============================================================
# MQTT 桥接
# ============================================================

class SerialToIoTBridge:
    """串口 → 华为云 IoTDA 桥接器"""

    def __init__(self, config: dict):
        self.device_id = config["device_id"]
        self.connected = Event()
        self.report_count = 0
        self.error_count = 0

        # 构建 MQTT 客户端
        device_secret = config.get("device_secret", "").strip()
        timestamp = config.get("timestamp", "").strip()
        if not timestamp:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")

        if device_secret:
            password = compute_password(device_secret, timestamp)
        else:
            password = config["password"]

        client_id = build_client_id(config["device_id"], timestamp)

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(config["device_id"], password)
        self.client.tls_set(
            ca_certs=None,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        self.report_topic = (
            f"$oc/devices/{self.device_id}/sys/properties/report"
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code.value if hasattr(reason_code, "value") else reason_code
        if rc == 0:
            print(f"[MQTT] 已连接 IoTDA (设备: {self.device_id})")
            self.connected.set()
        else:
            print(f"[MQTT] 连接失败 (rc={rc})")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code.value if hasattr(reason_code, "value") else reason_code
        print(f"[MQTT] 断开 (rc={rc})")
        self.connected.clear()

    def connect(self, timeout: float = 15) -> bool:
        host = self.config.get("hostname")
        port = self.config.get("port", 8883)
        if not host:
            print("[MQTT] 未配置 hostname")
            return False
        print(f"[MQTT] 连接 {host}:{port} ...")
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()
        if not self.connected.wait(timeout):
            print("[MQTT] 连接超时")
            return False
        return True

    def forward(self, raw_data: str):
        """解析串口数据并转发到 IoTDA"""
        try:
            services = parse_esp8266_data(raw_data)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[!] 数据解析失败: {e}")
            print(f"    原始数据: {raw_data.strip()[:120]}")
            self.error_count += 1
            return

        if not services:
            print(f"[!] 解析后无有效数据: {raw_data.strip()[:80]}")
            self.error_count += 1
            return

        event_time = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for svc in services:
            svc["event_time"] = event_time

        payload = json.dumps({"services": services})
        result = self.client.publish(self.report_topic, payload, qos=1)

        self.report_count += 1
        now = datetime.now().strftime("%H:%M:%S")
        status = "✓" if result.rc == 0 else "✗"

        # 简洁输出
        parts = []
        for svc in services:
            props_str = ", ".join(
                f"{k}={v}" for k, v in svc["properties"].items()
            )
            parts.append(f"[{svc['service_id']}] {props_str}")
        print(f"  [{now}] #{self.report_count} {status} {' | '.join(parts)}")

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()


# ============================================================
# 串口管理
# ============================================================

def list_serial_ports():
    """列出可用串口"""
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("  未检测到串口设备")
        return []
    print("  可用串口:")
    for p in ports:
        print(f"    {p.device} — {p.description}")
    return [p.device for p in ports]


def auto_detect_port() -> str:
    """自动检测最可能是 ESP8266 的串口"""
    ports = list(serial.tools.list_ports.comports())
    # 优先查找含 CH340 / CP210x / Silicon Labs 描述的串口（常见 ESP8266 USB 芯片）
    for p in ports:
        desc = p.description.lower()
        if any(k in desc for k in ["ch340", "cp210", "silicon", "esp", "uart"]):
            return p.device
    # 否则返回第一个可用串口
    if ports:
        return ports[0].device
    return None


# ============================================================
# 主程序
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="ESP8266 串口 → 华为云 IoTDA 桥接器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python esp8266_serial_bridge.py                    自动检测串口
  python esp8266_serial_bridge.py --port COM3         指定串口
  python esp8266_serial_bridge.py --port COM3 --baud 9600
  python esp8266_serial_bridge.py --list              列出可用串口
        """,
    )
    parser.add_argument("--config", "-c", default=None, help="配置文件路径")
    parser.add_argument("--port", "-p", default=None, help="串口名（如 COM3, /dev/ttyUSB0）")
    parser.add_argument("--baud", "-b", type=int, default=115200, help="波特率（默认 115200）")
    parser.add_argument("--list", "-l", action="store_true", help="列出可用串口后退出")
    parser.add_argument("--timeout", type=float, default=1.0, help="串口读取超时（秒）")
    parser.add_argument("--debug", action="store_true", help="调试模式：显示串口原始数据")
    parser.add_argument("--raw", action="store_true", help="仅监听串口原始输出，不转发 MQTT")
    return parser.parse_args()


def main():
    args = parse_args()

    # 列出串口
    if args.list:
        list_serial_ports()
        return

    # 检测串口
    port = args.port
    if not port:
        port = auto_detect_port()
        if not port:
            print("[X] 未检测到串口。请用 --port 指定或检查硬件连接。")
            print("   使用 --list 查看可用串口列表。")
            sys.exit(1)
        print(f"[*] 自动检测到串口: {port}")

    # 加载配置
    raw_config = load_config(args.config)

    if not raw_config.get("device_id"):
        print("[X] 未配置 device_id，请在 config.json 或环境变量中设置")
        sys.exit(1)
    if not raw_config.get("hostname"):
        print("[X] 未配置 MQTT hostname")
        sys.exit(1)

    print("=" * 56)
    print("  ESP8266 串口 → 华为云 IoTDA 桥接器")
    print("=" * 56)
    print(f"  设备 ID:   {raw_config['device_id']}")
    print(f"  串口:      {port} @ {args.baud} bps")
    print(f"  MQTT:      {raw_config['hostname']}:{raw_config.get('port', 8883)}")
    print("=" * 56)

    # 连接 MQTT
    bridge = SerialToIoTBridge(raw_config)
    bridge.config = raw_config  # 保存引用，connect 需要
    if not bridge.connect():
        print("[X] MQTT 连接失败，请检查凭据和网络")
        sys.exit(1)

    # 打开串口
    print(f"\n[*] 打开串口 {port} ...")
    try:
        ser = serial.Serial(port, args.baud, timeout=args.timeout)
        # 清空缓冲区
        ser.reset_input_buffer()
        print(f"[+] 串口已打开，等待 ESP8266 数据...\n")
    except serial.SerialException as e:
        print(f"[X] 串口打开失败: {e}")
        bridge.disconnect()
        sys.exit(1)

    # 主循环：读串口 → 转 MQTT
    buffer = ""
    idle_ticks = 0
    try:
        while True:
            try:
                if ser.in_waiting > 0:
                    raw_bytes = ser.read(ser.in_waiting)
                    idle_ticks = 0

                    if args.debug:
                        print(f"\n[DEBUG] 收到 {len(raw_bytes)} 字节: {raw_bytes.hex(' ')}")

                    text = raw_bytes.decode("utf-8", errors="replace")
                    buffer += text

                    if args.raw:
                        # 纯监听模式：直接打印，不解析不转发
                        sys.stdout.write(text)
                        sys.stdout.flush()
                        buffer = ""
                    else:
                        # 按行分割处理
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if line:
                                bridge.forward(line)

                else:
                    time.sleep(0.05)
                    idle_ticks += 1
                    # 每 10 秒打印一次等待提示
                    if idle_ticks % 200 == 0 and idle_ticks > 0:
                        idle_sec = idle_ticks * 0.05
                        print(f"  [*] 等待数据中... (已等待 {idle_sec:.0f}s)")

            except serial.SerialException as e:
                print(f"\n[!] 串口错误: {e}")
                print("    等待 3 秒后重试...")
                time.sleep(3)
                try:
                    ser.close()
                    ser = serial.Serial(port, args.baud, timeout=args.timeout)
                    buffer = ""
                    print("[+] 串口已重新连接")
                except Exception:
                    pass

    except KeyboardInterrupt:
        print("\n[*] 收到停止信号")
    finally:
        ser.close()
        bridge.disconnect()
        print(f"\n  运行报告: 转发 {bridge.report_count} 次, 错误 {bridge.error_count} 次")
        print("[−] 已关闭")


if __name__ == "__main__":
    main()
