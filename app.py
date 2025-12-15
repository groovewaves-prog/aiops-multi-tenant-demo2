import streamlit as st
import graphviz
import os
import time
import google.generativeai as genai

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
# ★追加機能: トポロジーからの動的ノード検索
# ==========================================
def find_target_node_id(topology, node_type=None, layer=None, keyword=None):
    """
    条件に合致するノードIDをトポロジーから動的に検索して返す。
    IDのハードコード（"WAN_ROUTER_01"など）を避けるためのロジック。
    """
    for node_id, node in topology.items():
        # 条件1: ノードタイプ (ROUTER, SWITCH, etc)
        if node_type and node.type != node_type:
            continue
        # 条件2: レイヤー (1, 2, 3...)
        if layer and node.layer != layer:
            continue
        # 条件3: キーワード検索 (メタデータやIDに含まれるか)
        if keyword:
            # IDまたはメタデータの値にキーワードが含まれるか
            hit = False
            if keyword in node_id: hit = True
            for v in node.metadata.values():
                if isinstance(v, str) and keyword in v: hit = True
            if not hit: continue
            
        return node_id # 最初に見つかったものを返す
    return None

# --- 関数: トポロジー図の生成 ---
def render_topology(alarms, root_cause_node, root_severity="CRITICAL"):
    graph = graphviz.Digraph()
    graph.attr(rankdir='TB')
    graph.attr('node', shape='box', style='rounded,filled', fontname='Helvetica')
    
    alarm_map = {a.device_id: a for a in alarms}
    alarmed_ids = set(alarm_map.keys())
    
    for node_id, node in TOPOLOGY.items():
        color = "#e8f5e9" # Default Green
        penwidth = "1"
        fontcolor = "black"
        label = f"{node_id}\n({node.type})"
        
        red_type = node.metadata.get("redundancy_type")
        if red_type:
            label += f"\n[{red_type} Redundancy]"
        
        vendor = node.metadata.get("vendor")
        if vendor:
            label += f"\n[{vendor}]"

        # 根本原因ノードの描画 (AI判定またはルール判定)
        if root_cause_node and node_id == root_cause_node.id:
            this_alarm = alarm_map.get(node_id)
            node_severity = this_alarm.severity if this_alarm else root_severity
            
            if node_severity == "CRITICAL":
                color = "#ffcdd2" # Red
            elif node_severity == "WARNING":
                color = "#fff9c4" # Yellow
            else:
                color = "#e8f5e9"
            
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

# --- サイドバー (障害対応のみ) ---
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
    if api_key:
        st.success("API Connected")
    else:
        st.warning("API Key Missing")
        user_key = st.text_input("Google API Key", type="password")
        if user_key: api_key = user_key

# --- セッション管理 ---
if "current_scenario" not in st.session_state:
    st.session_state.current_scenario = "正常稼働"

# シナリオ切り替え時のリセット処理
if st.session_state.current_scenario != selected_scenario:
    st.session_state.current_scenario = selected_scenario
    st.session_state.messages = []      
    st.session_state.chat_session = None 
    st.session_state.live_result = None 
    st.session_state.trigger_analysis = False
    st.session_state.verification_result = None
    if "remediation_plan" in st.session_state: del st.session_state.remediation_plan
    # ベイズエンジン初期化
    if "bayes_engine" in st.session_state: del st.session_state.bayes_engine
    st.rerun()

# ==========================================
# メインロジック
# ==========================================

alarms = []
root_severity = "CRITICAL"
target_device_id = None
is_live_mode = False

# 1. アラーム生成ロジック（★動的検索の実装）
if "Live" in selected_scenario:
    is_live_mode = True
    # Liveモード: 実機につなぐためアラームは出さない
elif "WAN全回線断" in selected_scenario:
    # ID指定をやめ、Type=ROUTERを探す
    target_device_id = find_target_node_id(TOPOLOGY, node_type="ROUTER")
    if target_device_id:
        alarms = simulate_cascade_failure(target_device_id, TOPOLOGY)

elif "FW片系障害" in selected_scenario:
    # ID指定をやめ、Type=FIREWALLを探す
    target_device_id = find_target_node_id(TOPOLOGY, node_type="FIREWALL")
    if target_device_id:
        alarms = [Alarm(target_device_id, "Heartbeat Loss", "WARNING")]
        root_severity = "WARNING"

