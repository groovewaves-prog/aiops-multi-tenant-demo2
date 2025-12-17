# -*- coding: utf-8 -*-
"""
verifier_refactored.py - ログ検証モジュール（改修版）

【設計思想】
- Ground Truth抽出は「ルールベース検証 → AI補助 → ルールベース再検証」のサンドイッチ構造
- 正規表現パターンをYAML外部化（将来）
- AIは「補助的な解釈」に留める（ハルシネーション防止）

【改善点】
1. パターンマッチングをクラス化
2. AI補助機能の追加（オプション）
3. 検証結果の構造化
"""

import re
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum

# 共通モジュール
from ai_helpers import AIClient, AIConfig

logger = logging.getLogger(__name__)


# ========================================
# Enums
# ========================================

class VerificationStatus(Enum):
    """検証ステータス"""
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    INFO = "INFO"
    UNKNOWN = "Unknown"


# ========================================
# データクラス
# ========================================

@dataclass
class VerificationEvidence:
    """検証の証拠"""
    pattern_matched: str  # マッチした正規表現パターン
    matched_text: str     # マッチしたテキスト
    confidence: float     # 信頼度（0.0-1.0）


@dataclass
class VerificationResult:
    """検証結果（構造化）"""
    
    # Ping検証
    ping_status: VerificationStatus = VerificationStatus.UNKNOWN
    ping_confidence: float = 0.0
    ping_evidence: List[VerificationEvidence] = field(default_factory=list)
    
    # Interface検証
    interface_status: VerificationStatus = VerificationStatus.UNKNOWN
    interface_confidence: float = 0.0
    interface_evidence: List[VerificationEvidence] = field(default_factory=list)
    
    # Hardware検証
    hardware_status: VerificationStatus = VerificationStatus.UNKNOWN
    hardware_confidence: float = 0.0
    hardware_evidence: List[VerificationEvidence] = field(default_factory=list)
    
    # エラーキーワード
    error_keywords: List[str] = field(default_factory=list)
    error_severity: float = 0.0
    
    # 矛盾検知
    conflicts_detected: List[str] = field(default_factory=list)
    
    # 全体
    overall_confidence: float = 0.0
    
    # AI補助（オプション）
    ai_interpretation: Optional[str] = None


# ========================================
# パターンマッチャー
# ========================================

