# -*- coding: utf-8 -*-
"""
network_ops_refactored.py - ネットワーク操作モジュール（改修版）

【改善点】
1. if-elif分岐を ScenarioDefinition ベースに置き換え
2. AI生成ロジックを ai_helpers.py に委譲
3. 実機接続/AI生成を明示的に分離
4. Safety判定を safety_rules.py に委譲
"""

import time
from typing import Dict, Any, Optional
from dataclasses import dataclass

# 共通モジュール
from scenario_manager import ScenarioDefinition, SimulationType, find_scenario
from ai_helpers import AIClient, generate_mock_log, AIConfig
from safety_rules import SafetyRuleEngine, SafetyJudgment

# Netmiko（実機接続用）
try:
    from netmiko import ConnectHandler
    NETMIKO_AVAILABLE = True
except ImportError:
    NETMIKO_AVAILABLE = False
    print("⚠️ Netmiko not available. Live diagnostics will be disabled.")


# ========================================
# 実機接続設定
# ========================================

SANDBOX_DEVICE = {
    'device_type': 'cisco_nxos',
    'host': 'sandbox-nxos-1.cisco.com',
    'username': 'admin',
    'password': 'Admin_1234!',
    'port': 22,
    'global_delay_factor': 2,
    'banner_timeout': 30,
    'conn_timeout': 30,
}


# ========================================
# 結果データクラス
# ========================================

@dataclass
class DiagnosticResult:
    """診断結果"""
    status: str  # SUCCESS, ERROR, SKIPPED
    log_output: str
    error_message: Optional[str] = None
    safety_judgment: Optional[SafetyJudgment] = None


# ========================================
# 診断実行（改修版）
# ========================================

class DiagnosticRunner:
    """
    診断実行の統合クラス
    
    【改善点】
    - if-elif地獄を SimulationType で置き換え
    - 各処理を明示的なメソッドに分離
    """
    
    def __init__(self):
        self.ai_client = AIClient()
    
    def run(
        self,
        scenario: ScenarioDefinition,
        target_node = None,
        config: AIConfig = None
    ) -> DiagnosticResult:
        """
        シナリオに応じた診断を実行
        
        Args:
            scenario: シナリオ定義
            target_node: 対象ノード（NetworkNodeオブジェクト）
            config: AI設定
        
        Returns:
            DiagnosticResult
        """
        
        # SimulationType に応じて処理を分岐（明示的）
        if scenario.simulation_type == SimulationType.SKIP:
            return self._run_skip(scenario)
        
        elif scenario.simulation_type == SimulationType.LIVE:
            return self._run_live_diagnostics(scenario)
        
        elif scenario.simulation_type == SimulationType.ERROR:
            return self._run_error_simulation(scenario)
        
        elif scenario.simulation_type == SimulationType.AI_MOCK:
            return self._run_ai_mock(scenario, target_node, config)
        
        else:
            return DiagnosticResult(
                status="ERROR",
                log_output="",
                error_message=f"Unknown simulation type: {scenario.simulation_type}"
            )
    
    def _run_skip(self, scenario: ScenarioDefinition) -> DiagnosticResult:
        """スキップ（正常稼働等）"""
        return DiagnosticResult(
            status="SKIPPED",
            log_output="No action required.",
            error_message=None
        )
    
    def _run_live_diagnostics(self, scenario: ScenarioDefinition) -> DiagnosticResult:
        """実機診断（Cisco Sandbox等）"""
        
        if not NETMIKO_AVAILABLE:
            return DiagnosticResult(
                status="ERROR",
                log_output="",
                error_message="Netmiko library not available"
            )
        
        commands = [
            "terminal length 0",
            "show version",
            "show interface brief",
            "show ip route"
        ]
        
        try:
            with ConnectHandler(**SANDBOX_DEVICE) as ssh:
                if not ssh.check_enable_mode():
                    ssh.enable()
                
                prompt = ssh.find_prompt()
                output = f"Connected to: {prompt}\n"
                
                for cmd in commands:
                    result = ssh.send_command(cmd)
                    output += f"\n{'='*30}\n[Command] {cmd}\n{result}\n"
                
                # 出力のサニタイゼーション
                sanitized = self._sanitize_output(output)
                
                return DiagnosticResult(
                    status="SUCCESS",
                    log_output=sanitized,
                    error_message=None
                )
        
        except Exception as e:
            return DiagnosticResult(
                status="ERROR",
                log_output="",
                error_message=f"Connection failed: {str(e)}"
            )
    
    def _run_error_simulation(self, scenario: ScenarioDefinition) -> DiagnosticResult:
        """エラーシミュレーション（全回線断等）"""
        return DiagnosticResult(
            status="ERROR",
            log_output="",
            error_message="Connection timed out (simulated network unreachability)"
        )
    
    def _run_ai_mock(
        self,
        scenario: ScenarioDefinition,
        target_node,
        config: AIConfig
    ) -> DiagnosticResult:
        """AI生成モックログ"""
        
        if not target_node:
            return DiagnosticResult(
                status="ERROR",
                log_output="",
                error_message="Target node not specified"
            )
        
        # ノードのメタデータ取得
        metadata = self._extract_node_metadata(target_node)
        
        try:
            # AI でログ生成
            raw_log = generate_mock_log(
                hostname=metadata["hostname"],
                vendor=metadata["vendor"],
                os_type=metadata["os_type"],
                model=metadata["model"],
                scenario_description=scenario.description,
                symptoms=scenario.symptoms,
                config=config
            )
            
            # サニタイゼーション
            sanitized_log = self._sanitize_output(raw_log)
            
            return DiagnosticResult(
                status="SUCCESS",
                log_output=sanitized_log,
                error_message=None
            )
        
        except Exception as e:
            return DiagnosticResult(
                status="ERROR",
                log_output="",
                error_message=f"AI generation failed: {str(e)}"
            )
    
    def _extract_node_metadata(self, node) -> Dict[str, str]:
        """ノードからメタデータを抽出"""
        
        # dict形式対応
        if isinstance(node, dict):
            metadata = node.get("metadata", {})
            return {
                "hostname": node.get("id", "UNKNOWN"),
                "vendor": metadata.get("vendor", "Generic"),
                "os_type": metadata.get("os", "Generic OS"),
                "model": metadata.get("model", "Generic Device")
            }
        
        # NetworkNodeオブジェクト対応
        if hasattr(node, "metadata"):
            md = getattr(node, "metadata", {})
            return {
                "hostname": getattr(node, "id", "UNKNOWN"),
                "vendor": md.get("vendor", "Generic"),
                "os_type": md.get("os", "Generic OS"),
                "model": md.get("model", "Generic Device")
            }
        
        # フォールバック
        return {
            "hostname": "UNKNOWN",
            "vendor": "Generic",
            "os_type": "Generic OS",
            "model": "Generic Device"
        }
    
    def _sanitize_output(self, text: str) -> str:
        """機密情報のサニタイゼーション"""
        import re
        
        rules = [
            (r'(password|secret) \d+ \S+', r'\1 <HIDDEN>'),
            (r'(encrypted password) \S+', r'\1 <HIDDEN>'),
            (r'(snmp-server community) \S+', r'\1 <HIDDEN>'),
            (r'(username \S+ privilege \d+ secret \d+) \S+', r'\1 <HIDDEN>'),
            # Public IPアドレスのマスク（プライベートIPは除外）
            (r'\b(?!(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.)\d{1,3}\.(?:\d{1,3}\.){2}\d{1,3}\b', '<MASKED_IP>'),
            # MACアドレスのマスク
            (r'([0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}', '<MASKED_MAC>'),
        ]
        
        for pattern, replacement in rules:
            text = re.sub(pattern, replacement, text)
        
        return text


