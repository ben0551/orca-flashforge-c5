#!/usr/bin/env bash
# Run both suites. Neither needs OrcaSlicer or a printer.
set -uo pipefail
cd "$(dirname "$0")"

status=0
echo "=== plugin behaviour (python) ==="
python3 tests/test_plugin.py || status=1

echo
echo "=== tab page (node) ==="
if command -v node >/dev/null 2>&1; then
  python3 tests/test_page_js.py || status=1
else
  echo "node not installed — skipping the page checks"
fi

echo
[ "$status" -eq 0 ] && echo "ALL GREEN" || echo "FAILURES ABOVE"
exit "$status"
