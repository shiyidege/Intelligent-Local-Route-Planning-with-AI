"""
城市导航 Agent - 主程序
运行: python agent.py
"""

import json
import os
import sys
from typing import Optional
from anthropic import Anthropic

sys.path.insert(0, str(__file__).rsplit("/", 1)[0])
from tools.map_tools import MapTools
from prompts.prompts import (
    ROUTER_PROMPT, AGENT_SYSTEM_PROMPT,
    PLANNER_PROMPT, REFLECTION_PROMPT, TOT_BRANCH_PROMPT
)

client = Anthropic()

# ─── Tool Schema（传给 Claude）────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "geocode_location",
        "description": "将地名/地址转为经纬度坐标。每个地点都必须先经过这一步。",
        "input_schema": {
            "type": "object",
            "properties": {
                "place_name": {"type": "string", "description": "地点名称，如'天安门广场'"},
                "city_hint": {"type": "string", "description": "城市名，用于消歧义，如'北京'"}
            },
            "required": ["place_name"]
        }
    },
    {
        "name": "get_road_network",
        "description": "以某坐标为中心加载 OpenStreetMap 路网图，后续路径计算依赖此图。",
        "input_schema": {
            "type": "object",
            "properties": {
                "center_lat": {"type": "number", "description": "中心点纬度"},
                "center_lon": {"type": "number", "description": "中心点经度"},
                "radius_m": {"type": "integer", "description": "加载半径（米），建议 3000-20000"},
                "network_type": {
                    "type": "string",
                    "enum": ["drive", "walk", "bike"],
                    "description": "路网类型"
                }
            },
            "required": ["center_lat", "center_lon", "radius_m"]
        }
    },
    {
        "name": "find_shortest_path",
        "description": "在已加载的路网上计算两点间最优路径。",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin_lat": {"type": "number"},
                "origin_lon": {"type": "number"},
                "dest_lat": {"type": "number"},
                "dest_lon": {"type": "number"},
                "algorithm": {
                    "type": "string",
                    "enum": ["dijkstra", "astar"],
                    "description": "dijkstra 适合一般情况；astar 在有好的启发式时更快"
                },
                "weight": {
                    "type": "string",
                    "enum": ["travel_time", "length"],
                    "description": "travel_time=最快路线，length=最短路线"
                },
                "avoid_highways": {"type": "boolean", "description": "是否避开高速路"}
            },
            "required": ["origin_lat", "origin_lon", "dest_lat", "dest_lon"]
        }
    },
    {
        "name": "check_route_constraints",
        "description": "验证路线是否满足用户设定的约束条件（距离、时间、收费等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "distance_m": {"type": "number"},
                "duration_s": {"type": "number"},
                "steps": {"type": "array", "items": {"type": "object"}},
                "constraints": {
                    "type": "object",
                    "description": "约束字典，包含 max_distance_km, max_duration_min, avoid_tolls, departure_time 等"
                }
            },
            "required": ["distance_m", "duration_s", "steps", "constraints"]
        }
    },
    {
        "name": "compare_routes",
        "description": "对比多条备选路线，给出综合推荐。ToT 模式下使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "routes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "distance_m": {"type": "number"},
                            "duration_s": {"type": "number"},
                            "num_tolls": {"type": "integer"},
                            "comfort_score": {"type": "number"}
                        }
                    },
                    "description": "待比较的路线列表"
                }
            },
            "required": ["routes"]
        }
    }
]


# ─── Router ───────────────────────────────────────────────────────────────────

def route_task(user_request: str) -> dict:
    """
    LLM Router: 分析请求，决定使用哪种策略。
    返回路由决策 dict。
    """
    print("  [Router] 分析任务...")
    resp = client.messages.create(
        model="claude-haiku-4-5",          # 用轻量模型做路由，省成本
        max_tokens=512,
        system=ROUTER_PROMPT,
        messages=[{"role": "user", "content": user_request}]
    )
    text = resp.content[0].text.strip()
    # 清理可能的 markdown 代码块
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        # 兜底：默认 react 策略
        decision = {
            "strategy": "react",
            "use_reflection": False,
            "complexity": "low",
            "extracted_locations": [],
            "constraints": {},
            "reason": "解析失败，使用默认策略"
        }
    print(f"  [Router] 策略={decision['strategy']}  理由={decision.get('reason', '')}")
    return decision


# ─── ReAct Agent ──────────────────────────────────────────────────────────────

