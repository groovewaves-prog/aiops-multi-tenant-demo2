# -*- coding: utf-8 -*-
"""
ai_helpers.py - AI推論の共通化・ベストプラクティス

【設計思想】
- AI呼び出しのボイラープレートを削減
- リトライ・エラーハンドリングを統一
- プロンプトテンプレートの管理
- レスポンス検証の標準化
"""

import os
import time
import json
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions


# ========================================
# 設定
# ========================================

@dataclass
class AIConfig:
    """AI設定の統一管理"""
    model_name: str = "gemini-1.5-flash"
    temperature: float = 0.0  # 決定論的（デフォルト）
    max_retries: int = 3
    retry_delay: float = 2.0  # 秒
    timeout: float = 30.0  # 秒
    
    # Response format
    response_mime_type: Optional[str] = None  # "application/json" 等


# ========================================
# エラーハンドリング
# ========================================

class AIError(Exception):
    """AI関連のエラー基底クラス"""
    pass


class AIConfigError(AIError):
    """API Key未設定等の設定エラー"""
    pass


class AIResponseError(AIError):
    """レスポンスが期待と異なる"""
    pass


class AITimeoutError(AIError):
    """タイムアウト"""
    pass


# ========================================
# AIクライアント（シングルトン）
# ========================================

class AIClient:
    """
    AI呼び出しの共通クライアント
    
    【特徴】
    - API Key管理の一元化
    - リトライロジックの統一
    - レスポンス検証の標準化
    """
    
    _instance: Optional['AIClient'] = None
    _configured: bool = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._configured:
            self._configure()
    
    def _configure(self):
        """API Keyの設定"""
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            # Streamlit secrets からも試行
            try:
                import streamlit as st
                if "GOOGLE_API_KEY" in st.secrets:
                    api_key = st.secrets["GOOGLE_API_KEY"]
            except Exception:
                pass
        
        if not api_key:
            raise AIConfigError(
                "GOOGLE_API_KEY not found. Set environment variable or Streamlit secret."
            )
        
        genai.configure(api_key=api_key)
        self._configured = True
    
    def create_model(self, config: AIConfig = None) -> genai.GenerativeModel:
        """モデルインスタンスを作成"""
        config = config or AIConfig()
        
        generation_config = {
            "temperature": config.temperature,
        }
        
        if config.response_mime_type:
            generation_config["response_mime_type"] = config.response_mime_type
        
        return genai.GenerativeModel(
            config.model_name,
            generation_config=generation_config
        )
    
    def generate_with_retry(
        self,
        prompt: str,
        config: AIConfig = None,
        stream: bool = False,
        validator: Callable[[str], bool] = None
    ) -> str:
        """
        リトライ付きでAI生成
        
        Args:
            prompt: プロンプト文字列
            config: AI設定
            stream: ストリーミング出力
            validator: レスポンス検証関数（失敗時はリトライ）
        
        Returns:
            生成されたテキスト
        
        Raises:
            AIResponseError: リトライ上限到達
            AITimeoutError: タイムアウト
        """
        config = config or AIConfig()
        model = self.create_model(config)
        
        for attempt in range(config.max_retries):
            try:
                # 生成実行
                response = model.generate_content(prompt, stream=stream)
                
                if stream:
                    # ストリーミングの場合は即座に返す
                    return response
                
                # テキスト取得
                text = response.text.strip()
                
                # バリデーション
                if validator and not validator(text):
                    raise AIResponseError("Response validation failed")
                
                return text
            
            except google_exceptions.ServiceUnavailable:
                if attempt == config.max_retries - 1:
                    raise AIResponseError("Service unavailable after retries")
                time.sleep(config.retry_delay * (attempt + 1))
            
            except google_exceptions.DeadlineExceeded:
                raise AITimeoutError(f"Request timeout after {config.timeout}s")
            
            except Exception as e:
                if attempt == config.max_retries - 1:
                    raise AIResponseError(f"AI generation failed: {e}")
                time.sleep(config.retry_delay)
        
        raise AIResponseError("Max retries reached")
    
    def generate_json(
        self,
        prompt: str,
        config: AIConfig = None,
        schema: Dict = None
    ) -> Dict:
        """
        JSON形式でレスポンスを取得
        
        Args:
            prompt: プロンプト（JSON形式を要求すること）
            config: AI設定
            schema: JSONスキーマ（検証用）
        
        Returns:
            パースされたJSON辞書
        """
        config = config or AIConfig()
        config.response_mime_type = "application/json"
        
        text = self.generate_with_retry(prompt, config)
        
        # Markdownコードブロックの除去
        text = text.replace("```json", "").replace("```", "").strip()
        
        try:
            result = json.loads(text)
        except json.JSONDecodeError as e:
            raise AIResponseError(f"Invalid JSON response: {e}\nText: {text}")
        
        # スキーマ検証（オプション）
        if schema:
            # 簡易検証（本格的にはjsonschemaライブラリ推奨）
            for key in schema.get("required", []):
                if key not in result:
                    raise AIResponseError(f"Missing required field: {key}")
        
        return result


