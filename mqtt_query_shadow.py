#!/usr/bin/env python3
"""
华为云 IoTDA — MQTT 设备侧查询与管理工具
============================================
通过 MQTTS 连接到华为云 IoTDA，以设备身份实现以下功能：
  1. 查询设备影子（Device Shadow）—— 获取所有服务的 desired / reported 属性
  2. 查询指定服务的实时属性
  3. 上报/发布设备属性数据到 IoTDA 平台（模拟传感器数据）
  4. 持续监控模式 + matplotlib 实时数据可视化

两种密码模式：
  模式 A — 直接提供 password（你从华为云获取的预计算密码）
  模式 B — 提供 device_secret，程序自动根据当前时间戳计算密码（推荐）

使用方式：
    python mqtt_query_shadow.py                    # 单次查询设备影子
    python mqtt_query_shadow.py --monitor           # 持续监控 + 实时绘图
    python mqtt_query_shadow.py --publish           # 模拟传感器数据上报
    python mqtt_query_shadow.py --config my.json    # 使用指定的配置文件

依赖：
    pip install paho-mqtt matplotlib
"""

import argparse
import hashlib
import hmac
import json
import os
import random
import ssl
import sys
import time
import uuid
from datetime import datetime, timezone
from threading import Event
from typing import Optional

import paho.mqtt.client as mqtt


# ============================================================
# 配置加载
# ============================================================

def load_config(config_path: str = None) -> dict:
    """
    加载配置，优先级：
      1. 命令行指定的配置文件
      2. 当前目录下的 config.json
      3. 环境变量
      4. 代码中的默认值
    """
    config = {
        "device_id": os.environ.get("IOT_DEVICE_ID", ""),
        "password": os.environ.get("IOT_PASSWORD", ""),
        "device_secret": os.environ.get("IOT_DEVICE_SECRET", ""),
        "hostname": os.environ.get("IOT_MQTT_HOST", ""),
        "port": int(os.environ.get("IOT_MQTT_PORT", "8883")),
        "timestamp": os.environ.get("IOT_TIMESTAMP", ""),
    }

    # 尝试从文件加载
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
                    print(f"[*] 已加载配置文件: {path}")
                    break
            except (json.JSONDecodeError, IOError) as e:
                print(f"[!] 配置文件读取失败 ({path}): {e}")

    return config


# ============================================================
# 密码计算（华为云 IoTDA MQTT 设备密钥认证）
# ============================================================