def run_react_agent(
    user_request: str,
    map_tools: MapTools,
    max_iterations: int = 10
) -> str:
    """
    ReAct 循环：适合简单点对点导航。
    LLM 每轮决定调哪个工具，直到 stop_reason == end_turn。
    """
    print("  [ReAct] 开始规划...")
    messages = [{"role": "user", "content": user_request}]

    for i in range(max_iterations):
        resp = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=2048,
            system=AGENT_SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages
        )

        # 收集 assistant 内容
        assistant_blocks = list(resp.content)
        messages.append({"role": "assistant", "content": assistant_blocks})

        if resp.stop_reason == "end_turn":
            # 返回最后的文本输出
            for block in resp.content:
                if block.type == "text":
                    return block.text
            return "规划完成（无文本输出）"

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    print(f"  [工具] {block.name}({_fmt_params(block.input)})")
                    result = _execute_tool(block.name, block.input, map_tools)
                    print(f"  [结果] {_fmt_result(result)}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            messages.append({"role": "user", "content": tool_results})

    return "达到最大迭代次数，规划未完成"


# ─── Plan-Execute Agent ───────────────────────────────────────────────────────

def run_plan_execute_agent(
    user_request: str,
    locations: list[str],
    map_tools: MapTools
) -> str:
    """
    Plan-Execute：先生成执行计划，再逐步执行每个子任务。
    适合多途经点问题。
    """
    print("  [Plan-Execute] 生成执行计划...")

    # Step 1: Planner 生成计划
    plan_resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=PLANNER_PROMPT,
        messages=[{
            "role": "user",
            "content": f"用户需求: {user_request}\n地点列表: {' → '.join(locations)}"
        }]
    )
    plan_text = plan_resp.content[0].text.strip()
    if plan_text.startswith("```"):
        plan_text = plan_text.split("```")[1].lstrip("json").strip()
        plan_text = plan_text.rsplit("```", 1)[0].strip()

    try:
        plan = json.loads(plan_text)
    except Exception:
        # 降级：如果解析失败，用 ReAct 兜底
        print("  [Plan-Execute] 计划解析失败，降级到 ReAct")
        return run_react_agent(user_request, map_tools)

    print(f"  [Planner] 计划: {plan.get('plan_summary', '')}")
    print(f"  [Planner] 共 {len(plan.get('steps', []))} 个子任务")

    # Step 2: 逐步执行
    step_results = {}
    for step in plan.get("steps", []):
        step_id = step["step_id"]
        print(f"  [Execute] 子任务 {step_id}: {step['from']} → {step['to']}")

        sub_request = (
            f"规划从「{step['from']}」到「{step['to']}」的路线，"
            f"优化目标：{step['params'].get('weight', 'travel_time')}"
        )
        sub_result = run_react_agent(sub_request, map_tools, max_iterations=6)
        step_results[step_id] = sub_result

    # Step 3: 合并输出
    merge_messages = [{
        "role": "user",
        "content": (
            f"用户原始需求: {user_request}\n\n"
            f"各段路线规划结果:\n"
            + "\n\n".join(
                f"段{k}: {v}" for k, v in step_results.items()
            )
            + "\n\n请汇总成完整的行程方案，包含总距离、总时间和分段说明。"
        )
    }]
    merge_resp = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=1024,
        system=AGENT_SYSTEM_PROMPT,
        messages=merge_messages
    )
    return merge_resp.content[0].text


# ─── ToT Agent ────────────────────────────────────────────────────────────────

def run_tot_agent(
    user_request: str,
    origin: str,
    destination: str,
    map_tools: MapTools
) -> str:
    """
    Tree of Thoughts：并行探索多条路线方案，再对比选优。
    """
    print("  [ToT] 生成多个探索方向...")

    # 生成分支
    branch_resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system="只输出 JSON，不要其他内容",
        messages=[{
            "role": "user",
            "content": TOT_BRANCH_PROMPT.format(
                user_request=user_request,
                origin=origin,
                destination=destination
            )
        }]
    )
    branch_text = branch_resp.content[0].text.strip().lstrip("```json").rstrip("```")
    try:
        branches = json.loads(branch_text).get("branches", [])
    except Exception:
        branches = [
            {"name": "最快路线", "algorithm": "dijkstra",
             "weight": "travel_time", "avoid_highways": False},
            {"name": "最短路线", "algorithm": "astar",
             "weight": "length", "avoid_highways": False},
        ]

    print(f"  [ToT] 探索 {len(branches)} 条路线分支")

    # 并行（顺序模拟）执行每个分支
    route_results = []
    for branch in branches:
        print(f"  [ToT 分支] {branch['name']}")
        sub_req = (
            f"从{origin}到{destination}，"
            f"{'避开高速，' if branch.get('avoid_highways') else ''}"
            f"使用{branch['algorithm']}算法，优化{branch['weight']}"
        )
        sub_result = run_react_agent(sub_req, map_tools, max_iterations=6)
        route_results.append({
            "name": branch["name"],
            "result": sub_result
        })

    # 对比所有方案
    compare_prompt = (
        f"用户需求: {user_request}\n\n"
        f"以下是 {len(route_results)} 条备选路线:\n\n"
        + "\n\n".join(f"【{r['name']}】\n{r['result']}" for r in route_results)
        + "\n\n请综合对比，给出明确推荐和理由。"
    )
    final_resp = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=1024,
        system=AGENT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": compare_prompt}]
    )
    return final_resp.content[0].text


# ─── Reflection ───────────────────────────────────────────────────────────────

