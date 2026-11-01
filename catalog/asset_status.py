"""アセットのステータス（このデータを今使ってよいか）。

画面や判定の挙動を左右するため、管理者が名前や選択肢を変えられるプロパティではなく、
値を固定した assets.status 列で持つ。値を変えるときは catalog/db.py の CHECK 制約も合わせて変える。
"""

ACTIVE = "運用中"
DEPRECATED = "非推奨"  # 動いているが、新しい利用は避けてほしい
RETIRED = "廃止"  # 維持をやめた。記録として残している

ASSET_STATUSES = [ACTIVE, DEPRECATED, RETIRED]
