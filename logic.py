"""
Google Antigravity AIOps Agent - ロジックモジュール
根本原因分析および重要度(Severity)の動的判定ロジックを実装。
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
    severity: str = "CRITICAL"

class CausalInferenceEngine:
    def __init__(self, topology: Dict[str, NetworkNode]):
        self.topology = topology

    def analyze_alarms(self, alarms: List[Alarm]) -> InferenceResult:
        alarmed_device_ids = {a.device_id for a in alarms}
        alarm_map = {a.device_id: a for a in alarms}

        sorted_alarms = sorted(
            alarms, 
            key=lambda a: self.topology[a.device_id].layer if a.device_id in self.topology else 999
        )
        
        if not sorted_alarms:
            return InferenceResult(None, "アラームなし", "DEFAULT", [], "INFO")

        top_alarm = sorted_alarms[0]
        top_node = self.topology.get(top_alarm.device_id)
        
        if not top_node:
             return InferenceResult(None, "不明なデバイス", "DEFAULT", alarms, "UNKNOWN")

        if top_node.redundancy_group:
            return self._analyze_redundancy(top_node, alarmed_device_ids, alarms, alarm_map)

        if top_node.parent_id:
            silent_res = self._check_silent_failure_for_parent(top_node.parent_id, alarmed_device_ids)
            if silent_res:
                return silent_res

        root_severity = top_alarm.severity
        return InferenceResult(
            root_cause_node=top_node,
            root_cause_reason=f"階層ルール: 最上位レイヤーのデバイス {top_node.id} でアラーム検知 ({top_alarm.message})",
            sop_key="HIERARCHY_FAILURE",
            related_alarms=alarms,
            severity=root_severity
        )

    def _analyze_redundancy(self, node: NetworkNode, alarmed_ids: Set[str], alarms: List[Alarm], alarm_map: Dict[str, Alarm]) -> InferenceResult:
        group_members = [n for n in self.topology.values() if n.redundancy_group == node.redundancy_group]
        down_members = [n for n in group_members if n.id in alarmed_ids]
        
        error_details = []
        for m in down_members:
            if m.id in alarm_map:
                error_details.append(f"{m.id}: {alarm_map[m.id].message}")
        details_str = ", ".join(error_details)

        if len(down_members) == len(group_members):
            return InferenceResult(
                root_cause_node=node,
                root_cause_reason=f"冗長性ルール: HAグループ {node.redundancy_group} 全停止。詳細: [{details_str}]",
                sop_key="HA_TOTAL_FAILURE",
                related_alarms=alarms,
                severity="CRITICAL"
            )
        else:
            return InferenceResult(
                root_cause_node=node,
                root_cause_reason=f"冗長性ルール: HAグループ {node.redundancy_group} 片系障害 (稼働継続)。検知内容: [{details_str}]",
                sop_key="HA_PARTIAL_FAILURE",
                related_alarms=alarms,
                severity="WARNING"
            )

    def _check_silent_failure_for_parent(self, parent_id: str, alarmed_ids: Set[str]) -> Optional[InferenceResult]:
        parent_node = self.topology.get(parent_id)
        if not parent_node:
            return None
            
        children = [n for n in self.topology.values() if n.parent_id == parent_id]
        children_down = sum(1 for c in children if c.id in alarmed_ids)
        
        if len(children) > 0 and children_down == len(children):
             return InferenceResult(
                root_cause_node=parent_node,
                root_cause_reason=f"サイレント障害推論: 親デバイス {parent_id} は沈黙していますが、配下の子デバイスが全滅しています。",
                sop_key="SILENT_FAILURE",
                related_alarms=[],
                severity="CRITICAL"
            )
        return None

def simulate_cascade_failure(root_cause_id: str, topology: Dict[str, NetworkNode]) -> List[Alarm]:
    generated_alarms = []
    generated_alarms.append(Alarm(root_cause_id, "Interface Down", "CRITICAL"))
    
    queue = [root_cause_id]
    processed = {root_cause_id}
    
    while queue:
        current_parent_id = queue.pop(0)
        children = [n for n in topology.values() if n.parent_id == current_parent_id]
        for child in children:
            if child.id not in processed:
                generated_alarms.append(Alarm(child.id, "Unreachable", "WARNING"))
                queue.append(child.id)
                processed.add(child.id)
                
    return generated_alarms
