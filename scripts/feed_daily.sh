#!/usr/bin/env bash
# 每天抓一次财经快讯，只留强相关（政策/行业/自选股），写日志。
#
# 设计上只读不写库：feed 的候选不会自动变成事件，要入库必须人跑
#   events_cli.py feed --pick N --stock 代码 --yes
# 这样即使提示词或模型出错，也不会往事件库里灌垃圾。
#
# 用法：
#   ./scripts/feed_daily.sh               # 正常抓一次
#   ./scripts/feed_daily.sh --if-missing  # 当天还没抓过才抓（@reboot 补跑用）
#
# crontab 里两条：
#   30 8 * * *  /mnt/d/stock-tracker/scripts/feed_daily.sh
#   @reboot     /mnt/d/stock-tracker/scripts/feed_daily.sh --if-missing
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/feed-$(date +%Y-%m-%d).log"
PY="${PYTHON:-/usr/bin/python3}"

# WSL 关机期间不会跑 cron，开机补一次；当天已有日志就跳过
if [ "${1:-}" = "--if-missing" ] && [ -s "$LOG" ]; then
    echo "[skip] 今天的日志已存在: $LOG"
    exit 0
fi

{
    echo "===== $(date '+%F %T') 开始抓取 ====="
    "$PY" scripts/events_cli.py feed --strong
    echo "===== $(date '+%F %T') 结束 ====="
} >> "$LOG" 2>&1

# 只留最近 30 天
find "$LOG_DIR" -name 'feed-*.log' -mtime +30 -delete 2>/dev/null

echo "[ok] 写入 $LOG"