def compute_password(device_secret: str, timestamp: str = None) -> str:
    """
    根据设备密钥（device_secret）和时间戳计算 MQTT 连接密码。
    算法: HMAC-SHA256(device_secret, timestamp)

    Args:
        device_secret: 华为云 IoTDA 设备密钥
        timestamp: 时间戳，格式 YYYYMMDDHH（如 "2026053114"），
                   留空则自动使用当前 UTC 时间

    Returns:
        64 字符的十六进制 HMAC-SHA256 密码
    """
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    password = hmac.new(
        device_secret.encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return password


def build_client_id(device_id: str, timestamp: str = None) -> str:
    """
    构建 MQTT Client ID。
    格式: {device_id}_0_0_{timestamp}
    """
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return f"{device_id}_0_0_{timestamp}"


def get_effective_config(config: dict) -> dict:
    """
    根据配置中是否提供了 device_secret，决定使用自动计算还是预置密码。
    返回最终的 MQTT 连接参数。

    重要：MQTT 密码 = HMAC-SHA256(device_secret, timestamp)
    所以 timestamp 必须和生成密码时使用的值一致。
    """
    device_secret = config.get("device_secret", "").strip()
    current_ts = datetime.now(timezone.utc).strftime("%Y%m%d%H")

    if device_secret:
        # 模式 B：使用设备密钥自动计算密码（推荐，不会过期）
        print(f"[*] 模式B — 设备密钥自动计算 (时间戳: {current_ts})")
        password = compute_password(device_secret, current_ts)
        client_id = build_client_id(config["device_id"], current_ts)
        print(f"   Client ID: {client_id}")
        print(f"   Password:  {password}")
    else:
        # 模式 A：使用预置密码
        cfg_timestamp = config.get("timestamp", "").strip()
        if not cfg_timestamp:
            # 没有指定时间戳，用当前时间 —— 但预置密码大概率不匹配！
            cfg_timestamp = current_ts
            print(
                f"[!] 警告：未配置 timestamp，使用当前时间 {cfg_timestamp}\n"
                f"    如果预置密码不是用这个时间戳生成的，认证将失败！\n"
                f"    建议：设置 device_secret 使用自动计算模式（不会过期），\n"
                f"    或在 config.json 中填入与密码匹配的 timestamp。"
            )
        else:
            print(f"[*] 模式A — 预置密码 (时间戳: {cfg_timestamp})")
        password = config["password"]
        client_id = build_client_id(config["device_id"], cfg_timestamp)

    return {
        "device_id": config["device_id"],
        "password": password,
        "clientId": client_id,
        "hostname": config["hostname"],
        "port": config["port"],
    }


# ============================================================
# 华为云 IoTDA MQTT Topic 定义
# ============================================================

class Topics:
    """华为云 IoTDA MQTT Topic 的工厂类"""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.base = f"$oc/devices/{device_id}"

    @property
    def shadow_get_req(self) -> str:
        return f"{self.base}/sys/shadow/get/request_id={{request_id}}"

    @property
    def shadow_get_resp(self) -> str:
        return f"{self.base}/sys/shadow/get/response/+"

    @property
    def properties_get_req(self) -> str:
        return f"{self.base}/sys/properties/get/request_id={{request_id}}"

    @property
    def properties_get_resp(self) -> str:
        return f"{self.base}/sys/properties/get/response/+"

    @property
    def properties_report(self) -> str:
        return f"{self.base}/sys/properties/report"

    @property
    def commands(self) -> str:
        return f"{self.base}/sys/commands/#"

    @property
    def messages_down(self) -> str:
        return f"{self.base}/sys/messages/down"

    @property
    def platform_wildcard(self) -> str:
        return f"{self.base}/sys/#"


# ============================================================
# 主客户端类
# ============================================================

class IoTDADeviceClient:
    """华为云 IoTDA MQTT 设备客户端"""

    def __init__(self, config: dict, verbose: bool = True):
        self.config = config
        self.device_id = config["device_id"]
        self.verbose = verbose
        self.topics = Topics(self.device_id)

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config["clientId"],
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(config["device_id"], config["password"])

        # 设置 TLS（MQTTS: 端口 8883）
        self.client.tls_set(
            ca_certs=None,
            certfile=None,
            keyfile=None,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )

        # 存储请求的响应结果
        self._pending_responses: dict = {}
        # 存储所有收到的消息（监控模式用）
        self._all_messages: list = []
        # 事件：有新消息到达时设置
        self._msg_event = Event()
        # 连接事件
        self._connected = Event()

        # 注册回调
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        if verbose:
            self.client.on_log = self._on_log

    # ---------- 回调 ----------

    def _on_log(self, client, userdata, level, buf):
        if level <= mqtt.MQTT_LOG_NOTICE:
            print(f"[MQTT] {buf}")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # VERSION2 回调：reason_code 是 ReasonCode 枚举对象
        rc = reason_code.value if hasattr(reason_code, 'value') else (reason_code or -1)
        rc_messages = {
            0: "连接成功",
            1: "协议版本错误",
            2: "Client ID 被拒绝",
            3: "服务器不可用",
            4: "用户名或密码错误",
            5: "未授权",
        }
        msg = rc_messages.get(rc, f"未知错误 (rc={rc})")
        print(f"[+] 连接结果: {msg}")

        if rc == 0:
            # 订阅所有需要的 topic
            topics_to_sub = [
                (self.topics.shadow_get_resp, "影子响应"),
                (self.topics.properties_get_resp, "属性查询响应"),
                (self.topics.commands, "平台命令"),
                (self.topics.messages_down, "平台消息"),
                (self.topics.platform_wildcard, "平台通配"),
            ]
            for topic, desc in topics_to_sub:
                client.subscribe(topic, qos=1)
                if self.verbose:
                    print(f"   已订阅 [{desc}]: {topic}")

            self._connected.set()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = msg.payload.decode("utf-8", errors="replace")

        if self.verbose:
            print(f"\n[MSG] 收到消息:")
            print(f"   Topic: {msg.topic}")
            print(f"   QoS: {msg.qos}")
            print(
                f"   Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}"
            )

        # 提取 request_id（topic 最后一段格式为 request_id=xxx）
        topic_parts = msg.topic.split("/")
        if len(topic_parts) >= 1:
            raw = topic_parts[-1]
            # 去掉 "request_id=" 前缀，提取纯 UUID
            request_id = raw.replace("request_id=", "", 1) if "=" in raw else raw
            self._pending_responses[request_id] = payload

        # 存储消息
        self._all_messages.append({
            "topic": msg.topic,
            "payload": payload,
            "qos": msg.qos,
            "time": time.time(),
        })

        # 通知有新消息
        self._msg_event.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        rc = reason_code.value if hasattr(reason_code, 'value') else (reason_code or -1)
        print(f"\n[-] 连接断开 (rc={rc})")
        if rc != 0:
            print("   [!] 非预期断开")
        self._connected.clear()

    # ---------- 连接 ----------

    def connect(self, timeout: int = 15):
        """连接到 IoTDA"""
        host = self.config["hostname"]
        port = self.config["port"]
        print(f"[~] 正在连接 {host}:{port} ...")
        print(f"   Client ID: {self.config['clientId']}")
        print(f"   Username:  {self.config['device_id']}")

        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()

        # 等待连接完成
        if not self._connected.wait(timeout):
            raise TimeoutError(
                "MQTT 连接超时，请检查凭据和网络"
            )
        return True

    def disconnect(self):
        """断开连接"""
        self.client.loop_stop()
        self.client.disconnect()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ---------- 设备影子 ----------

    def get_device_shadow(self, timeout: int = 15) -> dict:
        """
        获取设备影子 —— 包含所有服务的 desired/reported 属性。
        请求体为空表示查询所有服务的影子。
        """
        request_id = str(uuid.uuid4())
        topic = self.topics.shadow_get_req.format(request_id=request_id)

        print(f"\n[>>] 发送影子查询请求 (空body, 查询所有服务)...")
        print(f"   Topic: {topic}")
        print(f"   Request ID: {request_id}")

        result = self.client.publish(topic, payload="", qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"   [X] 发布失败: {mqtt.error_string(result.rc)}")
            return {}

        response = self._wait_response(request_id, timeout)
        return response if response else {}

    def query_service_properties(
        self, service_id: str, timeout: int = 15
    ) -> dict:
        """
        查询指定服务的属性（实时查询）。
        请求体中包含 service_id。
        """
        request_id = str(uuid.uuid4())
        topic = self.topics.properties_get_req.format(request_id=request_id)
        payload = json.dumps({"service_id": service_id})

        print(f"\n[>>] 查询服务 [{service_id}] 的属性...")
        print(f"   Topic: {topic}")
        print(f"   Payload: {payload}")

        result = self.client.publish(topic, payload=payload, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"   [X] 发布失败: {mqtt.error_string(result.rc)}")
            return {}

        response = self._wait_response(request_id, timeout)
        return response if response else {}

    # ---------- 属性上报 ----------

    def report_properties(
        self, services: list, event_time: str = None
    ) -> bool:
        """
        上报设备属性数据到 IoTDA 平台。

        Args:
            services: 服务列表，格式如下：
                [
                    {
                        "service_id": "Battery",
                        "properties": {"batteryLevel": 85, "batteryVoltage": 3.7},
                    }
                ]
            event_time: 事件时间，格式 "YYYYMMDDTHHMMSSZ"，留空则自动生成

        Returns:
            是否成功发布

        与参考示例 main0807_huawei.py 中 publish_message() 的模式一致，
        向 $oc/devices/{id}/sys/properties/report 发布 JSON 数据。
        """
        if event_time is None:
            event_time = datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )

        for svc in services:
            svc["event_time"] = event_time

        payload = json.dumps({"services": services})
        topic = self.topics.properties_report

        print(f"\n[>>] 上报属性数据...")
        print(f"   Topic: {topic}")
        print(f"   Payload: {payload}")

        result = self.client.publish(topic, payload=payload, qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"   [X] 发布失败: {mqtt.error_string(result.rc)}")
            return False
        print(f"   [OK] 已发布")
        return True

    def report_property(
        self, service_id: str, properties: dict
    ) -> bool:
        """上报单个服务的属性（便捷方法）"""
        return self.report_properties([
            {"service_id": service_id, "properties": properties}
        ])

    # ---------- 内部方法 ----------

    def _wait_response(self, request_id: str, timeout: int) -> Optional[dict]:
        """轮询等待指定 request_id 的响应"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if request_id in self._pending_responses:
                resp = self._pending_responses.pop(request_id)
                return resp
            time.sleep(0.3)
        print(f"   [!] 等待响应超时 ({timeout}s)")
        return None

    # ---------- 消息获取（监控用） ----------

    def get_pending_messages(self, clear: bool = True) -> list:
        """获取积累的消息列表"""
        msgs = list(self._all_messages)
        if clear:
            self._all_messages.clear()
            self._msg_event.clear()
        return msgs


# ============================================================
# 数据提取工具
# ============================================================

def extract_shadow_properties(shadow_data: dict) -> dict:
    """
    从影子数据中提取所有服务的 reported 属性。
    与参考示例 mqtt_client.py 中的解析模式一致。

    返回: { "service_id": {"prop1": val1, ...}, ... }
    """
    result = {}
    shadow_list = shadow_data.get("shadow", [])
    for svc in shadow_list:
        service_id = svc.get("service_id", "unknown")
        reported = svc.get("reported", {})
        if isinstance(reported, dict):
            props = reported.get("properties", reported)
            result[service_id] = {
                k: v
                for k, v in props.items()
                if k != "event_time"
            }
        desired = svc.get("desired", {})
        if isinstance(desired, dict) and desired:
            d_props = desired.get("properties", desired)
            result[f"{service_id} (desired)"] = {
                k: v
                for k, v in d_props.items()
                if k != "event_time"
            }
    return result


def extract_property_value(
    shadow_data: dict, service_id: str, property_name: str
) -> Optional[float]:
    """
    从影子数据中提取特定服务的特定属性值（数值）。
    与参考示例中提取 batteryVoltage / batteryLevel 的模式一致。
    """
    shadow_list = shadow_data.get("shadow", [])
    for svc in shadow_list:
        if svc.get("service_id") == service_id:
            reported = svc.get("reported", {})
            if isinstance(reported, dict):
                props = reported.get("properties", reported)
                if property_name in props:
                    try:
                        return float(props[property_name])
                    except (ValueError, TypeError):
                        return props[property_name]
    return None


# ============================================================
# 格式化输出
# ============================================================

def print_shadow(shadow_data: dict):
    """美化输出影子数据"""
    print("\n" + "=" * 60)
    print("[=] 设备影子数据 (Device Shadow)")
    print("=" * 60)

    if not shadow_data:
        print("(无数据)")
        return

    device_id = shadow_data.get(
        "object_device_id", shadow_data.get("device_id", "N/A")
    )
    print(f"设备 ID: {device_id}")

    shadow_list = shadow_data.get("shadow", [])
    if not shadow_list:
        print("(无影子数据)")
        return

    for idx, svc in enumerate(shadow_list, 1):
        service_id = svc.get("service_id", f"service_{idx}")
        print(f"\n  [{idx}] [*] 服务: {service_id}")

        desired = svc.get("desired", {})
        reported = svc.get("reported", {})

        if desired:
            print("      ┌─ Desired (期望值) ────────┐")
            desired_props = (
                desired.get("properties", desired)
                if isinstance(desired, dict)
                else {}
            )
            for prop, value in desired_props.items():
                if prop == "event_time":
                    print(f"      │  event_time: {value}")
                else:
                    print(
                        f"      │  {prop}: "
                        f"{json.dumps(value, ensure_ascii=False)}"
                    )

        if reported:
            print("      ┌─ Reported (上报值) ────────┐")
            reported_props = (
                reported.get("properties", reported)
                if isinstance(reported, dict)
                else {}
            )
            for prop, value in reported_props.items():
                if prop == "event_time":
                    print(f"      │  event_time: {value}")
                else:
                    print(
                        f"      │  {prop}: "
                        f"{json.dumps(value, ensure_ascii=False)}"
                    )

        if not desired and not reported:
            print("      (无属性数据)")

        version = svc.get("version")
        if version is not None:
            print(f"      版本: {version}")

    print("=" * 60)


def print_properties(result: dict):
    """美化输出属性查询结果"""
    print("\n" + "=" * 60)
    print("[=] 属性查询响应")
    print("=" * 60)
    response = result.get("response", result)
    service_id = response.get("service_id", "unknown")
    properties = response.get("properties", response.get("paras", {}))
    print(f"服务: {service_id}")
    if properties:
        for k, v in properties.items():
            print(f"  • {k}: {json.dumps(v, ensure_ascii=False)}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    print("=" * 60)


# ============================================================
# 模拟传感器数据生成器
# ============================================================

def generate_sensor_data(service_configs: list) -> list:
    """
    生成模拟传感器数据，用于测试上报。
    与参考示例 main080703_huawei.py 中随机生成温湿度数据的模式一致。

    Args:
        service_configs: 服务配置列表，每项为 (service_id, {prop_name: (type, min, max)})
                         例如: [("Battery", {"batteryLevel": ("int", 0, 100)})]
    """
    services = []
    for svc_id, prop_configs in service_configs:
        properties = {}
        for prop_name, (p_type, p_min, p_max) in prop_configs.items():
            if p_type in ("int", "integer"):
                properties[prop_name] = random.randint(p_min, p_max)
            elif p_type in ("float", "double"):
                properties[prop_name] = round(
                    random.uniform(p_min, p_max), 2
                )
            elif p_type == "gps":
                properties[prop_name] = {
                    "lon": round(random.uniform(p_min, p_max), 6),
                    "lat": round(random.uniform(p_min, p_max), 6),
                }
            elif p_type == "bool":
                properties[prop_name] = random.choice([0, 1])
        services.append({
            "service_id": svc_id,
            "properties": properties,
        })
    return services


# ============================================================
# 持续监控 + matplotlib 实时绘图
# ============================================================

def check_matplotlib() -> bool:
    """检查 matplotlib 是否可用"""
    try:
        __import__("matplotlib")
        return True
    except ImportError:
        return False


def run_monitor_mode(
    client: IoTDADeviceClient,
    target_services: list = None,
    interval: int = 3,
    max_points: int = 100,
):
    """
    持续监控模式 —— 周期性查询设备影子并使用 matplotlib 实时绘图。

    target_services: [(service_id, property_name), ...]
    与参考示例 mqtt_client.py 的实现模式一致。
    """
    if not check_matplotlib():
        print("❌ 请先安装 matplotlib: pip install matplotlib")
        sys.exit(1)

    import matplotlib
    # 使用支持中文的字体，避免中文字符显示为方块
    matplotlib.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
    matplotlib.rcParams['axes.unicode_minus'] = False
    import matplotlib.pyplot as plt
    from datetime import datetime as dt

    # 自动探测服务
    if target_services is None:
        print("[*] 自动探测设备服务模型...")
        shadow = client.get_device_shadow(timeout=15)
        target_services = []
        for svc in shadow.get("shadow", []):
            svc_id = svc.get("service_id", "")
            reported = svc.get("reported", {})
            if isinstance(reported, dict):
                props = reported.get("properties", reported)
                for prop_name in props:
                    if prop_name != "event_time":
                        target_services.append((svc_id, prop_name))

    if not target_services:
        print("❌ 未找到可监控的属性。请确认设备已上报过数据。")
        return

    # 打印待监控的属性
    print(f"\n[*] 将监控以下属性:")
    for svc_id, prop_name in target_services:
        print(f"    - {svc_id}.{prop_name}")

    # 初始化 matplotlib
    plt.ion()
    fig = plt.figure("IoTDA Device Monitor (MQTT)", figsize=(12, 8))

    # 数据存储
    ax_time = []
    ay_data = {}
    for svc_id, prop_name in target_services:
        key = f"{svc_id}.{prop_name}"
        ay_data[key] = []

    print(f"\n🔍 开始持续监控（间隔 {interval}s）...")
    print("   按 Ctrl+C 停止。\n")

    i = 0
    try:
        while True:
            try:
                shadow = client.get_device_shadow(timeout=10)
                now = dt.now().strftime("%H:%M:%S")
                ax_time.append(now)

                for svc_id, prop_name in target_services:
                    key = f"{svc_id}.{prop_name}"
                    val = extract_property_value(shadow, svc_id, prop_name)
                    ay_data[key].append(
                        val if val is not None else float("nan")
                    )

                # 限制历史数据点
                if len(ax_time) > max_points:
                    ax_time.pop(0)
                    for key in ay_data:
                        ay_data[key].pop(0)

                # 绘图
                fig.clf()
                n_plots = len(target_services)
                for idx, (svc_id, prop_name) in enumerate(target_services):
                    key = f"{svc_id}.{prop_name}"
                    ax = fig.add_subplot(n_plots, 1, idx + 1)
                    valid = [
                        v
                        for v in ay_data[key]
                        if v is not None and v == v
                    ]
                    label = f"{svc_id}.{prop_name}"
                    if valid:
                        avg = sum(valid) / len(valid)
                        label += f" (avg: {avg:.2f})"

                    ax.plot(
                        range(len(ay_data[key])),
                        ay_data[key],
                        marker="o" if len(ay_data[key]) < 30 else "",
                        markersize=3,
                        linewidth=1.5,
                        label=label,
                    )
                    ax.set_ylabel(prop_name)
                    ax.set_xlabel("采集次数")
                    ax.legend(loc="upper right", fontsize=8)
                    ax.grid(True, alpha=0.3)

                    if valid:
                        y_min, y_max = min(valid), max(valid)
                        margin = max((y_max - y_min) * 0.2, 1)
                        ax.set_ylim(y_min - margin, y_max + margin)

                fig.suptitle(
                    f"设备 {client.device_id} 属性监控 (MQTT)",
                    fontsize=12,
                    fontweight="bold",
                )
                fig.tight_layout()
                plt.pause(0.1)
                plt.ioff()

                i += 1
                if i % 10 == 0:
                    vals_str = ", ".join(
                        f"{k}={ay_data[k][-1]}" for k in ay_data
                    )
                    print(f"  [{now}] 已采集 {i} 次 | {vals_str}")

            except Exception as e:
                print(f"  [!] 查询失败: {e}，{interval}s 后重试...")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n\n[−] 监控已停止。")
    finally:
        plt.close("all")


# ============================================================
# 数据上报循环
# ============================================================

def run_publish_mode(
    client: IoTDADeviceClient,
    service_configs: list,
    interval: int = 10,
):
    """
    定时上报模拟传感器数据。
    与参考示例 main0807_huawei.py 中定时发布消息的模式一致。
    """
    print(f"[*] 开始定时上报数据（间隔 {interval}s）...")
    print("   按 Ctrl+C 停止。\n")

    count = 0
    try:
        while True:
            count += 1
            services = generate_sensor_data(service_configs)
            success = client.report_properties(services)
            if success:
                print(f"  [{count}] 数据上报成功")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[−] 数据上报停止。共上报 {count} 次。")


# ============================================================
# 命令行参数
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="华为云 IoTDA MQTT 设备查询与管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python mqtt_query_shadow.py                       单次查询设备影子
  python mqtt_query_shadow.py --monitor              持续监控 + 实时绘图
  python mqtt_query_shadow.py --publish              模拟传感器数据上报
  python mqtt_query_shadow.py --service Battery      查询指定服务属性
  python mqtt_query_shadow.py --config my_config.json 使用指定配置文件
  python mqtt_query_shadow.py --service Battery.batteryLevel --monitor  监控特定属性
  python mqtt_query_shadow.py --publish --interval 5  每5秒上报一次数据
        """,
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="配置文件路径（JSON 格式）",
    )
    parser.add_argument(
        "--service", "-s",
        default=None,
        help=(
            "服务ID[.属性名]。"
            "例如: 'Battery' 查询 Battery 服务; "
            "'Battery.batteryLevel' 只监控特定属性"
        ),
    )
    parser.add_argument(
        "--monitor", "-m",
        action="store_true",
        help="持续监控模式，循环查询并实时绘图",
    )
    parser.add_argument(
        "--publish", "-p",
        action="store_true",
        help="定时上报模拟传感器数据",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=3,
        help="监控/上报间隔（秒），默认 3（监控）或 10（上报）",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=100,
        help="图表最多显示的数据点数，默认 100",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="安静模式，减少输出",
    )

    return parser.parse_args()


