"""pytest 共通設定: テストをローカルの秘密情報から独立させる。

TWELVEDATA_API_KEY は本番では Keychain 経由 (scripts/run-with-secrets.sh) で注入されるが、
テスト群は全てモックでネットワークに出ないため、未設定ならダミー値を置いて
「キー未設定エラー」分岐に落ちないようにする。config.py が import された時点で
モジュール定数に束縛されるため、テストモジュールの import より前（このファイルの
トップレベル）で設定する必要がある。
"""

import os

os.environ.setdefault("TWELVEDATA_API_KEY", "test-dummy-key")
