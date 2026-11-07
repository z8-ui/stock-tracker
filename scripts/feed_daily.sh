#!/usr/bin/env bash
# 每天抓一次财经快讯，只留强相关（政策/行业/自选股），写日志。
#
# 设计上只读不写库：feed 的候选不会自动变成事件，要入库必须人跑
#   events_cli.py feed --pick N --stock 代码 --yes
# 这样即使提示词或模型出错，也不会往事件库里灌垃圾。
#
# 用法：
#   ./scripts/feed_daily.sh               # 正常抓一次
#   ./scripts/feed_daily.sh --if-missing  # 今天还没有“成功标记”才抓（@reboot 补跑用）
#
# crontab 里两条：
#   30 8 * * *  /mnt/d/stock-tracker/scripts/feed_daily.sh
#   @reboot     /mnt/d/stock-tracker/scripts/feed_daily.sh --if-missing
#
# 退出码：透传 events_cli 的退出码
#   0 = 成功（至少一个源成功；没有新候选也算成功）
#   1 = 任务级失败（数据库/抽取全失败等）
#   3 = 所有新闻源都失败
#   2 = 参数错误
# 旧实现用 `[ -s $LOG ]` 判断“今天跑过”，但日志只要有一行（哪怕全是报错）就算存在，
# 失败任务会被永久跳过。现在只认成功标记（logs/.feed_last_success.json）。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
LOG_DIR="${FEED_LOG_DIR:-logs}"
mkdir -p "$LOG_DIR" || exit 2
LOG="$LOG_DIR/feed-$(date +%Y-%m-%d).log"
MARK="$LOG_DIR/.feed_last_success.json"
PY="${PYTHON:-/usr/bin/python3}"
CLI="${FEED_CLI:-scripts/events_cli.py}"
TODAY="$(date +%Y-%m-%d)"

if [ "${1:-}" = "--if-missing" ]; then
    if [ -s "$MARK" ] && grep -q "\"date\": *\"$TODAY\"" "$MARK"; then
        echo "[skip] 今天已有成功采集标记: $MARK"
        exit 0
    fi
    echo "[run] 今天还没有成功标记，补跑一次"
fi

echo "===== $(date '+%F %T') 开始抓取 =====" >> "$LOG"

"$PY" "$CLI" feed --strong >> "$LOG" 2>&1
rc=$?

{
    echo "--- events_cli 退出码: $rc"
    "$PY" - <<'PYEOF'
import event_feed

try:
    print("--- 候选状态统计:", event_feed.decision_stats())
except Exception as e:                      # 统计失败不能掩盖主命令的退出码
    print(f"--- 状态统计失败: {type(e).__name__}: {e}")
PYEOF
    echo "===== $(date '+%F %T') 结束 (exit=$rc) ====="
} >> "$LOG" 2>&1

if [ "$rc" -eq 0 ]; then
    # 只在成功时写标记：失败后 --if-missing 会重新跑，而不是被“日志存在”骗过去
    printf '{"date": "%s", "ts": "%s", "exit": 0}\n' "$TODAY" "$(date '+%F %T')" > "$MARK"
else
    rm -f "$MARK" 2>/dev/null
fi

# 只留最近 30 天
find "$LOG_DIR" -name 'feed-*.log' -mtime +30 -delete 2>/dev/null

echo "[exit=$rc] 写入 $LOG"
exit "$rc"
