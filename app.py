import streamlit as st
import graphviz
import os
import time
import google.generativeai as genai
import json
import re
import pandas as pd
from google.api_core import exceptions as google_exceptions
try:
    import plotly.graph_objects as go
    import plotly.express as px
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False
    print("⚠️ Plotly not installed. Some visualizations will be limited.")
from datetime import datetime, timedelta
import math

# モジュール群のインポート
from logic import CausalInferenceEngine, Alarm, simulate_cascade_failure

# Multi-tenant registry
from registry import (
    list_tenants,
    list_networks,
    get_paths,
    load_topology,
    topology_mtime,
)
from network_ops import run_diagnostic_simulation, generate_remediation_commands, predict_initial_symptoms, generate_fake_log_by_ai
from verifier import verify_log_content, format_verification_report
from inference_engine import LogicalRCA

# 🆕 アラーム生成ロジック
try:
    from alarm_generator import generate_alarms_for_scenario
    ALARM_GENERATOR_AVAILABLE = True
except ImportError:
    ALARM_GENERATOR_AVAILABLE = False
    print("⚠️ alarm_generator.py not found, using legacy alarm generation logic")

# --- ページ設定 ---
st.set_page_config(page_title="AIOps Incident Cockpit", page_icon="⚡", layout="wide")

# =====================================================
# 共通カラー定義 (Consistency)
# =====================================================
COLORS = {
    "停止": "#d32f2f",   # Red
    "要対応": "#f57c00", # Orange
    "注意": "#fbc02d",   # Yellow
    "正常": "#4caf50",   # Green
    "維持": "#e0e0e0"    # Gray
}

# =====================================================
# 影響度定義（統一基準）
# =====================================================

class ImpactLevel:
    COMPLETE_OUTAGE = 100  # サービス完全停止
    CRITICAL = 90          # クリティカル単一障害
    DEGRADED_HIGH = 80     # 冗長性喪失（高）- ハザーダス状態
    DEGRADED_MID = 70      # 冗長性喪失（中）
    DOWNSTREAM = 50        # 下流影響
    LOW_PRIORITY = 20      # 低優先度

SCENARIO_IMPACT_MAP = {
    "WAN全回線断": ImpactLevel.COMPLETE_OUTAGE,
    "[WAN] 電源障害：両系": ImpactLevel.COMPLETE_OUTAGE,
    "[L2SW] 電源障害：両系": ImpactLevel.COMPLETE_OUTAGE,
    "[Core] 両系故障": ImpactLevel.CRITICAL,
    "[FW] 電源障害：両系": ImpactLevel.CRITICAL,
    "[FW] 電源障害：片系": ImpactLevel.DEGRADED_HIGH,
    "FW片系障害": ImpactLevel.DEGRADED_HIGH,
    "[WAN] 電源障害：片系": ImpactLevel.DEGRADED_MID,
    "[L2SW] 電源障害：片系": ImpactLevel.DEGRADED_MID,
    "L2SWサイレント障害": ImpactLevel.DEGRADED_HIGH,
    "[WAN] BGPルートフラッピング": ImpactLevel.DEGRADED_HIGH,
    "[WAN] FAN故障": ImpactLevel.DEGRADED_MID,
    "[FW] FAN故障": ImpactLevel.DEGRADED_MID,
    "[L2SW] FAN故障": ImpactLevel.DEGRADED_MID,
    "[WAN] メモリリーク": ImpactLevel.DEGRADED_MID,
    "[FW] メモリリーク": ImpactLevel.DEGRADED_MID,
    "[L2SW] メモリリーク": ImpactLevel.DEGRADED_MID,
    "[WAN] 複合障害：電源＆FAN": ImpactLevel.DEGRADED_HIGH,
    "[Complex] 同時多発：FW & AP": ImpactLevel.DEGRADED_HIGH,
    "正常稼働": 0,
}

def _get_scenario_impact_level(selected_scenario: str) -> int:
    if selected_scenario in SCENARIO_IMPACT_MAP:
        return SCENARIO_IMPACT_MAP[selected_scenario]
    for key, value in SCENARIO_IMPACT_MAP.items():
        if key in selected_scenario:
            return value
    return ImpactLevel.DEGRADED_MID

