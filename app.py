# -*- coding: utf-8 -*-
"""
aiops-multi-tenant-demo/app.py (clean & checked)

目的:
- 既存デモを壊さずに「全社一覧ビュー」を上部に追加
- マルチテナント (tenants/<TENANT>/networks/<NETWORK>/...) に対応
- IndentationError / NameError を根絶する（関数定義→呼び出しの順、余計な字下げなし）
- LogicalRCA は run_rca ではなく analyze を使用

前提:
- registry.py が存在する（tenants 構造の解決）
- inference_engine.py に LogicalRCA が存在する
- logic.py に simulate_cascade_failure が存在する（topology引数を取る）
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Any

import streamlit as st

# graphviz は環境により未導入の可能性があるため保護
try:
    import graphviz  # type: ignore
except Exception:
    graphviz = None

import pandas as pd

from inference_engine import LogicalRCA
from logic import simulate_cascade_failure

# registry が無い環境でも落ちないようにフォールバック
try:
    from registry import (
        list_tenants,
        list_networks,
        get_paths,
        load_topology,
        topology_mtime,
    )
    _HAS_REGISTRY = True
except Exception:
    _HAS_REGISTRY = False

# registry が無い場合は従来の data.TOPOLOGY を使う
try:
    from data import TOPOLOGY as FALLBACK_TOPOLOGY
except Exception:
    FALLBACK_TOPOLOGY = {}


# ============================================================
# Page config
# ============================================================
st.set_page_config(page_title="Antigravity Autonomous Agent", layout="wide")


# ============================================================
# Utilities
# ============================================================
def _get_node(topology: Dict[str, Any], node_id: str) -> Any:
    return topology.get(node_id)


def _node_type(node: Any) -> str:
    if node is None:
        return "UNKNOWN"
    if isinstance(node, dict):
        return str(node.get("type", "UNKNOWN"))
    return str(getattr(node, "type", "UNKNOWN"))


def _node_layer(node: Any) -> int:
    if node is None:
        return 999
    if isinstance(node, dict):
        try:
            return int(node.get("layer", 999))
        except Exception:
            return 999
    return int(getattr(node, "layer", 999))


def _node_children(topology: Dict[str, Any], node_id: str) -> List[str]:
    node = _get_node(topology, node_id)
    if node is None:
        return []
    # topology.json 形式
    if isinstance(node, dict):
        # children が list[str] で入っているケース / list[dict] のケース両対応
        ch = node.get("children", [])
        if isinstance(ch, list):
            out: List[str] = []
            for x in ch:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict) and "id" in x:
                    out.append(str(x["id"]))
            return out
        return []
    # NetworkNode 形式
    ch_obj = getattr(node, "children", [])
    out2: List[str] = []
    if isinstance(ch_obj, list):
        for c in ch_obj:
            cid = getattr(c, "id", None)
            if cid:
                out2.append(str(cid))
    return out2


def find_target_node_id(
    topology: Dict[str, Any],
    node_type: Optional[str] = None,
    layer: Optional[int] = None,
) -> Optional[str]:
    """トポロジーから条件に合うノードIDを1つ返す（デモ用）"""
    for node_id, node in topology.items():
        if node_type and _node_type(node) != node_type:
            continue
        if layer is not None and _node_layer(node) != layer:
            continue
        return node_id
    return None


def _make_alarms(topology: Dict[str, Any], scenario: str):
    """シナリオ→アラーム生成（topology引数で安全に）"""
    alarms = []
    if scenario == "WAN全回線断":
        nid = find_target_node_id(topology, node_type="ROUTER")
        if nid:
            alarms = simulate_cascade_failure(nid, topology)
    elif scenario == "FW片系障害":
        nid = find_target_node_id(topology, node_type="FIREWALL")
        if nid:
            alarms = simulate_cascade_failure(nid, topology, "Power Supply: Single Loss")
    elif scenario == "L2SWサイレント障害":
        nid = find_target_node_id(topology, node_type="SWITCH", layer=4)
        if nid:
            alarms = simulate_cascade_failure(nid, topology, "Link Degraded")
    return alarms


def render_topology_graph(topology: Dict[str, Any], alarms, analysis_results):
    """Graphviz でトポロジーを描画（graphviz 未導入ならスキップ）"""
    if graphviz is None:
        st.info("graphviz が未導入のため、トポロジーマップは表示できません。")
        return

    alarmed_ids = {a.device_id for a in alarms} if alarms else set()
    root_ids = {c["id"] for c in analysis_results if isinstance(c, dict) and c.get("prob", 0) > 0.6}

    dot = graphviz.Digraph()
    dot.attr(rankdir="LR")

    # nodes
    for node_id, node in topology.items():
        label = f"{node_id}\n({_node_type(node)})"

        fill = "#e8f5e9"
        penwidth = "1"
        fontcolor = "black"

        if node_id in alarmed_ids:
            fill = "#fff3e0"
            penwidth = "2"

        if node_id in root_ids:
            fill = "#ffebee"
            penwidth = "3"
            fontcolor = "#b71c1c"

        dot.node(node_id, label=label, style="filled", fillcolor=fill, penwidth=penwidth, fontcolor=fontcolor)

    # edges
    for parent_id in topology.keys():
        for child_id in _node_children(topology, parent_id):
            if child_id in topology:
                dot.edge(parent_id, child_id)

    st.graphviz_chart(dot, use_container_width=True)


# ============================================================
# Multi-tenant scope (sidebar)
# ============================================================
def _get_scope():
    if not _HAS_REGISTRY:
        return None, None, None

    tenants = list_tenants()
    tenant_id = st.sidebar.selectbox("Tenant", tenants, index=0)

    networks = list_networks(tenant_id)
    network_id = st.sidebar.selectbox("Network", networks, index=0)

    paths = get_paths(tenant_id, network_id)
    return tenant_id, network_id, paths


# ============================================================
# Sidebar: Scenario
# ============================================================
st.sidebar.markdown("### ⚡ Scenario Controller")
selected_scenario = st.sidebar.radio(
    "発生シナリオ",
    ["正常稼働", "WAN全回線断", "FW片系障害", "L2SWサイレント障害"],
)

# Title
st.title("⚡ Antigravity Autonomous Agent")


# ============================================================
# All Companies View (TOP)
# ============================================================
@st.cache_data(show_spinner=False)
def _summarize_scope(tenant_id: str, network_id: str, scenario: str, mtime: float):
    paths = get_paths(tenant_id, network_id)
    topology = load_topology(paths.topology_path)
    alarms = _make_alarms(topology, scenario)

    count = len(alarms)
    if count == 0:
        health = "Good"
    elif count < 5:
        health = "Watch"
    elif count < 15:
        health = "Degraded"
    else:
        health = "Down"

    suspected = None
    if alarms:
        try:
            rca = LogicalRCA(topology, config_dir=str(paths.config_dir))
            res = rca.analyze(alarms)
            if res and isinstance(res, list):
                suspected = res[0].get("id") if isinstance(res[0], dict) else str(res[0])
        except Exception:
            suspected = None

    return {"tenant": tenant_id, "network": network_id, "health": health, "alarms": count, "suspected": suspected}


def _render_all_companies_view(scenario: str):
    st.subheader("🏢 全社一覧ビュー（Top 10）")

    if not _HAS_REGISTRY:
        st.info("tenants/ 構成が未検出のため、全社一覧は表示できません。")
        st.divider()
        return

    rows = []
    for t in list_tenants():
        for n in list_networks(t):
            p = get_paths(t, n)
            rows.append(_summarize_scope(t, n, scenario, topology_mtime(p.topology_path)))

    rows.sort(key=lambda r: r["alarms"], reverse=True)

    # カード化（簡易）
    down = sum(1 for r in rows if r["health"] == "Down")
    degraded = sum(1 for r in rows if r["health"] == "Degraded")
    watch = sum(1 for r in rows if r["health"] == "Watch")
    good = sum(1 for r in rows if r["health"] == "Good")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Down", down)
    c2.metric("Degraded", degraded)
    c3.metric("Watch", watch)
    c4.metric("Good", good)

    st.markdown("#### Top 10（アラーム多い順）")
    for r in rows[:10]:
        a, b, c, d = st.columns([2.3, 1.2, 1.2, 3.0])
        a.write(f"**{r['tenant']} / {r['network']}**")
        b.write(r["health"])
        c.write(f"Alarms: {r['alarms']}")
        d.write(f"Suspected: {r['suspected'] or '-'}")

    st.divider()


# ★必ず selected_scenario 定義後、関数定義後に呼び出す
_render_all_companies_view(selected_scenario)


# ============================================================
# Single-tenant cockpit (below)
# ============================================================
tenant_id, network_id, paths = _get_scope()

if _HAS_REGISTRY and paths is not None:
    topology = load_topology(paths.topology_path)
    config_dir = str(paths.config_dir)
else:
    topology = FALLBACK_TOPOLOGY
    config_dir = "./configs"

alarms = _make_alarms(topology, selected_scenario)

st.markdown("### 🛡️ AIOps インシデント・コックピット")

col1, col2, col3 = st.columns(3)
with col1:
    st.metric("📉 ノイズ削減率", "98.5%", "高効率稼働中")
with col2:
    st.metric("📨 処理アラーム数", f"{len(alarms) * 15 if alarms else 0}件", "抑制済")
with col3:
    st.metric("🚨 要対応インシデント", f"{len(alarms)}件", "対処が必要" if alarms else "正常")

st.markdown("---")

analysis_results = []
if alarms:
    try:
        rca = LogicalRCA(topology, config_dir=config_dir)
        analysis_results = rca.analyze(alarms) or []
    except Exception as e:
        st.error(f"RCA実行でエラー: {e}")
        analysis_results = []

# Incident list (compact)
df_rows = []
for a in alarms:
    df_rows.append({"device_id": a.device_id, "severity": getattr(a, "severity", ""), "message": a.message})
df = pd.DataFrame(df_rows)

st.subheader("📋 インシデント一覧（抑制後）")
if len(df) == 0:
    st.success("現在、アクティブなインシデントはありません。")
else:
    st.dataframe(df, use_container_width=True, hide_index=True)

st.subheader("🧠 RCA候補（確率順）")
if not analysis_results:
    st.info("RCA候補はありません。")
else:
    # analysis_results は list[dict] を想定
    out = []
    for i, c in enumerate(analysis_results, 1):
        if isinstance(c, dict):
            out.append({"rank": i, "id": c.get("id"), "type": c.get("type"), "prob": c.get("prob"), "reason": c.get("reason")})
        else:
            out.append({"rank": i, "id": str(c), "type": "-", "prob": "-", "reason": "-"})
    st.dataframe(pd.DataFrame(out), use_container_width=True, hide_index=True)

st.subheader("🗺️ トポロジーマップ")
render_topology_graph(topology, alarms, analysis_results)
