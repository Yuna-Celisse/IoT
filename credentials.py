#!/usr/bin/env python3
"""
华为云 IoTDA 认证凭据管理模块
==============================
提供统一的凭据获取接口，支持：
  - REST API 方式：AK/SK（访问密钥/秘密访问密钥）
  - MQTT 设备方式：设备密钥（device_secret）

凭据优先级：
  1. 环境变量
  2. 当前目录下的 credentials.json 文件
  3. 代码中的默认值（仅供开发测试）

安全提示：
  请勿将真实的 AK/SK 硬编码在代码中或提交到版本控制系统。
  推荐使用环境变量或 credentials.json（已加入 .gitignore）。
"""

import json
import os
import sys


# ============================================================
# 凭据文件路径
# ============================================================

_CREDENTIALS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "credentials.json"
)


# ============================================================
# 凭据加载
# ============================================================

def _load_credentials_file(path: str = None) -> dict:
    """从 JSON 文件加载凭据"""
    if path is None:
        path = _CREDENTIALS_FILE
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[!] 凭据文件读取失败 ({path}): {e}", file=sys.stderr)
        return {}


def get_ak() -> str:
    """
    获取华为云访问密钥（Access Key）。
    优先级: 环境变量 HUAWEI_AK > credentials.json > 默认值
    """
    env_val = os.environ.get("HUAWEI_AK", "")
    if env_val:
        return env_val

    creds = _load_credentials_file()
    file_val = creds.get("ak", "")
    if file_val:
        return file_val

    # 默认值（仅供开发测试 —— 请勿在此处填写真实 AK）
    return ""


def get_sk() -> str:
    """
    获取华为云秘密访问密钥（Secret Key）。
    优先级: 环境变量 HUAWEI_SK > credentials.json > 默认值
    """
    env_val = os.environ.get("HUAWEI_SK", "")
    if env_val:
        return env_val

    creds = _load_credentials_file()
    file_val = creds.get("sk", "")
    if file_val:
        return file_val

    # 默认值（仅供开发测试 —— 请勿在此处填写真实 SK）
    return ""


def get_project_id() -> str:
    """
    获取华为云项目 ID。
    优先级: 环境变量 HUAWEI_PROJECT_ID > credentials.json > 默认值
    """
    env_val = os.environ.get("HUAWEI_PROJECT_ID", "")
    if env_val:
        return env_val

    creds = _load_credentials_file()
    file_val = creds.get("project_id", "")
    if file_val:
        return file_val

    return ""


def get_region() -> str:
    """
    获取 IoTDA 服务区域。
    优先级: 环境变量 HUAWEI_REGION > credentials.json > 默认值
    """
    env_val = os.environ.get("HUAWEI_REGION", "")
    if env_val:
        return env_val

    creds = _load_credentials_file()
    file_val = creds.get("region", "")
    if file_val:
        return file_val

    return "cn-east-3"


def get_device_secret() -> str:
    """
    获取 MQTT 设备密钥（device_secret）。
    优先级: 环境变量 IOT_DEVICE_SECRET > credentials.json > 空字符串
    """
    env_val = os.environ.get("IOT_DEVICE_SECRET", "")
    if env_val:
        return env_val

    creds = _load_credentials_file()
    return creds.get("device_secret", "")


def get_device_id() -> str:
    """
    获取默认设备 ID。
    优先级: 环境变量 HUAWEI_DEVICE_ID > credentials.json > 默认值
    """
    env_val = os.environ.get("HUAWEI_DEVICE_ID", "")
    if env_val:
        return env_val

    creds = _load_credentials_file()
    file_val = creds.get("device_id", "")
    if file_val:
        return file_val

    return ""


def get_mqtt_config() -> dict:
    """
    获取完整的 MQTT 连接配置。
    从环境变量或 credentials.json 读取。
    """
    creds = _load_credentials_file()

    return {
        "device_id": get_device_id(),
        "password": os.environ.get(
            "IOT_PASSWORD", creds.get("password", "")
        ),
        "device_secret": get_device_secret(),
        "hostname": os.environ.get(
            "IOT_MQTT_HOST",
            creds.get(
                "hostname",
                "",
            ),
        ),
        "port": int(
            os.environ.get("IOT_MQTT_PORT", creds.get("port", 8883))
        ),
        "timestamp": os.environ.get(
            "IOT_TIMESTAMP", creds.get("timestamp", "")
        ),
    }


def get_rest_api_config() -> dict:
    """
    获取完整的 REST API 配置。
    从环境变量或 credentials.json 读取。
    """
    return {
        "ak": get_ak(),
        "sk": get_sk(),
        "project_id": get_project_id(),
        "region": get_region(),
        "endpoint": os.environ.get(
            "HUAWEI_IOTDA_ENDPOINT",
            _load_credentials_file().get("endpoint", ""),
        ),
        "device_id": get_device_id(),
    }


def print_credentials_info():
    """打印当前凭据状态（隐藏敏感信息）"""
    import sys

    def mask(s: str, show: int = 6) -> str:
        if len(s) <= show:
            return "*" * len(s)
        return s[:show] + "*" * (len(s) - show)

    ak = get_ak()
    sk = get_sk()
    ds = get_device_secret()

    print("当前凭据状态：")
    print(f"  AK:              {mask(ak) if ak else '(未设置)'}")
    print(f"  SK:              {mask(sk) if sk else '(未设置)'}")
    print(f"  Project ID:      {get_project_id()}")
    print(f"  Region:          {get_region()}")
    print(f"  Device ID:       {get_device_id()}")
    print(
        f"  Device Secret:   "
        f"{mask(ds) if ds else '(未设置，使用预置密码)'}"
    )

    # 检查凭据来源
    sources = []
    if os.environ.get("HUAWEI_AK"):
        sources.append("环境变量")
    if os.path.isfile(_CREDENTIALS_FILE):
        sources.append(f"credentials.json ({_CREDENTIALS_FILE})")
    if not sources:
        sources.append("代码默认值（建议配置环境变量或 credentials.json）")
    print(f"  凭据来源: {', '.join(sources)}")


if __name__ == "__main__":
    print_credentials_info()
