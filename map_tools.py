"""
城市导航 Agent - 工具层
依赖: pip install osmnx networkx geopy requests
"""

import math
import json
import time
import pickle
import hashlib
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

# ─── 可选依赖（没安装时降级到 mock 数据）─────────────────────────────────────
try:
    import osmnx as ox
    import networkx as nx
    HAS_OSMNX = True
except ImportError:
    HAS_OSMNX = False
    print("[警告] osmnx 未安装，使用 mock 路网数据")

CACHE_DIR = Path("data/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ─── 数据结构 ─────────────────────────────────────────────────────────────────

@dataclass
class Location:
    name: str
    lat: float
    lon: float
    node_id: Optional[int] = None


@dataclass
class RouteStep:
    instruction: str       # "沿长安街向东行驶"
    street_name: str
    distance_m: float
    duration_s: float


@dataclass
class Route:
    origin: Location
    destination: Location
    waypoints: list         # 途经点 list[Location]
    total_distance_m: float
    total_duration_s: float
    steps: list[RouteStep]
    algorithm: str
    weight: str
    warnings: list[str]

    def summary(self) -> str:
        dist_km = self.total_distance_m / 1000
        duration_min = self.total_duration_s / 60
        return (
            f"从「{self.origin.name}」到「{self.destination.name}」\n"
            f"距离：{dist_km:.1f} 公里 | 预计时间：{duration_min:.0f} 分钟\n"
            f"算法：{self.algorithm}（优化目标：{self.weight}）\n"
            + (f"⚠️  注意：{'；'.join(self.warnings)}\n" if self.warnings else "")
        )


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

class MapTools:
    """
    Agent 可调用的地图工具集。
    每个方法对应一个 tool，返回值都是 dict（JSON 可序列化）。
    """

    def __init__(self, city: str = "Beijing, China"):
        self.city = city
        self._graph = None          # 懒加载路网图
        self._graph_cache_key = None

    # ── 1. 地名解析 ────────────────────────────────────────────────────────────

    def geocode_location(self, place_name: str, city_hint: str = "") -> dict:
        """
        将地名/地址转为经纬度坐标。
        
        Args:
            place_name: 地点名称，如"天安门广场"
            city_hint: 城市提示，避免同名地点歧义，如"北京"
        
        Returns:
            {name, lat, lon, full_address, confidence}
        """
        # 优先用 OSMnx 的 geocode
        if HAS_OSMNX:
            query = f"{place_name}, {city_hint or self.city}"
            try:
                cache_key = hashlib.md5(query.encode()).hexdigest()
                cache_file = CACHE_DIR / f"geo_{cache_key}.json"
                if cache_file.exists():
                    return json.loads(cache_file.read_text())

                point = ox.geocode(query)
                result = {
                    "name": place_name,
                    "lat": point[0],
                    "lon": point[1],
                    "full_address": query,
                    "confidence": 0.9,
                    "status": "ok"
                }
                cache_file.write_text(json.dumps(result))
                return result
            except Exception as e:
                return {"status": "error", "message": str(e), "name": place_name}

        # Mock 数据（北京常见地点）
        mock_locations = {
            "天安门": (39.9055, 116.3976),
            "鸟巢": (40.0090, 116.3915),
            "故宫": (39.9163, 116.3972),
            "颐和园": (40.0020, 116.2750),
            "三里屯": (39.9338, 116.4540),
            "望京": (40.0050, 116.4810),
            "中关村": (39.9835, 116.3140),
            "北京南站": (39.8651, 116.3783),
            "首都机场": (40.0799, 116.6031),
        }
        for key, (lat, lon) in mock_locations.items():
            if key in place_name:
                return {
                    "name": place_name,
                    "lat": lat, "lon": lon,
                    "full_address": f"{place_name}, 北京",
                    "confidence": 0.85,
                    "status": "ok"
                }
        # 找不到时返回随机北京坐标（demo 用）
        import random
        return {
            "name": place_name,
            "lat": 39.9 + random.uniform(-0.05, 0.05),
            "lon": 116.4 + random.uniform(-0.05, 0.05),
            "full_address": f"{place_name}, 北京（近似）",
            "confidence": 0.3,
            "status": "approximate",
            "warning": "地点未精确匹配，使用近似坐标"
        }

    # ── 2. 加载路网 ────────────────────────────────────────────────────────────

    def get_road_network(
        self,
        center_lat: float,
        center_lon: float,
        radius_m: int = 3000,
        network_type: str = "drive"
    ) -> dict:
        """
        以某坐标为中心，下载指定半径内的 OSM 路网。
        
        Args:
            center_lat/lon: 中心点
            radius_m: 半径（米），默认 3km
            network_type: drive / walk / bike
        
        Returns:
            {node_count, edge_count, bbox, status}
        """
        if not HAS_OSMNX:
            return {
                "status": "mock",
                "node_count": 1523,
                "edge_count": 3847,
                "bbox": [center_lat - 0.03, center_lon - 0.03,
                         center_lat + 0.03, center_lon + 0.03],
                "network_type": network_type
            }

        cache_key = f"{center_lat:.4f}_{center_lon:.4f}_{radius_m}_{network_type}"
        cache_file = CACHE_DIR / f"graph_{cache_key}.pkl"

        if cache_file.exists():
            with open(cache_file, "rb") as f:
                self._graph = pickle.load(f)
        else:
            try:
                self._graph = ox.graph_from_point(
                    (center_lat, center_lon),
                    dist=radius_m,
                    network_type=network_type
                )
                # 添加行驶时间属性
                self._graph = ox.add_edge_speeds(self._graph)
                self._graph = ox.add_edge_travel_times(self._graph)
                with open(cache_file, "wb") as f:
                    pickle.dump(self._graph, f)
            except Exception as e:
                return {"status": "error", "message": str(e)}

        G = self._graph
        return {
            "status": "ok",
            "node_count": G.number_of_nodes(),
            "edge_count": G.number_of_edges(),
            "bbox": [
                min(d["y"] for _, d in G.nodes(data=True)),
                min(d["x"] for _, d in G.nodes(data=True)),
                max(d["y"] for _, d in G.nodes(data=True)),
                max(d["x"] for _, d in G.nodes(data=True)),
            ],
            "network_type": network_type
        }

    # ── 3. 最短路径 ────────────────────────────────────────────────────────────

    def find_shortest_path(
        self,
        origin_lat: float,
        origin_lon: float,
        dest_lat: float,
        dest_lon: float,
        algorithm: str = "dijkstra",
        weight: str = "travel_time",
        avoid_highways: bool = False
    ) -> dict:
        """
        在路网上计算最短路径。
        
        Args:
            algorithm: dijkstra | astar
            weight: travel_time（最快）| length（最短）
            avoid_highways: 是否避开高速
        
        Returns:
            {distance_m, duration_s, node_path, edge_details, status}
        """
        if not HAS_OSMNX or self._graph is None:
            # Mock 路径结果
            dist = _haversine(origin_lat, origin_lon, dest_lat, dest_lon)
            speed = 30 if weight == "travel_time" else 50  # km/h
            duration = dist / (speed * 1000 / 3600)
            return {
                "status": "mock",
                "distance_m": round(dist),
                "duration_s": round(duration),
                "node_count": int(dist / 100),
                "algorithm": algorithm,
                "weight": weight,
                "steps": _generate_mock_steps(origin_lat, origin_lon,
                                               dest_lat, dest_lon, dist)
            }

        G = self._graph
        try:
            # 找最近节点
            orig_node = ox.distance.nearest_nodes(G, origin_lon, origin_lat)
            dest_node = ox.distance.nearest_nodes(G, dest_lon, dest_lat)

            if avoid_highways:
                # 过滤高速路边
                edges_to_remove = [
                    (u, v, k)
                    for u, v, k, d in G.edges(keys=True, data=True)
                    if d.get("highway") in ["motorway", "motorway_link"]
                ]
                G_filtered = G.copy()
                G_filtered.remove_edges_from(edges_to_remove)
            else:
                G_filtered = G

            # 运行算法
            if algorithm == "astar":
                path_nodes = nx.astar_path(
                    G_filtered, orig_node, dest_node,
                    weight=weight,
                    heuristic=lambda u, v: _haversine(
                        G.nodes[u]["y"], G.nodes[u]["x"],
                        G.nodes[v]["y"], G.nodes[v]["x"]
                    )
                )
            else:
                path_nodes = nx.shortest_path(
                    G_filtered, orig_node, dest_node, weight=weight
                )

            # 计算总距离和时间
            total_length = sum(
                G[u][v][0].get("length", 0)
                for u, v in zip(path_nodes[:-1], path_nodes[1:])
            )
            total_time = sum(
                G[u][v][0].get("travel_time", 0)
                for u, v in zip(path_nodes[:-1], path_nodes[1:])
            )

            # 提取路段信息
            steps = []
            for u, v in zip(path_nodes[:-1], path_nodes[1:]):
                edge_data = G[u][v][0]
                street = edge_data.get("name", "无名路")
                if isinstance(street, list):
                    street = street[0]
                steps.append({
                    "street": street,
                    "length_m": round(edge_data.get("length", 0)),
                    "speed_kph": edge_data.get("speed_kph", 30),
                    "highway_type": edge_data.get("highway", "unclassified")
                })

            return {
                "status": "ok",
                "distance_m": round(total_length),
                "duration_s": round(total_time),
                "node_count": len(path_nodes),
                "algorithm": algorithm,
                "weight": weight,
                "steps": steps[:20]  # 前20段，避免太长
            }

        except nx.NetworkXNoPath:
            return {
                "status": "error",
                "message": "起点和终点之间无可达路径，可能路网不连通"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── 4. 约束检查 ────────────────────────────────────────────────────────────

    def check_route_constraints(
        self,
        distance_m: float,
        duration_s: float,
        steps: list,
        constraints: dict
    ) -> dict:
        """
        验证路线是否满足用户约束。
        
        Args:
            constraints: {
                max_distance_km: float,
                max_duration_min: float,
                avoid_tolls: bool,
                avoid_highways: bool,
                vehicle_type: "car"|"bike"|"walk",
                departure_time: "HH:MM"  # 用于判断早晚高峰
            }
        
        Returns:
            {satisfied, violations, suggestions}
        """
        violations = []
        suggestions = []

        # 距离检查
        max_dist = constraints.get("max_distance_km")
        if max_dist and distance_m / 1000 > max_dist:
            violations.append(
                f"路线 {distance_m/1000:.1f}km 超过限制 {max_dist}km"
            )

        # 时间检查
        max_dur = constraints.get("max_duration_min")
        if max_dur and duration_s / 60 > max_dur:
            violations.append(
                f"预计 {duration_s/60:.0f} 分钟超过限制 {max_dur} 分钟"
            )

        # 高速路检查
        if constraints.get("avoid_highways"):
            highway_steps = [
                s for s in steps
                if s.get("highway_type") in ["motorway", "motorway_link"]
            ]
            if highway_steps:
                violations.append(f"路线经过 {len(highway_steps)} 段高速路")
                suggestions.append("尝试设置 avoid_highways=True 重新规划")

        # 出发时间（早晚高峰预警）
        dep_time = constraints.get("departure_time", "")
        if dep_time:
            try:
                hour = int(dep_time.split(":")[0])
                if 7 <= hour <= 9 or 17 <= hour <= 19:
                    actual_duration = duration_s * 1.5  # 高峰期1.5倍
                    suggestions.append(
                        f"早晚高峰期间实际耗时约 {actual_duration/60:.0f} 分钟"
                    )
            except Exception:
                pass

        return {
            "satisfied": len(violations) == 0,
            "violations": violations,
            "suggestions": suggestions,
            "summary": "✅ 满足所有约束" if not violations else f"❌ {len(violations)} 项不满足"
        }

    # ── 5. 比较多条路线 ────────────────────────────────────────────────────────

    def compare_routes(self, routes: list[dict]) -> dict:
        """
        对比多条备选路线，给出推荐。
        ToT 模式下用于评估多个 thought branch 的方案。
        
        Args:
            routes: [{name, distance_m, duration_s, num_tolls, comfort_score}]
        
        Returns:
            {recommended, ranking, reasoning}
        """
        if not routes:
            return {"error": "没有路线可比较"}

        scored = []
        for r in routes:
            # 综合评分：时间40% + 距离30% + 舒适度30%
            time_score = 100 - min(r.get("duration_s", 0) / 3600 * 100, 100)
            dist_score = 100 - min(r.get("distance_m", 0) / 50000 * 100, 100)
            comfort = r.get("comfort_score", 50)
            tolls_penalty = r.get("num_tolls", 0) * 10

            total_score = (
                time_score * 0.4 +
                dist_score * 0.3 +
                comfort * 0.3 -
                tolls_penalty
            )
            scored.append({**r, "score": round(total_score, 1)})

        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]

        return {
            "recommended": best["name"],
            "ranking": [
                {"rank": i+1, "name": r["name"], "score": r["score"],
                 "distance_km": round(r.get("distance_m", 0)/1000, 1),
                 "duration_min": round(r.get("duration_s", 0)/60)}
                for i, r in enumerate(scored)
            ],
            "reasoning": f"「{best['name']}」综合评分最高（{best['score']}分），"
                         f"距离 {best.get('distance_m',0)/1000:.1f}km，"
                         f"预计 {best.get('duration_s',0)/60:.0f} 分钟"
        }


# ─── 辅助函数 ─────────────────────────────────────────────────────────────────

def _haversine(lat1, lon1, lat2, lon2) -> float:
    """计算两点间球面距离（米）"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi/2)**2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _generate_mock_steps(lat1, lon1, lat2, lon2, total_dist) -> list:
    """生成 mock 路段数据（无 osmnx 时使用）"""
    streets = ["长安街", "建国路", "朝阳路", "东二环", "北三环", "中关村大街"]
    import random
    n_steps = max(3, int(total_dist / 800))
    steps = []
    for i in range(n_steps):
        steps.append({
            "street": random.choice(streets),
            "length_m": round(total_dist / n_steps + random.uniform(-100, 100)),
            "speed_kph": random.choice([30, 40, 50, 60]),
            "highway_type": "secondary"
        })
    return steps