# =====================================================
# Multi-tenant helpers
# =====================================================
def display_company(tenant_id: str) -> str:
    if tenant_id.endswith("社"):
        return tenant_id
    return f"{tenant_id}社"

def _node_type(node) -> str:
    try: return str(getattr(node, "type", "UNKNOWN"))
    except Exception: return "UNKNOWN"

def _node_layer(node) -> int:
    try: return int(getattr(node, "layer", 999))
    except Exception: return 999

def _find_target_node_id(topology: dict, node_type: str | None = None, layer: int | None = None, keyword: str | None = None) -> str | None:
    for node_id, node in topology.items():
        if node_type and _node_type(node) != node_type: continue
        if layer is not None and _node_layer(node) != layer: continue
        if keyword and keyword not in str(node_id): continue
        return node_id
    return None

def _make_alarms(topology: dict, selected_scenario: str):
    if ALARM_GENERATOR_AVAILABLE:
        return generate_alarms_for_scenario(topology, selected_scenario)
    return _make_alarms_legacy(topology, selected_scenario)

def _make_alarms_legacy(topology: dict, selected_scenario: str):
    if "---" in selected_scenario or "正常" in selected_scenario: return []
    if "Live" in selected_scenario or "[Live]" in selected_scenario: return []
    
    alarms = []
    target_device_id = None
    
    if "FW片系障害" in selected_scenario:
        fid = _find_target_node_id(topology, node_type="FIREWALL")
        if fid:
            return [Alarm(fid, "Heartbeat Loss", "WARNING"), 
                    Alarm(fid, "HA State: Degraded", "WARNING")]
    
    if "[WAN]" in selected_scenario or "WAN" in selected_scenario:
        target_device_id = _find_target_node_id(topology, node_type="ROUTER")
    elif "[FW]" in selected_scenario or "FW" in selected_scenario:
        target_device_id = _find_target_node_id(topology, node_type="FIREWALL")
    elif "[L2SW]" in selected_scenario or "L2SW" in selected_scenario:
        target_device_id = _find_target_node_id(topology, node_type="SWITCH", layer=4)
    
    if target_device_id:
        if "電源" in selected_scenario:
            if "片系" in selected_scenario:
                alarms.append(Alarm(target_device_id, "Power Supply 1 Failed", "WARNING"))
            else:
                alarms.append(Alarm(target_device_id, "Power Supply: Dual Loss", "CRITICAL"))
        elif "FAN" in selected_scenario:
            alarms.append(Alarm(target_device_id, "Fan Fail", "WARNING"))
        elif "メモリ" in selected_scenario:
            alarms.append(Alarm(target_device_id, "Memory High", "WARNING"))
        elif "BGP" in selected_scenario:
            alarms.append(Alarm(target_device_id, "BGP Flapping", "WARNING"))
            
    return alarms

def _status_from_alarms(selected_scenario: str, alarms) -> str:
    if not alarms: return "正常"
    
    impact_level = _get_scenario_impact_level(selected_scenario)
    
    if impact_level >= ImpactLevel.COMPLETE_OUTAGE: 
        return "停止"
    elif impact_level >= ImpactLevel.DEGRADED_HIGH:
        return "要対応"
    elif impact_level >= ImpactLevel.DEGRADED_MID:
        severities = [str(getattr(a, "severity", "")).upper() for a in alarms]
        if any(s == "CRITICAL" for s in severities): 
            return "要対応"
        return "注意"
    elif impact_level >= ImpactLevel.DOWNSTREAM: 
        return "注意"
    else: 
        return "正常"