elif "L2SWサイレント障害" in selected_scenario:
    # 配下のAPを探してアラームを出す（親はL2SWと想定）
    target_device_id = find_target_node_id(TOPOLOGY, node_type="SWITCH", layer=4)
    # L2SWが見つかったら、その子ノード(AP)を落とす
    if target_device_id:
        child_nodes = [nid for nid, n in TOPOLOGY.items() if n.parent_id == target_device_id]
        alarms = [Alarm(child, "Connection Lost", "CRITICAL") for child in child_nodes]

else:
    # カテゴリに基づく動的ターゲット検索
    if "[WAN]" in selected_scenario:
        target_device_id = find_target_node_id(TOPOLOGY, node_type="ROUTER")
    elif "[FW]" in selected_scenario:
        target_device_id = find_target_node_id(TOPOLOGY, node_type="FIREWALL")
    elif "[L2SW]" in selected_scenario:
        target_device_id = find_target_node_id(TOPOLOGY, node_type="SWITCH", layer=4)

    # ターゲットが見つかった場合のアラーム生成
    if target_device_id:
        if "電源障害：片系" in selected_scenario:
            alarms = [Alarm(target_device_id, "Power Supply 1 Failed", "WARNING")]
            root_severity = "WARNING"
        elif "電源障害：両系" in selected_scenario:
            if "FW" in target_device_id: # FWならデバイスダウン
                alarms = [Alarm(target_device_id, "Power Supply: Dual Loss (Device Down)", "CRITICAL")]
            else: # 他なら広域影響
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

# 2. ベイズエンジン初期化 & ★コックピット連動（初期症状の注入）
# シナリオに応じて、診断前から「それっぽいアラーム」をエンジンに入力しておく
if "bayes_engine" not in st.session_state:
    st.session_state.bayes_engine = BayesianRCA(TOPOLOGY)
    
    # === シナリオ別・初期インシデントデータの注入 ===
    # これにより、コックピットがシナリオとリンクした内容を表示するようになる
    if "BGP" in selected_scenario:
        # BGP障害なら、BGP Flappingアラームが出ていることをAIに教える
        st.session_state.bayes_engine.update_probabilities("alarm", "BGP Flapping")
    
    elif "全回線断" in selected_scenario or "両系" in selected_scenario:
        # 全断なら、Ping NGが出ていることを教える
        st.session_state.bayes_engine.update_probabilities("ping", "NG")
        st.session_state.bayes_engine.update_probabilities("log", "Interface Down")
        
    elif "片系" in selected_scenario:
        # 片系なら、HA Failoverが出ている
        st.session_state.bayes_engine.update_probabilities("alarm", "HA Failover")

    elif "FAN" in selected_scenario:
        # FAN故障は未知のエラーとして扱う（診断待ち）
        pass

# 3. ダッシュボード表示 (Incidents)
top_cause_candidate = None
if "bayes_engine" in st.session_state:
    top_cause_candidate = render_intelligent_alarm_viewer(st.session_state.bayes_engine, selected_scenario)


# 4. 画面分割 (左: マップと診断 / 右: AIチャットと修復)
col_map, col_chat = st.columns([1.2, 1])

with col_map:
    st.subheader("🌐 Network Topology")
    
    # AI推論結果があればそちらをルートとして強調表示
    current_root_node = None
    current_severity = "WARNING"
    
    if top_cause_candidate and top_cause_candidate["prob"] > 0.6:
        current_root_node = TOPOLOGY.get(top_cause_candidate["id"])
        current_severity = "CRITICAL"
    elif target_device_id:
        current_root_node = TOPOLOGY.get(target_device_id)
        current_severity = root_severity

    st.graphviz_chart(render_topology(alarms, current_root_node, current_severity), use_container_width=True)

    # ---------------------------
    # 診断実行エリア (Diagnostics)
    # ---------------------------
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
                    
                    # 検証ロジックの実行
                    log_content = res.get('sanitized_log', "")
                    verification = verify_log_content(log_content)
                    st.session_state.verification_result = verification
                    
                    # 診断完了トリガーON
                    st.session_state.trigger_analysis = True
                    
                elif res["status"] == "SKIPPED":
                    status.update(label="No Action Required", state="complete")
                else:
                    st.write("❌ Connection Failed.")
                    status.update(label="Diagnostics Failed", state="error")
            
            st.rerun()

    # 診断結果の表示（ログ）
    if st.session_state.live_result and st.session_state.live_result["status"] == "SUCCESS":
        st.success("Log Analysis Complete")
        with st.expander("📄 Raw Logs (Sanitized)", expanded=True):
            st.code(st.session_state.live_result["sanitized_log"], language="text")

