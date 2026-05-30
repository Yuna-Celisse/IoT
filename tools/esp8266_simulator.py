#!/usr/bin/env python3
"""
ESP8266 传感器模拟器 — 华为云 IoTDA
=====================================
模拟 ESP8266 设备持续采集多种传感器数据并通过 MQTT 上报到华为云 IoTDA 平台。

传感器类型（参考课程设计中的 STM32 + 外设）：
  - DHT11  温湿度传感器 → 温度(°C) / 湿度(%)
  - MQ135  空气质量传感器 → 气体浓度(ppm)
  - 土壤湿度传感器       → 土壤湿度(%)
  - 火焰传感器           → 火焰检测(0/1)
  - 电机状态             → 电机启停(0/1)
  - GPS 定位             → 经纬度
  - Battery              → 电池电量(%) / 电池电压(V)

数据生成策略（模拟真实传感器行为）：
  - sin_wave:   正弦波模拟温度等周期性变化
  - random_walk: 随机游走模拟湿度等缓变信号
  - decay:      线性衰减模拟电池消耗
  - random:     随机波动模拟气体浓度

使用方式：
  python esp8266_simulator.py                        # 默认：Battery 服务，10s 间隔
  python esp8266_simulator.py --full                 # 全部传感器，10s 间隔
  python esp8266_simulator.py --interval 5           # 5 秒上报间隔
  python esp8266_simulator.py --duration 1800        # 运行 30 分钟（1800 秒）
  python esp8266_simulator.py --full --log data.csv  # 全传感器 + 本地 CSV 记录

依赖：
  pip install paho-mqtt
"""

import argparse
import csv
import json
import math
import os
import random
import ssl
import sys
import time
from datetime import datetime, timezone
from threading import Event, Thread


# ============================================================
# 传感器数据生成器
# ============================================================

class SensorGenerator:
    """
    模拟传感器数据生成，使用多种策略产生逼真的传感器读数。

    每种传感器属性有独立的内部状态，随时间演化。
    """

    def __init__(self, seed: int = None):
        if seed is not None:
            random.seed(seed)
        # 内部状态 — 每个属性的当前值
        self._state: dict = {}
        # 运行时间计数器
        self._ticks: int = 0

    def _init_if_needed(self, key: str, initial: float):
        if key not in self._state:
            self._state[key] = initial

    def _sin_wave(
        self, key: str, center: float, amplitude: float,
        period: int, noise: float = 0.5,
    ) -> float:
        """
        正弦波生成器 — 模拟温度等周期性变化。
        center: 中心值
        amplitude: 振幅
        period: 周期（tick 数）
        noise: 随机噪声幅度
        """
        val = center + amplitude * math.sin(
            2 * math.pi * self._ticks / period
        )
        val += random.uniform(-noise, noise)
        self._state[key] = round(val, 1)
        return self._state[key]

    def _random_walk(
        self, key: str, initial: float, step: float,
        vmin: float, vmax: float,
    ) -> float:
        """
        随机游走生成器 — 模拟湿度等缓变信号。
        每一步随机增减一个小量，限制在 [vmin, vmax] 范围内。
        """
        self._init_if_needed(key, initial)
        delta = random.uniform(-step, step)
        val = self._state[key] + delta
        val = max(vmin, min(vmax, val))
        self._state[key] = round(val, 1)
        return self._state[key]

    def _decay(
        self, key: str, initial: float, rate: float,
        vmin: float, bounce: float = 5.0,
    ) -> float:
        """
        衰减生成器 — 模拟电池消耗。
        缓慢下降，偶尔小幅回升，限制在 [vmin, initial] 范围。
        """
        self._init_if_needed(key, initial)
        val = self._state[key] - rate
        # 偶尔小幅回升
        if random.random() < 0.05:
            val += random.uniform(0, bounce)
        val = max(vmin, min(initial, val))
        self._state[key] = round(val, 1)
        return self._state[key]

    def _random_range(
        self, key: str, vmin: float, vmax: float,
    ) -> float:
        """随机范围 — 模拟气体等突变信号"""
        val = random.uniform(vmin, vmax)
        self._state[key] = round(val, 1)
        return self._state[key]

    def _binary(self, key: str, prob_on: float = 0.5) -> int:
        """二值信号 — 模拟开关/火焰等"""
        val = 1 if random.random() < prob_on else 0
        self._state[key] = val
        return val

    def next_tick(self):
        """每个上报周期调用一次，推进时间"""
        self._ticks += 1

    # ---- 具体传感器属性 ----

    def temperature(self) -> float:
        """DHT11 温度 (°C)：日周期正弦波 + 随机噪声"""
        return self._sin_wave("temp", center=26, amplitude=8, period=40, noise=0.5)

    def humidity(self) -> float:
        """DHT11 湿度 (%)：随机游走，30-90% 范围"""
        return self._random_walk("hum", initial=60, step=3, vmin=30, vmax=90)

    def soil_humidity(self) -> float:
        """土壤湿度 (%)：慢速随机游走，20-80%"""
        return self._random_walk("soil", initial=50, step=2, vmin=20, vmax=80)

    def gas_mq135(self) -> float:
        """MQ135 气体浓度 (ppm)：40-120 随机波动"""
        return self._random_range("gas", 40, 120)

    def flame(self) -> int:
        """火焰传感器：大部分时间为 0（安全），偶尔 1（报警）"""
        return self._binary("flame", prob_on=0.05)

    def motor(self) -> int:
        """电机状态：70% 概率运行"""
        return self._binary("motor", prob_on=0.7)

    def gps_longitude(self) -> float:
        """GPS 经度：随机游走模拟漂移（杭州附近）"""
        return self._random_walk("lon", initial=120.21, step=0.002, vmin=120.18, vmax=120.24)

    def gps_latitude(self) -> float:
        """GPS 纬度：随机游走模拟漂移（杭州附近）"""
        return self._random_walk("lat", initial=30.19, step=0.002, vmin=30.16, vmax=30.22)

    def battery_level(self) -> float:
        """电池电量 (%)：缓慢衰减，偶尔回升"""
        return self._decay("bat_lvl", initial=88, rate=0.02, vmin=0, bounce=1.0)

    def battery_voltage(self) -> float:
        """电池电压 (V)：随电量变化"""
        level = self.battery_level()
        # 电压与电量正相关
        voltage = 3.0 + (level / 100.0) * 1.2  # 3.0V ~ 4.2V
        return round(voltage + random.uniform(-0.05, 0.05), 2)