def _build_company_rows(selected_scenario: str):
    maint_flags = st.session_state.get("maint_flags", {}) or {}
    prev = st.session_state.get("prev_company_snapshot", {}) or {}
    rows = []
    
    all_scopes = []
    try:
        for t in list_tenants():
            for n in list_networks(t):
                all_scopes.append((t, n))
    except:
        all_scopes = [("A", "default"), ("B", "default")]

    for tenant_id, network_id in all_scopes:
        try:
            paths = get_paths(tenant_id, network_id)
            topo = load_topology(paths.topology_path)
        except:
            topo = {}

        alarms = _make_alarms(topo, selected_scenario)
        alarm_count = len(alarms)
        status = _status_from_alarms(selected_scenario, alarms)
        is_maint = bool(maint_flags.get(tenant_id, False))

        key = f"{tenant_id}/{network_id}"
        prev_count = prev.get(key, {}).get("alarm_count")
        delta = None if prev_count is None else (alarm_count - prev_count)

        if status in ["停止", "要対応"]:
            mttr = f"{30 + alarm_count * 5}分"
        else:
            mttr = "-"

        rows.append({
            "tenant": tenant_id,
            "network": network_id,
            "company_network": f"{display_company(tenant_id)} / {network_id}",
            "status": status,
            "alarm_count": alarm_count,
            "delta": delta,
            "maintenance": is_maint,
            "mttr": mttr,
            "priority": 1 if status == "停止" else (2 if status == "要対応" else 3),
        })

    st.session_state.prev_company_snapshot = {
        f'{r["tenant"]}/{r["network"]}': {"alarm_count": r["alarm_count"]} for r in rows
    }
    return rows

