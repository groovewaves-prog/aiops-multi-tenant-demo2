"""
Antigravity AIOps - Logical Inference Engine (Rule-Based / Deterministic)
ベイズ確率ではなく、発生しているアラームとトポロジー情報に基づき、
論理的・決定論的に根本原因を特定するエンジン。
"""

class LogicalRCA:
    def __init__(self, topology):
        self.topology = topology
        
        # ■ 障害シグネチャ（知識ベース）
        # 「この種のアラームの組み合わせが発生したら、この障害と特定する」というルール
        # lambda内で .lower() を使うことで、大文字小文字の揺らぎを吸収
        self.signatures = [
            {
                "type": "Hardware/Critical_Multi_Fail",
                "label": "複合ハードウェア障害",
                "rules": lambda alarms: any("power supply" in a.message.lower() for a in alarms) and any("fan" in a.message.lower() for a in alarms),
                "base_score": 1.0
            },
            {
                "type": "Hardware/Physical",
                "label": "ハードウェア障害 (電源/デバイス)",
                "rules": lambda alarms: any(k in a.message.lower() for a in alarms for k in ["power supply", "device down"]),
                "base_score": 0.95
            },
            {
                "type": "Network/Link",
                "label": "物理リンク/インターフェース障害",
                "rules": lambda alarms: any(k in a.message.lower() for a in alarms for k in ["interface down", "connection lost", "heartbeat loss"]),
                "base_score": 0.90
            },
            {
                "type": "Hardware/Fan",
                "label": "冷却ファン故障",
                "rules": lambda alarms: any("fan fail" in a.message.lower() for a in alarms),
                "base_score": 0.70
            },
            {
                "type": "Config/Software",
                "label": "設定ミス/プロトコル障害",
                "rules": lambda alarms: any(k in a.message.lower() for a in alarms for k in ["bgp", "ospf", "config"]),
                "base_score": 0.60
            },
            {
                "type": "Resource/Capacity",
                "label": "リソース枯渇 (CPU/Memory)",
                "rules": lambda alarms: any(k in a.message.lower() for a in alarms for k in ["cpu", "memory", "high"]),
                "base_score": 0.50
            }
        ]

    def analyze(self, current_alarms):
        """
        現在のアラームリストを入力とし、デバイスごとのリスクスコアを算出する。
        """
        candidates = []
        
        # 1. アラームをデバイスIDごとにグループ化
        device_alarms = {}
        for alarm in current_alarms:
            if alarm.device_id not in device_alarms:
                device_alarms[alarm.device_id] = []
            device_alarms[alarm.device_id].append(alarm)
            
        # 2. デバイスごとにルール適合度を評価
        for device_id, alarms in device_alarms.items():
            best_match = None
            max_score = 0.0
            
            # 全シグネチャをチェックし、最も重篤なものを採用
            for sig in self.signatures:
                if sig["rules"](alarms):
                    # アラーム数に応じた加点 (確信度の補強)
                    # 基本スコア + (関連アラーム数 * 0.05) ※最大1.0
                    score = min(sig["base_score"] + (len(alarms) * 0.02), 1.0)
                    
                    if score > max_score:
                        max_score = score
                        best_match = sig
            
            # シグネチャにマッチした場合
            if best_match:
                candidates.append({
                    "id": device_id,
                    "type": best_match["type"],
                    "label": best_match["label"],
                    "prob": max_score, # リスクスコア (0.0 - 1.0)
                    "alarms": [a.message for a in alarms]
                })
            # どのアラーム定義にも当てはまらないが、アラームが出ている場合（その他）
            elif alarms:
                candidates.append({
                    "id": device_id,
                    "type": "Unknown/Other",
                    "label": "その他異常検知",
                    "prob": 0.3, # 低めのスコア
                    "alarms": [a.message for a in alarms]
                })

        # 3. ★トポロジー相関分析 (サイレント障害検知)
        # 「配下のデバイスが複数ダウンしているのに、自分は無言」な親ノードを探す
        
        down_children_count = {} # parent_id -> count
        
        for alarm in current_alarms:
            msg = alarm.message.lower()
            # 接続断系のアラームが出ている機器の親を特定
            if "connection lost" in msg or "interface down" in msg:
                # 安全対策: デバイスIDがトポロジーに存在するか確認
                node = self.topology.get(alarm.device_id)
                if node and node.parent_id:
                    pid = node.parent_id
                    down_children_count[pid] = down_children_count.get(pid, 0) + 1

        for parent_id, count in down_children_count.items():
            # 閾値: 配下が2台以上同時に死んでいる場合
            if count >= 2:
                # 安全対策: 親IDがトポロジーに存在するか確認
                parent_node = self.topology.get(parent_id)
                if not parent_node: continue 

                # 親ノードが既に候補（アラーム持ち）かどうか確認
                existing = next((c for c in candidates if c['id'] == parent_id), None)
                
                if existing:
                    # 既に候補ならスコアを強化 (アラーム + 配下全滅 = 確定)
                    existing['prob'] = 1.0
                    existing['label'] += " (配下多重断)"
                else:
                    # アラームが無いのに配下が死んでいる -> サイレント障害として新規追加
                    candidates.append({
                        "id": parent_id,
                        "type": "Network/Silent",
                        "label": "サイレント障害 (配下デバイス一斉断)",
                        "prob": 0.98, # 非常に高い危険度
                        "alarms": [f"Downstream Impact: {count} devices lost"]
                    })

        # 4. ソート (スコアが高い順)
        candidates.sort(key=lambda x: x["prob"], reverse=True)
        
        # 候補がない場合（正常稼働）
        if not candidates:
            candidates.append({
                "id": "System", 
                "type": "Normal", 
                "label": "正常稼働中", 
                "prob": 0.0,
                "alarms": []
            })
            
        return candidates
