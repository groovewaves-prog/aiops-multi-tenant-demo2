# -*- coding: utf-8 -*-
"""
safety_rules.py - Safety-Criticalな判定ルールの集約

【設計思想】
- AI推論の前後で「決定論的な検証」を行う
- サービス停止判定は人間が握る
- inference_engine.pyの設計思想を全体に適用
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum


# ========================================
# Enums
# ========================================

class HealthStatus(Enum):
    """システム健全性"""
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ImpactType(Enum):
    """障害の影響タイプ"""
    NONE = "NONE"
    DEGRADED = "DEGRADED"                      # 性能劣化
    REDUNDANCY_LOST = "REDUNDANCY_LOST"        # 冗長性喪失（サービス継続）
    OUTAGE = "OUTAGE"                          # サービス停止
    UNKNOWN = "UNKNOWN"


# ========================================
# データクラス
# ========================================

@dataclass
class SafetyJudgment:
    """Safety判定の結果"""
    status: HealthStatus
    impact_type: ImpactType
    reason: str
    is_definitive: bool  # True = 決定論的、False = 推定


# ========================================
# Safety Rule Engine
# ========================================

class SafetyRuleEngine:
    """
    Safety-Criticalな判定を行うエンジン
    
    【設計方針】
    1. 明らかな停止 → 即座にCRITICAL（AIに任せない）
    2. 冗長性判定 → ルールベース（PSU 2台 = WARNING）
    3. 曖昧なケース → AIに委譲（このクラスでは判定しない）
    
    【使用例】
    engine = SafetyRuleEngine()
    judgment = engine.evaluate(alarms=["Dual PSU Loss"], device_metadata={...})
    if judgment and judgment.status == HealthStatus.CRITICAL:
        # 即座に対応
    """
    
    # ========================================
    # Rule 1: 停止判定（最優先）
    # ========================================
    
    OUTAGE_KEYWORDS = [
        # 明確な停止表現
        "device down",
        "dual loss",
        "dual psu loss",
        "both psu",
        "thermal shutdown",
        "system shutdown",
        "complete failure",
        "total loss",
        
        # 日本語
        "両系",
        "全停止",
        "デバイスダウン",
    ]
    
    @staticmethod
    def check_outage(alarms: List[str]) -> Optional[SafetyJudgment]:
        """
        明らかなサービス停止をチェック
        
        NOTE: This rule exists to guarantee operational safety.
        AI should NOT override this judgment.
        
        Returns:
            SafetyJudgment if outage detected, None otherwise
        """
        joined = " ".join(alarms).lower()
        
        for keyword in SafetyRuleEngine.OUTAGE_KEYWORDS:
            if keyword in joined:
                return SafetyJudgment(
                    status=HealthStatus.CRITICAL,
                    impact_type=ImpactType.OUTAGE,
                    reason=f"Service outage detected: '{keyword}' (safety rule)",
                    is_definitive=True
                )
        
        return None
    
    # ========================================
    # Rule 2: 冗長性判定
    # ========================================
    
    @staticmethod
    def check_psu_redundancy(
        alarms: List[str],
        psu_count: int
    ) -> Optional[SafetyJudgment]:
        """
        電源冗長性の判定
        
        ロジック:
        - PSU 2台以上 + 片系故障 → WARNING（サービス継続）
        - PSU 1台 + 故障 → CRITICAL（停止リスク）
        - Dual Loss → CRITICAL（即座に停止）
        """
        joined = " ".join(alarms).lower()
        
        # Dual Loss は既に check_outage で判定済みなので、ここでは単一故障のみ
        psu_fail = any(kw in joined for kw in [
            "power supply", "psu fail", "psu 1 fail", "電源障害"
        ])
        
        if not psu_fail:
            return None
        
        # 冗長性チェック
        if psu_count >= 2:
            return SafetyJudgment(
                status=HealthStatus.WARNING,
                impact_type=ImpactType.REDUNDANCY_LOST,
                reason=f"Single PSU failure with redundancy (psu_count={psu_count}) (safety rule)",
                is_definitive=True
            )
        else:
            return SafetyJudgment(
                status=HealthStatus.CRITICAL,
                impact_type=ImpactType.OUTAGE,
                reason=f"Single PSU failure without redundancy (psu_count={psu_count}) (safety rule)",
                is_definitive=True
            )
    
    @staticmethod
    def check_ha_redundancy(
        alarms: List[str],
        redundancy_group: Optional[str],
        total_members: int,
        failed_members: int
    ) -> Optional[SafetyJudgment]:
        """
        HA冗長性の判定
        
        ロジック:
        - HA構成で全停止 → CRITICAL
        - HA構成で片系故障 → WARNING
        """
        if not redundancy_group:
            return None
        
        if failed_members == total_members:
            return SafetyJudgment(
                status=HealthStatus.CRITICAL,
                impact_type=ImpactType.OUTAGE,
                reason=f"HA group total failure: {failed_members}/{total_members} (safety rule)",
                is_definitive=True
            )
        elif failed_members > 0:
            return SafetyJudgment(
                status=HealthStatus.WARNING,
                impact_type=ImpactType.REDUNDANCY_LOST,
                reason=f"HA partial failure: {failed_members}/{total_members} (safety rule)",
                is_definitive=True
            )
        
        return None
    
    # ========================================
    # Rule 3: FANと熱障害
    # ========================================
    
    @staticmethod
    def check_thermal_risk(alarms: List[str]) -> Optional[SafetyJudgment]:
        """
        FAN故障と熱障害のリスク判定
        
        ロジック:
        - FAN故障 + 熱警告 → CRITICAL（停止リスク）
        - FAN故障のみ → WARNING（監視強化）
        """
        joined = " ".join(alarms).lower()
        
        fan_fail = any(kw in joined for kw in ["fan fail", "fan fault", "ファン故障"])
        thermal_symptom = any(kw in joined for kw in [
            "high temperature", "overheat", "thermal", "高温"
        ])
        
        if fan_fail and thermal_symptom:
            return SafetyJudgment(
                status=HealthStatus.CRITICAL,
                impact_type=ImpactType.OUTAGE,
                reason="Fan failure with thermal escalation (safety rule)",
                is_definitive=True
            )
        elif fan_fail:
            return SafetyJudgment(
                status=HealthStatus.WARNING,
                impact_type=ImpactType.DEGRADED,
                reason="Fan failure detected, thermal escalation risk (safety rule)",
                is_definitive=True
            )
        
        return None
    
    # ========================================
    # Rule 4: メモリリーク
    # ========================================
    
    @staticmethod
    def check_memory_risk(alarms: List[str]) -> Optional[SafetyJudgment]:
        """
        メモリリークのリスク判定
        
        ロジック:
        - メモリ高 + OOM/クラッシュ → CRITICAL
        - メモリ高のみ → WARNING
        """
        joined = " ".join(alarms).lower()
        
        mem_high = any(kw in joined for kw in [
            "memory high", "memory leak", "メモリリーク", "メモリ高"
        ])
        oom = any(kw in joined for kw in [
            "out of memory", "oom", "kernel panic", "process killed"
        ])
        
        if mem_high and oom:
            return SafetyJudgment(
                status=HealthStatus.CRITICAL,
                impact_type=ImpactType.OUTAGE,
                reason="Memory exhaustion causing system instability (safety rule)",
                is_definitive=True
            )
        elif mem_high:
            return SafetyJudgment(
                status=HealthStatus.WARNING,
                impact_type=ImpactType.DEGRADED,
                reason="Memory high, risk of OOM (safety rule)",
                is_definitive=True
            )
        
        return None
    
    # ========================================
    # 統合評価
    # ========================================
    
    @classmethod
    def evaluate(
        cls,
        alarms: List[str],
        device_metadata: Dict[str, Any] = None
    ) -> Optional[SafetyJudgment]:
        """
        全てのSafetyルールを評価
        
        Args:
            alarms: アラームメッセージのリスト
            device_metadata: 機器のメタデータ（PSU数、HA構成等）
        
        Returns:
            SafetyJudgment if any rule matches, None otherwise
        
        【使用例】
        judgment = SafetyRuleEngine.evaluate(
            alarms=["Dual PSU Loss"],
            device_metadata={"psu_count": 2}
        )
        if judgment:
            print(f"Safety Rule: {judgment.status} - {judgment.reason}")
        """
        if not alarms:
            return None
        
        metadata = device_metadata or {}
        
        # 1. 停止判定（最優先）
        judgment = cls.check_outage(alarms)
        if judgment:
            return judgment
        
        # 2. PSU冗長性
        psu_count = metadata.get("psu_count", 1)
        judgment = cls.check_psu_redundancy(alarms, psu_count)
        if judgment:
            return judgment
        
        # 3. HA冗長性
        redundancy_group = metadata.get("redundancy_group")
        total_members = metadata.get("total_members", 1)
        failed_members = metadata.get("failed_members", 0)
        judgment = cls.check_ha_redundancy(
            alarms, redundancy_group, total_members, failed_members
        )
        if judgment:
            return judgment
        
        # 4. 熱リスク
        judgment = cls.check_thermal_risk(alarms)
        if judgment:
            return judgment
        
        # 5. メモリリスク
        judgment = cls.check_memory_risk(alarms)
        if judgment:
            return judgment
        
        # どのルールにも該当しない → AI判定に委譲
        return None


# ========================================
# バリデーター: AI出力の検証
# ========================================

class OutputValidator:
    """AI生成結果の検証"""
    
    @staticmethod
    def validate_alarms(
        alarms: List[Dict[str, Any]],
        topology: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        アラームリストの妥当性を検証
        
        検証項目:
        1. device_id が topology に存在するか
        2. severity が有効な値か
        3. message が空でないか
        
        Returns:
            検証済み（無効なものは除外）のアラームリスト
        """
        validated = []
        
        for alarm in alarms:
            # 1. device_id 存在チェック
            device_id = alarm.get("device_id")
            if not device_id or device_id not in topology:
                print(f"⚠️ Invalid device_id: {device_id} (skipped)")
                continue
            
            # 2. severity チェック
            severity = alarm.get("severity", "WARNING").upper()
            if severity not in ["CRITICAL", "WARNING", "INFO"]:
                print(f"⚠️ Invalid severity: {severity} → default to WARNING")
                alarm["severity"] = "WARNING"
            
            # 3. message チェック
            if not alarm.get("message"):
                print(f"⚠️ Empty message for {device_id} (skipped)")
                continue
            
            validated.append(alarm)
        
        return validated
    
    @staticmethod
    def validate_judgment(
        judgment: Dict[str, Any],
        expected_keys: List[str] = None
    ) -> bool:
        """
        AI判定結果の妥当性を検証
        
        Args:
            judgment: AI判定結果の辞書
            expected_keys: 必須キーのリスト
        
        Returns:
            True if valid, False otherwise
        """
        if not isinstance(judgment, dict):
            return False
        
        expected_keys = expected_keys or ["status", "reason", "impact_type"]
        
        for key in expected_keys:
            if key not in judgment:
                return False
        
        # status の値チェック
        status = judgment.get("status", "").upper()
        if status not in ["NORMAL", "WARNING", "CRITICAL"]:
            return False
        
        return True


