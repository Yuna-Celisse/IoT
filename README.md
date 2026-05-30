# IoT 本地实时联网监控系统

通过 Python 控制 ESP8266 上报传感器数据到华为云 IoTDA，并轮询云端影子实现本地实时可视化监控。

## 架构

```
┌────────────────────────────────────────────────────────────────┐
│                        Python (本地 PC)                         │
│                                                                │
│  ┌──────────────────────┐        ┌─────────────────────────┐   │
│  │ esp8266_controller   │        │ iot_monitor             │   │
│  │                      │        │                         │   │
│  │ SensorGenerator      │        │ MQTTDataCollector       │   │
│  │ (正弦波/随机游走)      │        │ (QThread 轮询影子)       │   │
│  │         ↓            │        │         ↓               │   │
│  │ AT 指令 → 串口        │        │ PropertyChartWidget     │   │
│  │                      │        │ (pyqtgraph 实时曲线)     │   │
│  └─────────┬────────────┘        └───────────┬─────────────┘   │
│            │ 串口 COMx                        │ MQTTS:8883      │
└────────────┼─────────────────────────────────┼────────────────┘
             │                                 │
      ┌──────┴──────┐                  ┌───────┴───────┐
      │  ESP8266     │    MQTT:1883     │  华为云 IoTDA  │
      │  (AT 固件)   │────────────────▶│  设备影子       │
      └─────────────┘                  └───────────────┘
```

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.json config.json   # 填入真实凭据

# 终端 1 — AT 指令控制 ESP8266 上报数据
python esp8266_controller.py --port COM15

# 终端 2 — 实时监控（Qt 窗口，可拖动）
python iot_monitor.py
```

## 项目结构

```
IoT/
├── common/                    # 公共模块
│   ├── __init__.py
│   └── iot_common.py          # 配置加载 / 密码计算 / Topic 定义 / 影子提取
│
├── esp8266_controller.py      # [主] AT 指令 → ESP8266 上报
├── iot_monitor.py             # [主] Qt 实时监控窗口
│
├── tools/                     # 辅助工具
│   ├── esp8266_simulator.py   # 纯软件模拟器（无需硬件）
│   ├── mqtt_query_shadow.py   # MQTT 查询 / 上报 / 监控 一体
│   ├── esp8266_serial_bridge.py  # 串口桥接（ESP8266 JSON → MQTT）
│   └── huawei_iotda_query.py  # REST API 查询（AK/SK 签名）
│
├── config/                    # 配置文件
│   ├── config.json            # 本地配置 (gitignored)
│   └── config.example.json    # 配置模板
│
├── docs/                      # 文档
│   └── techdocuments.md       # 详细技术文档
│
├── README.md
├── requirements.txt
└── .gitignore
```

## 工具栈

| 层 | 工具 | 用途 |
|------|------|------|
| 设备通信 | `pyserial` | 串口 AT 指令控制 ESP8266 |
| 云端通信 | `paho-mqtt` | MQTT 连接华为云 |
| 签名认证 | `hmac` + `hashlib` | HMAC-SHA256 计算设备密码 |
| 界面 | `PyQt5` + `pyqtgraph` | 可拖动实时折线图窗口 |
| 数据 | `csv` / `json` | 本地 CSV 记录 + JSON 解析 |

## 核心流程

```
SensorGenerator（正弦波/随机游走生成模拟数据）
       ↓
AT 指令通过串口 → ESP8266（AT 固件）
       ↓
ESP8266 MQTT:1883 publish → 华为云 IoTDA 设备影子
       ↓
MQTTDataCollector（QThread MQTTS:8883 轮询影子）
       ↓
PropertyChartWidget（pyqtgraph 增量更新曲线）
```

## 关键细节

- **密码** = `HMAC-SHA256(device_secret, "YYYYMMDDHH")`，时间戳 ±24h 有效
- **影子查询 Topic**：`request_id=` 而非 `request/`（华为云格式）
- **Qt 线程**：MQTT 在 `QThread`，`pyqtSignal` 抛给主线程渲染，UI 不卡
- **端口**：ESP8266 AT 固件用 `1883`（无 TLS），Python 监控用 `8883`（TLS）
- **公共模块**：`iot_common.py` 统一管理 `load_config` / `compute_password` / `build_client_id`