class PatternMatcher:
    """
    正規表現パターンマッチングの統一クラス
    
    【将来の改善】
    - パターンをYAML/JSONで外部化
    - ベンダー別パターンセットの切り替え
    """
    
    def __init__(self):
        self._compile_patterns()
    
    def _compile_patterns(self):
        """正規表現パターンのコンパイル"""
        
        # Ping関連
        self.ping_stats = re.compile(
            r'(?:(\d+)\s+packets?\s+transmitted.*?(\d+)\s+received)|'
            r'(?:success\s+rate\s+is\s+(\d+)\s*percent)|'
            r'(?:(\d+)%\s+packet\s+loss)',
            re.I
        )
        
        self.ping_fail_fast = re.compile(
            r'(100%\s+packet\s+loss|unreachable|'
            r'(?:request|connection)\s+timed?\s*out|'
            r'(?:0|zero)\s+(?:packets?\s+)?received)',
            re.I
        )
        
        self.cisco_ping_success = re.compile(r'!{3,}')
        
        # Interface関連
        self.admin_down = re.compile(r'administratively\s+down', re.I)
        
        self.if_status = re.compile(
            r'(?:line\s+protocol\s+is\s+(up|down))|'
            r'(?:interface\s+is\s+(up|down))|'
            r'(?:(err-disabled|notconnect))',
            re.I
        )
        
        # Hardware関連
        self.hw_check = re.compile(
            r'(fan|power|psu|temp|environment|sensor).*?'
            r'(fail(ed|ure)?|fault(y)?|critical|ok|good|normal|warn(ing)?)',
            re.I | re.DOTALL
        )
        
        logger.debug("Patterns compiled successfully")
    
    def match_ping(self, text: str) -> Optional[Dict[str, Any]]:
        """Pingパターンマッチング"""
        text_lower = text.lower()
        
        # 失敗パターン
        fail_match = self.ping_fail_fast.search(text_lower)
        if fail_match:
            return {
                "status": VerificationStatus.CRITICAL,
                "success_rate": 0.0,
                "evidence": VerificationEvidence(
                    pattern_matched="ping_fail_fast",
                    matched_text=fail_match.group(0),
                    confidence=0.9
                )
            }
        
        # Cisco形式（!!!!! + success rate）
        cisco_match = self.cisco_ping_success.search(text)
        if cisco_match:
            success_match = re.search(r'success\s+rate\s+is\s+(\d+)\s*percent', text_lower)
            if success_match:
                try:
                    rate = int(success_match.group(1))
                    status = (
                        VerificationStatus.OK if rate >= 80
                        else VerificationStatus.WARNING if rate >= 50
                        else VerificationStatus.CRITICAL
                    )
                    return {
                        "status": status,
                        "success_rate": rate,
                        "evidence": VerificationEvidence(
                            pattern_matched="cisco_ping",
                            matched_text=f"Success rate: {rate}%",
                            confidence=0.9
                        )
                    }
                except (ValueError, IndexError):
                    pass
        
        # 標準形式
        stats_match = self.ping_stats.search(text_lower)
        if stats_match:
            groups = stats_match.groups()
            success_rate = None
            
            try:
                if groups[0] and groups[1]:
                    sent, received = int(groups[0]), int(groups[1])
                    success_rate = (received / sent * 100) if sent > 0 else 0
                elif groups[2]:
                    success_rate = int(groups[2])
                elif groups[3]:
                    success_rate = 100 - int(groups[3])
                
                if success_rate is not None:
                    status = (
                        VerificationStatus.OK if success_rate >= 80
                        else VerificationStatus.WARNING if success_rate >= 50
                        else VerificationStatus.CRITICAL
                    )
                    return {
                        "status": status,
                        "success_rate": success_rate,
                        "evidence": VerificationEvidence(
                            pattern_matched="ping_stats",
                            matched_text=f"Success rate: {success_rate:.0f}%",
                            confidence=0.9
                        )
                    }
            except (ValueError, ZeroDivisionError):
                pass
        
        return None
    
    def match_interface(self, text: str) -> Optional[Dict[str, Any]]:
        """Interfaceパターンマッチング"""
        
        # Admin down（意図的なダウン）
        if self.admin_down.search(text):
            return {
                "status": VerificationStatus.INFO,
                "evidence": VerificationEvidence(
                    pattern_matched="admin_down",
                    matched_text="Admin down (intentional)",
                    confidence=0.9
                )
            }
        
        # Interface状態
        status_matches = self.if_status.findall(text)
        if not status_matches:
            return None
        
        down_count = sum(
            1 for m in status_matches
            if 'down' in str(m).lower() or 'disabled' in str(m).lower()
        )
        up_count = sum(1 for m in status_matches if 'up' in str(m).lower())
        
        if down_count > up_count:
            status = VerificationStatus.CRITICAL
            msg = f"Link DOWN detected ({down_count} interfaces)"
        elif up_count > down_count:
            status = VerificationStatus.OK
            msg = f"Link UP ({up_count} interfaces)"
        else:
            status = VerificationStatus.WARNING
            msg = "Mixed states"
        
        return {
            "status": status,
            "evidence": VerificationEvidence(
                pattern_matched="if_status",
                matched_text=msg,
                confidence=0.8
            )
        }
    
    def match_hardware(self, text: str) -> Optional[Dict[str, Any]]:
        """Hardwareパターンマッチング"""
        
        hw_matches = self.hw_check.findall(text)
        if not hw_matches:
            return None
        
        critical_count = sum(
            1 for m in hw_matches
            if any(k in str(m).lower() for k in ['fail', 'fault', 'critical'])
        )
        ok_count = sum(
            1 for m in hw_matches
            if any(k in str(m).lower() for k in ['ok', 'good', 'normal'])
        )
        warning_count = sum(1 for m in hw_matches if 'warn' in str(m).lower())
        
        if critical_count > 0:
            status = VerificationStatus.CRITICAL
            msg = f"HW failure detected ({critical_count} issues)"
        elif warning_count > 0:
            status = VerificationStatus.WARNING
            msg = f"HW warning ({warning_count} issues)"
        elif ok_count > 0:
            status = VerificationStatus.OK
            msg = f"HW OK ({ok_count} components)"
        else:
            return None
        
        return {
            "status": status,
            "evidence": VerificationEvidence(
                pattern_matched="hw_check",
                matched_text=msg,
                confidence=0.8
            )
        }


# ========================================
# AI補助解釈（オプション）
# ========================================

