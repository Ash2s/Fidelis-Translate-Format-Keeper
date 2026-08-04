#!/usr/bin/env bash
# Auto-deploy script triggered by GitHub webhook
set -e

cd /home/admin/apps/Immigration-Translation

# Log
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Webhook triggered — pulling latest code..." >> /tmp/deploy.log

# Discard any local changes (we're tracking the remote)
git fetch origin master 2>&1 >> /tmp/deploy.log
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Already up-to-date, no action needed." >> /tmp/deploy.log
    echo "NO_UPDATE"
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Changes detected. Updating $LOCAL -> $REMOTE" >> /tmp/deploy.log

# Pull changes
git reset --hard origin/master 2>&1 >> /tmp/deploy.log

# Re-apply the subpath compatibility patch (frontend absolute-path fix)
# (webhook 用 git reset 会覆盖本地改动，这里重新打补丁保证子路径部署不回归)
python3 - <<'PYEOF' >> /tmp/deploy.log 2>&1
import re
p = '/home/admin/apps/Immigration-Translation/static/index.html'
s = open(p).read()
changed = False
# 1) API_BASE 自适应子路径：若未注入 apiBase() 则注入
if 'function apiBase()' not in s:
    s = s.replace(
        "const API_BASE = window.location.port === '17573' ? 'http://localhost:8000/api' : '/api';",
        "// 自动适配子路径部署（nginx 子路径）：页面在 /xxx/ 下时 API 自动带上该前缀\nfunction apiBase() { const m = location.pathname.match(/^\\/[^/]+\\//); return m ? m[0].replace(/\\/$/, '') : ''; }\nconst API_BASE = window.location.port === '17573' ? 'http://localhost:8000/api' : (apiBase() + '/api');",
        1
    )
    changed = True
# 2) COLT 图标绝对路径改相对路径
if 'src="/static/COLT.png"' in s:
    s = s.replace('src="/static/COLT.png"', 'src="static/COLT.png"')
    changed = True
open(p, 'w').write(s)
print('patched fidelis index.html:', 'patched' if changed else 'idempotent')
PYEOF

# Restart service
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Restarting service..." >> /tmp/deploy.log
OLD_PID=$(pgrep -f 'uvicorn.*main:app.*8000' | head -1)
kill "$OLD_PID" 2>/dev/null || true
sleep 1
cd /home/admin/apps/Immigration-Translation
nohup ./venv/bin/python3 ./venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/immigration_translation.log 2>&1 &

# Also update the static folder copy if needed
cp static/index.html /home/admin/Immigration-Translation/static/index.html 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deploy complete. New PID: $(pgrep -f 'uvicorn.*main:app.*8000' | head -1)" >> /tmp/deploy.log
echo "UPDATED"
