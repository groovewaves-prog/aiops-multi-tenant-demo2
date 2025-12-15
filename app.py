import streamlit as st
import graphviz
import os
import time
import google.generativeai as genai
from network_ops import run_diagnostic_simulation, generate_remediation_commands, predict_initial_symptoms

# モジュール群のインポート
from data import TOPOLOGY
from logic import CausalInferenceEngine, Alarm, simulate_cascade_failure
from network_ops import run_diagnostic_simulation, generate_remediation_commands
from verifier import verify_log_content, format_verification_report
from dashboard import render_intelligent_alarm_viewer
from bayes_engine import BayesianRCA

# --- ページ設定 ---
st.set_page_config(page_title="Antigravity Autonomous", page_icon="⚡", layout="wide")

# ==========================================
# 関数定義
# ==========================================
def find_target_node_id(topology, node_type=None, layer=None, keyword=None):
    """トポロジーから条件に合うノードIDを検索"""
    for node_id, node in topology.items():
        if node_type and node.type != node_type: continue
        if layer and node.layer != layer: continue
        if keyword:
            hit = False
            if keyword in node_id: hit = True
            for v in node.metadata.values():
                if isinstance(v, str) and keyword in v: hit = True
            if not hit: continue
        return node_id
    return None

def load_config_by_id(device_id):
    """configsフォルダから設定ファイルを読み込む"""
    possible_paths = [f"configs/{device_id}.txt", f"{device_id}.txt"]
    for path in possible_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    return "Config file not found."

def render_topology(alarms, root_cause_node, root_severity="CRITICAL"):
    """トポロジー図の描画"""
    graph = graphviz.Digraph()
    graph.attr(rankdir='TB')
    graph.attr('node', shape='box', style='rounded,filled', fontname='Helvetica')
    
    alarm_map = {a.device_id: a for a in alarms}
    alarmed_ids = set(alarm_map.keys())
    
    for node_id, node in TOPOLOGY.items():
        color = "#e8f5e9"
        penwidth = "1"
        fontcolor = "black"
        label = f"{node_id}\n({node.type})"
        
        red_type = node.metadata.get("redundancy_type")
        if red_type: label += f"\n[{red_type} Redundancy]"
        vendor = node.metadata.get("vendor")
        if vendor: label += f"\n[{vendor}]"

        if root_cause_node and node_id == root_cause_node.id:
            this_alarm = alarm_map.get(node_id)
            node_severity = this_alarm.severity if this_alarm else root_severity
            if node_severity == "CRITICAL": color = "#ffcdd2"
            elif node_severity == "WARNING": color = "#fff9c4"
            else: color = "#e8f5e9"
            penwidth = "3"
            label += "\n[ROOT CAUSE]"
        elif node_id in alarmed_ids:
            color = "#fff9c4" 
        
        graph.node(node_id, label=label, fillcolor=color, color='black', penwidth=penwidth, fontcolor=fontcolor)
    
    for node_id, node in TOPOLOGY.items():
        if node.parent_id:
            graph.edge(node.parent_id, node_id)
            parent_node = TOPOLOGY.get(node.parent_id)
            if parent_node and parent_node.redundancy_group:
                partners = [n.id for n in TOPOLOGY.values() 
                           if n.redundancy_group == parent_node.redundancy_group and n.id != parent_node.id]
                for partner_id in partners:
                    graph.edge(partner_id, node_id)
    return graph

# --- UI構築 ---
st.title("⚡ Antigravity Autonomous Agent")

api_key = None
if "GOOGLE_API_KEY" in st.secrets:
    api_key = st.secrets["GOOGLE_API_KEY"]
else:
    api_key = os.environ.get("GOOGLE_API_KEY")

