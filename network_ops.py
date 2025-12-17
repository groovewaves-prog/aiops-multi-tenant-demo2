"""
Google Antigravity AIOps Agent - Network Operations Module
"""
import re
import os
import time
import json
import google.generativeai as genai
from netmiko import ConnectHandler

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

def sanitize_output(text: str) -> str:
    rules = [
        (r'(password|secret) \d+ \S+', r'\1 <HIDDEN_PASSWORD>'),
        (r'(encrypted password) \S+', r'\1 <HIDDEN_PASSWORD>'),
        (r'(snmp-server community) \S+', r'\1 <HIDDEN_COMMUNITY>'),
        (r'(username \S+ privilege \d+ secret \d+) \S+', r'\1 <HIDDEN_SECRET>'),
        (r'\b(?!(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.)\d{1,3}\.(?:\d{1,3}\.){2}\d{1,3}\b', '<MASKED_PUBLIC_IP>'),
        (r'([0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}', '<MASKED_MAC>'),
    ]
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    return text

def generate_fake_log_by_ai(scenario_name, target_node, api_key):
    """
    シナリオ名と機器メタデータから、AIが自律的に障害ログを生成する
    """
    if not api_key: return "Error: API Key Missing"
    
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        "gemini-1.5-flash",
        generation_config={"temperature": 0.2}
    )
    
    # ノード情報（JSONから取得）
    vendor = target_node.metadata.get("vendor", "Generic")
    os_type = target_node.metadata.get("os", "Generic OS")
    model_name = target_node.metadata.get("model", "Generic Device")
    hostname = target_node.id

    # プロンプト：AIへの指示書
    prompt = f"""
    あなたはネットワーク機器のCLIシミュレーター（熟練エンジニアのロールプレイング）です。
    ユーザーが指定した「障害シナリオ」に基づいて、トラブルシューティング時に実行されるであろう
    **「コマンド」とその「実行結果ログ」** を生成してください。

    【入力情報】
    - 対象ホスト名: {hostname}
    - ベンダー: {vendor}
    - OS種別: {os_type}
    - モデル: {model_name}
    - **発生している障害シナリオ**: 「{scenario_name}」

    【AIへの指示】
    1. **シナリオの解釈**: 提供されたシナリオ名から、技術的にどのような状態であるべきか推測してください。
    2. **コマンド選択**: その障害を確認するために、このベンダー({vendor})でよく使われる確認コマンドを2〜3個選んでください。
    3. **ログ生成**: 選んだコマンドに対し、シナリオ通りの異常状態を示す出力を生成してください。
    4. **リアリティ**: タイムスタンプやプロンプトを含め、本物のCLI画面のように出力してください。

    【出力形式】
    解説不要。CLIのテキストデータのみを出力してください。
    Markdownのコードブロックは使用しないでください（生テキストで出力）。
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI Generation Error: {e}"

def generate_config_from_intent(target_node, current_config, intent_text, api_key):
    if not api_key: return "Error: API Key Missing"
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"temperature": 0.0})
    
    vendor = target_node.metadata.get("vendor", "Unknown Vendor")
    os_type = target_node.metadata.get("os", "Unknown OS")
    
    prompt = f"""
    ネットワーク設定生成。
    対象: {target_node.id} ({vendor} {os_type})
    現在のConfig: {current_config}
    Intent: {intent_text}
    出力: 投入用コマンドのみ (Markdownコードブロック)
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Config Gen Error: {e}"

def generate_health_check_commands(target_node, api_key):
    if not api_key: return "Error: API Key Missing"
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"temperature": 0.0})
    
    vendor = target_node.metadata.get("vendor", "Unknown Vendor")
    os_type = target_node.metadata.get("os", "Unknown OS")
    
    prompt = f"Netmiko正常性確認コマンドを3つ生成せよ。対象: {vendor} {os_type}。出力: コマンドのみ箇条書き"
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Command Gen Error: {e}"