# ============================================================
# 传感器配置预设
# ============================================================

# 完整传感器配置 — 与课程设计中的 stm32 服务定义一致
FULL_SENSOR_CONFIG = [
    # Battery 服务（当前设备已定义）
    {
        "service_id": "Battery",
        "properties": {
            "batteryLevel":    ("battery_level",    "int"),
            "batteryVoltage":  ("battery_voltage",  "float"),
        },
    },
    # STM32 环境传感器（需在产品模型中定义 stm32 服务）
    {
        "service_id": "stm32",
        "properties": {
            "DHT11_T":   ("temperature",    "float"),
            "DHT11_H":   ("humidity",       "float"),
            "SOIL_H":    ("soil_humidity",  "float"),
            "MQ135":     ("gas_mq135",      "int"),
            "FLAME":     ("flame",          "int"),
            "motor":     ("motor",          "int"),
            "GPS":       ("gps_location",   "gps"),
        },
    },
]

# 简化配置 — 仅 Battery（当前设备一定可用）
BATTERY_ONLY_CONFIG = [
    {
        "service_id": "Battery",
        "properties": {
            "batteryLevel":    ("battery_level",   "int"),
            "batteryVoltage":  ("battery_voltage", "float"),
        },
    },
]


import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.iot_common import load_config, compute_password, build_client_id


# ============================================================
# ESP8266 模拟器主类
# ============================================================