# --- サイドバー ---
with st.sidebar:
    st.header("⚡ Scenario Controller")
    SCENARIO_MAP = {
        "基本・広域障害": ["正常稼働", "1. WAN全回線断", "2. FW片系障害", "3. L2SWサイレント障害"],
        "WAN Router": ["4. [WAN] 電源障害：片系", "5. [WAN] 電源障害：両系", "6. [WAN] BGPルートフラッピング", "7. [WAN] FAN故障", "8. [WAN] メモリリーク"],
        "Firewall (Juniper)": ["9. [FW] 電源障害：片系", "10. [FW] 電源障害：両系", "11. [FW] FAN故障", "12. [FW] メモリリーク"],
        "L2 Switch": ["13. [L2SW] 電源障害：片系", "14. [L2SW] 電源障害：両系", "15. [L2SW] FAN故障", "16. [L2SW] メモリリーク"],
        "Live Mode": ["99. [Live] Cisco実機診断"]
    }
    selected_category = st.selectbox("対象カテゴリ:", list(SCENARIO_MAP.keys()))
    selected_scenario = st.radio("発生シナリオ:", SCENARIO_MAP[selected_category])
    st.markdown("---")
    if api_key: st.success("API Connected")
    else:
        st.warning("API Key Missing")
        user_key = st.text_input("Google API Key", type="password")
        if user_key: api_key = user_key

# --- セッション管理 (★ここを修正: 全変数を確実に初期化) ---
if "current_scenario" not in st.session_state:
    st.session_state.current_scenario = "正常稼働"

# 必須変数がなければ初期化 (AttributeError防止)
if "live_result" not in st.session_state:
    st.session_state.live_result = None
if "messages" not in st.session_state:
    st.session_state.messages = []
if "chat_session" not in st.session_state:
    st.session_state.chat_session = None
if "trigger_analysis" not in st.session_state:
    st.session_state.trigger_analysis = False
if "verification_result" not in st.session_state:
    st.session_state.verification_result = None
if "generated_report" not in st.session_state:
    st.session_state.generated_report = None

# シナリオ切り替え時のリセット処理
if st.session_state.current_scenario != selected_scenario:
    st.session_state.current_scenario = selected_scenario
    st.session_state.messages = []      
    st.session_state.chat_session = None 
    st.session_state.live_result = None 
    st.session_state.trigger_analysis = False
    st.session_state.verification_result = None
    st.session_state.generated_report = None
    if "remediation_plan" in st.session_state: del st.session_state.remediation_plan
    if "bayes_engine" in st.session_state: del st.session_state.bayes_engine
    st.rerun()

# ==========================================
# メインロジック
# ==========================================
alarms = []
root_severity = "CRITICAL"
target_device_id = None
is_live_mode = False

# 1. アラーム生成
if "Live" in selected_scenario: is_live_mode = True
elif "WAN全回線断" in selected_scenario:
    target_device_id = find_target_node_id(TOPOLOGY, node_type="ROUTER")
    if target_device_id: alarms = simulate_cascade_failure(target_device_id, TOPOLOGY)
elif "FW片系障害" in selected_scenario:
    target_device_id = find_target_node_id(TOPOLOGY, node_type="FIREWALL")
    if target_device_id:
        alarms = [Alarm(target_device_id, "Heartbeat Loss", "WARNING")]
        root_severity = "WARNING"
elif "L2SWサイレント障害" in selected_scenario:
    target_device_id = find_target_node_id(TOPOLOGY, node_type="SWITCH", layer=4)
    if target_device_id:
        child_nodes = [nid for nid, n in TOPOLOGY.items() if n.parent_id == target_device_id]
        alarms = [Alarm(child, "Connection Lost", "CRITICAL") for child in child_nodes]
else:
    if "[WAN]" in selected_scenario: target_device_id = find_target_node_id(TOPOLOGY, node_type="ROUTER")
    elif "[FW]" in selected_scenario: target_device_id = find_target_node_id(TOPOLOGY, node_type="FIREWALL")
    elif "[L2SW]" in selected_scenario: target_device_id = find_target_node_id(TOPOLOGY, node_type="SWITCH", layer=4)

    if target_device_id:
        if "電源障害：片系" in selected_scenario:
            alarms = [Alarm(target_device_id, "Power Supply 1 Failed", "WARNING")]
            root_severity = "WARNING"
        elif "電源障害：両系" in selected_scenario:
            if "FW" in target_device_id:
                alarms = [Alarm(target_device_id, "Power Supply: Dual Loss (Device Down)", "CRITICAL")]
            else:
                alarms = simulate_cascade_failure(target_device_id, TOPOLOGY, "Power Supply: Dual Loss (Device Down)")
            root_severity = "CRITICAL"
        elif "BGP" in selected_scenario:
            alarms = [Alarm(target_device_id, "BGP Flapping", "WARNING")]
            root_severity = "WARNING"
        elif "FAN" in selected_scenario:
            alarms = [Alarm(target_device_id, "Fan Fail", "WARNING")]
            root_severity = "WARNING"
        elif "メモリ" in selected_scenario:
            alarms = [Alarm(target_device_id, "Memory High", "WARNING")]
            root_severity = "WARNING"