# ========================================
# プロンプトテンプレート管理
# ========================================

class PromptTemplate:
    """プロンプトテンプレート"""
    
    def __init__(self, template: str, required_vars: List[str] = None):
        self.template = template
        self.required_vars = required_vars or []
    
    def render(self, **kwargs) -> str:
        """変数を埋め込んでプロンプトを生成"""
        # 必須変数チェック
        missing = [v for v in self.required_vars if v not in kwargs]
        if missing:
            raise ValueError(f"Missing required variables: {missing}")
        
        try:
            return self.template.format(**kwargs)
        except KeyError as e:
            raise ValueError(f"Template variable not provided: {e}")


# ========================================
# 共通プロンプトテンプレート
# ========================================

SCENARIO_CLASSIFICATION_TEMPLATE = PromptTemplate(
    template="""
You are a network operations AI. Classify the following failure scenario.

Scenario: "{scenario_description}"

Available categories:
{categories}

Output Format (JSON only):
{{
  "matched_category": "exact category name from the list",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}
""",
    required_vars=["scenario_description", "categories"]
)


LOG_GENERATION_TEMPLATE = PromptTemplate(
    template="""
You are a network CLI simulator. Generate realistic command outputs for the following scenario.

Device Info:
- Hostname: {hostname}
- Vendor: {vendor}
- OS: {os_type}
- Model: {model}

Failure Scenario: {scenario_description}

Expected Symptoms: {symptoms}

Generate CLI output showing these symptoms. Include:
1. Command prompts (realistic)
2. Timestamps
3. Relevant show commands (2-3)
4. Output indicating the failure

Output raw CLI text only (no markdown, no code blocks).
""",
    required_vars=["hostname", "vendor", "os_type", "model", "scenario_description", "symptoms"]
)


ALARM_GENERATION_TEMPLATE = PromptTemplate(
    template="""
You are a network monitoring AI. Generate appropriate alarms based on the failure scenario.

Network Topology Summary:
{topology_summary}

Failure Scenario:
- Name: {scenario_name}
- Description: {scenario_description}
- Impact Scope: {impact_scope}
- Expected Severity: {severity}

Target Device Hints:
{target_hints}

Rules:
1. Cascade failures: parent down → all children unreachable
2. HA/Redundancy: single failure in HA → WARNING (service continues)
3. Silent failures: parent quiet but children fail → suspect parent

Output Format (JSON only):
{{
  "alarms": [
    {{
      "device_id": "exact ID from topology",
      "message": "specific symptom",
      "severity": "CRITICAL|WARNING|INFO"
    }}
  ],
  "reasoning": "brief explanation"
}}
""",
    required_vars=["topology_summary", "scenario_name", "scenario_description", 
                   "impact_scope", "severity", "target_hints"]
)


# ========================================
# 便利関数
# ========================================

def classify_scenario(
    scenario_description: str,
    categories: List[str],
    config: AIConfig = None
) -> Dict[str, Any]:
    """
    シナリオをカテゴリに分類
    
    【使用例】
    result = classify_scenario(
        "WAN回線が全断しました",
        ["WAN障害", "FW障害", "SW障害"]
    )
    print(result["matched_category"])  # "WAN障害"
    """
    client = AIClient()
    
    prompt = SCENARIO_CLASSIFICATION_TEMPLATE.render(
        scenario_description=scenario_description,
        categories="\n".join(f"- {c}" for c in categories)
    )
    
    return client.generate_json(prompt, config)


