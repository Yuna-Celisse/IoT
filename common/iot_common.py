#!/usr/bin/env python3
"""
IoT 公共模块 — 华为云 IoTDA
============================
提供所有脚本共享的配置加载、密码计算、Topic 定义等功能。
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone


# ============================================================
# 配置加载
# ============================================================

def load_config(config_path: str = None) -> dict:
    """
    加载配置。优先级：环境变量 > config.json > 默认值。

    config.json 应包含：
      - device_id:     设备 ID
      - password:      预计算密码（模式 A）
      - device_secret: 设备密钥（模式 B，HMAC 自动计算密码）
      - hostname:      MQTT 主机地址
      - port:          MQTT 端口（1883=无加密, 8883=TLS）
      - timestamp:     密码对应的时间戳（模式 A 需要）
    """
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
    # common/iot_common.py → 项目根目录
    _proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    search_paths.append(os.path.join(_proj_root, "config", "config.json"))
    search_paths.append(os.path.join(_proj_root, "config.json"))
    search_paths.append("config.json")  # CWD 兜底

    for path in search_paths:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    file_config = json.load(f)
                    for key in config:
                        if key in file_config and file_config[key]:
                            config[key] = file_config[key]
                    break
            except (json.JSONDecodeError, IOError):
                pass

    return config


# ============================================================
# MQTT 认证
# ============================================================

def compute_password(device_secret: str, timestamp: str = None) -> str:
    """HMAC-SHA256 计算 MQTT 连接密码
    Password = HMAC-SHA256(key=timestamp, msg=device_secret)
    参考华为云文档：以secret为内容，时间戳为密钥
    """
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return hmac.new(
        timestamp.encode("utf-8"),       # key = 时间戳
        device_secret.encode("utf-8"),   # msg = secret
        hashlib.sha256,
    ).hexdigest()


def build_client_id(device_id: str, timestamp: str = None, suffix: int = 0) -> str:
    """构建 MQTT Client ID：{device_id}_0_{suffix}_{timestamp}"""
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return f"{device_id}_0_{suffix}_{timestamp}"


def get_effective_mqtt_params(config: dict) -> dict:
    """
    根据配置决定使用模式 A（预置密码）还是模式 B（HMAC 自动计算）。
    返回 {device_id, password, clientId, hostname, port}
    """
    device_secret = config.get("device_secret", "").strip()
    current_ts = datetime.now(timezone.utc).strftime("%Y%m%d%H")

    if device_secret:
        password = compute_password(device_secret, current_ts)
        client_id = build_client_id(config["device_id"], current_ts)
    else:
        cfg_ts = config.get("timestamp", "").strip() or current_ts
        password = config["password"]
        client_id = build_client_id(config["device_id"], cfg_ts)

    return {
        "device_id": config["device_id"],
        "password": password,
        "clientId": client_id,
        "hostname": config["hostname"],
        "port": config["port"],
    }


# ============================================================
# Topic 定义
# ============================================================

class IoTDAtopics:
    """华为云 IoTDA MQTT Topic 工厂"""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.base = f"$oc/devices/{device_id}"

    def shadow_get_req(self, request_id: str) -> str:
        return f"{self.base}/sys/shadow/get/request_id={request_id}"

    @property
    def shadow_get_resp_sub(self) -> str:
        return f"{self.base}/sys/shadow/get/response/+"

    def properties_get_req(self, request_id: str) -> str:
        return f"{self.base}/sys/properties/get/request_id={request_id}"

    @property
    def properties_get_resp_sub(self) -> str:
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
# 影子数据提取
# ============================================================

def extract_shadow_properties(shadow: dict) -> dict:
    """从设备影子中提取所有服务的 reported 属性"""
    result = {}
    for svc in shadow.get("shadow", []):
        svc_id = svc.get("service_id", "unknown")
        reported = svc.get("reported", {})
        if isinstance(reported, dict):
            props = reported.get("properties", reported)
            result[svc_id] = {
                k: v for k, v in props.items() if k != "event_time"
            }
    return result


def extract_property_value(shadow: dict, service_id: str, property_name: str):
    """从影子中提取特定服务的特定属性值"""
    for svc in shadow.get("shadow", []):
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
