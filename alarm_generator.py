# -*- coding: utf-8 -*-
"""
alarm_generator.py - アラーム生成ロジック（app.pyから抽出）

【目的】
app.pyの590-650行にある150行のif-elif分岐を独立したモジュールとして抽出。
UIコードと業務ロジックを分離し、保守性を向上させる。

【設計】
- app.pyからの呼び出しインターフェースは完全互換
- 内部実装は改善可能（将来的にAI化も可能）
- トポロジーと既存のlogic.pyに依存
"""

from typing import List, Dict, Any, Optional
from logic import Alarm, simulate_cascade_failure


# ========================================
# アラーム生成の統一インターフェース
# ========================================

def generate_alarms_for_scenario(
    topology: Dict[str, Any],
    selected_scenario: str
) -> List[Alarm]:
    """
    シナリオ名からアラームを生成
    
    【app.pyからの移行】
    app.pyの590-650行の分岐ロジックをそのまま移植。
    外部からは同じように見えるが、コードが整理されている。
    
    Args:
        topology: ネットワークトポロジー辞書（data.TOPOLOGY）
        selected_scenario: シナリオ名文字列（例: "WAN全回線断"）
    
    Returns:
        List[Alarm]: 生成されたアラームのリスト
    """
    
    # 正常稼働・スキップ系
    if "---" in selected_scenario or "正常" in selected_scenario:
        return []
    
    # Live実機診断（アラーム生成なし）
    if "Live" in selected_scenario or "[Live]" in selected_scenario:
        return []
    
    # ========================================
    # 基本・広域障害
    # ========================================
    
    # 1. WAN全回線断
    if "WAN全回線断" in selected_scenario:
        target = _find_target_node_id(topology, node_type="ROUTER")
        if target:
            return simulate_cascade_failure(target, topology)
        return []
    
    # 2. FW片系障害
    if "FW片系障害" in selected_scenario:
        target = _find_target_node_id(topology, node_type="FIREWALL")
        if target:
            return [Alarm(target, "Heartbeat Loss", "WARNING")]
        return []
    
    # 3. L2SWサイレント障害
    if "L2SWサイレント障害" in selected_scenario:
        target = _find_target_node_id(topology, node_type="SWITCH", layer=4, keyword="L2")
        if not target:
            target = _find_target_node_id(topology, keyword="L2_SW")
        if not target:
            target = _find_target_node_id(topology, node_type="SWITCH")
        
        if target and target in topology:
            # 直下の子ノードを探す
            children = [
                nid for nid, n in topology.items()
                if _get_parent_id(n) == target
            ]
            
            # 子が見つからない場合はAPを探す
            if not children:
                children = [
                    nid for nid, n in topology.items()
                    if _get_node_type(n).upper() in ("ACCESS_POINT", "AP")
                ]
            
            if children:
                return [Alarm(child, "Connection Lost", "CRITICAL") for child in children[:4]]
            
            # 最終フォールバック
            return [Alarm(target, "Silent Degradation Suspected", "WARNING")]
        
        return []
    
    # ========================================
    # 複合・同時多発
    # ========================================
    
    # 17. 複合障害：電源＆FAN
    if "複合障害" in selected_scenario:
        target = _find_target_node_id(topology, node_type="ROUTER")
        if target:
            return [
                Alarm(target, "Power Supply 1 Failed", "CRITICAL"),
                Alarm(target, "Fan Fail", "WARNING")
            ]
        return []
    
    # 18. 同時多発：FW & AP
    if "同時多発" in selected_scenario:
        alarms = []
        fw = _find_target_node_id(topology, node_type="FIREWALL")
        ap = _find_target_node_id(topology, node_type="ACCESS_POINT")
        
        if fw:
            alarms.append(Alarm(fw, "Heartbeat Loss", "WARNING"))
        if ap:
            alarms.append(Alarm(ap, "Connection Lost", "CRITICAL"))
        
        return alarms
    
    # ========================================
    # デバイス種別特定型シナリオ
    # ========================================
    
    # デバイスタイプを判定
    target_device_id = None
    
    if "[WAN]" in selected_scenario:
        target_device_id = _find_target_node_id(topology, node_type="ROUTER")
    elif "[FW]" in selected_scenario:
        target_device_id = _find_target_node_id(topology, node_type="FIREWALL")
    elif "[L2SW]" in selected_scenario:
        target_device_id = _find_target_node_id(topology, node_type="SWITCH", layer=4)
    
    if not target_device_id:
        return []
    
    # 障害種別を判定
    
    # 電源障害：片系
    if "電源障害：片系" in selected_scenario:
        return [Alarm(target_device_id, "Power Supply 1 Failed", "WARNING")]
    
    # 電源障害：両系
    if "電源障害：両系" in selected_scenario:
        if "FW" in str(target_device_id):
            return [Alarm(target_device_id, "Power Supply: Dual Loss (Device Down)", "CRITICAL")]
        return simulate_cascade_failure(target_device_id, topology, "Power Supply: Dual Loss (Device Down)")
    
    # BGPルートフラッピング
    if "BGP" in selected_scenario:
        return [Alarm(target_device_id, "BGP Flapping", "WARNING")]
    
    # FAN故障
    if "FAN" in selected_scenario:
        return [Alarm(target_device_id, "Fan Fail", "WARNING")]
    
    # メモリリーク
    if "メモリ" in selected_scenario:
        return [Alarm(target_device_id, "Memory High", "WARNING")]
    
    # マッチしない場合
    return []


