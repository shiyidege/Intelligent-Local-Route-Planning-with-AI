"""
评估脚本：对比 Agent 路线规划 vs 纯算法基线
运行: python eval/benchmark.py
"""

import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.map_tools import MapTools, _haversine

# ─── 测试集 ───────────────────────────────────────────────────────────────────

TEST_CASES = [
    {
        "id": "tc001",
        "description": "简单点对点，市区内",
        "request": "从天安门到鸟巢",
        "origin": {"name": "天安门", "lat": 39.9055, "lon": 116.3976},
        "destination": {"name": "鸟巢", "lat": 40.0090, "lon": 116.3915},
        "expected_constraints": {"max_distance_km": 20, "max_duration_min": 60},
        "strategy": "react"
    },
    {
        "id": "tc002",
        "description": "长距离，机场路线",
        "request": "从故宫到首都机场",
        "origin": {"name": "故宫", "lat": 39.9163, "lon": 116.3972},
        "destination": {"name": "首都机场", "lat": 40.0799, "lon": 116.6031},
        "expected_constraints": {"max_distance_km": 60, "max_duration_min": 90},
        "strategy": "react"
    },
    {
        "id": "tc003",
        "description": "避开高速约束",
        "request": "从颐和园到三里屯，不走高速",
        "origin": {"name": "颐和园", "lat": 40.002, "lon": 116.275},
        "destination": {"name": "三里屯", "lat": 39.9338, "lon": 116.454},
        "expected_constraints": {"avoid_highways": True, "max_duration_min": 90},
        "strategy": "react"
    },
]


# ─── 基线：纯 Dijkstra（不用 LLM）────────────────────────────────────────────

def baseline_dijkstra(origin: dict, destination: dict) -> dict:
    """直接调工具，不经过 LLM 的纯算法基线"""
    tools = MapTools()
    start_time = time.time()

    # 计算中心点和半径
    mid_lat = (origin["lat"] + destination["lat"]) / 2
    mid_lon = (origin["lon"] + destination["lon"]) / 2
    dist = _haversine(origin["lat"], origin["lon"],
                      destination["lat"], destination["lon"])
    radius = max(3000, int(dist * 0.7))

    # 加载路网
    tools.get_road_network(mid_lat, mid_lon, radius_m=radius)

    # 直接跑 Dijkstra
    result = tools.find_shortest_path(
        origin["lat"], origin["lon"],
        destination["lat"], destination["lon"],
        algorithm="dijkstra",
        weight="travel_time"
    )

    elapsed = time.time() - start_time
    return {
        "method": "baseline_dijkstra",
        "distance_m": result.get("distance_m", 0),
        "duration_s": result.get("duration_s", 0),
        "latency_s": round(elapsed, 2),
        "llm_calls": 0,
        "status": result.get("status", "unknown")
    }


# ─── Agent 评估 ───────────────────────────────────────────────────────────────

def evaluate_agent(request: str, expected_constraints: dict) -> dict:
    """运行 Agent 并记录关键指标"""
    from agent import CityNavAgent

    start_time = time.time()
    llm_call_count = [0]

    # Monkey-patch 计数（实际项目用 callback 或 middleware）
    original_create = CityNavAgent.__init__
    agent = CityNavAgent()

    try:
        result_text = agent.navigate(request)
        elapsed = time.time() - start_time

        # 简单从文字中提取数字（实际项目返回结构化数据）
        constraint_satisfied = True
        if "分钟" in result_text:
            import re
            mins = re.findall(r'(\d+)\s*分钟', result_text)
            if mins:
                actual_min = int(mins[0])
                max_min = expected_constraints.get("max_duration_min", 999)
                constraint_satisfied = actual_min <= max_min

        return {
            "method": "agent",
            "latency_s": round(elapsed, 2),
            "constraint_satisfied": constraint_satisfied,
            "result_length": len(result_text),
            "status": "ok"
        }
    except Exception as e:
        return {
            "method": "agent",
            "latency_s": round(time.time() - start_time, 2),
            "status": "error",
            "error": str(e)
        }


# ─── 报告输出 ─────────────────────────────────────────────────────────────────

def run_benchmark(use_agent: bool = False):
    """
    运行基准测试。
    use_agent=False 时只跑基线（不消耗 API 额度），方便快速验证工具层。
    """
    print("城市导航 Agent 基准测试")
    print("=" * 60)

    results = []
    for tc in TEST_CASES:
        print(f"\n测试: {tc['id']} - {tc['description']}")
        print(f"请求: {tc['request']}")

        # 基线
        baseline = baseline_dijkstra(tc["origin"], tc["destination"])
        straight_dist = _haversine(
            tc["origin"]["lat"], tc["origin"]["lon"],
            tc["destination"]["lat"], tc["destination"]["lon"]
        )
        detour_ratio = (baseline["distance_m"] / straight_dist
                        if straight_dist > 0 else 1.0)

        print(f"  基线 Dijkstra:")
        print(f"    距离: {baseline['distance_m']/1000:.1f} km")
        print(f"    时间: {baseline['duration_s']/60:.0f} min")
        print(f"    绕路比: {detour_ratio:.2f}x (直线距离 {straight_dist/1000:.1f}km)")
        print(f"    耗时: {baseline['latency_s']}s")

        row = {
            "id": tc["id"],
            "description": tc["description"],
            "straight_dist_km": round(straight_dist / 1000, 1),
            "baseline": baseline,
            "detour_ratio": round(detour_ratio, 2),
        }

        # Agent（可选）
        if use_agent:
            agent_result = evaluate_agent(tc["request"], tc["expected_constraints"])
            print(f"  Agent:")
            print(f"    延迟: {agent_result['latency_s']}s")
            print(f"    约束满足: {agent_result.get('constraint_satisfied', 'N/A')}")
            row["agent"] = agent_result

        results.append(row)

    # 汇总
    print("\n" + "=" * 60)
    print("汇总")
    print(f"{'测试ID':<10} {'直线(km)':<12} {'Dijkstra(km)':<15} {'绕路比':<10}")
    for r in results:
        b = r["baseline"]
        print(
            f"{r['id']:<10} "
            f"{r['straight_dist_km']:<12} "
            f"{b['distance_m']/1000:<15.1f} "
            f"{r['detour_ratio']:<10}"
        )

    # 保存结果
    output_path = Path("eval/results.json")
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n结果已保存到 {output_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-agent", action="store_true",
                        help="同时测试 Agent（会消耗 API 额度）")
    args = parser.parse_args()
    run_benchmark(use_agent=args.with_agent)
