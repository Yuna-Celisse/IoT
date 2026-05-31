#!/usr/bin/env python3
"""
IoT 本地实时监控软件 — 华为云 IoTDA（Qt 版）
================================================
基于 PyQt5 + pyqtgraph 构建的本地实时联网监控软件：
  - 可拖动、可缩放的 Qt 原生窗口
  - 多面板实时滚动折线图（pyqtgraph，性能优于 matplotlib）
  - 每面板独立统计：当前值 / 最小值 / 最大值 / 平均值
  - MQTT 后台线程轮询设备影子，不阻塞 UI
  - CSV 数据记录
  - 支持长时间运行（≥ 30 分钟）

使用方式：
  python iot_monitor.py
  python iot_monitor.py --service Battery
  python iot_monitor.py --interval 5 --duration 1800 --log data.csv

依赖：
  pip install paho-mqtt PyQt5 pyqtgraph
"""

import argparse
import csv
import json
import ssl
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from PyQt5 import QtCore, QtWidgets
import pyqtgraph as pg

from common.iot_common import load_config, compute_password, build_client_id

# pyqtgraph 全局外观
pg.setConfigOptions(
    antialias=True,
    background=(30, 30, 35),
    foreground=(200, 200, 200),
)
pg.setConfigOption("leftButtonPan", False)


# ============================================================
# MQTT 数据采集线程
# ============================================================

class MQTTDataCollector(QtCore.QThread):
    """
    后台线程：通过 MQTT 轮询设备影子，采集数据并通过信号发送到主线程。
    """

    # 数据更新信号：(values_dict, elapsed_seconds)
    data_ready = QtCore.pyqtSignal(dict, float)
    # 连接状态信号
    connection_changed = QtCore.pyqtSignal(bool, str)
    # 查询计数
    query_count_changed = QtCore.pyqtSignal(int, int)

    def __init__(self, config: dict, monitor_services: list, interval: float = 3.0):
        super().__init__()
        self.config = config
        self.device_id = config["device_id"]
        self.monitor_services = monitor_services
        self.interval = interval
        self._running = False
        self._connected = False

    def run(self):
        """线程主循环"""
        self._running = True

        # 构建 MQTT 客户端
        device_secret = self.config.get("device_secret", "").strip()
        timestamp = self.config.get("timestamp", "").strip()
        if not timestamp:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        if device_secret:
            password = compute_password(device_secret, timestamp)
        else:
            password = self.config["password"]

        client_id = build_client_id(self.config["device_id"], timestamp, suffix=1)

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(self.config["device_id"], password)
        client.tls_set(
            ca_certs=None,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )

        # 连接状态
        connected_event = self._make_event()
        pending_responses = {}

        def on_connect(c, u, flags, reason_code, properties=None):
            rc = reason_code.value if hasattr(reason_code, "value") else reason_code
            if rc == 0:
                c.subscribe(
                    f"$oc/devices/{self.device_id}/sys/shadow/get/response/+",
                    qos=1,
                )
                connected_event.set()
                self._connected = True
                self.connection_changed.emit(True, "已连接")
            else:
                self.connection_changed.emit(False, f"连接失败 (rc={rc})")

        def on_message(c, u, msg):
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            raw = msg.topic.split("/")[-1]
            req_id = raw.replace("request_id=", "", 1) if "=" in raw else raw
            pending_responses[req_id] = payload

        def on_disconnect(c, u, flags, reason_code, properties=None):
            rc = reason_code.value if hasattr(reason_code, "value") else reason_code
            self._connected = False
            self.connection_changed.emit(False, f"已断开 (rc={rc})")

        client.on_connect = on_connect
        client.on_message = on_message
        client.on_disconnect = on_disconnect

        # 连接
        try:
            client.connect(self.config["hostname"], self.config["port"], keepalive=60)
            client.loop_start()

            if not connected_event.wait(15):
                self.connection_changed.emit(False, "连接超时")
                client.loop_stop()
                client.disconnect()
                return

            query_count = 0
            error_count = 0
            start_time = time.time()
            req_topic = f"$oc/devices/{self.device_id}/sys/shadow/get/request_id={{req_id}}"

            while self._running:
                # 查询影子
                req_id = str(uuid.uuid4())
                topic = req_topic.format(req_id=req_id)
                result = client.publish(topic, "", qos=1)

                if result.rc != 0:
                    error_count += 1
                    time.sleep(self.interval)
                    continue

                # 等待响应
                t0 = time.time()
                shadow = None
                while time.time() - t0 < 10:
                    if req_id in pending_responses:
                        shadow = pending_responses.pop(req_id)
                        break
                    time.sleep(0.2)

                if shadow is None:
                    error_count += 1
                    time.sleep(self.interval)
                    continue

                query_count += 1
                elapsed = time.time() - start_time

                # 提取属性值
                values = self._extract_values(shadow)
                self.data_ready.emit(values, elapsed)
                self.query_count_changed.emit(query_count, error_count)

                # 等待下一周期
                time.sleep(self.interval)

        except Exception as e:
            self.connection_changed.emit(False, f"错误: {e}")
        finally:
            client.loop_stop()
            client.disconnect()

    def stop(self):
        self._running = False

    def _make_event(self):
        """创建线程安全的事件"""
        from threading import Event
        return Event()

    def _extract_values(self, shadow: dict) -> dict:
        """从影子数据中提取监控属性的值"""
        result = {}
        shadow_list = shadow.get("shadow", [])
        for svc_id, prop_name in self.monitor_services:
            key = f"{svc_id}.{prop_name}"
            found = False
            for svc in shadow_list:
                if svc.get("service_id") == svc_id:
                    reported = svc.get("reported", {})
                    if isinstance(reported, dict):
                        props = reported.get("properties", reported)
                        if prop_name in props:
                            try:
                                result[key] = float(props[prop_name])
                            except (ValueError, TypeError):
                                result[key] = props[prop_name]
                            found = True
                            break
            if not found:
                result[key] = float("nan")
        return result