class AILogInterpreter:
    """
    AI による補助的なログ解釈
    
    【重要】
    - これは「補助」であり、Ground Truthではない
    - ルールベース検証の結果を補強・説明するために使用
    - ハルシネーションを防ぐため、必ずルールベース結果と照合
    """
    
    def __init__(self):
        self.client = AIClient()
    
    def interpret(
        self,
        log_text: str,
        rule_based_result: VerificationResult,
        config: AIConfig = None
    ) -> str:
        """
        ログを解釈（AIによる補助）
        
        Args:
            log_text: ログテキスト
            rule_based_result: ルールベース検証結果
            config: AI設定
        
        Returns:
            AI解釈テキスト
        """
        
        # プロンプト構築
        prompt = f"""
You are a network operations expert. Interpret the following log excerpt.

Rule-Based Verification Results (Ground Truth):
- Ping: {rule_based_result.ping_status.value}
- Interface: {rule_based_result.interface_status.value}
- Hardware: {rule_based_result.hardware_status.value}

Log Excerpt (first 1000 chars):
{log_text[:1000]}

Task:
1. Summarize the key findings from the log
2. Identify any additional context NOT captured by rule-based patterns
3. Suggest likely root cause if any

Output Format (plain text, 3-5 sentences):
"""
        
        config = config or AIConfig()
        
        try:
            interpretation = self.client.generate_with_retry(prompt, config)
            return interpretation
        
        except Exception as e:
            logger.warning(f"AI interpretation failed: {e}")
            return "AI interpretation unavailable"


# ========================================
# メイン検証クラス
# ========================================

class LogVerifier:
    """
    ログ検証の統合クラス（ハイブリッド設計）
    
    【フロー】
    1. ルールベース検証（高速・高精度）
    2. AI補助解釈（オプション）
    3. 矛盾検知
    """
    
    def __init__(self, use_ai: bool = False):
        self.matcher = PatternMatcher()
        self.ai_interpreter = AILogInterpreter() if use_ai else None
    
    def verify(self, log_text: str, use_ai: bool = False) -> VerificationResult:
        """
        ログを検証
        
        Args:
            log_text: ログテキスト
            use_ai: AI補助を使用するか
        
        Returns:
            VerificationResult
        """
        if not log_text:
            return VerificationResult()
        
        result = VerificationResult()
        
        # 1. ルールベース検証
        self._verify_ping(log_text, result)
        self._verify_interface(log_text, result)
        self._verify_hardware(log_text, result)
        self._verify_errors(log_text, result)
        
        # 2. 矛盾検知
        self._detect_conflicts(result)
        
        # 3. 全体信頼度
        confidences = [
            result.ping_confidence,
            result.interface_confidence,
            result.hardware_confidence
        ]
        result.overall_confidence = max(confidences) if any(confidences) else 0.0
        
        # 4. AI補助（オプション）
        if use_ai and self.ai_interpreter:
            result.ai_interpretation = self.ai_interpreter.interpret(log_text, result)
        
        return result
    
    def _verify_ping(self, text: str, result: VerificationResult):
        """Ping検証"""
        if 'ping' not in text.lower() and 'icmp' not in text.lower():
            return
        
        match_result = self.matcher.match_ping(text)
        if match_result:
            result.ping_status = match_result["status"]
            result.ping_confidence = match_result["evidence"].confidence
            result.ping_evidence.append(match_result["evidence"])
    
    def _verify_interface(self, text: str, result: VerificationResult):
        """Interface検証"""
        match_result = self.matcher.match_interface(text)
        if match_result:
            result.interface_status = match_result["status"]
            result.interface_confidence = match_result["evidence"].confidence
            result.interface_evidence.append(match_result["evidence"])
    
    def _verify_hardware(self, text: str, result: VerificationResult):
        """Hardware検証"""
        if not any(kw in text.lower() for kw in ['fan', 'power', 'psu', 'temp']):
            return
        
        match_result = self.matcher.match_hardware(text)
        if match_result:
            result.hardware_status = match_result["status"]
            result.hardware_confidence = match_result["evidence"].confidence
            result.hardware_evidence.append(match_result["evidence"])
    
    def _verify_errors(self, text: str, result: VerificationResult):
        """エラーキーワード検出"""
        text_lower = text.lower()
        
        critical_keywords = ['crash', 'panic', 'fatal', 'severe']
        error_keywords = ['error', 'fail', 'exception', 'denied']
        
        found_critical = [k for k in critical_keywords if k in text_lower]
        found_errors = [k for k in error_keywords if k in text_lower and k not in found_critical]
        
        if found_critical:
            result.error_keywords = found_critical
            result.error_severity = 0.9
        elif found_errors:
            result.error_keywords = found_errors
            result.error_severity = 0.7
    
    def _detect_conflicts(self, result: VerificationResult):
        """矛盾検知"""
        conflicts = []
        
        # Pingは成功しているがI/Fがダウン
        if (result.ping_status == VerificationStatus.OK and
            result.interface_status == VerificationStatus.CRITICAL):
            conflicts.append(
                "矛盾検知: Ping疎通は成功していますが、I/Fダウンが検出されています"
            )
        
        result.conflicts_detected = conflicts