# 2. ベイズエンジン初期化 & 初期証拠注入
if "bayes_engine" not in st.session_state:
    st.session_state.bayes_engine = BayesianRCA(TOPOLOGY)
    
    if selected_scenario != "正常稼働" and api_key:
        # ★ここが変更点: if分岐を廃止し、AIに症状を予測させる
        # シナリオ名だけ渡せば、AIが「BGPならBGP Flappingだ」と判断してJSONを返す
        initial_symptoms = predict_initial_symptoms(selected_scenario, api_key)
        
        # AIが返した症状をベイズエンジンに注入
        if initial_symptoms.get("alarm"):
            st.session_state.bayes_engine.update_probabilities("alarm", initial_symptoms["alarm"])
        
        if initial_symptoms.get("ping") == "NG":
            st.session_state.bayes_engine.update_probabilities("ping", "NG")
            
        if initial_symptoms.get("log"):
            st.session_state.bayes_engine.update_probabilities("log", initial_symptoms["log"])

# 3. コックピット表示
selected_incident_candidate = None
if "bayes_engine" in st.session_state:
    selected_incident_candidate = render_intelligent_alarm_viewer(st.session_state.bayes_engine, selected_scenario)

# 4. 画面分割
col_map, col_chat = st.columns([1.2, 1])

# === 左カラム: トポロジーと診断 ===
with col_map:
    st.subheader("🌐 Network Topology")
    
    current_root_node = None
    current_severity = "WARNING"
    
    if selected_incident_candidate and selected_incident_candidate["prob"] > 0.6:
        current_root_node = TOPOLOGY.get(selected_incident_candidate["id"])
        current_severity = "CRITICAL"
    elif target_device_id:
        current_root_node = TOPOLOGY.get(target_device_id)
        current_severity = root_severity

    st.graphviz_chart(render_topology(alarms, current_root_node, current_severity), use_container_width=True)

    st.markdown("---")
    st.subheader("🛠️ Auto-Diagnostics")
    
    if st.button("🚀 診断実行 (Run Diagnostics)", type="primary"):
        if not api_key:
            st.error("API Key Required")
        else:
            with st.status("Agent Operating...", expanded=True) as status:
                st.write("🔌 Connecting to device...")
                target_node_obj = TOPOLOGY.get(target_device_id) if target_device_id else None
                
                res = run_diagnostic_simulation(selected_scenario, target_node_obj, api_key)
                st.session_state.live_result = res
                
                if res["status"] == "SUCCESS":
                    st.write("✅ Log Acquired & Sanitized.")
                    status.update(label="Diagnostics Complete!", state="complete", expanded=False)
                    log_content = res.get('sanitized_log', "")
                    verification = verify_log_content(log_content)
                    st.session_state.verification_result = verification
                    st.session_state.trigger_analysis = True
                elif res["status"] == "SKIPPED":
                    status.update(label="No Action Required", state="complete")
                else:
                    st.write("❌ Connection Failed.")
                    status.update(label="Diagnostics Failed", state="error")
            st.rerun()

