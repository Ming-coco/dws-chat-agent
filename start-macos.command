#!/bin/zsh
set -euo pipefail

APP_DIR="${0:A:h}"
cd "$APP_DIR"

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  open -a TextEdit .env
  print "已创建 .env。填写配置后，再双击 start-macos.command。"
  read -r "?按回车关闭…"
  exit 0
fi

python3 dm_agent.py doctor
./manage.sh start
open http://127.0.0.1:8765/
print "DWS Chat Agent 已启动，控制台已打开。"
