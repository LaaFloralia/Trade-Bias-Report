# fundamental-macro-analysis — シークレット参照テンプレート（1Password CLI `op run` 用）
# コミット対象。値は書かない。op:// 参照のみ。保管庫は Agents のみ。
# 実行: ./scripts/run-with-secrets.sh uv run python main.py

TWELVEDATA_API_KEY=op://Agents/TwelveData/credential
FRED_API_KEY=op://Agents/Fred/credential

# Bias Report HTML の発行先（Supabase Storage・固定 URL、2026-08-23〜）
SUPABASE_URL=op://Agents/FundamentalMacroAnalysis/supabase_url
SUPABASE_SERVICE_ROLE_KEY=op://Agents/FundamentalMacroAnalysis/supabase_service_role_key

# MyFXBook 公式API（コミュニティ見通し。画面はボット確認で取得不可のため、2026-09-26〜）
MYFXBOOK_USERNAME=op://Agents/Myfxbook/username
MYFXBOOK_PASSWORD=op://Agents/Myfxbook/password