# === 右カラム: AI Analyst Report (詳細版) ===
with col_chat:
    st.subheader("📝 AI Analyst Report")
    
    # --- A. 状況報告 (Situation Report) ---
    if selected_incident_candidate:
        cand = selected_incident_candidate
        
        if "generated_report" not in st.session_state or st.session_state.generated_report is None:
            if api_key and selected_scenario != "正常稼働":
                with st.spinner("AI Analyst is writing a detailed report..."):
                    target_conf = load_config_by_id(cand['id'])
                    
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel("gemma-3-12b-it")
                    
                    prompt = f"""
                    あなたはネットワーク運用監視のプロフェッショナルです。
                    以下の障害インシデントについて、顧客向けの「詳細な状況報告レポート」を作成してください。
                    
                    【入力情報】
                    - 発生シナリオ: {selected_scenario}
                    - 根本原因候補: {cand['id']} ({cand['type']})
                    - AI確信度: {cand['prob']:.1%}
                    - 対象機器Config: 
                    {target_conf[:1500]} (抜粋)

                    【出力フォーマット要件】
                    以下の見出し構成で出力してください。Markdown形式を利用してください。
                    
                    ### 状況報告：{cand['id']} ({cand['type']})
                    **1. 障害概要**
                    **2. 影響**
                    **3. 詳細情報** (機器名、HAグループ、障害内容、バージョン、設定情報など)
                    **4. 対応**
                    **5. 今後の対応**
                    """
                    
                    try:
                        resp = model.generate_content(prompt)
                        st.session_state.generated_report = resp.text
                    except Exception as e:
                        st.session_state.generated_report = f"Report Generation Error: {e}"
            else:
                 st.session_state.generated_report = "監視中... 異常は検知されていません。"

        if st.session_state.generated_report:
            with st.container(border=True):
                st.markdown(st.session_state.generated_report)
    
    # --- B. 診断実行結果 (Sanitized Logs) ---
    if st.session_state.live_result:
        res = st.session_state.live_result
        if res["status"] == "SUCCESS":
            st.markdown("---")
            st.subheader("🔍 Diagnostic Results")
            with st.expander("📄 診断ログ出力 (🔒 Sanitized)", expanded=True):
                if st.session_state.verification_result:
                    v = st.session_state.verification_result
                    st.caption(f"Verification: {v.get('hardware_status', 'N/A')} / {v.get('interface_status', 'N/A')}")
                st.code(res["sanitized_log"], language="text")
        elif res["status"] == "ERROR":
            st.error(f"診断エラー: {res.get('error')}")

    # --- C. 自動修復 & チャット ---
    st.markdown("---")
    st.subheader("🤖 Remediation & Chat")

    if selected_incident_candidate and selected_incident_candidate["prob"] > 0.8:
        if "remediation_plan" not in st.session_state:
            if st.button("✨ 修復プランを作成 (Generate Fix)"):
                 if not api_key: st.error("API Key Required")
                 else:
                    with st.spinner("Generating plan..."):
                        t_node = TOPOLOGY.get(selected_incident_candidate["id"])
                        plan_md = generate_remediation_commands(
                            selected_scenario, 
                            f"Identified Root Cause: {selected_incident_candidate['type']}", 
                            t_node, api_key
                        )
                        st.session_state.remediation_plan = plan_md
                        st.rerun()
        
        if "remediation_plan" in st.session_state:
            with st.container(border=True):
                st.info("AI Generated Recovery Procedure")
                st.markdown(st.session_state.remediation_plan)
            
            col_exec1, col_exec2 = st.columns(2)
            with col_exec1:
                if st.button("🚀 修復実行 (Execute)", type="primary"):
                    with st.status("Applying Fix...", expanded=True):
                        time.sleep(1)
                        st.write("⚙️ Config pushed.")
                        time.sleep(1)
                    st.balloons()
                    st.success("System Recovered.")
                    if st.button("リセット"):
                        del st.session_state.remediation_plan
                        st.session_state.current_scenario = "正常稼働"
                        st.rerun()
            with col_exec2:
                 if st.button("キャンセル"):
                    del st.session_state.remediation_plan
                    st.rerun()

    # チャット (常時表示)
    with st.expander("💬 Chat with AI Agent", expanded=False):
        if st.session_state.chat_session is None and api_key and selected_scenario != "正常稼働":
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemma-3-12b-it")
            st.session_state.chat_session = model.start_chat(history=[])

        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]): st.markdown(msg["content"])

        if prompt := st.chat_input("Ask details..."):
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"): st.markdown(prompt)
            if st.session_state.chat_session:
                with st.chat_message("assistant"):
                    with st.spinner("Thinking..."):
                        res = st.session_state.chat_session.send_message(prompt)
                        st.markdown(res.text)
                        st.session_state.messages.append({"role": "assistant", "content": res.text})

# ベイズ更新トリガー (診断後)
if st.session_state.trigger_analysis and st.session_state.live_result:
    if st.session_state.verification_result:
        v_res = st.session_state.verification_result
        if "NG" in v_res.get("ping_status", ""):
                st.session_state.bayes_engine.update_probabilities("ping", "NG")
        if "DOWN" in v_res.get("interface_status", ""):
                st.session_state.bayes_engine.update_probabilities("log", "Interface Down")
    st.session_state.trigger_analysis = False
    st.rerun()
