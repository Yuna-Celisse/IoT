# IoT 本地实时联网监控系统

基于 Python 通过 MQTT 协议连接华为云 IoTDA，通过 AT 指令控制 ESP8266 上报传感器数据，并轮询云端影子实现本地实时可视化监控。

## 架构

```
┌──────────────────────────────────────────────────────────┐
│                     Python (本地 PC)                      │
│                                                          │
│  ┌────────────────────┐     ┌───────────────────────┐    │
│  │ esp8266_controller │     │    iot_monitor         │    │
│  │                    │     │                        │    │
│  │ SensorGenerator    │     │ MQTTDataCollector      │    │
│  │ (正弦波/随机游走)    │     │ (QThread 后台轮询影子)   │    │
│  │        ↓           │     │        ↓               │    │
│  │ AT指令 → 串口       │     │ PropertyChartWidget    │    │
│  │                    │     │ (pyqtgraph 增量曲线)    │    │
│  └────────┬───────────┘     └───────────┬───────────┘    │
│           │ 串口(USB)                   │ MQTTS:8883      │
└───────────┼─────────────────────────────┼────────────────┘
            │                             │
     ┌──────┴──────┐              ┌───────┴───────┐
     │  ESP8266     │   MQTT:1883  │  华为云 IoTDA  │
     │  (AT 固件)   │─────────────▶│  设备影子       │
     └─────────────┘              └───────────────┘
```

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.json config.json   # 填入真实凭据

# 终端 1 — AT 指令控制 ESP8266 上报数据
python esp8266_controller.py --port COM15

# 终端 2 — 实时监控（Qt 界面，可拖动）
python iot_monitor.py
```

## 实现方式

Python 通过 MQTT 协议连接华为云 IoTDA，通过 AT 指令控制 ESP8266 上报数据，另一端轮询云端影子实时展示。

| 层 | 工具 | 用途 |
|------|------|------|
| 设备通信 | `pyserial` | 串口发送 AT 指令控制 ESP8266 |
| 云端通信 | `paho-mqtt` | MQTT 客户端，连接华为云 8883 端口 |
| 签名认证 | `hmac` + `hashlib` | HMAC-SHA256 计算设备密码 |
| 界面 | `PyQt5` + `pyqtgraph` | 实时折线图窗口（可拖动、可缩放） |
| 数据 | `csv` / `json` | 本地 CSV 记录 + JSON 解析 |

## 核心流程

```
SensorGenerator（正弦波/随机游走生成模拟数据）
       ↓
AT 指令通过串口 → ESP8266（AT 固件）
       ↓
ESP8266 MQTT publish → 华为云 IoTDA
       ↓
华为云 IoTDA 设备影子（存储 reported 属性）
       ↓
MQTTDataCollector（QThread 后台轮询影子）
       ↓
PropertyChartWidget（pyqtgraph 增量更新曲线）
```

## 关键细节

- **密码** = `HMAC-SHA256(device_secret, "YYYYMMDDHH")`，时间戳必须与生成密码时一致
- **影子查询** Topic 使用 `request_id=` 而非 `request/`（华为云特有格式）
- **Qt 线程**：MQTT 跑在 `QThread`，通过 `pyqtSignal` 将数据抛给主线程渲染，UI 不卡
- **AT 固件**：ESP8266 使用非加密 MQTT（端口 1883），Python 监控使用加密 MQTTS（端口 8883）

## 文件说明

| 文件 | 说明 |
|------|------|
| `esp8266_controller.py` | AT 指令控制器，通过串口控制 ESP8266 上报模拟数据 |
| `esp8266_serial_bridge.py` | 串口桥接模式，读取 ESP8266 串口 JSON 数据转发云端 |
| `esp8266_simulator.py` | 纯软件模拟器，无需 ESP8266 硬件直接上报云端 |
| `iot_monitor.py` | 本地实时监控软件（PyQt5 + pyqtgraph，窗口可拖动） |
| `mqtt_query_shadow.py` | MQTT 完整查询工具（影子/属性查询 + 上报 + 监控） |
| `huawei_iotda_query.py` | REST API 查询工具（AK/SK 签名方式） |
| `credentials.py` | 凭据管理模块 |
| `config.example.json` | 配置模板 |

## 详细文档

▶ **[技术文档](techdocuments.md)** — 系统架构、MQTT Topic 参考、传感器模拟策略、故障排除。
