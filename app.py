import streamlit as st
import graphviz
import os
import google.generativeai as genai

from data import TOPOLOGY
from logic import CausalInferenceEngine, Alarm, simulate_cascade_failure
from network_ops import run_diagnostic_simulation

# --- ページ設定 ---
st.set_page_config(page_title="Antigravity Live", page_icon="⚡", layout="wide")

# --- 関数: トポロジー図の生成 ---
def render_topology(alarms, root_cause_node, root_severity="CRITICAL"):
    graph = graphviz.Digraph()
    graph.attr(rankdir='TB')
    graph.attr('node', shape='box', style='rounded,filled', fontname='Helvetica')
    
    alarmed_ids = {a.device_id for a in alarms}
    
    for node_id, node in TOPOLOGY.items():
        color = "#e8f5e9" # Default Green
        penwidth = "1"
        fontcolor = "black"
        label = f"{node_id}\n({node.type})"
        
        # 内部冗長情報があればラベルに追記 (例: +PSU)
        if node.internal_redundancy:
            label += f"\n[{node.internal_redundancy} Redundancy]"

        # 根本原因の強調
        if root_cause_node and node_id == root_cause_node.id:
            if root_severity == "CRITICAL":
                color = "#ffcdd2" # Red
            elif root_severity == "WARNING":
                color = "#fff9c4" # Yellow
            else:
                color = "#e8f5e9"
            
            penwidth = "3"
            label += "\n[ROOT CAUSE]"
            
        elif node_id in alarmed_ids:
            color = "#fff9c4" # 連鎖アラーム
        
        graph.node(node_id, la
