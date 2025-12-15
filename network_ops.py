"""
Google Antigravity AIOps Agent - Network Operations Module
"""
import re
import os
import time
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
    （ルールベースの分岐を廃止）
    """
    if not api_key: return "Error: API Key Missing"
    
    genai.configure(api_key=api_key)
    # 推論能力が高いモデルを使用
    model = genai.GenerativeModel(
        "gemma-3-12b-it",
        generation_config={"temperature": 0.2} # 多少の創造性を持たせるため0.0から少し上げる
    )
    
    # ノード情報（JSONから取得）
    vendor = target_node.metadata.get("vendor", "Generic")
    os_type = target_node.metadata.get("os", "Generic OS")
    model_name = target_node.metadata.get("model", "Generic Device")
    hostname = target_node.id

    # プロンプト：AIへの指示書
    # 具体的な「電源ならこうしろ」という指示を削除し、
    # 「シナリオ名を解釈して、それっぽいログを作れ」というメタな指示に変更
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
    1. **シナリオの解釈**: 提供されたシナリオ名（例: "電源障害", "BGP Flapping", "Cable Cut"など）から、技術的にどのような状態であるべきか推測してください。
    2. **コマンド選択**: その障害を確認するために、このベンダー({vendor})でよく使われる確認コマンドを2〜3個選んでください。（例: show environment, show log, show ip bgp sum, show interface 等）
    3. **ログ生成**: 選んだコマンドに対し、シナリオ通りの異常状態を示す出力を生成してください。
       - 電源障害なら: Power Supply Status を Faulty/Failed にする。
       - インターフェース障害なら: Protocol Down にする。
       - 正常稼働なら: 全て OK/Up にする。
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
    model = genai.GenerativeModel("gemma-3-12b-it", generation_config={"temperature": 0.0})
    
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
    model = genai.GenerativeModel("gemma-3-12b-it", generation_config={"temperature": 0.0})
    
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
    model = genai.GenerativeModel("gemma-3-12b-it", generation_config={"temperature": 0.0})
    
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
    * 例：「故障した電源ユニット(PSU1)を交換してください」「LANケーブルを再結線してください」など。
    * ソフトウェア設定のみで直る場合は「特になし」で構いません。

    ### 2. 復旧コマンド (Recovery Config)
    * 設定変更や再起動が必要な場合のコマンド。
    * 物理交換だけで復旧する場合でも、念のためのインターフェースリセット手順などを記載してください。
    * コマンドは Markdownのコードブロック(```) で囲んでください。

    ### 3. 正常性確認コマンド (Verification Commands)
    * 対応後に正常に戻ったかを確認するためのコマンド（showコマンドやpingなど）。
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
