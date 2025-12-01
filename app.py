import streamlit as st
import graphviz
import os
import google.generativeai as genai

# データ・ロジック・運用モジュールのインポート
from data import TOPOLOGY
# simulate_cascade_failure を追加でインポート
from logic import CausalInferenceEngine, Alarm, simulate_cascade_failure
# 実機接続の代わりにスタブ(シミュレーション)関数を使用
from network_ops import run_diagnostic_simulation

# --- ページ設定 ---
st.set_page_config(page_title="Antigravity Live", page_icon="⚡", layout="wide")

# --- 関数: トポロジー図の生成 (冗長構成対応) ---
def render_topology(alarms, root_cause_node):
    graph = graphviz.Digraph()
    graph.attr(rankdir='TB')
    graph.attr('node', shape='box', style='rounded,filled', fontname='Helvetica')
    
    alarmed_ids = {a.device_id for a in alarms}
    
    # ノード描画
    for node_id, node in TOPOLOGY.items():
        color = "#e8f5e9" # Default Green
        penwidth = "1"
        fontcolor = "black"
        label = f"{node_id}\n({node.type})"
        
        # 根本原因は赤、アラーム発生中は黄色
        if root_cause_node and node_id == root_cause_node.id:
            color = "#ffcdd2" # Root Cause Red
            penwidth = "3"
            label += "\n[ROOT CAUSE]"
        elif node_id in alarmed_ids:
            color = "#fff9c4" # Alarm Yellow
        
        graph.node(node_id, label=label, fillcolor=color, color='black', penwidth=penwidth, fontcolor=fontcolor)
    
    # エッジ描画
    for node_id, node in TOPOLOGY.items():
        if node.parent_id:
            graph.edge(node.parent_id, node_id)
            
            # 親がHAグループの場合、相方からも線を引く
            parent_node = TOPOLOGY.get(node.parent_id)
            if parent_node and parent_node.redundancy_group:
                partners = [n.id for n in TOPOLOGY.values() 
                           if n.redundancy_group == parent_node.redundancy_group and n.id != parent_node.id]
                for partner_id in partners:
                    graph.edge(partner_id, node_id)
    return graph

# --- 関数: Config自動読み込み (IDベース) ---
def load_config_by_id(device_id):
    path = f"configs/{device_id}.txt"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None
    return None

# --- UI構築 ---
st.title("⚡ Antigravity AI Agent (Live Demo)")

# APIキー取得
api_key = None
if "GOOGLE_API_KEY" in st.secrets:
    api_key = st.secrets["GOOGLE_API_KEY"]
else:
    api_key = os.environ.get("GOOGLE_API_KEY")

# サ
