#!/usr/bin/env bash
# 用法：backfill-step.sh 分鐘 指令...
# 在時間內自己結束（先送 INT、60 秒後強制），避免被 Actions 的步驟逾時砍掉後還佔著資料庫的鎖。
# 失敗時把最後 15 行錯誤訊息寫成公開的 warning 註記（登入前看不到記錄檔）。
set -u
minutes=$1
shift
log=$(mktemp)
timeout -s INT -k 60 "${minutes}m" "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    why="超過 ${minutes} 分鐘，已中斷（下次續跑）"
  else
    why="結束代碼 $rc"
  fi
  msg=$(tail -n 15 "$log" | awk 'BEGIN { ORS = "%0A" } { gsub(/%/, "%25"); gsub(/[[:cntrl:]]/, ""); print }')
  echo "::warning title=$* — ${why}::${msg}"
fi
rm -f "$log"
exit 0
