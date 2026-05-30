# IoT 本地实时联网监控系统

基于 Python 构建的本地实时联网软件，通过 MQTT 协议连接华为云 IoTDA 平台，实现 ESP8266 传感器数据模拟采集与本地实时可视化监控。

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置凭据（复制模板并填入真实值）
cp config.example.json config.json

# 终端 1 — 启动 ESP8266 模拟器
python esp8266_simulator.py --interval 5 --duration 1800

# 终端 2 — 启动实时监控（Qt 界面，可拖动）
python iot_monitor.py --interval 5 --duration 1800 --log data.csv
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `esp8266_simulator.py` | ESP8266 设备模拟器，模拟传感器数据采集并上报华为云 |
| `iot_monitor.py` | 本地实时监控软件（PyQt5 + pyqtgraph，窗口可拖动） |
| `mqtt_query_shadow.py` | MQTT 完整查询工具（影子/属性查询 + 上报 + 监控） |
| `huawei_iotda_query.py` | REST API 查询工具（AK/SK 签名方式） |
| `credentials.py` | 凭据管理模块 |
| `config.example.json` | 配置模板 |

## 详细文档

▶ **[技术文档](techdocuments.md)** — 包含系统架构、MQTT Topic 参考、传感器模拟策略、故障排除等完整技术细节。
