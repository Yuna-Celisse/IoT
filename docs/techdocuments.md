# IoT 本地实时联网监控系统

> **项目目标**：基于 Python 构建本地实时联网软件，通过 MQTT 协议连接华为云 IoTDA 平台，实现 ESP8266 传感器数据模拟采集、云平台数据交互与本地实时可视化监控。

---

## 目录

1. [项目概述](#1-项目概述)
2. [系统架构](#2-系统架构)
3. [文件清单与模块说明](#3-文件清单与模块说明)
4. [华为云 IoTDA 平台集成](#4-华为云-iotda-平台集成)
5. [认证与凭据管理](#5-认证与凭据管理)
6. [数据流设计](#6-数据流设计)
7. [MQTT Topic 参考](#7-mqtt-topic-参考)
8. [传感器模拟策略](#8-传感器模拟策略)
9. [配置指南](#9-配置指南)
10. [使用手册](#10-使用手册)
11. [故障排除](#11-故障排除)
12. [依赖环境](#12-依赖环境)

---

## 1. 项目概述

本项目实现了从设备端到云端再到本地监控端的完整 IoT 数据链路：

```text
┌─────────────────┐       上报数据        ┌─────────────────┐       轮询影子       ┌─────────────────┐
│  ESP8266 模拟器  │ ──────────────────▶  │  华为云 IoTDA    │ ◀───────────────── │  本地监控软件    │
│                 │   sys/properties     │                 │   sys/shadow/get  │                 │
│  • 传感器数据采集 │      /report       │  设备影子 (Shadow) │                  │  • 实时折线图    │
│  • MQTT 上报     │                    │                │                     │  • 数据统计      │
│  • CSV 本地记录  │                    │  Battery 服务   │                     │  • CSV 日志      │
└─────────────────┘                     └─────────────────┘                    └─────────────────┘
```

**核心特性**：

- **双通道数据获取**：同时支持 MQTT 设备侧通信（端口 8883）和 REST API 应用侧调用（端口 443）
- **真实传感器行为模拟**：正弦波、随机游走、衰减等多种数据生成策略
- **实时可视化监控**：matplotlib 多面板动态折线图，含统计面板（当前值 / 最小值 / 最大值 / 平均值）
- **长时间运行**：支持设定运行时长（≥ 30 分钟），含断线重连与错误重试
- **数据持久化**：CSV 格式本地记录，含 UTC 时间戳与运行时长字段
- **安全设计**：凭据与代码分离，敏感文件通过 .gitignore 排除

---

## 2. 系统架构

### 2.1 分层架构

```text
┌──────────────────────────────────────────────────────┐
│                    表现层 (Presentation)               │
│   iot_monitor.py — matplotlib 图表 / 控制台统计输出    │
├──────────────────────────────────────────────────────┤
│                    业务逻辑层 (Business Logic)          │
│   esp8266_simulator.py — 传感器数据生成 / 上报调度      │
│   mqtt_query_shadow.py  — 影子查询 / 属性提取          │
│   huawei_iotda_query.py — REST API 签名请求            │
├──────────────────────────────────────────────────────┤
│                    基础设施层 (Infrastructure)          │
│   credentials.py — 凭据统一管理                        │
│   paho-mqtt       — MQTT 客户端 (CallbackAPI V2)      │
│   requests        — HTTP 客户端 (HMAC-SHA256 签名)     │
└──────────────────────────────────────────────────────┘
```

### 2.2 部署拓扑

```text
┌────────────────────────────────────────────────────────────────┐
│                        本地 PC (Windows 11)                      │
│                                                                  │
│  ┌─────────────────────┐          ┌─────────────────────┐        │
│  │ 终端 1: 模拟器       │          │ 终端 2: 监控软件      │        │
│  │ esp8266_simulator.py │          │ iot_monitor.py      │        │
│  │                     │          │                     │        │
│  │ [SensorGenerator]   │          │ [IoTMonitor]        │        │
│  │      ↓              │          │      ↓              │        │
│  │ [ESP8266Simulator]  │          │ [Matplotlib 图表]   │        │
│  │      ↓              │          │ [CSV Logger]        │        │
│  │ [MQTT → Cloud]      │          │ [MQTT ← Cloud]      │        │
│  └─────────┬───────────┘          └─────────┬───────────┘        │
│            │                                │                     │
└────────────┼────────────────────────────────┼─────────────────────┘
             │          Internet              │
             └────────────┬───────────────────┘
                          │
               ┌──────────┴──────────┐
               │  华为云 IoTDA 平台    │
               │  cn-east-3 区域       │
               │  端口: 1883 / 8883   │
               └─────────────────────┘
```

---

## 3. 文件清单与模块说明

### 3.1 核心程序文件

| 文件 | 行数 | 角色 | 说明 |
|------|------|------|------|
| `esp8266_simulator.py` | 664 | 设备模拟器 | 模拟 ESP8266 采集传感器数据并上报华为云 |
| `iot_monitor.py` | 765 | 监控客户端 | 本地实时联网软件，轮询影子数据并绘制图表 |
| `mqtt_query_shadow.py` | 1139 | MQTT 查询工具 | 影子查询 / 属性查询 / 数据上报 / 持续监控 |
| `huawei_iotda_query.py` | 858 | REST API 工具 | 通过 AK/SK 签名的 REST API 查询设备信息 |
| `credentials.py` | 234 | 凭据管理模块 | 从环境变量或 JSON 文件统一获取认证信息 |

### 3.2 配置文件

| 文件 | 说明 | 是否提交 Git |
|------|------|-------------|
| `config.json` | 本地运行配置（含真实凭据） | ❌ `.gitignore` 排除 |
| `config.example.json` | 配置模板（纯占位符） | ✅ 可安全提交 |
| `credentials.json` | 凭据文件（可选） | ❌ `.gitignore` 排除 |
| `credentials.example.json` | 凭据模板 | ✅ |
| `requirements.txt` | Python 依赖 | ✅ |

### 3.3 模块依赖关系

```text
config.json ─────┬────▶ mqtt_query_shadow.py ──── paho-mqtt
                 │           ↑
                 ├────▶ iot_monitor.py ────────── paho-mqtt + matplotlib
                 │           ↑
                 ├────▶ esp8266_simulator.py ──── paho-mqtt
                 │           ↑
                 └────▶ credentials.py (公共工具)
                             ↑
                 huawei_iotda_query.py ────────── requests + matplotlib
```

---

## 4. 华为云 IoTDA 平台集成

### 4.1 平台地址

| 类型 | 地址格式 | 端口 | TLS |
|------|----------|------|-----|
| MQTT 设备接入 | `{实例ID}.st1.iotda-device.{区域}.myhuaweicloud.com` | 1883 | 否 |
| MQTTS 设备接入 | 同上 | 8883 | 是 |
| HTTPS 应用接入 | `{实例ID}.st1.iotda-app.{区域}.myhuaweicloud.com` | 443 | 是 |

### 4.2 设备影子 (Device Shadow)

设备影子是 IoTDA 平台上存储的设备状态 JSON 文档，结构如下：

```json
{
    "object_device_id": "6a1811f718855b39c51e52eb_M002",
    "shadow": [
        {
            "service_id": "Battery",
            "desired": {
                "properties": null,
                "event_time": null
            },
            "reported": {
                "properties": {
                    "batteryLevel": 88,
                    "batteryVoltage": 3.91
                },
                "event_time": "20260530T174652Z"
            },
            "version": 10
        }
    ]
}
```

- **desired**：应用侧期望的设备状态（通过 REST API 或控制台下发的配置）
- **reported**：设备实际上报的状态（通过 MQTT `sys/properties/report` 上报）
- **version**：每次更新递增，用于乐观锁冲突检测

### 4.3 支持的 API 操作

| 操作 | 通信方式 | Topic / Endpoint |
|------|----------|------------------|
| 查询设备影子 | MQTT | `$oc/devices/{id}/sys/shadow/get/request_id={req_id}` |
| 查询设备属性 | MQTT | `$oc/devices/{id}/sys/properties/get/request_id={req_id}` |
| 上报设备属性 | MQTT | `$oc/devices/{id}/sys/properties/report` |
| 平台命令下发 | MQTT | `$oc/devices/{id}/sys/commands/#` |
| 平台消息下发 | MQTT | `$oc/devices/{id}/sys/messages/down` |
| 查询影子 (REST) | HTTPS | `GET /v5/iot/{project_id}/devices/{device_id}/shadow` |
| 列出设备 (REST) | HTTPS | `GET /v5/iot/{project_id}/devices` |

---

## 5. 认证与凭据管理

### 5.1 两种认证方式

#### 方式 A：MQTT 设备密钥认证（推荐）

```
密码 = HMAC-SHA256(device_secret, timestamp)
Client ID = {device_id}_0_0_{timestamp}
```

- **时间戳格式**：`YYYYMMDDHH`（UTC 时间，如 `2026053016`）
- **有效期**：±24 小时（华为云默认允许的时间偏差）
- **优点**：无需存储长期密码，每次自动计算，不会过期
- **配置文件**：设置 `"device_secret": "你的设备密钥"`，`"password"` 留空

#### 方式 B：预置密码

- 直接使用在华为云控制台中获取的预计算密码
- **注意**：密码与生成时的时间戳绑定，需 `config.json` 中 `timestamp` 与密码匹配
- **配置文件**：设置 `"password": "预计算密码"`，`"timestamp": "匹配的时间戳"`

### 5.2 REST API 认证（AK/SK 签名）

采用华为云 SDK-HMAC-SHA256 签名算法：

```
签名密钥 = HMAC-SHA256(HMAC-SHA256(HMAC-SHA256(HMAC-SHA256("SDK"+SK, date), region), service), "sdk_request")
Authorization = SDK-HMAC-SHA256 Credential={AK}/{date}/{region}/iotda/sdk_request, SignedHeaders=..., Signature=...
```

涉及环境变量：`HUAWEI_AK`、`HUAWEI_SK`、`HUAWEI_PROJECT_ID`

### 5.3 凭据优先级

```
环境变量 > config.json (gitignored) > credentials.json (gitignored) > 代码默认值(空字符串)
```

---

## 6. 数据流设计

### 6.1 上报流程（模拟器 → 云平台）

```text
┌─────────────────────────────────────────────────────┐
│ ESP8266 模拟器主循环                                  │
│                                                     │
│  while 未超时:                                       │
│    1. generator.next_tick()     # 推进传感器时间       │
│    2. _collect_sensor_data()    # 采集所有传感器读数     │
│    3. _report(services)         # JSON 序列化 → MQTT   │
│    4. _log_to_csv(services)     # 本地 CSV 持久化      │
│    5. sleep(interval)           # 等待下一周期          │
└─────────────────────────────────────────────────────┘

上报 JSON 格式:
{
    "services": [
        {
            "service_id": "Battery",
            "properties": {
                "batteryLevel": 88,
                "batteryVoltage": 3.91
            },
            "event_time": "20260530T174652Z"
        }
    ]
}
```

### 6.2 监控流程（云平台 → 本地监控）

```text
┌─────────────────────────────────────────────────────┐
│ IoT 监控客户端主循环                                   │
│                                                     │
│  while 未超时:                                       │
│    1. query_shadow()            # 发送影子查询请求     │
│    2. extract_values(shadow)    # 解析 shadow JSON    │
│    3. data_buffers.append()     # 更新历史数据队列     │
│    4. _csv_writer.writerow()    # CSV 记录            │
│    5. 更新 matplotlib 图表      # plt.pause(0.1)      │
│    6. 控制台统计输出             # 每 5 次打印一次      │
│    7. sleep(interval)           # 等待下一周期          │
└─────────────────────────────────────────────────────┘

查询请求 Topic:
$oc/devices/{device_id}/sys/shadow/get/request_id={uuid}

响应 Topic:
$oc/devices/{device_id}/sys/shadow/get/response/request_id={uuid}
```

### 6.3 数据缓冲与统计

- 数据缓冲区：`collections.deque(maxlen=200)`，自动丢弃旧数据
- 统计计算：对缓冲区内所有有效数值计算 min / max / avg / current
- CSV 格式：`timestamp_utc, elapsed_s, {service}.{property}...`

---

## 7. MQTT Topic 参考

### 7.1 设备侧 Topic（本项目使用）

| Topic | 方向 | QoS | 说明 |
|-------|------|-----|------|
| `$oc/devices/{id}/sys/shadow/get/request_id={req}` | 设备→平台 | 1 | 请求设备影子 |
| `$oc/devices/{id}/sys/shadow/get/response/request_id={req}` | 平台→设备 | 1 | 影子查询响应 |
| `$oc/devices/{id}/sys/properties/get/request_id={req}` | 设备→平台 | 1 | 请求实时属性 |
| `$oc/devices/{id}/sys/properties/get/response/request_id={req}` | 平台→设备 | 1 | 属性查询响应 |
| `$oc/devices/{id}/sys/properties/report` | 设备→平台 | 1 | 上报设备属性 |
| `$oc/devices/{id}/sys/commands/#` | 平台→设备 | 1 | 平台命令下发 |
| `$oc/devices/{id}/sys/messages/down` | 平台→设备 | 1 | 平台消息下发 |

> **注意**：Topic 中的 `request_id={req}` 使用等号 `=` 而非斜杠 `/`。这是华为云 IoTDA 的实际格式，与某些旧版文档不同。MQTT `+` 通配符匹配整个层级，订阅 `response/+` 即可匹配所有响应。

### 7.2 MQTT 订阅设计

```python
# 订阅所有平台→设备的消息
client.subscribe("$oc/devices/{id}/sys/#", qos=1)

# 精确订阅影子响应
client.subscribe("$oc/devices/{id}/sys/shadow/get/response/+", qos=1)

# 精确订阅属性查询响应
client.subscribe("$oc/devices/{id}/sys/properties/get/response/+", qos=1)
```

---

## 8. 传感器模拟策略

### 8.1 数据生成算法

`SensorGenerator` 类为每种传感器属性维护独立内部状态，使用以下策略模拟真实传感器行为：

| 策略 | 算法 | 适用场景 | 参数 |
|------|------|----------|------|
| **sin_wave** | `center + amplitude × sin(2π×tick/period) + noise` | 温度（日周期） | center, amplitude, period, noise |
| **random_walk** | `prev + U(-step, +step)` 限制在 `[min, max]` | 湿度（缓变） | initial, step, min, max |
| **decay** | `prev − rate` 偶尔随机回升 | 电池（消耗） | initial, rate, min, bounce |
| **random_range** | `U(min, max)` | 气体浓度（波动） | min, max |
| **binary** | `1 if U(0,1) < p else 0` | 开关/报警 | prob_on |

### 8.2 传感器配置

#### 默认配置（Battery 服务）

| 属性 | 生成策略 | 参数 | 类型 |
|------|----------|------|------|
| `batteryLevel` | decay | initial=88, rate=0.02, min=0 | int |
| `batteryVoltage` | 与 batteryLevel 相关 | 3.0V ~ 4.2V, noise=±0.05 | float |

#### 完整配置（Battery + stm32 服务）

| 属性 | 生成策略 | 参数 | 类型 |
|------|----------|------|------|
| `DHT11_T` | sin_wave | center=26, amplitude=8, period=40 | float |
| `DHT11_H` | random_walk | initial=60, step=3, range=[30,90] | float |
| `SOIL_H` | random_walk | initial=50, step=2, range=[20,80] | float |
| `MQ135` | random_range | range=[40,120] | int |
| `FLAME` | binary | p=0.05 | int |
| `motor` | binary | p=0.70 | int |
| `GPS` | random_walk | lon=120.21±0.06, lat=30.19±0.06 | object |

### 8.3 随机种子

通过 `--seed` 参数可固定随机种子，使传感器数据序列可复现，便于调试和对比测试。

---

## 9. 配置指南

### 9.1 快速配置

**步骤 1**：复制配置模板

```bash
cp config.example.json config.json
```

**步骤 2**：编辑 `config.json`，填入华为云 IoTDA 控制台获取的真实值

```json
{
    "device_id":     "你的设备ID",
    "password":      "MQTT密码（或留空使用设备密钥）",
    "device_secret": "设备密钥（推荐，HMAC自动计算密码）",
    "hostname":      "你的实例ID.st1.iotda-device.cn-east-3.myhuaweicloud.com",
    "port":          8883,
    "timestamp":     ""
}
```

**步骤 3**：运行程序

```bash
python esp8266_simulator.py    # 启动模拟器
python iot_monitor.py           # 启动监控（另一个终端）
```

### 9.2 环境变量方式（不创建 config.json）

```bash
# Windows PowerShell
$env:IOT_DEVICE_ID="你的设备ID"
$env:IOT_DEVICE_SECRET="你的设备密钥"
$env:IOT_MQTT_HOST="你的MQTT主机地址"

# Linux / Git Bash
export IOT_DEVICE_ID="你的设备ID"
export IOT_DEVICE_SECRET="你的设备密钥"
export IOT_MQTT_HOST="你的MQTT主机地址"
```

### 9.3 REST API 方式额外配置

```bash
export HUAWEI_AK="你的AK"
export HUAWEI_SK="你的SK"
export HUAWEI_PROJECT_ID="你的项目ID"
export HUAWEI_IOTDA_ENDPOINT="你的应用接入端点"
```

---

## 10. 使用手册

### 10.1 ESP8266 设备模拟器

```bash
# 基础用法 — Battery 服务，10 秒间隔
python esp8266_simulator.py

# 全传感器模式
python esp8266_simulator.py --full

# 自定义间隔和时长（5 秒间隔，运行 30 分钟）
python esp8266_simulator.py --interval 5 --duration 1800

# 带 CSV 日志和固定随机种子
python esp8266_simulator.py --full --log sensor_data.csv --seed 42

# 完整 30 分钟模拟
python esp8266_simulator.py --full -i 5 -d 1800 --log data.csv
```

**命令行参数**：

| 参数 | 简写 | 默认值 | 说明 |
|------|------|--------|------|
| `--config` | `-c` | 自动搜索 | 配置文件路径 |
| `--interval` | `-i` | 10 | 上报间隔（秒） |
| `--duration` | `-d` | None (无限) | 运行时长（秒） |
| `--full` | `-f` | False | 全传感器模式 |
| `--log` | `-l` | None | CSV 日志路径 |
| `--seed` | | None | 随机种子 |

### 10.2 本地实时监控软件

```bash
# 基础用法 — 自动探测所有属性
python iot_monitor.py

# 监控指定服务
python iot_monitor.py --service Battery

# 监控特定属性（可多次使用 -s）
python iot_monitor.py -s Battery.batteryLevel -s Battery.batteryVoltage

# 完整监控配置（5 秒间隔，30 分钟，CSV 记录）
python iot_monitor.py --interval 5 --duration 1800 --log monitor_data.csv
```

**命令行参数**：

| 参数 | 简写 | 默认值 | 说明 |
|------|------|--------|------|
| `--config` | `-c` | 自动搜索 | 配置文件路径 |
| `--service` | `-s` | 自动探测 | 监控属性（可多次使用） |
| `--interval` | `-i` | 3 | 查询间隔（秒） |
| `--duration` | `-d` | None (无限) | 运行时长（秒） |
| `--log` | `-l` | None | CSV 日志路径 |
| `--max-points` | | 200 | 图表最大数据点数 |

### 10.3 MQTT 查询工具

```bash
# 单次影子查询
python mqtt_query_shadow.py

# 持续监控 + 实时图表
python mqtt_query_shadow.py --monitor

# 上报模拟数据
python mqtt_query_shadow.py --publish --interval 10

# 指定服务查询
python mqtt_query_shadow.py --service Battery
```

### 10.4 REST API 查询工具

```bash
# 单次查询设备影子
python huawei_iotda_query.py

# 列出所有设备
python huawei_iotda_query.py --list-devices

# 持续监控 + 实时图表
python huawei_iotda_query.py --monitor --service Battery.batteryLevel

# 查询指定设备
python huawei_iotda_query.py --device "设备ID"
```

### 10.5 典型场景：完整 30 分钟测试

**终端 1** — 模拟器：

```bash
python esp8266_simulator.py --full --interval 5 --duration 1800 --log sensor_30min.csv
```

**终端 2** — 监控：

```bash
python iot_monitor.py --interval 5 --duration 1800 --log monitor_30min.csv
```

完成后会生成两份 CSV 文件：
- `sensor_30min.csv`：模拟器上报的传感器数据
- `monitor_30min.csv`：监控端从云端获取的实际数据

两者可对照验证数据一致性。

---

## 11. 故障排除

### 11.1 连接问题

| 现象 | 原因 | 解决 |
|------|------|------|
| `rc=4` 用户名或密码错误 | timestamp 与密码不匹配 | 使用 `device_secret` 自动计算；或确保 `config.json` 中 timestamp 正确 |
| `rc=2` Client ID 被拒绝 | Client ID 已在平台存在 | 等待旧连接超时，或更改 timestamp |
| TLS 证书错误 | 系统 CA 证书不全 | 尝试端口 1883 (非 TLS)，或更新系统证书 |
| 连接超时 | 网络不通 / hostname 错误 | `nslookup` 检查 DNS，确认 hostname 格式正确 |
| REST API `401` | AK/SK 或 project_id 错误 | 在华为云控制台「我的凭证」中核对 |

### 11.2 数据问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 影子查询无响应 | Topic 格式错误 | 确认使用 `request_id={id}` 而非 `request/{id}` |
| 影子返回空 `[]` | 设备从未上报过数据 | 先运行 `--publish` 上报一次测试数据 |
| 属性查询超时 | properties/get 可能不支持 | 改用影子查询（已验证可用） |
| 图表中文乱码 | 系统无中文字体 | 安装 `Microsoft YaHei` 或 `SimHei` 字体 |

### 11.3 MQTT 回调版本

本项目使用 paho-mqtt `CallbackAPIVersion.VERSION2`，回调签名已更新：

```python
# VERSION2 回调签名
on_connect(client, userdata, flags, reason_code, properties)
on_disconnect(client, userdata, flags, reason_code, properties)
on_message(client, userdata, msg)  # 未变
```

`reason_code` 是 `ReasonCode` 枚举对象，通过 `.value` 获取整数。

---

## 12. 依赖环境

### 12.1 Python 版本

- Python ≥ 3.8（推荐 3.10+）

### 12.2 pip 依赖

```
paho-mqtt >= 1.6.0    # MQTT 客户端（华为云 IoTDA 设备侧通信）
requests >= 2.28.0    # HTTP 请求（华为云 IoTDA REST API 签名调用）
matplotlib >= 3.5.0   # 实时数据可视化（监控模式绘图）
```

安装：

```bash
pip install -r requirements.txt
```

### 12.3 网络要求

- 出站端口 1883 (MQTT) 或 8883 (MQTTS)
- 出站端口 443 (HTTPS, REST API)
- DNS 可解析 `*.myhuaweicloud.com`

### 12.4 运行环境

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows / Linux / macOS |
| Python | 3.8+ |
| 图形环境 | 监控模式需要 GUI（matplotlib 图形窗口） |
| 磁盘空间 | 30 分钟 CSV 日志约 50-100 KB |

---

## 附录 A：项目文件导航

```text
IoT/
├── esp8266_simulator.py      # ESP8266 传感器模拟器（设备端）
├── iot_monitor.py             # 本地实时监控软件（监控端）
├── mqtt_query_shadow.py       # MQTT 查询 / 监控 / 上报 一体化工具
├── huawei_iotda_query.py      # REST API 查询工具（应用端）
├── credentials.py             # 凭据管理公共模块
├── config.json                # 本地配置（gitignored，含真实凭据）
├── config.example.json        # 配置模板（可提交）
├── credentials.example.json   # 凭据模板（可提交）
├── requirements.txt           # Python 依赖列表
├── .gitignore                 # Git 忽略规则
└── 技术文档.md                 # 本文件
```

## 附录 B：参考资源

- 华为云 IoTDA 产品文档：https://support.huaweicloud.com/iotda/
- 华为云 IoTDA MQTT 接入指南
- 华为云 API 签名指南（SDK-HMAC-SHA256）
- paho-mqtt 文档：https://pypi.org/project/paho-mqtt/
