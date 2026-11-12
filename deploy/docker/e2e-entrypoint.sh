#!/bin/sh
set -eu

source_dir="${HL_APP_E2E_SOURCE_DIR:-/src}"
work=/tmp/hl-work
mkdir -p "$work"
cp -a "$source_dir/backend" "$source_dir/frontend" "$source_dir/tests" "$work/"
cp -a /opt/hl-frontend/node_modules "$work/frontend/"
export PYTHONPATH="$work/backend"
cd "$work/frontend"
exec npm run test:e2e -- "$@"
