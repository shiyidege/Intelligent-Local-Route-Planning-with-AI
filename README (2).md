# 城市导航 Agent

用自然语言规划城市路线，完整展示四组件 Agent 架构。

## 快速开始

```bash
# 1. 安装依赖
pip install anthropic osmnx networkx geopy

# 2. 设置 API Key
export ANTHROPIC_API_KEY=your_key_here

# 3. 运行 Agent
python agent.py

# 4. 只跑基线测试（不消耗 API 额度）
python eval/benchmark.py

# 5. 完整评估（含 Agent）
python eval/benchmark.py --with-agent
```

## 项目结构

```
city_nav_agent/
├── agent.py              # 主程序：Router + ReAct/Plan-Execute/ToT + Reflection
├── tools/
│   └── map_tools.py      # 工具层：geocode / 路网 / Dijkstra / 约束检查
├── prompts/
│   └── prompts.py        # 所有 prompt 集中管理
├── eval/
│   └── benchmark.py      # 基线对比评估
└── data/
    └── cache/            # OSMnx 路网缓存（自动生成）
```

## 四个组件的触发条件

| 请求类型 | 触发策略 | 示例 |
|---------|---------|------|
| 简单点对点 | **ReAct** | "从天安门到鸟巢" |
| 多途经点 | **Plan-Execute** | "经过超市和药店回家" |
| 需要对比方案 | **ToT** | "高速和国道哪条快" |
| 高优先级场景 | 任意 + **Reflection** | "赶飞机" "急诊" |

## 数据集

- **OSM 路网**：osmnx 自动下载，首次运行约 10-30 秒，后续有缓存
- **没有 osmnx**：自动降级到 Mock 数据，功能正常但路径为估算值

## 评估指标

- **绕路比**：实际路长 / 直线距离，理想值 1.2-1.8x
- **约束满足率**：用户设定的时间/距离约束命中率
- **LLM 调用次数**：Agent vs 基线的效率对比
- **延迟**：端到端响应时间