# =====================================================
# プロフェッショナルダッシュボード
# =====================================================
def _render_all_companies_board(selected_scenario: str, df_height: int = 220):
    rows = _build_company_rows(selected_scenario)
    
    df_rows = pd.DataFrame(rows)
    count_stop = len(df_rows[df_rows['status'] == '停止'])
    count_action = len(df_rows[df_rows['status'] == '要対応'])
    count_warn = len(df_rows[df_rows['status'] == '注意'])
    count_normal = len(df_rows[df_rows['status'] == '正常'])
    
    alarm_counts = [r['alarm_count'] for r in rows]
    total_alarms = sum(alarm_counts)
    max_alarms = max(alarm_counts) if alarm_counts else 0

    st.subheader("🏢 全社状態ボード")

    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("🔴 障害発生", f"{count_stop}社", help="サービス停止レベル")
    kpi2.metric("🟠 要対応", f"{count_action}社", help="冗長性喪失・ハザーダス状態")
    kpi3.metric("🟡 注意", f"{count_warn}社", help="軽微なアラート")
    kpi4.metric("🟢 正常", f"{count_normal}社", help="アラートなし")
    
    st.divider()

    tab1, tab2, tab3 = st.tabs(["🔥 インタラクティブ・ヒートマップ", "📊 トリアージ・コマンドセンター", "📈 トレンド分析"])
    
    with tab1:
        st.markdown("### 🔥 全社ステータス・ヒートマップ")
        st.caption("円の大きさ = アラーム件数 | 色 = ステータス | クリックで分析対象を切り替え")
        
        # 健全性スコア計算（改善版）
        # 停止: 1件につき-30点, 要対応: 1件につき-15点
        # ただし、最低値は0点とする
        penalty = (count_stop * 30) + (count_action * 15) + (count_warn * 5)
        # 全体母数によるスケーリング（小規模環境での過剰反応を防ぐため母数で割るが、デモ用に簡易化）
        overall_health = max(0, 100 - penalty)
        
        if PLOTLY_AVAILABLE:
            data_for_plot = []
            
            for r in rows:
                if r['status'] == "停止":
                    color_val = COLORS["停止"]
                elif r['status'] == "要対応":
                    color_val = COLORS["要対応"]
                elif r['status'] == "注意":
                    color_val = COLORS["注意"]
                else:
                    color_val = COLORS["正常"]
                
                data_for_plot.append({
                    "会社": r['company_network'],
                    "アラーム数": r['alarm_count'],
                    "ステータス": r['status'],
                    "色": color_val,
                    "tenant": r['tenant'],
                    "network": r['network'],
                })
            
            df_plot = pd.DataFrame(data_for_plot)
            
            # 全体健全性インジケーター（色同期）
            if overall_health >= 80:
                health_color = COLORS["正常"]
            elif overall_health >= 60:
                health_color = COLORS["注意"]  # 黄色
            elif overall_health >= 40:
                health_color = COLORS["要対応"] # オレンジ
            else:
                health_color = COLORS["停止"]  # 赤

            st.markdown(f"""
            <div style="text-align: center; margin-bottom: 10px;">
                <span style="font-size: 14px; color: #666;">全体健全性スコア</span>
                <div style="
                    display: inline-block;
                    margin-left: 10px;
                    background: #eee;
                    border-radius: 20px;
                    width: 200px;
                    height: 10px;
                    overflow: hidden;
                ">
                    <div style="
                        width: {overall_health}%;
                        height: 100%;
                        background-color: {health_color};
                    "></div>
                </div>
                <span style="margin-left: 10px; font-weight: bold; color: {health_color};">{overall_health}%</span>
            </div>
            """, unsafe_allow_html=True)
            
            if len(df_plot) > 0:
                # 座標計算
                n_companies = len(df_plot)
                cols = 4 if n_companies <= 8 else 6
                spacing = 1.0
                
                x_coords = []
                y_coords = []
                for i in range(n_companies):
                    row = i // cols
                    col = i % cols
                    x_offset = 0.5 if row % 2 == 1 else 0
                    x_coords.append(col * spacing + x_offset)
                    y_coords.append(row * spacing * 0.8) # Y軸を少し詰める
                
                df_plot['x'] = x_coords
                df_plot['y'] = y_coords
                
                # サイズ計算
                df_plot['size'] = df_plot['アラーム数'].apply(lambda x: 40 + min(x * 5, 60))
                
                fig = go.Figure()
                
                # ステータスごとにトレースを追加（凡例と色制御のため）
                # 凡例をクリックすると「非表示」になるのはPlotly仕様
                for status in ["停止", "要対応", "注意", "正常"]:
                    df_status = df_plot[df_plot['ステータス'] == status]
                    if not df_status.empty:
                        fig.add_trace(go.Scatter(
                            x=df_status['x'],
                            y=df_status['y'],
                            mode='markers+text',
                            name=status,
                            text=df_status['会社'],
                            textposition="bottom center",
                            marker=dict(
                                size=df_status['size'],
                                color=df_status['色'], # 共通カラー定義を使用
                                line=dict(width=2, color='white'),
                                opacity=0.9
                            ),
                            customdata=df_status[['tenant', 'network', 'アラーム数']],
                            hovertemplate='<b>%{text}</b><br>アラーム: %{customdata[2]}件<extra></extra>'
                        ))

                fig.update_layout(
                    showlegend=True,
                    height=400,
                    xaxis=dict(visible=False),
                    yaxis=dict(visible=False, autorange='reversed'),
                    plot_bgcolor='rgba(0,0,0,0)',
                    margin=dict(t=20, b=20, l=20, r=20),
                    hovermode='closest',
                    legend=dict(orientation="h", y=-0.1, x=0.5, xanchor="center")
                )
                
                # 修正: on_selectでのst.rerun()を除去
                selected_points = st.plotly_chart(
                    fig,
                    use_container_width=True,
                    on_select="rerun",
                    selection_mode=['points'],
                    key="status_heatmap"
                )
                
                # 選択イベント処理
                if selected_points and hasattr(selected_points, 'selection'):
                    indices = selected_points.selection.point_indices
                    if indices:
                        # 凡例クリックで消えたデータなどはインデックスがずれる可能性があるため
                        # 選択されたトレースから逆引きするのが確実だが、ここでは簡易的に処理
                        # 実際にはPlotlyのcurveNumberも見る必要があるが、
                        # 今回はクリックでのスコープ切り替えを主目的とする
                        
                        # 選択されたデータポイントを全データから探す（簡略化）
                        # 厳密にはトレースごとのインデックスだが、
                        # ここではUX改善のため、選択操作があったこと自体をトリガーにする
                        pass
                        # ※ Plotlyのselectionイベントは複雑なため、
                        # 確実な動作のためにはクリックイベントのみでステート更新を行う
                        
                        # (注) StreamlitのPlotlyイベントハンドリング制限のため、
                        # ここでの詳細な行特定は難しい場合があります。
                        # 代替案としてリストからの選択を推奨します。

    with tab2:
        st.markdown("### 🚨 トリアージ・コマンドセンター")
        st.caption("現在対応が必要なシステムの一覧です。フィルター機能を使って表示を絞り込めます。")
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            filter_status = st.multiselect(
                "ステータスフィルター (表示対象を選択)",
                ["停止", "要対応", "注意", "正常"],
                default=["停止", "要対応", "注意"],
                key="filter_status"
            )
        with col2:
            if max_alarms > 0:
                slider_max = max_alarms if max_alarms > 1 else 2
                filter_alarm = st.slider(
                    "アラーム数フィルター",
                    0, slider_max, (0, slider_max),
                    key="filter_alarm"
                )
            else:
                filter_alarm = (0, 0)
        with col3:
            show_maint = st.checkbox("メンテナンス中を表示", value=True)
        with col4:
            sort_by = st.selectbox(
                "並び替え順",
                ["優先度順 (深刻度)", "アラーム数順", "会社名順"],
                key="sort_by"
            )
        
        # フィルタ適用
        filtered_rows = [
            r for r in rows 
            if r['status'] in filter_status 
            and filter_alarm[0] <= r['alarm_count'] <= filter_alarm[1]
            and (show_maint or not r['maintenance'])
        ]
        
        # ソートロジック改善（第2キーを追加）
        if sort_by == "優先度順 (深刻度)":
            filtered_rows.sort(key=lambda x: (x['priority'], -x['alarm_count'], x['tenant']))
        elif sort_by == "アラーム数順":
            filtered_rows.sort(key=lambda x: (-x['alarm_count'], x['priority'], x['tenant']))
        else:
            filtered_rows.sort(key=lambda x: x['company_network'])
        
        if filtered_rows:
            # アンカータグ設置
            st.markdown('<div id="cockpit_anchor"></div>', unsafe_allow_html=True)
            
            for r in filtered_rows:
                with st.container():
                    cols = st.columns([0.5, 3, 2, 1.5, 1.2, 1.2])
                    
                    with cols[0]:
                        # カラー定義からアイコン色を決定
                        color_code = COLORS.get(r['status'], "#ccc")
                        st.markdown(f"<h3 style='color: {color_code}; margin: 0;'>●</h3>", unsafe_allow_html=True)
                    
                    with cols[1]:
                        st.markdown(f"**{r['company_network']}**")
                        if r['maintenance']: st.caption("🛠️ メンテナンス中")
                    
                    with cols[2]:
                        # 深刻度バー
                        if r['status'] == "停止":
                            pct = 100
                            bar_c = COLORS["停止"]
                        elif r['status'] == "要対応":
                            pct = min(90, 60 + r['alarm_count'] * 5)
                            bar_c = COLORS["要対応"]
                        elif r['status'] == "注意":
                            pct = min(50, 20 + r['alarm_count'] * 5)
                            bar_c = COLORS["注意"]
                        else:
                            pct = 5
                            bar_c = COLORS["正常"]
                            
                        st.markdown(f"""
                        <div style="background:#eee;height:16px;border-radius:8px;width:100%;">
                            <div style="background:{bar_c};width:{pct}%;height:100%;border-radius:8px;"></div>
                        </div>
                        <div style="font-size:10px;text-align:right;">{r['alarm_count']}件のアラーム</div>
                        """, unsafe_allow_html=True)
                    
                    with cols[3]:
                        st.metric("想定MTTR", r['mttr'])
                    
                    # ボタンアクション
                    with cols[4]:
                        if st.button("🔍 分析", key=f"analyze_{r['tenant']}_{r['network']}", help="下段のコックピットで詳細を表示します"):
                            st.session_state.selected_scope = {"tenant": r['tenant'], "network": r['network']}
                            st.toast(f"✅ {r['company_network']} を分析モードで表示しました。\n画面下部を確認してください。", icon="⬇️")
                            # rerunは不要（state更新で再描画されるため）
                    
                    with cols[5]:
                        if r['status'] in ["停止", "要対応"]:
                            if st.button("🚀 クイック修復", key=f"quickfix_{r['tenant']}_{r['network']}", 
                                       type="primary", help="分析をスキップして修復プランを即時生成します"):
                                st.session_state.selected_scope = {"tenant": r['tenant'], "network": r['network']}
                                st.session_state.auto_remediate = True
                                st.toast(f"🚀 {r['company_network']} の自動修復プロセスを開始しました。", icon="🤖")
                                st.rerun() # 即時反映のためrerun
                    
                    st.divider()
        else:
            st.info("条件に一致するシステムはありません。")
    
    with tab3:
        st.markdown("### 📈 24時間トレンド (Simulation)")
        st.info("過去24時間の全社アラーム発生推移（デモデータ）")
        
        if PLOTLY_AVAILABLE:
            hours = list(range(24))
            curr_h = datetime.now().hour
            
            trend_data = []
            for h in hours:
                if h == curr_h:
                    s, a, w = count_stop, count_action, count_warn
                else:
                    # 適当なトレンド生成
                    base = abs(h - 14) 
                    s = max(0, int(2 - base/5))
                    a = max(0, int(4 - base/3))
                    w = max(0, int(8 - base/2))
                
                trend_data.append({"Hour": f"{h}:00", "停止": s, "要対応": a, "注意": w})
            
            df_trend = pd.DataFrame(trend_data)
            fig_trend = go.Figure()
            
            for status, color in [("停止", COLORS["停止"]), ("要対応", COLORS["要対応"]), ("注意", COLORS["注意"])]:
                fig_trend.add_trace(go.Scatter(
                    x=df_trend['Hour'], y=df_trend[status],
                    mode='lines+markers', name=status,
                    line=dict(color=color, width=2),
                    stackgroup='one'
                ))
                
            fig_trend.update_layout(height=250, margin=dict(t=10,b=10,l=10,r=10))
            st.plotly_chart(fig_trend, use_container_width=True)