def generate_mock_log(
    hostname: str,
    vendor: str,
    os_type: str,
    model: str,
    scenario_description: str,
    symptoms: List[str],
    config: AIConfig = None
) -> str:
    """
    障害ログを生成
    
    【使用例】
    log = generate_mock_log(
        hostname="WAN_ROUTER_01",
        vendor="Cisco",
        os_type="IOS-XE",
        model="ISR 4451-X",
        scenario_description="両系電源障害",
        symptoms=["Dual PSU Loss", "Device Down"]
    )
    """
    client = AIClient()
    config = config or AIConfig()
    
    prompt = LOG_GENERATION_TEMPLATE.render(
        hostname=hostname,
        vendor=vendor,
        os_type=os_type,
        model=model,
        scenario_description=scenario_description,
        symptoms=", ".join(symptoms)
    )
    
    return client.generate_with_retry(prompt, config)


def generate_alarms_ai(
    topology_summary: Dict,
    scenario_name: str,
    scenario_description: str,
    impact_scope: str,
    severity: str,
    target_hints: Dict,
    config: AIConfig = None
) -> List[Dict[str, Any]]:
    """
    AIでアラームを生成
    
    【使用例】
    alarms = generate_alarms_ai(
        topology_summary={"WAN_ROUTER_01": {"type": "ROUTER", ...}},
        scenario_name="WAN全回線断",
        scenario_description="...",
        impact_scope="cascade",
        severity="CRITICAL",
        target_hints={"device_type": "ROUTER"}
    )
    """
    client = AIClient()
    
    prompt = ALARM_GENERATION_TEMPLATE.render(
        topology_summary=json.dumps(topology_summary, indent=2, ensure_ascii=False),
        scenario_name=scenario_name,
        scenario_description=scenario_description,
        impact_scope=impact_scope,
        severity=severity,
        target_hints=json.dumps(target_hints, ensure_ascii=False)
    )
    
    result = client.generate_json(prompt, config)
    return result.get("alarms", [])


# ========================================
# デコレーター: AI呼び出しの自動リトライ
# ========================================

def with_ai_retry(retries: int = 3, delay: float = 2.0):
    """
    関数にリトライ機能を追加するデコレーター
    
    【使用例】
    @with_ai_retry(retries=3)
    def my_ai_function():
        ...
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except (google_exceptions.ServiceUnavailable, AIResponseError) as e:
                    if attempt == retries - 1:
                        raise
                    time.sleep(delay * (attempt + 1))
        return wrapper
    return decorator


# ========================================
# 使用例
# ========================================

if __name__ == "__main__":
    print("=" * 80)
    print("AI Helpers - Usage Examples")
    print("=" * 80)
    
    try:
        # 1. シンプルな生成
        print("\n1. Simple Generation:")
        client = AIClient()
        response = client.generate_with_retry("Hello, who are you?")
        print(f"  Response: {response[:100]}...")
        
        # 2. JSON生成
        print("\n2. JSON Generation:")
        json_prompt = """
        Generate a JSON object with these fields:
        - status: "OK" or "ERROR"
        - message: a brief message
        
        Output JSON only, no markdown.
        """
        result = client.generate_json(json_prompt.strip())
        print(f"  Result: {result}")
        
        # 3. テンプレート使用
        print("\n3. Template Usage:")
        prompt = SCENARIO_CLASSIFICATION_TEMPLATE.render(
            scenario_description="ルーターが停止しました",
            categories="WAN障害\nFW障害\nSW障害"
        )
        print(f"  Rendered Prompt (first 200 chars):\n  {prompt[:200]}...")
        
        print("\n✅ AI Helpers are working correctly!")
    
    except AIConfigError as e:
        print(f"\n⚠️ API Key not configured: {e}")
        print("  Set GOOGLE_API_KEY environment variable to run this demo.")
    
    except Exception as e:
        print(f"\n❌ Error: {e}")