# ============================================================
# 辅助函数
# ============================================================

def auto_discover_services(config: dict) -> list:
    """自动探测设备影子中的所有服务属性"""
    device_secret = config.get("device_secret", "").strip()
    timestamp = config.get("timestamp", "").strip()
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    if device_secret:
        password = compute_password(device_secret, timestamp)
    else:
        password = config["password"]
    client_id = build_client_id(config["device_id"], timestamp)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(config["device_id"], password)
    client.tls_set(
        ca_certs=None,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLSv1_2,
    )

    from threading import Event
    connected = Event()
    response_data = {}

    def _on_connect(c, u, f, rc, props=None):
        r = rc.value if hasattr(rc, "value") else rc
        if r == 0:
            connected.set()

    def _on_message(c, u, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        raw = msg.topic.split("/")[-1]
        rid = raw.replace("request_id=", "", 1) if "=" in raw else raw
        response_data[rid] = payload

    client.on_connect = _on_connect
    client.on_message = _on_message

    DEVICE_ID = config["device_id"]
    resp_topic = f"$oc/devices/{DEVICE_ID}/sys/shadow/get/response/+"

    client.connect(config["hostname"], config["port"], keepalive=60)
    client.loop_start()
    if not connected.wait(10):
        client.loop_stop()
        client.disconnect()
        return []

    client.subscribe(resp_topic, qos=1)
    time.sleep(0.5)

    req_id = str(uuid.uuid4())
    req_topic = f"$oc/devices/{DEVICE_ID}/sys/shadow/get/request_id={req_id}"
    client.publish(req_topic, "", qos=1)

    t0 = time.time()
    while req_id not in response_data and time.time() - t0 < 10:
        time.sleep(0.3)

    client.loop_stop()
    client.disconnect()

    if req_id not in response_data:
        return []

    shadow = response_data[req_id]
    services = []
    for svc in shadow.get("shadow", []):
        svc_id = svc.get("service_id", "")
        reported = svc.get("reported", {})
        if isinstance(reported, dict):
            props = reported.get("properties", reported)
            for prop_name in props:
                if prop_name != "event_time":
                    services.append((svc_id, prop_name))
    return services


# ============================================================
# Qt 主窗口
# ============================================================

# 配色方案
CURVE_COLORS = [
    (33, 150, 243),    # Blue
    (255, 87, 34),     # Orange
    (76, 175, 80),     # Green
    (255, 193, 7),     # Amber
    (156, 39, 176),    # Purple
    (0, 188, 212),     # Cyan
    (233, 30, 99),     # Pink
    (121, 85, 72),     # Brown
]


class PropertyChartWidget(QtWidgets.QWidget):
    """单个属性的实时图表面板（曲线 + 统计）"""

    def __init__(self, label: str, color: tuple, parent=None):
        super().__init__(parent)
        self.label = label
        self.color = color
        self.buffer_size = 200
        self.time_data = deque(maxlen=self.buffer_size)
        self.value_data = deque(maxlen=self.buffer_size)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        # 标题行
        title_layout = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(
            f"<b style='color: rgb{color}'>{label}</b>"
        )
        self.title_label.setStyleSheet("font-size: 13px;")
        title_layout.addWidget(self.title_label)
        title_layout.addStretch()

        # 当前值大字显示
        self.current_label = QtWidgets.QLabel("--")
        self.current_label.setStyleSheet(
            f"font-size: 18px; font-weight: bold; color: rgb{color};"
        )
        title_layout.addWidget(self.current_label)
        layout.addLayout(title_layout)

        # pyqtgraph 图表
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setMinimumHeight(150)
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel("left", "")
        self.plot_widget.setLabel("bottom", "运行时间 (秒)")

        # 曲线
        pen = pg.mkPen(color=color, width=2)
        self.curve = self.plot_widget.plot(pen=pen, name=label)

        # 填充
        fill_brush = pg.mkBrush(color[0], color[1], color[2], 40)
        self._baseline_curve = pg.PlotDataItem([0], [0])
        self.fill = pg.FillBetweenItem(
            self.curve,
            self._baseline_curve,
            brush=fill_brush,
        )
        self.plot_widget.addItem(self.fill)

        layout.addWidget(self.plot_widget)

        # 统计行
        stats_layout = QtWidgets.QHBoxLayout()
        stats_style = "font-size: 11px; color: #aaa;"
        self.min_label = QtWidgets.QLabel("最低: --")
        self.min_label.setStyleSheet(stats_style)
        self.max_label = QtWidgets.QLabel("最高: --")
        self.max_label.setStyleSheet(stats_style)
        self.avg_label = QtWidgets.QLabel("平均: --")
        self.avg_label.setStyleSheet(stats_style)
        stats_layout.addWidget(self.min_label)
        stats_layout.addWidget(self.max_label)
        stats_layout.addWidget(self.avg_label)
        stats_layout.addStretch()
        layout.addLayout(stats_layout)

    def append_data(self, elapsed: float, value):
        """追加一个数据点"""
        self.time_data.append(elapsed)
        self.value_data.append(value if value == value else None)

        # 更新曲线
        t_list = list(self.time_data)
        v_list = [v if v is not None else float("nan") for v in self.value_data]
        self.curve.setData(t_list, v_list)

        # 更新填充参考线
        if v_list:
            valid = [v for v in v_list if v == v]
            if valid:
                baseline = min(valid) - (max(valid) - min(valid)) * 0.1
                self._baseline_curve.setData(
                    [t_list[0], t_list[-1]], [baseline, baseline]
                )

        # 更新当前值
        if value is not None and value == value:
            self.current_label.setText(f"{value:.2f}" if isinstance(value, float) else str(value))

        # 更新统计
        valid = [v for v in self.value_data if v is not None and v == v]
        if valid:
            self.min_label.setText(f"最低: {min(valid):.2f}")
            self.max_label.setText(f"最高: {max(valid):.2f}")
            self.avg_label.setText(f"平均: {sum(valid)/len(valid):.2f}")


class IoTMonitorWindow(QtWidgets.QMainWindow):
    """IoT 实时监控主窗口"""

    def __init__(
        self,
        config: dict,
        monitor_services: list,
        interval: float = 3.0,
        log_file: str = None,
        duration: float = None,
    ):
        super().__init__()
        self.config = config
        self.monitor_services = monitor_services
        self.interval = interval
        self.log_file = log_file
        self.duration = duration
        self.start_time = None
        self._csv_file = None
        self._csv_writer = None

        self._setup_ui()
        self._setup_collector()

    def _setup_ui(self):
        """构建界面"""
        self.setWindowTitle(f"IoT 实时监控 — {self.config['device_id']}")
        self.resize(1200, 200 + 180 * len(self.monitor_services))
        self.setMinimumSize(800, 400)

        # 启用拖动
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, False)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ---- 顶部信息栏 ----
        info_layout = QtWidgets.QHBoxLayout()

        self.device_label = QtWidgets.QLabel(
            f"设备: <b>{self.config['device_id']}</b>"
        )
        self.device_label.setStyleSheet("font-size: 14px;")
        info_layout.addWidget(self.device_label)

        info_layout.addStretch()

        self.time_label = QtWidgets.QLabel("运行: 00:00")
        self.time_label.setStyleSheet("font-size: 14px; color: #4CAF50;")
        info_layout.addWidget(self.time_label)

        self.count_label = QtWidgets.QLabel("查询: 0 次")
        self.count_label.setStyleSheet("font-size: 13px; color: #aaa;")
        info_layout.addWidget(self.count_label)

        self.status_label = QtWidgets.QLabel("● 连接中...")
        self.status_label.setStyleSheet("font-size: 13px; color: #FFC107;")
        info_layout.addWidget(self.status_label)

        # 停止按钮
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setStyleSheet(
            "QPushButton { background: #d32f2f; color: white; padding: 4px 16px; "
            "border-radius: 4px; font-size: 12px; }"
            "QPushButton:hover { background: #f44336; }"
        )
        self.stop_btn.clicked.connect(self._on_stop)
        info_layout.addWidget(self.stop_btn)

        main_layout.addLayout(info_layout)

        # 分隔线
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color: #555;")
        main_layout.addWidget(line)

        # ---- 图表面板（可滚动） ----
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        scroll_content = QtWidgets.QWidget()
        self.charts_layout = QtWidgets.QVBoxLayout(scroll_content)
        self.charts_layout.setContentsMargins(0, 0, 0, 0)
        self.charts_layout.setSpacing(4)

        self.chart_widgets = []
        for idx, (svc_id, prop_name) in enumerate(self.monitor_services):
            key = f"{svc_id}.{prop_name}"
            color = CURVE_COLORS[idx % len(CURVE_COLORS)]
            chart = PropertyChartWidget(key, color)
            self.charts_layout.addWidget(chart)
            self.chart_widgets.append(chart)

        self.charts_layout.addStretch()
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll)

        # ---- 底部状态栏 ----
        footer = QtWidgets.QHBoxLayout()
        self.hint_label = QtWidgets.QLabel(
            "提示: 窗口可拖动 · 面板可滚动 · 数据自动刷新"
        )
        self.hint_label.setStyleSheet("font-size: 11px; color: #666;")
        footer.addWidget(self.hint_label)
        footer.addStretch()
        if self.log_file:
            log_label = QtWidgets.QLabel(f"CSV: {self.log_file}")
            log_label.setStyleSheet("font-size: 11px; color: #666;")
            footer.addWidget(log_label)
        main_layout.addLayout(footer)

        # 整体暗色风格
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e22;
            }
            QWidget {
                background-color: #1e1e22;
                color: #ccc;
            }
            QLabel {
                background: transparent;
            }
        """)

    def _setup_collector(self):
        """初始化后台数据采集线程"""
        self.collector = MQTTDataCollector(
            self.config, self.monitor_services, self.interval
        )
        self.collector.data_ready.connect(self._on_data_ready)
        self.collector.connection_changed.connect(self._on_connection_changed)
        self.collector.query_count_changed.connect(self._on_query_count_changed)

    def start(self):
        """启动监控"""
        self.start_time = time.time()

        # 打开 CSV 日志
        if self.log_file:
            self._csv_file = open(self.log_file, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            header = ["timestamp_utc", "elapsed_s"]
            for svc_id, prop_name in self.monitor_services:
                header.append(f"{svc_id}.{prop_name}")
            self._csv_writer.writerow(header)
            self._csv_file.flush()

        # 启动后台采集线程
        self.collector.start()

        # 定时更新 UI 时钟
        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(self._update_clock)
        self._timer.start(1000)

        self.show()

    # ---------- 槽函数 ----------

    def _on_data_ready(self, values: dict, elapsed: float):
        """接收到新数据点（在 Qt 主线程中执行）"""
        for chart in self.chart_widgets:
            key = chart.label
            val = values.get(key, float("nan"))
            chart.append_data(elapsed, val)

        # CSV 记录
        if self._csv_writer:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = [now, f"{elapsed:.1f}"]
            for svc_id, prop_name in self.monitor_services:
                key = f"{svc_id}.{prop_name}"
                row.append(values.get(key, ""))
            self._csv_writer.writerow(row)
            self._csv_file.flush()

        # 检查运行时长
        if self.duration and elapsed >= self.duration:
            self._on_stop()

    def _on_connection_changed(self, connected: bool, message: str):
        """连接状态变化"""
        if connected:
            self.status_label.setText("● 已连接")
            self.status_label.setStyleSheet("font-size: 13px; color: #4CAF50;")
        else:
            self.status_label.setText(f"● {message}")
            self.status_label.setStyleSheet("font-size: 13px; color: #f44336;")

    def _on_query_count_changed(self, count: int, errors: int):
        """查询计数更新"""
        success = count - errors
        rate = f"{100*success/count:.0f}%" if count > 0 else "--"
        self.count_label.setText(f"查询: {count} 次 (成功率 {rate})")

    def _update_clock(self):
        """每秒更新运行时间显示"""
        if self.start_time:
            elapsed = int(time.time() - self.start_time)
            m, s = divmod(elapsed, 60)
            self.time_label.setText(f"运行: {m:02d}:{s:02d}")

    def _on_stop(self):
        """停止监控"""
        self.collector.stop()
        self._timer.stop()
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None

        # 输出统计
        print()
        print("=" * 60)
        print("  监控运行报告")
        print("=" * 60)
        for chart in self.chart_widgets:
            valid = [v for v in chart.value_data if v is not None and v == v]
            print(f"  {chart.label}:")
            if valid:
                print(f"    当前: {valid[-1]:.2f}  最低: {min(valid):.2f}  "
                      f"最高: {max(valid):.2f}  平均: {sum(valid)/len(valid):.2f}")
        print()

        self.status_label.setText("● 已停止")
        self.status_label.setStyleSheet("font-size: 13px; color: #aaa;")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText("已停止")
        self.stop_btn.setStyleSheet(
            "QPushButton { background: #555; color: #999; padding: 4px 16px; "
            "border-radius: 4px; font-size: 12px; }"
        )

    def closeEvent(self, event):
        """关闭窗口时清理资源"""
        self._on_stop()
        self.collector.wait(3000)
        event.accept()


# ============================================================
# 命令行解析
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="IoT 本地实时监控软件（Qt 版）— 华为云 IoTDA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python iot_monitor.py                              自动探测属性
  python iot_monitor.py --service Battery            监控 Battery 服务
  python iot_monitor.py -s Battery.batteryLevel      监控特定属性
  python iot_monitor.py --log data.csv -i 5 -d 1800  5s间隔，30分钟

配合 ESP8266 模拟器:
  终端1: python esp8266_simulator.py --full -i 5 -d 1800
  终端2: python iot_monitor.py --log data.csv -i 5 -d 1800
        """,
    )
    parser.add_argument("--config", "-c", default=None, help="配置文件路径")
    parser.add_argument(
        "--service", "-s", action="append", default=None,
        help="监控的服务/属性，可多次使用。如: -s Battery.batteryLevel",
    )
    parser.add_argument(
        "--interval", "-i", type=float, default=3.0,
        help="查询间隔（秒），默认 3",
    )
    parser.add_argument(
        "--duration", "-d", type=float, default=None,
        help="运行时长（秒），默认无限。例如 1800 = 30 分钟",
    )
    parser.add_argument(
        "--log", "-l", default=None, help="CSV 数据日志文件路径",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Qt 应用
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("IoT Monitor")

    # 加载配置
    raw_config = load_config(args.config)

    if not raw_config.get("hostname"):
        print("[X] 未配置 MQTT hostname")
        print("   请设置环境变量 IOT_MQTT_HOST 或创建 config.json")
        sys.exit(1)

    # 解析监控属性
    monitor_services = None
    if args.service:
        monitor_services = []
        for spec in args.service:
            parts = spec.split(".")
            if len(parts) == 2:
                monitor_services.append((parts[0], parts[1]))
            elif len(parts) == 1:
                monitor_services.append((parts[0], None))

    # 自动探测不完整的属性
    if monitor_services and any(pn is None for _, pn in monitor_services):
        print("[*] 探测设备服务模型...")
        all_services = auto_discover_services(raw_config)
        if not all_services:
            print("[!] 无法自动探测，请使用 --service 手动指定")
            sys.exit(1)
        resolved = []
        for svc_id, prop_name in monitor_services:
            if prop_name is None:
                for asvc, aprop in all_services:
                    if asvc == svc_id:
                        resolved.append((asvc, aprop))
            else:
                resolved.append((svc_id, prop_name))
        monitor_services = resolved

    if not monitor_services:
        print("[*] 自动探测设备所有属性...")
        monitor_services = auto_discover_services(raw_config)

    if not monitor_services:
        print("[X] 未找到可监控的属性。")
        print("   请确认设备已上报过数据，或使用 --service 手动指定")
        sys.exit(1)

    print(f"[*] 监控 {len(monitor_services)} 个属性:")
    for svc_id, prop_name in monitor_services:
        print(f"    - {svc_id}.{prop_name}")

    # 创建主窗口
    window = IoTMonitorWindow(
        config=raw_config,
        monitor_services=monitor_services,
        interval=args.interval,
        log_file=args.log,
        duration=args.duration,
    )
    window.start()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