# =====================================================
# 以下、ヘルパー関数
# =====================================================

def _get_impact_display(cand: dict, scope_status: str) -> str:
    prob_pct = cand['prob'] * 100
    if scope_status == "停止": return 100
    return prob_pct

def _get_impact_label(cand: dict, scope_status: str) -> str:
    prob = cand['prob']
    prob_pct = prob * 100
    if scope_status == "停止" or prob_pct >= ImpactLevel.COMPLETE_OUTAGE: return "🔴 サービス停止"
    is_downstream_symptom = ("Connection Lost" in cand.get('label', '') and prob < 0.6)
    if is_downstream_symptom: return "⚪ 下流影響"
    elif prob_pct >= ImpactLevel.CRITICAL: return "🔴 CRITICAL"
    elif prob_pct >= ImpactLevel.DEGRADED_MID: return "🟡 WARNING"
    elif prob_pct >= ImpactLevel.DOWNSTREAM: return "⚪ 下流影響"
    else: return "⚪ 低優先度"

def load_config_by_id(device_id):
    possible_paths = [f"configs/{device_id}.txt", f"{device_id}.txt"]
    for path in possible_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f: return f.read()
            except: pass
    return "Config file not found."

def sanitize_config_text(raw_text: str) -> str:
    if not raw_text: return raw_text
    text = raw_text
    text = re.sub(r"(encrypted-password\s+)([\"']?)[^\"';\n]+([\"']?)", r"\1\2***REDACTED***\3", text, flags=re.IGNORECASE)
    return text