class ESP8266Simulator:
    """
    ESP8266 设备模拟器。

    模拟 ESP8266 通过 MQTT 连接华为云 IoTDA，
    周期性采集传感器数据并上报到平台。
    """

    def __init__(
        self,
        config: dict,
        sensor_configs: list,
        interval: float = 10.0,
        log_file: str = None,
    ):
        """
        Args:
            config: MQTT 连接配置
            sensor_configs: 传感器配置列表
            interval: 上报间隔（秒）
            log_file: CSV 日志文件路径（可选）
        """
        self.config = config
        self.device_id = config["device_id"]
        self.interval = interval
        self.sensor_configs = sensor_configs

        # CSV 日志
        self._csv_file = None
        self._csv_writer = None
        if log_file:
            self._csv_file = open(log_file, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            # 写入 CSV 表头
            header = ["timestamp", "service_id"]
            for sc in sensor_configs:
                header.extend(sc["properties"].keys())
            self._csv_writer.writerow(header)
            self._csv_file.flush()
            print(f"[*] CSV 日志: {log_file}")

        # 传感器数据生成器
        self.generator = SensorGenerator()

        # MQTT 连接状态
        self._connected = Event()
        self._stop = Event()

        # 统计
        self.report_count = 0
        self.error_count = 0
        self.start_time = None

        # 构建 MQTT 客户端
        import paho.mqtt.client as mqtt
        timestamp = config.get("timestamp", "").strip()
        if not timestamp:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")

        # 密码
        device_secret = config.get("device_secret", "").strip()
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

        # TLS
        self.client.tls_set(
            ca_certs=None,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )

        # 回调
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        # 上报 Topic
        self.report_topic = (
            f"$oc/devices/{self.device_id}/sys/properties/report"
        )

    # ---------- MQTT 回调 ----------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code.value if hasattr(reason_code, "value") else reason_code
        if rc == 0:
            print("[+] ESP8266 已连接到华为云 IoTDA")
            self._connected.set()
        else:
            print(f"[!] 连接失败 (rc={rc})")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code.value if hasattr(reason_code, "value") else reason_code
        print(f"[-] 连接断开 (rc={rc})")
        self._connected.clear()

    # ---------- 运行控制 ----------

    def connect(self, timeout: float = 15) -> bool:
        """连接到 IoTDA"""
        host = self.config["hostname"]
        port = self.config["port"]
        print(f"[~] ESP8266 正在连接 {host}:{port} ...")

        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()

        if not self._connected.wait(timeout):
            print("[X] ESP8266 连接超时")
            return False
        return True

    def disconnect(self):
        """断开连接"""
        self._stop.set()
        self.client.loop_stop()
        self.client.disconnect()
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None

    # ---------- 数据采集与上报 ----------

    def _collect_sensor_data(self) -> list:
        """
        采集所有传感器的当前读数。
        返回华为云 IoTDA 属性上报格式的服务列表。
        """
        services = []
        for sc in self.sensor_configs:
            service_id = sc["service_id"]
            properties = {}

            for prop_name, (gen_method, prop_type) in sc["properties"].items():
                # 调用生成器对应方法获取传感器读数
                gen_func = getattr(self.generator, gen_method, None)
                if gen_func is None:
                    print(f"[!] 未知生成方法: {gen_method}")
                    continue

                raw_value = gen_func()

                # 类型转换
                if prop_type == "int":
                    value = int(raw_value)
                elif prop_type == "float":
                    value = round(float(raw_value), 2) if isinstance(raw_value, (int, float)) else raw_value
                elif prop_type == "gps":
                    # GPS 以对象形式上报
                    value = {
                        "lon": round(self.generator.gps_longitude(), 6),
                        "lat": round(self.generator.gps_latitude(), 6),
                    }
                else:
                    value = raw_value

                properties[prop_name] = value

            services.append({
                "service_id": service_id,
                "properties": properties,
            })

        return services

    def _report(self, services: list) -> bool:
        """上报数据到 IoTDA"""
        event_time = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for svc in services:
            svc["event_time"] = event_time

        payload = json.dumps({"services": services})
        result = self.client.publish(self.report_topic, payload, qos=1)

        if result.rc == 0:
            return True
        else:
            print(f"[!] 发布失败: {result.rc}")
            return False

    def _log_to_csv(self, services: list):
        """将数据写入 CSV 日志"""
        if not self._csv_writer:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for svc in services:
            row = [now, svc["service_id"]]
            for prop_name in self.sensor_configs_by_service.get(
                svc["service_id"], {}
            ):
                row.append(svc["properties"].get(prop_name, ""))
            self._csv_writer.writerow(row)
        self._csv_file.flush()

    # ---------- 主循环 ----------

    def run(self, duration: float = None):
        """
        运行模拟器主循环。

        Args:
            duration: 运行时长（秒），None 表示无限运行直到 Ctrl+C
        """
        # 构建 service_id -> property_names 的快速索引
        self.sensor_configs_by_service = {}
        for sc in self.sensor_configs:
            self.sensor_configs_by_service[sc["service_id"]] = list(
                sc["properties"].keys()
            )

        self.start_time = time.time()
        print()
        print("=" * 60)
        print("  ESP8266 传感器模拟器 已启动")
        print("=" * 60)
        print(f"  设备 ID:    {self.device_id}")
        print(f"  上报间隔:   {self.interval}s")
        print(f"  传感器服务: {len(self.sensor_configs)} 个")
        for sc in self.sensor_configs:
            props = ", ".join(sc["properties"].keys())
            print(f"    • {sc['service_id']}: {props}")
        if duration:
            print(f"  运行时长:   {duration}s ({duration/60:.1f} 分钟)")
        else:
            print(f"  运行时长:   无限 (Ctrl+C 停止)")
        print("=" * 60)
        print()

        try:
            while not self._stop.is_set():
                loop_start = time.time()

                # 1. 推进时间（让传感器值随时间演化）
                self.generator.next_tick()

                # 2. 采集数据
                services = self._collect_sensor_data()

                # 3. 上报到华为云
                success = self._report(services)
                self.report_count += 1

                # 4. 本地日志
                self._log_to_csv(services)

                # 5. 控制台输出
                now = datetime.now().strftime("%H:%M:%S")
                status = "✓" if success else "✗"
                # 构建一行简洁的输出
                parts = []
                for svc in services:
                    props_str = ", ".join(
                        f"{k}={v}" for k, v in svc["properties"].items()
                    )
                    parts.append(f"[{svc['service_id']}] {props_str}")
                print(f"  [{now}] #{self.report_count} {status} {' | '.join(parts)}")

                # 6. 检查运行时长
                if duration and (time.time() - self.start_time) >= duration:
                    print(f"\n[*] 已达到设定运行时长 ({duration}s)，停止模拟。")
                    break

                # 7. 等待下一个上报周期
                elapsed = time.time() - loop_start
                sleep_time = max(0, self.interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n[*] 收到停止信号...")
        finally:
            elapsed_total = time.time() - self.start_time
            print()
            print("=" * 60)
            print("  模拟器运行报告")
            print("=" * 60)
            print(f"  运行时长:   {elapsed_total:.0f}s ({elapsed_total/60:.1f} 分钟)")
            print(f"  上报次数:   {self.report_count}")
            print(f"  错误次数:   {self.error_count}")
            if self.report_count > 0:
                print(f"  成功率:     {100*(self.report_count-self.error_count)/self.report_count:.1f}%")
            print("=" * 60)


# ============================================================
# 命令行入口
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="ESP8266 传感器模拟器 — 华为云 IoTDA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python esp8266_simulator.py                         # 默认 Battery 服务
  python esp8266_simulator.py --full                  # 全传感器模式
  python esp8266_simulator.py -i 5 -d 1800            # 5s 间隔，运行 30 分钟
  python esp8266_simulator.py --full --log data.csv   # 全传感器 + CSV 日志
        """,
    )
    parser.add_argument(
        "--config", "-c", default=None, help="配置文件路径"
    )
    parser.add_argument(
        "--interval", "-i", type=float, default=10.0,
        help="上报间隔（秒），默认 10",
    )
    parser.add_argument(
        "--duration", "-d", type=float, default=None,
        help="运行时长（秒），默认无限。例如 1800 = 30 分钟",
    )
    parser.add_argument(
        "--full", "-f", action="store_true",
        help="使用全传感器配置（Battery + stm32 环境传感器）",
    )
    parser.add_argument(
        "--log", "-l", default=None, help="CSV 日志文件路径",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="随机种子（用于可复现的测试数据）",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 加载配置
    raw_config = load_config(args.config)

    # 验证必要配置
    if not raw_config.get("hostname"):
        print("[X] 未配置 MQTT hostname")
        sys.exit(1)

    # 选择传感器配置
    if args.full:
        sensor_configs = FULL_SENSOR_CONFIG
    else:
        sensor_configs = BATTERY_ONLY_CONFIG

    # 设置随机种子（使传感器数据的生成可复现）
    if args.seed is not None:
        random.seed(args.seed)

    # 创建模拟器
    simulator = ESP8266Simulator(
        config=raw_config,
        sensor_configs=sensor_configs,
        interval=args.interval,
        log_file=args.log,
    )

    # 连接并运行
    try:
        if not simulator.connect():
            sys.exit(1)
        simulator.run(duration=args.duration)
    except Exception as e:
        print(f"\n[X] 运行错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        simulator.disconnect()
        print("[−] ESP8266 模拟器已关闭")


if __name__ == "__main__":
    main()