# 5. ベイズ更新処理 (トリガーがONの場合)
# 診断実行後の「追加証拠」による確率更新
if st.session_state.trigger_analysis and st.session_state.live_result:
    if st.session_state.verification_result:
        v_res = st.session_state.verification_result
        # 証拠投入
        if "NG" in v_res.get("ping_status", ""):
                st.session_state.bayes_engine.update_probabilities("ping", "NG")
        if "DOWN" in v_res.get("interface_status", ""):
                st.session_state.bayes_engine.update_probabilities("log", "Interface Down")
    
    st.session_state.trigger_analysis = False
    st.rerun()


# 6. 右カラム: AIチャット & 修復アクション
with col_chat:
    st.subheader("🤖 AI Analyst & Remediation")
    
    # ---------------------------
    # 自動修復 (Closed Loop)
    # ---------------------------
    if top_cause_candidate and top_cause_candidate["prob"] > 0.8:
        st.markdown(f"""
        <div style="background-color:#e3f2fd;padding:15px;border-radius:10px;border-left:5px solid #2196f3;margin-bottom:20px;">
            <strong>🚀 Action Required</strong><br>
            AI has identified <b>{top_cause_candidate['id']}</b> as the root cause.<br>
            Auto-remediation is available.
        </div>
        """, unsafe_allow_html=True)

        if "remediation_plan" not in st.session_state:
            if st.button("✨ 修復プランを作成 (Generate Fix)"):
                 if not api_key:
                    st.error("API Key Required")
                 else:
                    with st.spinner("Generating config..."):
                        t_node = TOPOLOGY.get(top_cause_candidate["id"])
                        cmds = generate_remediation_commands(
                            selected_scenario, 
                            f"Identified Root Cause: {top_cause_candidate['type']}", 
                            t_node, 
                            api_key
                        )
                        st.session_state.remediation_plan = cmds
                        st.rerun()
        
        if "remediation_plan" in st.session_state:
            with st.expander("🛠️ Proposed Config", expanded=True):
                st.code(st.session_state.remediation_plan, language="cisco")
            
            c1, c2 = st.columns(2)
            with c1:
                if st.button("🚀 修復実行 (Execute)", type="primary"):
                    with st.status("Applying Fix...", expanded=True) as status:
                        time.sleep(1)
                        st.write("⚙️ Config pushed.")
                        time.sleep(1)
                        status.update(label="Restored!", state="complete")
                    st.balloons()
                    st.success("System Recovered.")
                    if st.button("デモをリセット"):
                        del st.session_state.remediation_plan
                        st.session_state.current_scenario = "正常稼働"
                        st.rerun()
            with c2:
                if st.button("キャンセル"):
                    del st.session_state.remediation_plan
                    st.rerun()
        st.markdown("---")

    # ---------------------------
    # AIチャット (Chat Interface)
    # ---------------------------
    # チャットセッションの初期化
    if st.session_state.chat_session is None and api_key and selected_scenario != "正常稼働":
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemma-3-12b-it")
        st.session_state.chat_session = model.start_chat(history=[])
        
        # 診断直後なら、初期分析コメントをAIに生成させる
        if st.session_state.live_result:
            initial_prompt = f"""
            状況報告を行ってください。
            シナリオ: {selected_scenario}
            診断ログ: {st.session_state.live_result.get('sanitized_log', 'N/A')}
            推論された原因: {top_cause_candidate['id'] if top_cause_candidate else '解析中'}
            """
            try:
                response = st.session_state.chat_session.send_message(initial_prompt)
                st.session_state.messages.append({"role": "assistant", "content": response.text})
            except:
                pass

    # チャット履歴の表示
    chat_container = st.container(height=400)
    with chat_container:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    # ユーザー入力
    if prompt := st.chat_input("AIエージェントに質問..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with chat_container:
            with st.chat_message("user"):
                st.markdown(prompt)
        
        if st.session_state.chat_session:
            with chat_container:
                with st.chat_message("assistant"):
                    with st.spinner("Thinking..."):
                        try:
                            response = st.session_state.chat_session.send_message(prompt)
                            st.markdown(response.text)
                            st.session_state.messages.append({"role": "assistant", "content": response.text})
                        except Exception as e:
                            st.error(f"Error: {e}")
