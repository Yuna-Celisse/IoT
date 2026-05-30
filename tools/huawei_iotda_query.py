#!/usr/bin/env python3
"""
华为云 IoTDA 设备属性查询与管理工具
========================================
通过华为云 IoTDA REST API 实现以下功能：
  1. 查询设备影子（Device Shadow）—— 所有服务的 desired / reported 属性
  2. 查询设备列表
  3. 持续监控模式 + matplotlib 实时数据可视化
  4. 属性数据提取与时间序列绘图

使用方式：
    python huawei_iotda_query.py                # 单次查询设备影子
    python huawei_iotda_query.py --monitor      # 持续监控 + 实时绘图
    python huawei_iotda_query.py --list-devices # 列出所有设备
    python huawei_iotda_query.py --service Battery  # 查询指定服务属性

前置条件：
    pip install requests matplotlib
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlparse

import requests


# ============================================================
# 配置区域 —— 请替换为你自己的华为云 IoTDA 信息
# 推荐通过环境变量设置，避免将密钥硬编码在代码中
# ============================================================
CONFIG = {
    # 华为云访问密钥（Access Key）—— 通过环境变量 HUAWEI_AK 设置
    "ak": os.environ.get("HUAWEI_AK", ""),
    # 华为云秘密访问密钥（Secret Access Key）—— 通过环境变量 HUAWEI_SK 设置
    "sk": os.environ.get("HUAWEI_SK", ""),
    # 项目 ID（在华为云控制台 "我的凭证" 中查看）—— 通过环境变量 HUAWEI_PROJECT_ID 设置
    "project_id": os.environ.get("HUAWEI_PROJECT_ID", ""),
    # IoTDA 服务区域（例如：cn-north-4, cn-east-3, cn-south-1）
    "region": os.environ.get("HUAWEI_REGION", "cn-east-3"),
    # IoTDA 终端节点（请在环境变量 HUAWEI_IOTDA_ENDPOINT 中设置标准版/企业版端点）
    "endpoint": os.environ.get("HUAWEI_IOTDA_ENDPOINT", ""),
    # 要查询的设备 ID —— 通过环境变量 HUAWEI_DEVICE_ID 设置
    "device_id": os.environ.get("HUAWEI_DEVICE_ID", ""),
}

# 监控间隔（秒）
MONITOR_INTERVAL = 3

# 图表显示的历史数据点数量上限
MAX_PLOT_POINTS = 100


def get_endpoint() -> str:
    """获取 IoTDA 服务终端节点"""
    if CONFIG["endpoint"]:
        return CONFIG["endpoint"]
    return f"iotda.{CONFIG['region']}.myhuaweicloud.com"


def hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sha256_hex(msg: bytes) -> str:
    return hashlib.sha256(msg).hexdigest()


def build_signing_key(sk: str, region: str, service: str, date_stamp: str) -> bytes:
    """
    构建华为云签名密钥（SDK-HMAC-SHA256）
    参考：华为云 API 签名指南
    """
    k_date = hmac_sha256(("SDK" + sk).encode("utf-8"), date_stamp)
    k_region = hmac_sha256(k_date, region)
    k_service = hmac_sha256(k_region, service)
    k_signing = hmac_sha256(k_service, "sdk_request")
    return k_signing


def sign_request(
    method: str,
    url: str,
    headers: dict,
    body: str,
    ak: str,
    sk: str,
    region: str,
    service: str,
) -> dict:
    """
    使用 AK/SK 对请求进行 SDK-HMAC-SHA256 签名，返回带 Authorization 头的 headers。
    """
    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = parsed.path or "/"

    # 如果有 query string，按 key 排序
    query_string = parsed.query
    if query_string:
        pairs = sorted(
            q.split("=", 1) for q in query_string.split("&")
        )
        canonical_query = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs
        )
    else:
        canonical_query = ""

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    # 内容哈希（空 body 也要计算）
    payload_hash = sha256_hex(body.encode("utf-8"))

    # 构造 Canonical Headers
    signed_headers_list = ["content-type", "host", "x-sdk-date"]
    headers_to_sign = {
        "content-type": headers.get("Content-Type", "application/json"),
        "host": host,
        "x-sdk-date": timestamp,
    }

    # 合并用户自定义 headers（以 x- 开头的）
    for k, v in headers.items():
        low = k.lower()
        if low not in signed_headers_list and low.startswith("x-"):
            signed_headers_list.append(low)
            headers_to_sign[low] = v

    signed_headers_list.sort()
    canonical_headers = "".join(
        f"{k}:{headers_to_sign[k].strip()}\n" for k in signed_headers_list
    )
    signed_headers_str = ";".join(signed_headers_list)

    # 构造 Canonical Request
    canonical_request = "\n".join([
        method.upper(),
        canonical_uri,
        canonical_query,
        canonical_headers,
        signed_headers_str,
        payload_hash,
    ])

    # 构造 String to Sign
    algorithm = "SDK-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/sdk_request"
    string_to_sign = "\n".join([
        algorithm,
        timestamp,
        credential_scope,
        sha256_hex(canonical_request.encode("utf-8")),
    ])

    # 计算签名
    signing_key = build_signing_key(sk, region, service, date_stamp)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    # Authorization 头
    authorization = (
        f"{algorithm} Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers_str}, Signature={signature}"
    )

    # 组装最终 headers
    signed_headers = dict(headers)
    signed_headers["Host"] = host
    signed_headers["X-Sdk-Date"] = timestamp
    signed_headers["Authorization"] = authorization
    if body:
        signed_headers["Content-Length"] = str(len(body.encode("utf-8")))

    return signed_headers


# ============================================================
# IoTDA API 操作
# ============================================================

class HuaweiIoTDA:
    """华为云 IoTDA 客户端（REST API 方式）"""

    def __init__(self, config: dict):
        self.ak = config["ak"]
        self.sk = config["sk"]
        self.project_id = config["project_id"]
        self.region = config["region"]
        self.endpoint = get_endpoint()
        self.service = "iotda"

    def _request(
        self, method: str, path: str, query: str = "", body: dict = None
    ) -> dict:
        """发送签名后的请求到 IoTDA"""
        body_str = json.dumps(body) if body else ""
        url = f"https://{self.endpoint}{path}"
        if query:
            url += f"?{query}"

        headers = {
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json",
        }

        signed_headers = sign_request(
            method=method,
            url=url,
            headers=headers,
            body=body_str,
            ak=self.ak,
            sk=self.sk,
            region=self.region,
            service=self.service,
        )

        response = requests.request(
            method=method,
            url=url,
            headers=signed_headers,
            data=body_str.encode("utf-8") if body_str else None,
            timeout=30,
        )

        if response.status_code >= 400:
            print(
                f"[错误] HTTP {response.status_code}: {response.text}",
                file=sys.stderr,
            )
        response.raise_for_status()
        return response.json()

    # ---- 设备影子（Device Shadow）相关 ----

    def get_device_shadow(self, device_id: str) -> dict:
        """
        查询设备影子数据 —— 包含所有服务的 desired / reported 属性。
        GET /v5/iot/{project_id}/devices/{device_id}/shadow
        """
        path = f"/v5/iot/{self.project_id}/devices/{device_id}/shadow"
        return self._request("GET", path)

    def update_device_shadow(
        self, device_id: str, shadow_data: list
    ) -> dict:
        """
        更新设备影子（应用侧修改 desired 属性）。
        PUT /v5/iot/{project_id}/devices/{device_id}/shadow
        """
        path = f"/v5/iot/{self.project_id}/devices/{device_id}/shadow"
        body = {"shadow": shadow_data}
        return self._request("PUT", path, body=body)

    # ---- 设备属性查询 ----

    def query_device_properties(
        self, device_id: str, service_id: str
    ) -> dict:
        """
        查询设备的某个服务的属性（实时查询）。
        GET /v5/iot/{project_id}/devices/{device_id}/properties?service_id={service_id}
        """
        path = f"/v5/iot/{self.project_id}/devices/{device_id}/properties"
        query = f"service_id={service_id}"
        return self._request("GET", path, query=query)

    # ---- 设备列表 ----

    def list_devices(self, limit: int = 10, offset: int = 0) -> dict:
        """
        查询设备列表。
        GET /v5/iot/{project_id}/devices
        """
        path = f"/v5/iot/{self.project_id}/devices"
        query = f"limit={limit}&offset={offset}"
        return self._request("GET", path, query=query)

    # ---- 设备详情 ----

    def get_device_detail(self, device_id: str) -> dict:
        """
        查询单个设备详情。
        GET /v5/iot/{project_id}/devices/{device_id}
        """
        path = f"/v5/iot/{self.project_id}/devices/{device_id}"
        return self._request("GET", path)

    # ---- 产品（Product）相关 ----

    def list_products(self, limit: int = 10, offset: int = 0) -> dict:
        """
        查询产品列表。
        GET /v5/iot/{project_id}/products
        """
        path = f"/v5/iot/{self.project_id}/products"
        query = f"limit={limit}&offset={offset}"
        return self._request("GET", path, query=query)


# ============================================================
# 数据提取工具
# ============================================================

def extract_shadow_properties(
    shadow: dict,
) -> dict:
    """
    从设备影子数据中提取所有服务的 reported 属性。
    返回格式: { "service_id": {"prop1": val1, "prop2": val2}, ... }

    参考示例中解析 shadow[0]["reported"]["properties"] 的模式。
    """
    result = {}
    shadow_list = shadow.get("shadow", [])
    for svc in shadow_list:
        service_id = svc.get("service_id", "unknown")
        reported = svc.get("reported", {})

        # reported 可能是 {"properties": {...}, "event_time": "..."}
        # 也可能是直接的属性字典
        if isinstance(reported, dict):
            props = reported.get("properties", None)
            if props is None:
                # 直接是属性值
                props = {
                    k: v for k, v in reported.items()
                    if k != "event_time"
                }
            result[service_id] = props
        desired = svc.get("desired", {})
        if isinstance(desired, dict) and desired:
            desired_props = desired.get("properties", desired)
            key = f"{service_id} (desired)"
            result[key] = {
                k: v for k, v in desired_props.items()
                if k != "event_time"
            }

    return result


def extract_property_value(
    shadow: dict, service_id: str, property_name: str
) -> Optional[float]:
    """
    从影子数据中提取指定服务的指定属性值（数值类型），用于绘图。
    与参考示例中提取 batteryVoltage / batteryLevel 的模式一致。
    """
    shadow_list = shadow.get("shadow", [])
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

def print_device_shadow(shadow: dict):
    """美化输出设备影子数据"""
    print("\n" + "=" * 60)
    print("📦 设备影子数据 (Device Shadow)")
    print("=" * 60)

    device_id = shadow.get("device_id", "N/A")
    print(f"设备 ID: {device_id}")

    shadow_list = shadow.get("shadow", [])
    if not shadow_list:
        print("(无影子数据)")
        return

    for idx, svc in enumerate(shadow_list, 1):
        service_id = svc.get("service_id", "unknown")
        print(f"\n  [{idx}] 服务: {service_id}")

        desired = svc.get("desired", {})
        reported = svc.get("reported", {})

        if desired:
            print("      ┌─ Desired (期望值) ─┐")
            desired_props = (
                desired.get("properties", desired)
                if isinstance(desired, dict) else {}
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
            print("      ┌─ Reported (上报值) ─┐")
            reported_props = (
                reported.get("properties", reported)
                if isinstance(reported, dict) else {}
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


def print_device_properties(result: dict):
    """美化输出设备属性查询结果"""
    print("\n" + "=" * 60)
    print("📋 设备属性查询结果")
    print("=" * 60)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("=" * 60)


def print_devices(devices: dict):
    """美化输出设备列表"""
    print("\n" + "=" * 60)
    print("📱 设备列表")
    print("=" * 60)
    device_list = devices.get("devices", [])
    if not device_list:
        print("(无设备)")
        return

    for d in device_list:
        device_id = d.get("device_id", "N/A")
        name = d.get("device_name", "N/A")
        status = d.get("status", "N/A")
        print(f"  • {name} ({device_id}) — 状态: {status}")

    count = devices.get("page", {}).get("count", len(device_list))
    print(f"\n  共 {count} 个设备")
    print("=" * 60)


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
    client: HuaweiIoTDA,
    device_id: str,
    services: list = None,
):
    """
    持续监控模式 —— 周期性查询设备影子并使用 matplotlib 实时绘图。

    与参考示例 mqtt_client.py / main0726.py 的实现模式一致：
      - plt.ion() 开启交互模式
      - 维护 ax, ay_* 列表存储历史数据
      - while True 循环中轮询、提取数据、绘图
      - plt.pause() 刷新图表
    """
    if not check_matplotlib():
        print("❌ 请先安装 matplotlib: pip install matplotlib")
        sys.exit(1)

    import matplotlib
    matplotlib.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
    matplotlib.rcParams['axes.unicode_minus'] = False
    import matplotlib.pyplot as plt
    from datetime import datetime as dt

    # 自动发现服务及其属性
    if services is None:
        print("[*] 自动探测设备服务模型...")
        try:
            shadow = client.get_device_shadow(device_id)
            services = []
            for svc in shadow.get("shadow", []):
                svc_id = svc.get("service_id", "")
                reported = svc.get("reported", {})
                if isinstance(reported, dict):
                    props = reported.get("properties", reported)
                    for prop_name in props:
                        if prop_name != "event_time":
                            services.append((svc_id, prop_name))
        except Exception as e:
            print(f"[!] 自动探测失败: {e}")
            print("   请使用 --service 参数手动指定服务和属性")
            sys.exit(1)

    if not services:
        print("❌ 未找到可监控的属性。请确认设备已上报过数据。")
        sys.exit(1)

    print(f"\n[*] 将监控以下属性:")
    for svc_id, prop_name in services:
        print(f"    - {svc_id}.{prop_name}")

    # 初始化 matplotlib
    plt.ion()
    fig = plt.figure("IoTDA Device Monitor", figsize=(12, 8))

    # 为每个属性维护数据列表
    ax_time = []
    ay_data = {}  # key: "service.prop" -> [values]

    for svc_id, prop_name in services:
        key = f"{svc_id}.{prop_name}"
        ay_data[key] = []

    print(f"\n🔍 开始持续监控设备 {device_id}（间隔 {MONITOR_INTERVAL}s）...")
    print("   按 Ctrl+C 停止。\n")

    i = 0
    try:
        while True:
            try:
                shadow = client.get_device_shadow(device_id)
                now = dt.now().strftime("%H:%M:%S")
                ax_time.append(now)

                # 提取数据
                for svc_id, prop_name in services:
                    key = f"{svc_id}.{prop_name}"
                    val = extract_property_value(
                        shadow, svc_id, prop_name
                    )
                    ay_data[key].append(val if val is not None else float("nan"))

                # 限制历史数据点数量
                if len(ax_time) > MAX_PLOT_POINTS:
                    ax_time.pop(0)
                    for key in ay_data:
                        ay_data[key].pop(0)

                # 绘图
                fig.clf()

                n_plots = len(services)
                for idx, (svc_id, prop_name) in enumerate(services):
                    key = f"{svc_id}.{prop_name}"
                    ax = fig.add_subplot(n_plots, 1, idx + 1)
                    valid = [
                        v for v in ay_data[key]
                        if v is not None and v == v  # exclude NaN
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

                    # 动态 Y 轴范围
                    if valid:
                        y_min, y_max = min(valid), max(valid)
                        margin = max((y_max - y_min) * 0.2, 1)
                        ax.set_ylim(y_min - margin, y_max + margin)

                fig.suptitle(
                    f"设备 {device_id} 属性监控",
                    fontsize=12,
                    fontweight="bold",
                )
                fig.tight_layout()
                plt.pause(0.1)
                plt.ioff()

                i += 1
                if i % 10 == 0:
                    print(
                        f"  [{now}] 已采集 {i} 次 | "
                        f"{', '.join(f'{k}={ay_data[k][-1]}' for k in ay_data)}"
                    )

            except requests.exceptions.RequestException as e:
                print(f"  [!] 请求失败: {e}，{MONITOR_INTERVAL}s 后重试...")

            time.sleep(MONITOR_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n[−] 监控已停止。")
    finally:
        plt.close("all")


# ============================================================
# 主程序
# ============================================================

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="华为云 IoTDA 设备属性查询工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python huawei_iotda_query.py                    单次查询设备影子
  python huawei_iotda_query.py --monitor           持续监控 + 实时绘图
  python huawei_iotda_query.py --list-devices      列出所有设备
  python huawei_iotda_query.py --service Battery   查询指定服务属性
  python huawei_iotda_query.py --service Battery.batteryLevel --monitor  只监控特定属性
  python huawei_iotda_query.py --device <设备ID>   查询指定设备
        """,
    )
    parser.add_argument(
        "--device", "-d",
        default=None,
        help="设备 ID（默认使用 CONFIG 中的 device_id）",
    )
    parser.add_argument(
        "--service", "-s",
        default=None,
        help=(
            "服务ID[.属性名]，用于指定要监控的服务/属性。"
            "例如: 'Battery' 监控 Battery 服务的所有属性; "
            "'Battery.batteryLevel' 只监控特定属性"
        ),
    )
    parser.add_argument(
        "--monitor", "-m",
        action="store_true",
        help="持续监控模式，循环查询并使用 matplotlib 实时绘图",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=MONITOR_INTERVAL,
        help=f"监控模式下的查询间隔（秒），默认 {MONITOR_INTERVAL}",
    )
    parser.add_argument(
        "--list-devices", "-l",
        action="store_true",
        help="列出所有设备",
    )
    parser.add_argument(
        "--list-products",
        action="store_true",
        help="列出所有产品",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="查询设备详情",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=MAX_PLOT_POINTS,
        help=f"图表最多显示的数据点数，默认 {MAX_PLOT_POINTS}",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # 更新全局配置
    global MONITOR_INTERVAL, MAX_PLOT_POINTS
    MONITOR_INTERVAL = args.interval
    MAX_PLOT_POINTS = args.max_points

    # 检查配置
    missing = []
    for key in ["ak", "sk", "project_id"]:
        if not CONFIG[key] or CONFIG[key].startswith("your-"):
            missing.append(key)

    if missing:
        print("❌ 请先配置以下信息（可通过环境变量或直接修改 CONFIG 字典）：")
        for m in missing:
            env_map = {
                "ak": "HUAWEI_AK",
                "sk": "HUAWEI_SK",
                "project_id": "HUAWEI_PROJECT_ID",
            }
            print(f"   - {m} (环境变量: {env_map[m]})")
        print()
        print("示例：")
        print('  export HUAWEI_AK="your-access-key"')
        print('  export HUAWEI_SK="your-secret-key"')
        print('  export HUAWEI_PROJECT_ID="your-project-id"')
        print('  export HUAWEI_REGION="cn-north-4"')
        sys.exit(1)

    client = HuaweiIoTDA(CONFIG)
    device_id = args.device or CONFIG["device_id"]

    # ---- 列出设备 ----
    if args.list_devices:
        try:
            devices = client.list_devices(limit=50)
            print_devices(devices)
        except Exception as e:
            print(f"列出设备失败: {e}")
            sys.exit(1)
        return

    # ---- 列出产品 ----
    if args.list_products:
        try:
            products = client.list_products(limit=50)
            print("\n📦 产品列表:")
            print(json.dumps(products, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"列出产品失败: {e}")
            sys.exit(1)
        return

    # ---- 设备详情 ----
    if args.detail:
        try:
            detail = client.get_device_detail(device_id)
            print("\n📱 设备详情:")
            print(json.dumps(detail, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"查询设备详情失败: {e}")
            sys.exit(1)
        return

    # ---- 解析 services 参数 ----
    services = None
    if args.service:
        parts = args.service.split(".")
        if len(parts) == 2:
            services = [(parts[0], parts[1])]
        else:
            # 只指定了 service_id，需要先查询一次获取有哪些属性
            services = [(parts[0], None)]  # None 表示自动发现

    # ---- 监控模式 ----
    if args.monitor:
        # 如果只指定了 service_id 没指定属性名，需要先探测
        if services and services[0][1] is None:
            svc_id = services[0][0]
            print(f"[*] 自动探测服务 '{svc_id}' 的属性...")
            try:
                shadow = client.get_device_shadow(device_id)
                found_services = []
                for svc in shadow.get("shadow", []):
                    if svc.get("service_id") == svc_id:
                        reported = svc.get("reported", {})
                        props = (
                            reported.get("properties", reported)
                            if isinstance(reported, dict) else {}
                        )
                        for prop_name in props:
                            if prop_name != "event_time":
                                found_services.append((svc_id, prop_name))
                if not found_services:
                    print(
                        f"❌ 服务 '{svc_id}' 中未找到任何属性。"
                        f"请确认服务名是否正确。"
                    )
                    sys.exit(1)
                services = found_services
            except Exception as e:
                print(f"[!] 探测失败: {e}")
                sys.exit(1)

        # 如果没指定 service，传 None 让 run_monitor_mode 自动探测
        if services and services[0][1] is None:
            services = None

        run_monitor_mode(client, device_id, services)
        return

    # ---- 默认：单次查询设备影子 ----
    if device_id.startswith("your-"):
        print("⚠️  未指定设备 ID，将先列出所有设备...")
        print()
        try:
            devices = client.list_devices(limit=20)
            print_devices(devices)
            print(
                "\n请设置环境变量 HUAWEI_DEVICE_ID "
                "或使用 --device 参数来指定要查询的设备。"
            )
        except Exception as e:
            print(f"列出设备失败: {e}")
        sys.exit(0)

    print(f"🔍 正在查询设备 {device_id} ...")

    try:
        # 查询设备影子
        print("\n[设备影子]")
        shadow = client.get_device_shadow(device_id)
        print_device_shadow(shadow)

        # 如果指定了 service_id，也查询实时属性
        if args.service:
            svc_id = args.service.split(".")[0]
            print(f"\n[实时属性查询: {svc_id}]")
            props = client.query_device_properties(device_id, svc_id)
            print_device_properties(props)

    except requests.exceptions.HTTPError as e:
        print(f"\n查询失败: {e}")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"\n网络请求失败: {e}")
        sys.exit(1)

    print("\n💡 提示：")
    print("   - 使用 --monitor / -m 进入持续监控模式（含实时绘图）")
    print("   - 使用 --list-devices / -l 列出所有设备")
    print("   - 使用 --service Battery.batteryLevel --monitor 监控特定属性")
    print(f"   - 区域: {CONFIG['region']}  |  终端节点: {get_endpoint()}")


if __name__ == "__main__":
    main()
