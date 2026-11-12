#!/bin/sh
# Configure the extracted native Linux/UOS bundle without installing or downloading anything.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$root/autoskill/SKILL.md" ]; then
  skill="$root/autoskill"
else
  root=$(CDPATH= cd -- "$root/.." && pwd)
  skill="$root/skills/autoskill"
fi
case "$(uname -m)" in
  x86_64|amd64) target=linux-x64 ;;
  aarch64|arm64) target=linux-arm64 ;;
  *) echo '仅提供 Linux x86_64 / ARM64 包。' >&2; exit 2 ;;
esac
vendor="$skill/vendor/$target"
if [ ! -f "$vendor/python/bin/python3" ]; then
  echo "没有匹配 CPU 的离线运行时：$vendor/python/bin/python3。源码安装见 docs/Linux-DSH.md。" >&2
  exit 2
fi
# Some graphical ZIP extractors drop Unix executable bits. All paths are inside this bundle.
chmod u+x "$skill/autoskill" "$vendor/python/bin/"python* "$vendor/agent-browser/agent-browser"
for binary in "$vendor"/chrome/chrome-*/chrome "$vendor"/chrome/chrome-*/chrome_crashpad_handler \
  "$vendor"/chrome/chrome-*/chrome_sandbox "$vendor"/chromedriver/chromedriver-*/chromedriver \
  "$vendor/python/lib/python3.12/site-packages/playwright/driver/node"; do
  if [ -f "$binary" ]; then chmod u+x "$binary"; fi
done
export PYTHONPATH="$skill/app" PYTHONNOUSERSITE=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
exec "$vendor/python/bin/python3" -m autoskill.platforms "$@"