# ========================================
# ヘルパー関数（内部使用）
# ========================================

def _find_target_node_id(
    topology: Dict[str, Any],
    node_type: Optional[str] = None,
    layer: Optional[int] = None,
    keyword: Optional[str] = None
) -> Optional[str]:
    """
    トポロジーから条件に合うノードIDを検索
    
    Args:
        topology: トポロジー辞書
        node_type: ノードタイプ（例: "ROUTER", "FIREWALL"）
        layer: レイヤー番号（例: 1, 2, 3）
        keyword: デバイスID部分一致検索（例: "L2", "WAN"）
    
    Returns:
        マッチしたノードID、見つからない場合はNone
    """
    for node_id, node in topology.items():
        # ノードタイプチェック
        if node_type:
            n_type = _get_node_type(node)
            if n_type != node_type:
                continue
        
        # レイヤーチェック
        if layer is not None:
            n_layer = _get_node_layer(node)
            if n_layer != layer:
                continue
        
        # キーワードチェック
        if keyword:
            # デバイスIDに含まれるか
            if keyword in node_id:
                return node_id
            
            # メタデータに含まれるか
            metadata = _get_node_metadata(node)
            if any(keyword in str(v) for v in metadata.values()):
                return node_id
            
            # マッチしない場合は次へ
            continue
        
        # すべての条件を満たした
        return node_id
    
    return None


def _get_node_type(node) -> str:
    """ノードからタイプを取得（dict/objectの両対応）"""
    if isinstance(node, dict):
        return node.get("type", "UNKNOWN")
    if hasattr(node, "type"):
        return getattr(node, "type", "UNKNOWN")
    return "UNKNOWN"


def _get_node_layer(node) -> int:
    """ノードからレイヤーを取得（dict/objectの両対応）"""
    if isinstance(node, dict):
        return node.get("layer", 999)
    if hasattr(node, "layer"):
        return getattr(node, "layer", 999)
    return 999


def _get_parent_id(node) -> Optional[str]:
    """ノードから親IDを取得（dict/objectの両対応）"""
    if isinstance(node, dict):
        return node.get("parent_id")
    if hasattr(node, "parent_id"):
        return getattr(node, "parent_id")
    return None


def _get_node_metadata(node) -> Dict[str, Any]:
    """ノードからメタデータを取得（dict/objectの両対応）"""
    if isinstance(node, dict):
        return node.get("metadata", {})
    if hasattr(node, "metadata"):
        md = getattr(node, "metadata", {})
        return md if isinstance(md, dict) else {}
    return {}


# ========================================
# テスト用コード（モジュール単独実行時）
# ========================================

if __name__ == "__main__":
    print("=" * 80)
    print("alarm_generator.py - Test Mode")
    print("=" * 80)
    
    # モックトポロジー
    mock_topology = {
        "WAN_ROUTER_01": {
            "type": "ROUTER",
            "layer": 1,
            "parent_id": None,
            "metadata": {"vendor": "Cisco"}
        },
        "FW_01_PRIMARY": {
            "type": "FIREWALL",
            "layer": 2,
            "parent_id": "WAN_ROUTER_01",
            "metadata": {"role": "Active"}
        },
        "L2_SW_01": {
            "type": "SWITCH",
            "layer": 4,
            "parent_id": "CORE_SW_01",
            "metadata": {"location": "Floor 1"}
        },
        "AP_01": {
            "type": "ACCESS_POINT",
            "layer": 5,
            "parent_id": "L2_SW_01",
            "metadata": {}
        }
    }
    
    # テストシナリオ
    test_scenarios = [
        "WAN全回線断",
        "FW片系障害",
        "L2SWサイレント障害",
        "[WAN] 電源障害：片系",
        "[FW] BGPルートフラッピング",
        "複合障害",
        "正常稼働"
    ]
    
    print("\n📋 Testing alarm generation for various scenarios:\n")
    
    for scenario in test_scenarios:
        alarms = generate_alarms_for_scenario(mock_topology, scenario)
        print(f"Scenario: {scenario}")
        print(f"  Generated alarms: {len(alarms)}")
        for alarm in alarms:
            print(f"    - {alarm.device_id}: {alarm.message} ({alarm.severity})")
        print()
    
    print("✅ alarm_generator.py test completed!")