# ========================================
# 後方互換性関数
# ========================================

def run_diagnostic_simulation(
    scenario_type: str,
    target_node = None,
    api_key: str = None
) -> Dict[str, Any]:
    """
    後方互換性のためのラッパー関数
    
    【非推奨】
    新しいコードでは DiagnosticRunner を直接使用してください。
    """
    
    # シナリオを検索
    scenario = find_scenario(scenario_type)
    
    if not scenario:
        return {
            "status": "ERROR",
            "sanitized_log": "",
            "error": f"Unknown scenario: {scenario_type}"
        }
    
    # 実行
    runner = DiagnosticRunner()
    result = runner.run(scenario, target_node)
    
    # 旧形式に変換
    return {
        "status": result.status,
        "sanitized_log": result.log_output,
        "error": result.error_message
    }


# ========================================
# その他の関数（ほぼそのまま移植）
# ========================================

def generate_config_from_intent(target_node, current_config: str, intent_text: str, api_key: str) -> str:
    """
    意図（Intent）から設定を生成
    
    【改善余地】
    - ai_helpers.py にテンプレート化すべき
    """
    if not api_key:
        return "Error: API Key Missing"
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"temperature": 0.0})
        
        # メタデータ取得
        if isinstance(target_node, dict):
            metadata = target_node.get("metadata", {})
            vendor = metadata.get("vendor", "Unknown")
            os_type = metadata.get("os", "Unknown")
        else:
            md = getattr(target_node, "metadata", {})
            vendor = md.get("vendor", "Unknown")
            os_type = md.get("os", "Unknown")
        
        prompt = f"""
        ネットワーク設定生成。
        対象: {getattr(target_node, 'id', 'UNKNOWN')} ({vendor} {os_type})
        現在のConfig: {current_config}
        Intent: {intent_text}
        出力: 投入用コマンドのみ (Markdownコードブロック)
        """
        
        response = model.generate_content(prompt)
        return response.text
    
    except Exception as e:
        return f"Config Generation Error: {e}"


