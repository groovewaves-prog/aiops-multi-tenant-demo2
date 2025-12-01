"""
Google Antigravity AIOps Agent - ロジックモジュール
根本原因分析のための因果推論エンジンに加え、
デモ用のアラーム自動生成（シミュレーター）機能を提供します。
"""

from typing import List, Dict, Set, Optional
from dataclasses import dataclass
from data import TOPOLOGY, NetworkNode

@dataclass
class Alarm:
    device_id: str
    message: str
    severity: str # CRITICAL, WARNING, INFO

@dataclass
class InferenceResult:
    root_cause_node: Optional[NetworkNode]
    root_cause_reason: str
    sop_key: str
    related_alarms: List[Alarm]

class CausalInferenceEngine:
    def __init__(self, topology: Dict[str, NetworkNode]):
        self.topology = topology

    def analyze_alarms(self, alarms: List[Alarm]) -> InferenceResult:
        # (分析ロジックは変更なし - そのまま使用)
        alarmed_device_ids = {a.device_id for a in alarms}
        sorted_alarms = sorted(
            alarms, 
            key=lambda a: self.topology[a.device_id].layer if a.device_id in self.topology else 999
        )
        
        if not sorted_alarms:
            return InferenceResult(None, "アラームなし", "DEFAULT", [])

        top_alarm = sorted_alarms[0]
        top_node = self.topology.get(top_alarm.device_id)
        
        if not top_node:
             return InferenceResult(None, "不明なデバイス", "DEFAULT", alarms)

        # 冗長性ルール
        if top_node.redundancy_group:
            return self._analyze_redundancy(top_node, alarmed_device_ids, alarms)

        # サイレント障害推論
        if top_node.parent_id:
            silent_failure_result = self._check_silent_failure_for_parent(top_node.parent_id, alarmed_device_ids)
            if silent_failure_result:
                return silent_failure_result

        # デフォルト: 階層ルール
        return InferenceResult(
            root_cause_node=top_node,
            root_cause_reason=f"階層ルール: 最上位レイヤーのデバイス {top_node.id} がダウンしています。",
            sop_key="HIERARCHY_FAILURE",
            related_alarms=alarms
        )

    def _analyze_redundancy(self, node: NetworkNode, alarmed_ids: Set[str], alarms: List[Alarm]) -> InferenceResult:
        group_members = [n for n in self.topology.values() if n.redundancy_group == node.redundancy_group]
        down_members = [n for n in group_members if n.id in alarmed_ids]
        
        if len(down_members) == len(group_members):
            return InferenceResult(
                root_cause_node=node,
                root_cause_reason=f"冗長性ルール: HAグループ {node.redundancy_group} の全メンバーがダウンしています。",
                sop_key="HA_TOTAL_FAILURE",
                related_alarms=alarms
            )
        else:
            return InferenceResult(
                root_cause_node=node,
                root_cause_reason=f"冗長性ルール: HAグループ {node.redundancy_group} で単一ノード障害が発生しました。フェイルオーバーは有効です。",
                sop_key="HA_PARTIAL_FAILURE",
                related_alarms=alarms
            )

    def _check_silent_failure_for_parent(self, parent_id: str, alarmed_ids: Set[str]) -> Optional[InferenceResult]:
        parent_node = self.topology.get(parent_id)
        if not parent_node:
            return None
            
        children = [n for n in self.topology.values() if n.parent_id == parent_id]
        children_down_count = sum(1 for child in children if child.id in alarmed_ids)
        
        if len(children) > 0 and children_down_count == len(children):
             return InferenceResult(
                root_cause_node=parent_node,
                root_cause_reason=f"サイレント障害推論: 親デバイス {parent_id} は沈黙していますが、配下の子デバイスが全滅しています。",
                sop_key="SILENT_FAILURE",
                related_alarms=[] 
            )
        return None

# ★★★ 追加機能: 障害波及シミュレーター ★★★
def simulate_cascade_failure(root_cause_id: str, topology: Dict[str, NetworkNode]) -> List[Alarm]:
    """
    指定された機器がダウンした場合、その配下にある全機器のアラームを自動生成する。
    これにより、手動でアラームリストを管理する必要がなくなる。
    """
    generated_alarms = []
    
    # 1. 根本原因機器のアラーム
    generated_alarms.append(Alarm(root_cause_id, "Interface Down", "CRITICAL"))
    
    # 2. 配下機器の探索 (幅優先探索 BFS)
    queue = [root_cause_id]
    processed = {root_cause_id}
    
    while queue:
        current_parent_id = queue.pop(0)
        
        # 親IDが一致する子機器を探す
        children = [
            node for node in topology.values() 
            if node.parent_id == current_parent_id
        ]
        
        for child in children:
            if child.id not in processed:
                # 子機器のアラーム生成
                # (親が死んでいるので到達不能アラームが出る想定)
                generated_alarms.append(Alarm(child.id, "Unreachable", "WARNING"))
                
                # さらにその子を探すためにキューに追加
                queue.append(child.id)
                processed.add(child.id)
                
    return generated_alarms