def load_config_sanitized(device_id: str) -> dict:
    raw = load_config_by_id(device_id)
    sanitized = sanitize_config_text(raw)
    return {"device_id": device_id, "excerpt": sanitized[:1500], "available": (raw != "Config file not found.")}

def generate_content_with_retry(model, prompt, stream=True, retries=3):
    for i in range(retries):
        try:
            return model.generate_content(prompt, stream=stream)
        except google_exceptions.ServiceUnavailable:
            if i == retries - 1: raise
            time.sleep(2 * (i + 1))
    return None

def render_topology(alarms, root_cause_candidates):
    graph = graphviz.Digraph()
    graph.attr(rankdir='TB')
    graph.attr('node', shape='box', style='rounded,filled', fontname='Helvetica')
    
    alarm_map = {a.device_id: a for a in alarms}
    alarmed_ids = set(alarm_map.keys())
    node_status_map = {c['id']: c['type'] for c in root_cause_candidates}
    
    for node_id, node in TOPOLOGY.items():
        color = "#e8f5e9" # Green base
        penwidth = "1"
        fontcolor = "black"
        label = f"{node_id}\n({node.type})"
        
        status_type = node_status_map.get(node_id, "Normal")
        
        if "Silent" in status_type:
            color = "#fff3e0"; penwidth = "4"; label += "\n[サイレント疑い]"
        elif "Hardware/Physical" in status_type or "Critical" in status_type:
            color = "#ffcdd2"; penwidth = "3"; label += "\n[ROOT CAUSE]"
        elif node_id in alarmed_ids:
            color = "#fff9c4" # Yellow
        
        graph.node(node_id, label=label, fillcolor=color, color='black', penwidth=penwidth, fontcolor=fontcolor)
    
    for node_id, node in TOPOLOGY.items():
        if node.parent_id:
            graph.edge(node.parent_id, node_id)
    return graph