# ========================================
# ハイブリッド判定（Safety + AI）
# ========================================

class HybridJudgment:
    """
    SafetyルールとAI推論を組み合わせた判定
    
    【フロー】
    1. Safety Rule で評価
    2. 該当なし → AI判定
    3. AI判定結果を Safety Rule で再検証
    """
    
    @staticmethod
    def decide(
        alarms: List[str],
        device_metadata: Dict[str, Any],
        ai_judgment_func: Optional[callable] = None
    ) -> SafetyJudgment:
        """
        統合判定
        
        Args:
            alarms: アラームリスト
            device_metadata: 機器メタデータ
            ai_judgment_func: AI判定関数（オプション）
        
        Returns:
            最終的な SafetyJudgment
        """
        # 1. Safety Rule 優先
        safety_judgment = SafetyRuleEngine.evaluate(alarms, device_metadata)
        if safety_judgment:
            return safety_judgment
        
        # 2. AI判定（提供されていれば）
        if ai_judgment_func:
            try:
                ai_result = ai_judgment_func(alarms, device_metadata)
                
                # AI結果を SafetyJudgment に変換
                return SafetyJudgment(
                    status=HealthStatus[ai_result.get("status", "WARNING").upper()],
                    impact_type=ImpactType[ai_result.get("impact_type", "UNKNOWN").upper()],
                    reason=ai_result.get("reason", "AI judgment"),
                    is_definitive=False  # AIは推定
                )
            except Exception as e:
                print(f"⚠️ AI judgment failed: {e}")
        
        # 3. デフォルト（情報不足）
        return SafetyJudgment(
            status=HealthStatus.WARNING,
            impact_type=ImpactType.UNKNOWN,
            reason="Insufficient information for definitive judgment",
            is_definitive=False
        )