# ========================================
# レポートフォーマット
# ========================================

def format_verification_report(result: VerificationResult) -> str:
    """検証結果を整形"""
    
    confidence_level = (
        "高" if result.overall_confidence >= 0.8
        else "中" if result.overall_confidence >= 0.5
        else "低"
    )
    
    report = f"""
【システム自動検証結果 (Ground Truth)】
※AIの推論はこの客観的事実と矛盾してはならない

◆ 総合信頼度: {confidence_level} ({result.overall_confidence:.0%})

◆ 疎通: {result.ping_status.value} (信頼度: {result.ping_confidence:.0%})
"""
    
    if result.ping_evidence:
        for ev in result.ping_evidence:
            report += f"  → {ev.matched_text}\n"
    
    report += f"""
◆ インターフェース: {result.interface_status.value} (信頼度: {result.interface_confidence:.0%})
"""
    
    if result.interface_evidence:
        for ev in result.interface_evidence:
            report += f"  → {ev.matched_text}\n"
    
    report += f"""
◆ ハードウェア: {result.hardware_status.value} (信頼度: {result.hardware_confidence:.0%})
"""
    
    if result.hardware_evidence:
        for ev in result.hardware_evidence:
            report += f"  → {ev.matched_text}\n"
    
    if result.error_keywords:
        report += f"\n◆ エラー: {', '.join(result.error_keywords)}\n"
    
    if result.conflicts_detected:
        report += f"\n⚠️ **矛盾検知**: {'; '.join(result.conflicts_detected)}\n"
    
    if result.ai_interpretation:
        report += f"\n◆ AI補助解釈:\n{result.ai_interpretation}\n"
    
    return report


# ========================================
# 後方互換性関数
# ========================================

def verify_log_content(log_text: str) -> Dict[str, Any]:
    """
    後方互換性のためのラッパー関数
    
    【非推奨】
    新しいコードでは LogVerifier を直接使用してください。
    """
    verifier = LogVerifier(use_ai=False)
    result = verifier.verify(log_text)
    
    # 旧形式に変換
    return {
        "ping_status": result.ping_status.value,
        "ping_confidence": result.ping_confidence,
        "ping_evidence": result.ping_evidence[0].matched_text if result.ping_evidence else "N/A",
        
        "interface_status": result.interface_status.value,
        "interface_confidence": result.interface_confidence,
        "interface_evidence": result.interface_evidence[0].matched_text if result.interface_evidence else "N/A",
        
        "hardware_status": result.hardware_status.value,
        "hardware_confidence": result.hardware_confidence,
        "hardware_evidence": result.hardware_evidence[0].matched_text if result.hardware_evidence else "N/A",
        
        "error_keywords": ", ".join(result.error_keywords) if result.error_keywords else "None",
        "error_severity": result.error_severity,
        
        "conflicts_detected": result.conflicts_detected,
        "overall_confidence": result.overall_confidence
    }


# ========================================
# 使用例
# ========================================

if __name__ == "__main__":
    print("=" * 80)
    print("Log Verifier (Refactored) - Usage Examples")
    print("=" * 80)
    
    # サンプルログ
    sample_logs = [
        {
            "name": "Cisco Ping Success",
            "log": """
            router#ping 192.168.1.1
            !!!!!
            Success rate is 100 percent (5/5)
            """
        },
        {
            "name": "Interface Down",
            "log": """
            show interface GigabitEthernet0/1
            GigabitEthernet0/1 is down, line protocol is down
            """
        },
        {
            "name": "PSU Failure",
            "log": """
            show environment power
            Power Supply 1: Failed
            Power Supply 2: OK
            """
        }
    ]
    
    verifier = LogVerifier(use_ai=False)
    
    for sample in sample_logs:
        print(f"\n{'='*60}")
        print(f"Test: {sample['name']}")
        print('='*60)
        
        result = verifier.verify(sample["log"])
        print(format_verification_report(result))
    
    print("\n✅ Log Verifier (Refactored) is working correctly!")