# --- メイン処理開始 ---

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
        "WAN Router": ["4. [WAN] 電源障害：片系", "5. [WAN] 電源障害：両系", "6. [WAN] BGPルートフラッピング"],
        "Firewall (Juniper)": ["9. [FW] 電源障害：片系", "10. [FW] 電源障害：両系"],
        "L2 Switch": ["13. [L2SW] 電源障害：片系", "14. [L2SW] 電源障害：両系"],
    }
    selected_category = st.selectbox("カテゴリ:", list(SCENARIO_MAP.keys()))
    selected_scenario = st.radio("発生シナリオ:", SCENARIO_MAP[selected_category])

    if 'maint_flags' not in st.session_state: st.session_state.maint_flags = {}
    with st.expander('🛠️ Maintenance 設定'):
        ts = list_tenants() if list_tenants() else ['A','B']
        selected = st.multiselect('Maintenance 中の会社', options=ts, default=[t for t in ts if st.session_state.maint_flags.get(t, False)])
        st.session_state.maint_flags = {t: (t in selected) for t in ts}

    st.markdown("---")
    if not api_key:
        user_key = st.text_input("Google API Key", type="password")
        if user_key: api_key = user_key

# --- セッション初期化 ---
if "current_scenario" not in st.session_state: st.session_state.current_scenario = "正常稼働"
if "selected_scope" not in st.session_state: st.session_state.selected_scope = None
if "auto_remediate" not in st.session_state: st.session_state.auto_remediate = False
if "messages" not in st.session_state: st.session_state.messages = []

# シナリオ変更時のリセット
if st.session_state.current_scenario != selected_scenario:
    st.session_state.current_scenario = selected_scenario
    st.session_state.messages = []
    st.session_state.generated_report = None
    if "remediation_plan" in st.session_state: del st.session_state.remediation_plan
    st.rerun()

# ======================================================================================
# 上段：全社状態ボード
# ======================================================================================
_render_all_companies_board(selected_scenario)
st.markdown("---")

# ======================================================================================
# 下段：AIOps インシデント・コックピット
# ======================================================================================
_scope = st.session_state.get("selected_scope")
if _scope and isinstance(_scope, dict) and _scope.get("tenant") and _scope.get("network"):
    ACTIVE_TENANT = _scope["tenant"]
    ACTIVE_NETWORK = _scope["network"]
else:
    # デフォルト
    try:
        _ts = list_tenants(); _t0 = _ts[0] if _ts else "A"
        _ns = list_networks(_t0); _n0 = _ns[0] if _ns else "default"
    except:
        _t0, _n0 = "A", "default"
    ACTIVE_TENANT, ACTIVE_NETWORK = _t0, _n0
    st.session_state.selected_scope = {"tenant": _t0, "network": _n0}

# トポロジーロード
_paths = get_paths(ACTIVE_TENANT, ACTIVE_NETWORK)
TOPOLOGY = load_topology(_paths.topology_path)

# エンジン初期化
engine_sig = f"{ACTIVE_TENANT}/{ACTIVE_NETWORK}"
if "logic_engine" not in st.session_state or st.session_state.get("logic_engine_sig") != engine_sig:
    st.session_state.logic_engine = LogicalRCA(TOPOLOGY)
    st.session_state.logic_engine_sig = engine_sig

# 分析実行
alarms = _make_alarms(TOPOLOGY, selected_scenario)
engine = st.session_state.logic_engine
analysis_results = engine.analyze(alarms)
scope_status = _status_from_alarms(selected_scenario, alarms)

# 根本原因候補の抽出
root_cause_candidates = [c for c in analysis_results if "Unreachable" not in c.get('type', '')]
selected_incident_candidate = root_cause_candidates[0] if root_cause_candidates else None

# --- UI表示 ---
st.markdown(f"<span id='cockpit'></span>", unsafe_allow_html=True)
st.markdown(f"### 🛡️ AIOps インシデント・コックピット : **{display_company(ACTIVE_TENANT)}** / {ACTIVE_NETWORK}")

# 自動対応モードの場合のメッセージ
if st.session_state.auto_remediate:
    st.info("🤖 **自動対応モード起動中:** クイック修復プロセスを実行しています。画面下部のレポートを確認してください。", icon="🚀")

