from pathlib import Path

content = r'''import json
from typing import List, Dict, Any, Tuple
import streamlit as st

# ==========================================================
# Dashboard 表示ロジック（v2）
# ==========================================================
# 追加要件:
# 1) 「自動修復が可能」表示条件を impact_type ベースにする
# 2) tier を折りたたみ表示（expander）順として使う
# ==========================================================

# ----------------------------
# Auto-remediation policy
# ----------------------------
# 安全側のデフォルト:
# - 物理対応が必要なものは「自動修復が可能」にしない
# - 上位障害の影響としての Unreachable は自動修復対象にしない
#
# ※将来、remediation_engine を実装する場合は、
#   ここを「提案可能」「承認後に実行可能」などに段階分けするのが推奨です。
AUTO_REMEDIATE_ALLOW_TYPES = {
    # 設定・ソフトウェア起因は自動化余地が大きい
    "Config/Software",
    "Software",
    "Config",
    # 冗長動作中（縮退）は自動切り戻し・状態確認の自動化余地がある
    "Hardware/Redundancy",
    "REDUNDANCY_LOST",
    "DEGRADED",
}

AUTO_REMEDIATE_DENY_TYPES = {
    # 物理交換・現地確認が必要になりやすい
    "Hardware/Physical",
    "Hardware/Critical_Multi_Fail",
    # 上位断のカスケードは「根本原因ではない」扱い
    "Network/Unreachable",
    # 明示的なサービス断・停止
    "OUTAGE",
    "DeviceDown",
    # AIエラー等は自動化できない
    "AI_ERROR",
    "UNKNOWN",
}


def should_show_auto_remediation(item: Dict[str, Any]) -> bool:
    impact_type = str(item.get("type", "UNKNOWN"))
    if impact_type in AUTO_REMEDIATE_DENY_TYPES:
        return False
    if impact_type in AUTO_REMEDIATE_ALLOW_TYPES:
        return True
    # 未知の type は安全側に倒す（表示しない）
    return False


# ----------------------------
# Severity classification
# ----------------------------
def classify_display_status(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    inference_engine の結果を UI 表示用に正規化する。

    ルール:
    - type == Network/Unreachable は根本原因扱いしない
    - prob > 0.85 かつ Unreachable 以外 → 根本原因
    - それ以外は 影響・注意・情報 として扱う
    """
    prob = float(item.get("prob", 0.0))
    impact_type = str(item.get("type", "UNKNOWN"))

    if impact_type == "Network/Unreachable":
        return {
            "severity": "⚫ 応答なし（上位障害の影響）",
            "color": "gray",
            "is_root": False,
        }

    if prob >= 0.85:
        return {
            "severity": "🔴 危険（根本原因）",
            "color": "red",
            "is_root": True,
        }

    if prob >= 0.6:
        return {
            "severity": "🟠 注意（影響あり）",
            "color": "orange",
            "is_root": False,
        }

    return {
        "severity": "🟡 情報",
        "color": "yellow",
        "is_root": False,
    }


def normalize_tier(item: Dict[str, Any]) -> int:
    """
    tier を必ず int にする。無い場合は 3。
    """
    try:
        return int(item.get("tier", 3))
    except Exception:
        return 3


def sort_key(item: Dict[str, Any]) -> Tuple[int, float]:
    """
    tier 昇順 → prob 降順
    """
    tier = normalize_tier(item)
    prob = float(item.get("prob", 0.0))
    return (tier, -prob)


# ----------------------------
# Rendering
# ----------------------------
def render_incident_table(results: List[Dict[str, Any]]):
    """
    AIOps インシデント・コックピット表示（tier で折りたたみ）
    """
    st.subheader("🧠 AIOps インシデント・コックピット")

    results = sorted(results, key=sort_key)

    # tier ごとにグルーピング
    tiers: Dict[int, List[Dict[str, Any]]] = {}
    for item in results:
        t = normalize_tier(item)
        tiers.setdefault(t, []).append(item)

    # tier の表示順（小さいほど上位）
    for tier in sorted(tiers.keys()):
        title = f"Tier {tier}（優先度 {'高' if tier == 1 else '中' if tier == 2 else '低'}）"
        expanded = True if tier == 1 else False

        with st.expander(title, expanded=expanded):
            items = tiers[tier]
            for idx, item in enumerate(items, start=1):
                ui = classify_display_status(item)
                auto_flag = "🚀 自動修復が可能" if should_show_auto_remediation(item) else "🧑 手動対応 / 承認が必要"

                st.markdown(
                    f"""**{idx}. {ui['severity']}**  
- デバイス: `{item.get('id')}`  
- 原因: `{item.get('label')}`  
- 確信度: `{item.get('prob')}`  
- 分類: `{item.get('type')}`  
- 理由: {item.get('reason')}  
- 対応: {auto_flag}
"""
                )
                st.markdown("---")


# ==========================================================
# Streamlit Entry Point
# ==========================================================
def main():
    st.title("🚦 AIOps Incident Dashboard")

    st.caption(
        """
        - 根本原因は 赤 (🔴) のみ
        - 上位障害に起因する Unreachable は 根本原因にしない
        - tier を折りたたみ表示順に利用
        - 自動修復表示は impact_type に基づき安全側で判定
        """
    )

    uploaded = st.file_uploader(
        "inference_engine の解析結果(JSON)をアップロードしてください",
        type=["json"]
    )

    if uploaded:
        try:
            results = json.load(uploaded)
            if not isinstance(results, list):
                st.error("JSON は配列形式である必要があります")
                return
            render_incident_table(results)
        except Exception as e:
            st.error(f"JSON 読み込みエラー: {e}")
    else:
        st.info("解析結果 JSON をアップロードすると表示されます。")


if __name__ == "__main__":
    main()
'''

path = Path("/mnt/data/dashboard_fixed_v2.py")
path.write_text(content, encoding="utf-8")
str(path), path.stat().st_size

