#!/bin/bash
# ─────────────────────────────────────────────────────────
# strm_sync.sh - 同步入口脚本（供 cron / 刮削工具调用）
# ─────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="python3"
SYNC_SCRIPT="$SCRIPT_DIR/strm_sync.py"
LOCKFILE="/tmp/strm_sync.lock"
LOG="/var/log/strm_sync.log"

# ── 防止并发执行 ──────────────────────────────────────────
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 另一个同步实例正在运行 (PID: $LOCK_PID)，退出"
        exit 0
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 清理过期锁文件"
        rm -f "$LOCKFILE"
    fi
fi

echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

# ── 解析参数 ─────────────────────────────────────────────
MODE="${1:-incremental}"  # incremental | full | metadata-only

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动同步 (模式: $MODE)" | tee -a "$LOG"

case "$MODE" in
    incremental)
        $PYTHON "$SYNC_SCRIPT"
        ;;
    full)
        # 全量同步 + 清理孤立文件，建议每周运行一次
        $PYTHON "$SYNC_SCRIPT" --full-cleanup
        ;;
    metadata-only)
        $PYTHON "$SYNC_SCRIPT" --metadata-only
        ;;
    *)
        echo "用法: $0 [incremental|full|metadata-only]"
        exit 1
        ;;
esac

EXIT_CODE=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 同步结束 (退出码: $EXIT_CODE)" | tee -a "$LOG"
exit $EXIT_CODE