# ============================================================
# 主程序
# ============================================================

# 默认模拟传感器配置（与参考示例 main0807_huawei.py 中的数据结构一致）
DEFAULT_SENSOR_CONFIGS = [
    (
        "stm32",
        {
            "MQ135": ("int", 30, 100),
            "DHT11_T": ("float", 10, 35),
            "DHT11_H": ("float", 30, 90),
            "SOIL_H": ("int", 20, 80),
            "motor": ("int", 0, 1),
            "FLAME": ("int", 0, 1),
        },
    ),
    (
        "Battery",
        {
            "batteryLevel": ("int", 0, 100),
            "batteryVoltage": ("float", 3.0, 4.2),
        },
    ),
]


def main():
    args = parse_args()

    # 加载配置
    mqtt_config_raw = load_config(args.config)

    # 验证必要配置
    if not args.publish:  # 查询模式需要 hostname
        if not mqtt_config_raw.get("hostname"):
            print("❌ 未配置 MQTT hostname")
            print("   请在 config.json 中设置 hostname 或设置环境变量 IOT_MQTT_HOST")
            sys.exit(1)

    print("=" * 56)
    print("  华为云 IoTDA — MQTT 设备查询与管理")
    print("=" * 56)
    print()

    effective_config = get_effective_config(mqtt_config_raw)
    client = IoTDADeviceClient(
        effective_config, verbose=not args.quiet
    )

    try:
        # 连接 IoTDA
        client.connect(timeout=15)

        # ---- 数据上报模式 ----
        if args.publish:
            run_publish_mode(
                client,
                DEFAULT_SENSOR_CONFIGS,
                interval=max(args.interval, 5),
            )
            return

        # ---- 解析 service 参数 ----
        monitor_services = None
        if args.service:
            parts = args.service.split(".")
            if len(parts) == 2:
                monitor_services = [(parts[0], parts[1])]
            else:
                monitor_services = [(parts[0], None)]

        # ---- 监控模式 ----
        if args.monitor:
            if (
                monitor_services
                and monitor_services[0][1] is None
            ):
                # 只指定了 service_id，需要探测属性
                svc_id = monitor_services[0][0]
                print(f"[*] 自动探测服务 '{svc_id}' 的属性...")
                shadow = client.get_device_shadow(timeout=15)
                found = []
                for svc in shadow.get("shadow", []):
                    if svc.get("service_id") == svc_id:
                        reported = svc.get("reported", {})
                        props = (
                            reported.get("properties", reported)
                            if isinstance(reported, dict)
                            else {}
                        )
                        for pn in props:
                            if pn != "event_time":
                                found.append((svc_id, pn))
                if not found:
                    print(
                        f"❌ 服务 '{svc_id}' 中未找到属性。"
                    )
                    sys.exit(1)
                monitor_services = found

            run_monitor_mode(
                client,
                monitor_services,
                interval=args.interval,
                max_points=args.max_points,
            )
            return

        # ---- 等待观察平台消息 ----
        print("\n[*] 等待 5 秒，观察平台是否有主动下发...")
        time.sleep(5)

        # ---- 单次查询模式 ----
        # 尝试多种查询方式
        target_service = (
            args.service.split(".")[0] if args.service else "Battery"
        )
        target_property = (
            args.service.split(".")[1]
            if args.service and "." in args.service
            else "batteryLevel"
        )

        queries = [
            (
                "影子查询 (空body)",
                lambda: client.get_device_shadow(timeout=10),
            ),
            (
                f"属性查询 service_id='{target_service}'",
                lambda: client.query_service_properties(
                    target_service, timeout=10
                ),
            ),
            (
                "属性查询 service_id=''",
                lambda: client.query_service_properties("", timeout=10),
            ),
        ]

        all_results = {}
        for label, query_fn in queries:
            print(f"\n[>>] {label} ...")
            try:
                result = query_fn()
                if result:
                    all_results[label] = result
                    print(f"     [OK] 收到响应!")
            except Exception as e:
                print(f"     [X] {e}")

        # 输出结果
        print("\n" + "=" * 56)
        print("  查询结果汇总")
        print("=" * 56)

        if all_results:
            print(f"  共收到 {len(all_results)} 个响应\n")
            for label, result in all_results.items():
                print(f"  --- {label} ---")
                print(json.dumps(result, indent=2, ensure_ascii=False))
                print()

                # 尝试提取目标属性值
                shadow_list = result.get("shadow", [])
                for svc in shadow_list:
                    if svc.get("service_id") == target_service:
                        reported = svc.get("reported", {})
                        if isinstance(reported, dict):
                            props = reported.get("properties", reported)
                            if target_property in props:
                                print(
                                    f">>> {target_service}.{target_property}"
                                    f" = {props[target_property]}"
                                )

                response = result.get("response", {})
                props = response.get(
                    "properties", response.get("paras", {})
                )
                if props and target_property in props:
                    print(
                        f">>> {target_service}.{target_property}"
                        f" = {props[target_property]}"
                    )
        else:
            print("  [!] 所有查询均无响应")
            print()
            print("  可能原因:")
            print("  1. 设备物模型中未定义对应服务")
            print("  2. 设备从未上报过属性数据")
            print(
                "  3. 请在华为云 IoTDA 控制台确认该设备的「物模型」中有哪些服务"
            )
            print("  4. 尝试使用 --publish 先上报一些测试数据")

        # 等待收尾
        print("\n[*] 等待 3 秒收尾...")
        time.sleep(3)

    except TimeoutError as e:
        print(f"\n[X] {e}")
        sys.exit(1)
    except ssl.SSLError as e:
        print(f"\n[X] TLS 证书错误: {e}")
        print(
            "   提示: 可能需要设置 tls_insecure_set(True) 或导入华为云证书"
        )
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"\n[X] 连接被拒绝，请检查 hostname/port 是否正确")
        sys.exit(1)
    except Exception as e:
        print(f"\n[X] 发生错误: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        client.disconnect()
        print("[-] 已断开连接")


if __name__ == "__main__":
    main()