def generate_remediation_commands(scenario, analysis_result, target_node, api_key):
    """
    障害シナリオと分析結果に基づき、復旧手順（物理対応＋コマンド＋確認）を生成する
    """
    if not api_key: return "Error: API Key Missing"
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"temperature": 0.0})
    
    prompt = f"""
    あなたは熟練したネットワークエンジニアです。
    発生している障害に対して、オペレーターが実行すべき**「完全な復旧手順書」**を作成してください。
    
    対象デバイス: {target_node.id} ({target_node.metadata.get('vendor')} {target_node.metadata.get('os')})
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
    * 対応後に正常に戻ったかを確認するためのコマンド。
    * 必ず3つ以上提示してください。
    * コマンドは Markdownのコードブロック(```) で囲んでください。
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Remediation Gen Error: {e}"

def run_diagnostic_simulation(scenario_type, target_node=None, api_key=None):
    time.sleep(1.5)
    
    if "---" in scenario_type or "正常" in scenario_type:
        return {"status": "SKIPPED", "sanitized_log": "No action required.", "error": None}

    if "[Live]" in scenario_type:
        commands = ["terminal length 0", "show version", "show interface brief", "show ip route"]
        try:
            with ConnectHandler(**SANDBOX_DEVICE) as ssh:
                if not ssh.check_enable_mode(): ssh.enable()
                prompt = ssh.find_prompt()
                raw_output = f"Connected to: {prompt}\n"
                for cmd in commands:
                    output = ssh.send_command(cmd)
                    raw_output += f"\n{'='*30}\n[Command] {cmd}\n{output}\n"
        except Exception as e:
            return {"status": "ERROR", "sanitized_log": "", "error": str(e)}
        return {"status": "SUCCESS", "sanitized_log": sanitize_output(raw_output), "error": None}
            
    elif "全回線断" in scenario_type or "サイレント" in scenario_type or "両系" in scenario_type:
        return {"status": "ERROR", "sanitized_log": "", "error": "Connection timed out"}

    else:
        if api_key and target_node:
            raw_output = generate_fake_log_by_ai(scenario_type, target_node, api_key)
            return {"status": "SUCCESS", "sanitized_log": sanitize_output(raw_output), "error": None}
        else:
            return {"status": "ERROR", "sanitized_log": "", "error": "API Key or Target Node Missing"}

def predict_initial_symptoms(scenario_name, api_key):
    """
    障害シナリオ名から、発生しうる「初期症状（アラーム、ログ、Pingなど）」を
    AIに推論させ、ベイズエンジンへの入力データとして返す。
    """
    if not api_key: return {}
    
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"temperature": 0.0})
    
    prompt = f"""
    あなたはネットワーク監視システムのAIエージェントです。
    指定された「障害シナリオ」において、監視システムが最初に検知するであろう「初期症状」を推論してください。

    **シナリオ**: {scenario_name}

    【出力要件】
    1. 以下のキーを持つ **JSON形式** で出力すること。解説は不要。
       - "alarm": アラームメッセージ
       - "ping": 疎通状態 (例: "NG", "OK")
       - "log": ログキーワード
    
    2. 値は以下のキーワードリストから最も適切なものを選んでください。
       - アラーム系: "BGP Flapping", "Fan Fail", "Heartbeat Loss", "Connection Lost", "Power Supply 1 Failed", "Power Supply: Dual Loss (Device Down)"
       - ログ系: "Interface Down", "Power Fail", "Config Error", "High Temperature"
       - Ping系: "NG", "OK"

    **例**:
    シナリオ: "[WAN] BGPルートフラッピング"
    出力: {{ "alarm": "BGP Flapping", "ping": "OK", "log": "" }}
    """
    
    try:
        response = model.generate_content(prompt)
        text = response.text
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"Symptom Prediction Error: {e}")
        return {}
