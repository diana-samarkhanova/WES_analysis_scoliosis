#!/usr/bin/env bash
set -euo pipefail
SETUP_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
BOOTSTRAP_PY="${WES_QC_BOOTSTRAP_PYTHON:-}"
if [[ -z "$BOOTSTRAP_PY" ]]; then BOOTSTRAP_PY="$(type -P python3 || true)"; fi
[[ -n "$BOOTSTRAP_PY" && -x "$BOOTSTRAP_PY" ]] || {
    printf 'Python 3 is required; set WES_QC_BOOTSTRAP_PYTHON to its executable if needed.\n' >&2
    exit 2
}
if (($# == 0)); then set -- start; fi
exec "$BOOTSTRAP_PY" "$SETUP_DIR/launch_qc.py" "$@"