def generate_health_check_commands(target_node, api_key: str) -> str:
    """正常性確認コマンドを生成"""
    if not api_key:
        return "Error: API Key Missing"
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"temperature": 0.0})
        
        if isinstance(target_node, dict):
            metadata = target_node.get("metadata", {})
            vendor = metadata.get("vendor", "Unknown")
            os_type = metadata.get("os", "Unknown")
        else:
            md = getattr(target_node, "metadata", {})
            vendor = md.get("vendor", "Unknown")
            os_type = md.get("os", "Unknown")
        
        prompt = f"Netmiko正常性確認コマンドを3つ生成せよ。対象: {vendor} {os_type}。出力: コマンドのみ箇条書き"
        
        response = model.generate_content(prompt)
        return response.text
    
    except Exception as e:
        return f"Command Generation Error: {e}"


def generate_remediation_commands(
    scenario: str,
    analysis_result: str,
    target_node,
    api_key: str
) -> str:
    """復旧手順を生成"""
    if not api_key:
        return "Error: API Key Missing"
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"temperature": 0.0})
        
        if isinstance(target_node, dict):
            metadata = target_node.get("metadata", {})
            device_id = target_node.get("id", "UNKNOWN")
            vendor = metadata.get("vendor", "Unknown")
            os_type = metadata.get("os", "Unknown")
        else:
            device_id = getattr(target_node, "id", "UNKNOWN")
            md = getattr(target_node, "metadata", {})
            vendor = md.get("vendor", "Unknown")
            os_type = md.get("os", "Unknown")
        
        prompt = f"""
        あなたは熟練したネットワークエンジニアです。
        発生している障害に対して、オペレーターが実行すべき**「完全な復旧手順書」**を作成してください。
        
        対象デバイス: {device_id} ({vendor} {os_type})
        発生シナリオ: {scenario}
        AI分析結果: {analysis_result}
        
        【重要: 出力要件】
        以下の3つのセクションを必ず含めてください。Markdown形式で出力すること。

        ### 1. 物理・前提アクション (Physical Actions)
        * 電源障害やケーブル断、FAN故障の場合、「交換手順」や「結線確認」を具体的に指示してください。
        * ソフトウェア設定のみで直る場合は「特になし」で構いません。

        ### 2. 復旧コマンド (Recovery Config)
        * 設定変更や再起動が必要な場合のコマンド。
        * コマンドは Markdownのコードブロック(```) で囲んでください。

        ### 3. 正常性確認コマンド (Verification Commands)
        * 対応後に正常に戻ったかを確認するためのコマンド（showコマンドやpingなど）。
        * 必ず3つ以上提示してください。
        * コマンドは Markdownのコードブロック(```) で囲んでください。
        """
        
        response = model.generate_content(prompt)
        return response.text
    
    except Exception as e:
        return f"Remediation Generation Error: {e}"


def predict_initial_symptoms(scenario_name: str, api_key: str) -> Dict[str, str]:
    """
    初期症状を予測（ベイズ推論用）
    
    【改善余地】
    - ai_helpers.py にテンプレート化すべき
    """
    if not api_key:
        return {}
    
    try:
        import google.generativeai as genai
        import json
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"temperature": 0.0})
        
        prompt = f"""
        あなたはネットワーク監視システムのAIエージェントです。
        指定された「障害シナリオ」において、監視システムが最初に検知するであろう「初期症状」を推論してください。

        **シナリオ**: {scenario_name}

        【出力要件】
        JSON形式で出力すること（Markdownコードブロック不要）。
        {{
          "alarm": "アラームメッセージ (例: BGP Flapping, Fan Fail等)",
          "ping": "疎通状態 (NG or OK)",
          "log": "ログキーワード (例: Interface Down等)"
        }}
        """
        
        response = model.generate_content(prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    
    except Exception as e:
        print(f"Symptom Prediction Error: {e}")
        return {}


# ========================================
# 使用例
# ========================================

if __name__ == "__main__":
    print("=" * 80)
    print("Network Operations (Refactored) - Usage Examples")
    print("=" * 80)
    
    from scenario_manager import DEFAULT_CATALOG
    
    # テストシナリオ
    test_scenarios = ["WAN全回線断", "FW片系障害", "正常稼働", "[Live] Cisco実機診断"]
    
    runner = DiagnosticRunner()
    
    for scenario_name in test_scenarios:
        print(f"\n{'='*60}")
        print(f"Scenario: {scenario_name}")
        print('='*60)
        
        scenario = find_scenario(scenario_name)
        if not scenario:
            print(f"  ❌ Scenario not found")
            continue
        
        print(f"  Type: {scenario.simulation_type.value}")
        print(f"  Severity: {scenario.severity.value}")
        
        # モックノード
        mock_node = {
            "id": "TEST_ROUTER_01",
            "metadata": {
                "vendor": "Cisco",
                "os": "IOS-XE",
                "model": "ISR 4451-X"
            }
        }
        
        # 診断実行（AI無効）
        # result = runner.run(scenario, mock_node)
        # print(f"  Result: {result.status}")
    
    print("\n✅ Network Operations (Refactored) is ready!")