def run_reflection(
    user_request: str,
    route_result: str,
    distance_km: float = 0,
    duration_min: float = 0
) -> tuple[str, bool]:
    """
    对路线方案做自我批评，决定是否需要修正。
    返回 (reflection_comment, needs_revision)
    """
    print("  [Reflection] 审查路线方案...")
    prompt = REFLECTION_PROMPT.format(
        user_request=user_request,
        distance_km=f"{distance_km:.1f}",
        duration_min=f"{duration_min:.0f}",
        key_streets="见上方路线详情",
        constraint_check="已通过基础检查"
    )
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system="只输出 JSON",
        messages=[{
            "role": "user",
            "content": f"{prompt}\n\n路线结果:\n{route_result}"
        }]
    )
    text = resp.content[0].text.strip().lstrip("```json").rstrip("```")
    try:
        review = json.loads(text)
        issues = review.get("issues", [])
        verdict = review.get("verdict", "approve")
        needs_revision = verdict == "revise"

        comment = ""
        if issues:
            comment = "⚠️  Reflection 发现: " + "；".join(issues)
            if review.get("recommendations"):
                comment += "\n💡 建议: " + "；".join(review["recommendations"])
        else:
            comment = "✅ Reflection 通过：方案合理"

        print(f"  [Reflection] {comment}")
        return comment, needs_revision
    except Exception:
        return "Reflection 解析失败，跳过审查", False


# ─── 工具执行层 ───────────────────────────────────────────────────────────────

def _execute_tool(tool_name: str, tool_input: dict, map_tools: MapTools) -> dict:
    try:
        if tool_name == "geocode_location":
            return map_tools.geocode_location(**tool_input)
        elif tool_name == "get_road_network":
            return map_tools.get_road_network(**tool_input)
        elif tool_name == "find_shortest_path":
            return map_tools.find_shortest_path(**tool_input)
        elif tool_name == "check_route_constraints":
            return map_tools.check_route_constraints(**tool_input)
        elif tool_name == "compare_routes":
            return map_tools.compare_routes(**tool_input)
        else:
            return {"error": f"未知工具: {tool_name}"}
    except Exception as e:
        return {"error": str(e), "tool": tool_name}


def _fmt_params(params: dict) -> str:
    short = {k: v for k, v in list(params.items())[:3]}
    return json.dumps(short, ensure_ascii=False)


def _fmt_result(result: dict) -> str:
    if "error" in result:
        return f"❌ {result['error']}"
    keys = ["status", "distance_m", "duration_s", "node_count", "satisfied"]
    parts = [f"{k}={result[k]}" for k in keys if k in result]
    return ", ".join(parts) if parts else str(result)[:80]


# ─── 主入口 ───────────────────────────────────────────────────────────────────

class CityNavAgent:
    def __init__(self, city: str = "Beijing, China"):
        self.map_tools = MapTools(city=city)
        self.city = city

    def navigate(self, user_request: str) -> str:
        """
        主入口：接受自然语言导航请求，返回规划结果。
        """
        print(f"\n{'='*60}")
        print(f"用户请求: {user_request}")
        print(f"{'='*60}")

        # Step 1: Router
        decision = route_task(user_request)
        strategy = decision.get("strategy", "react")
        use_reflection = decision.get("use_reflection", False)
        locations = decision.get("extracted_locations", [])
        constraints = decision.get("constraints", {})

        # Step 2: 执行对应策略
        if strategy == "react":
            result = run_react_agent(user_request, self.map_tools)

        elif strategy == "plan_execute":
            if len(locations) >= 2:
                result = run_plan_execute_agent(
                    user_request, locations, self.map_tools
                )
            else:
                result = run_react_agent(user_request, self.map_tools)

        elif strategy == "tot":
            origin = locations[0] if locations else "起点"
            destination = locations[-1] if len(locations) > 1 else "终点"
            result = run_tot_agent(
                user_request, origin, destination, self.map_tools
            )

        else:
            result = run_react_agent(user_request, self.map_tools)

        # Step 3: Reflection（可选）
        if use_reflection:
            reflection_comment, needs_revision = run_reflection(
                user_request, result
            )
            if needs_revision:
                print("  [Reflection] 需要修正，重新规划...")
                result = run_react_agent(
                    user_request + "（请特别注意：" + reflection_comment + "）",
                    self.map_tools
                )
            result = result + "\n\n" + reflection_comment

        print(f"\n{'='*60}")
        print("最终结果:")
        print(result)
        return result


# ─── 命令行入口 ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = CityNavAgent(city="Beijing, China")

    test_cases = [
        # 简单 → ReAct
        "从天安门到鸟巢怎么走？",

        # 多途经点 → Plan-Execute
        "我要从中关村，经过三里屯，最后到北京南站，帮我规划路线",

        # 对比路线 → ToT + Reflection
        "从故宫到首都机场，走高速和走国道哪个更快？我8点要登机",
    ]

    for req in test_cases:
        agent.navigate(req)
        print("\n")