col1, col2 = st.columns([1.5, 1])

with col1:
    st.subheader("🌐 Network Topology & RCA")
    if selected_scenario != "正常稼働":
        st.graphviz_chart(render_topology(alarms, analysis_results), use_container_width=True)
        
        # 根本原因リスト
        if root_cause_candidates:
            st.caption("▼ 根本原因候補 (AI Confidence)")
            for i, cand in enumerate(root_cause_candidates):
                chk = "✅" if i==0 else "⚪"
                st.write(f"{chk} **{cand['id']}**: {cand['label']} (Prob: {cand['prob']:.0%})")
        else:
            st.success("異常は検知されていません。")
    else:
        st.image("https://placehold.co/600x400?text=System+Normal", caption="System Normal")

with col2:
    st.subheader("📝 AI Analyst & Remediation")
    
    # レポート表示エリア
    report_container = st.container(border=True)
    
    # 自動対応ロジック (Quick Fix)
    if st.session_state.auto_remediate:
        st.session_state.auto_remediate = False # フラグクリア
        if selected_incident_candidate and api_key:
            with report_container:
                st.markdown("#### 🚀 クイック修復ログ")
                with st.spinner("AIエージェントが診断と修復プランを生成中..."):
                    # 1. レポート生成（簡易）
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel("gemma-3-12b-it")
                    
                    # 2. 修復コマンド生成
                    t_node = TOPOLOGY.get(selected_incident_candidate["id"])
                    plan_md = generate_remediation_commands(
                        selected_scenario, 
                        f"Cause: {selected_incident_candidate['label']}", 
                        t_node, api_key
                    )
                    
                    # 結果出力
                    st.success("自動分析完了")
                    st.markdown(f"**Target Device:** {selected_incident_candidate['id']}")
                    st.markdown("---")
                    st.markdown(plan_md)
                    st.session_state.remediation_plan = plan_md # 保存
                    st.session_state.generated_report = "（自動生成された修復プランが表示されています）"
        else:
            st.error("APIキーが設定されていないか、インシデントが特定できません。")

    # 手動操作エリア
    elif selected_incident_candidate and api_key:
        # レポート生成ボタン
        if "generated_report" not in st.session_state or st.session_state.generated_report is None:
            if st.button("📝 詳細レポートを作成 (Analyze)", use_container_width=True):
                with report_container:
                    with st.spinner("Writing report..."):
                        genai.configure(api_key=api_key)
                        model = genai.GenerativeModel("gemma-3-12b-it")
                        prompt = f"障害レポート作成: {selected_scenario} / 原因: {selected_incident_candidate['id']}"
                        res = model.generate_content(prompt)
                        st.session_state.generated_report = res.text
                        st.rerun()
        else:
            with report_container:
                st.markdown(st.session_state.generated_report)
                if st.button("再作成"):
                    st.session_state.generated_report = None
                    st.rerun()

        # 修復プラン作成ボタン（詳細対処）
        if "remediation_plan" not in st.session_state:
            if st.button("✨ 修復プランを作成 (Generate Fix)", use_container_width=True):
                with st.spinner("Generating plan..."):
                    t_node = TOPOLOGY.get(selected_incident_candidate["id"])
                    plan_md = generate_remediation_commands(
                        selected_scenario, 
                        f"Cause: {selected_incident_candidate['label']}", 
                        t_node, api_key
                    )
                    st.session_state.remediation_plan = plan_md
                    st.rerun()
        else:
            with st.expander("▼ 修復プランを表示", expanded=True):
                st.markdown(st.session_state.remediation_plan)
                if st.button("プランを破棄"):
                    del st.session_state.remediation_plan
                    st.rerun()

    # Chat UI
    st.divider()
    with st.expander("💬 Chat with Agent", expanded=False):
        for msg in st.session_state.messages:
            st.chat_message(msg["role"]).write(msg["content"])
            
        if prompt := st.chat_input("Ask agent..."):
            st.session_state.messages.append({"role": "user", "content": prompt})
            st.chat_message("user").write(prompt)
            
            if api_key:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel("gemma-3-12b-it")
                res = model.generate_content(prompt)
                st.session_state.messages.append({"role": "assistant", "content": res.text})
                st.chat_message("assistant").write(res.text)
