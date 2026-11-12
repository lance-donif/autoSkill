#!/bin/sh
# Start the installed Linux DSH Web host; setup is documented in docs/Linux-DSH.md.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tools="${AUTOSKILL_DSH_TOOLS:-$root/../autoskill-linux-tools}"
PATH="$tools/node_modules/.bin:$PATH"
export PATH
if ! command -v dsh >/dev/null 2>&1; then
  echo "DSH is not installed. Follow $root/docs/Linux-DSH.md" >&2
  exit 2
fi
cd "$root"
exec dsh web --no-open "$@"