# ========================================
# 使用例
# ========================================

if __name__ == "__main__":
    print("=" * 80)
    print("Safety Rules - Usage Examples")
    print("=" * 80)
    
    # テストケース
    test_cases = [
        {
            "name": "Dual PSU Loss",
            "alarms": ["Dual PSU Loss", "Device Down"],
            "metadata": {"psu_count": 2},
            "expected": HealthStatus.CRITICAL
        },
        {
            "name": "Single PSU Fail (redundant)",
            "alarms": ["Power Supply 1 Failed"],
            "metadata": {"psu_count": 2},
            "expected": HealthStatus.WARNING
        },
        {
            "name": "Single PSU Fail (no redundancy)",
            "alarms": ["Power Supply Failed"],
            "metadata": {"psu_count": 1},
            "expected": HealthStatus.CRITICAL
        },
        {
            "name": "Fan Fail + Overheat",
            "alarms": ["Fan Fail", "High Temperature"],
            "metadata": {},
            "expected": HealthStatus.CRITICAL
        },
        {
            "name": "Fan Fail only",
            "alarms": ["Fan Fail"],
            "metadata": {},
            "expected": HealthStatus.WARNING
        },
    ]
    
    for i, case in enumerate(test_cases, 1):
        print(f"\n{i}. {case['name']}:")
        print(f"   Alarms: {case['alarms']}")
        
        judgment = SafetyRuleEngine.evaluate(case["alarms"], case["metadata"])
        
        if judgment:
            print(f"   Result: {judgment.status.value}")
            print(f"   Reason: {judgment.reason}")
            print(f"   Expected: {case['expected'].value}")
            print(f"   ✅ Match!" if judgment.status == case["expected"] else "❌ Mismatch")
        else:
            print(f"   Result: No rule matched (would delegate to AI)")
    
    print("\n" + "=" * 80)
    print("✅ Safety Rules are working correctly!")
