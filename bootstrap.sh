#!/usr/bin/env bash
# Run inside Ubuntu WSL. No sudo, shell activation, or edits to Miniforge base.
set -Eeuo pipefail
umask 077

MODE="${1:-inspect}"
case "$MODE" in inspect|install) ;; *) printf 'Usage: bash bootstrap.sh [inspect|install]\n' >&2; exit 2;; esac
SETUP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="${WES_QC_PROJECT:-$HOME/wes_qc_project}"
TOOLS_ROOT="$PROJECT_ROOT/tools"
STATE_DIR="$PROJECT_ROOT/setup"
QC_PREFIX="$TOOLS_ROOT/qc-env"
TASK_CONDA_EXE="${WES_QC_CONDA_EXE:-${CONDA_EXE:-}}"
if [[ -z "$TASK_CONDA_EXE" ]]; then TASK_CONDA_EXE="$(type -P conda || true)"; fi
if [[ -z "$TASK_CONDA_EXE" ]]; then
    for candidate in "$HOME/miniforge3/bin/conda" "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda"; do
        if [[ -x "$candidate" ]]; then TASK_CONDA_EXE="$candidate"; break; fi
    done
fi
[[ -n "$TASK_CONDA_EXE" && -x "$TASK_CONDA_EXE" ]] || {
    printf 'Conda is required. Activate it or set WES_QC_CONDA_EXE to its absolute executable path.\n' >&2
    exit 2
}
TASK_CONDA_EXE="$(readlink -f -- "$TASK_CONDA_EXE")"
MINIFORGE="$(dirname -- "$(dirname -- "$TASK_CONDA_EXE")")"
BASE_PY="$MINIFORGE/bin/python"
SELECTED="$STATE_DIR/qc_env_path.txt"
YAML="$SETUP_DIR/environment.yml"
[[ "$PROJECT_ROOT" == /* ]] || { printf 'WES_QC_PROJECT must be an absolute Linux path.\n' >&2; exit 2; }


[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || { printf 'Run this in Ubuntu WSL (Linux x86_64).\n' >&2; exit 2; }
[[ -x "$BASE_PY" && -x "$TASK_CONDA_EXE" && -r "$YAML" ]] || { printf 'Miniforge Python/conda or environment.yml is missing.\n' >&2; exit 2; }
command -v timeout >/dev/null || { printf 'The timeout command is required.\n' >&2; exit 2; }
for item in "$TOOLS_ROOT" "$QC_PREFIX" "$SELECTED"; do
    [[ ! -L "$item" ]] || { printf 'Refusing symbolic-link output path: %s\n' "$item" >&2; exit 2; }
done
mkdir -p -- "$TOOLS_ROOT"/{pkgs,envs,tmp,cache,config,data} "$STATE_DIR/setup_logs" "$STATE_DIR/environment_metadata"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
LOG="$STATE_DIR/setup_logs/bootstrap-${MODE}-${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1
trap 'rc=$?; printf "[%s] bootstrap exit=%s\n" "$(date -Is)" "$rc"; exit "$rc"' EXIT
printf '[%s] Mode: %s; log: %s\n' "$(date -Is)" "$MODE" "$LOG"

# Keep caches, temporary files, and configuration writes within the project.
# register_envs=false avoids changing ~/.conda/environments.txt.
export PYTHONDONTWRITEBYTECODE=1
export CONDA_REGISTER_ENVS=false CONDA_AUTO_UPDATE_CONDA=false
export CONDA_NOTIFY_OUTDATED_CONDA=false CONDA_REPORT_ERRORS=false
export CONDA_NUMBER_CHANNEL_NOTICES=0 CONDA_CHANNEL_PRIORITY=strict
export CONDA_PKGS_DIRS="$TOOLS_ROOT/pkgs" CONDA_ENVS_PATH="$TOOLS_ROOT/envs"
export TMPDIR="$TOOLS_ROOT/tmp" XDG_CACHE_HOME="$TOOLS_ROOT/cache"
export XDG_CONFIG_HOME="$TOOLS_ROOT/config" XDG_DATA_HOME="$TOOLS_ROOT/data"
export CONDARC=/dev/null LC_ALL=C.UTF-8
: > "$SELECTED"

# Read known prefixes without "conda env list", which can rewrite its registry.
# Also parse this deliberately simple YAML without eval or a YAML dependency.
"$BASE_PY" - "$MINIFORGE" "$QC_PREFIX" "$STATE_DIR" "$YAML" "$TOOLS_ROOT/tmp/specs.txt" <<'PY'
import glob, json, os, pathlib, re, sys
base, target, setup, yaml_path, spec_path = map(pathlib.Path, sys.argv[1:])
text = yaml_path.read_text(encoding="utf-8")
parts = text.split("dependencies:\n")
if len(parts) != 2:
    raise SystemExit("Expected one dependencies section in environment.yml")
specs = []
for line in parts[1].splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    m = re.fullmatch(r"  - (python|fastqc|multiqc)=([0-9]+(?:\.[0-9]+)*)", line)
    if not m:
        raise SystemExit("Unexpected dependency syntax: " + line)
    specs.append((m[1], m[2]))
expected = {"python": "3.12", "fastqc": "0.12.1", "multiqc": "1.35"}
if dict(specs) != expected or len(specs) != len(expected):
    raise SystemExit("Unexpected package constraints; review bootstrap and YAML together.")
spec_path.write_text("".join(f"{k}={v}\n" for k, v in specs), encoding="utf-8")
candidates = [target, base]
registry = (pathlib.Path.home() / ".conda" / "environments.txt")
if registry.is_file():
    with registry.open(encoding="utf-8") as handle:
        raw = handle.read(1024 * 1024)
    candidates.extend(pathlib.Path(p.strip()) for p in raw.splitlines()[:256] if p.strip().startswith("/"))
for root in (base / "envs", (pathlib.Path.home() / ".conda" / "envs"), target.parent / "envs"):
    if root.is_dir():
        candidates.extend(sorted(root.iterdir())[:256])
active = os.environ.get("CONDA_PREFIX")
if active and active.startswith("/"):
    candidates.append(pathlib.Path(active))
rows, seen, selected = [], set(), None
for prefix in candidates[:768]:
    prefix = prefix.resolve()
    if str(prefix) in seen or not (prefix / "conda-meta").is_dir():
        continue
    seen.add(str(prefix))
    versions = {}
    for name in expected:
        for file in sorted((prefix / "conda-meta").glob(name + "-*.json"))[:16]:
            try:
                record = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("name") == name:
                versions[name] = record.get("version", "")
    matched = all(versions.get(n) == v or (n == "python" and versions.get(n, "").startswith(v + ".")) for n, v in expected.items())
    executable = all(os.access(prefix / "bin" / n, os.X_OK) for n in ("python", "fastqc", "multiqc", "java"))
    ready = bool(matched and executable)
    rows.append({"prefix": str(prefix), "versions": versions, "matches_requested_tools": ready})
    if ready and selected is None:
        selected = str(prefix)
report = {"requested_versions": expected, "checked_environments": rows, "selected_prefix_before_runtime_check": selected}
(setup / "environment_discovery.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
if selected:
    (setup / "qc_env_path.txt").write_text(selected + "\n", encoding="utf-8")
print(json.dumps(report, indent=2))
PY
mapfile -t SPECS < "$TOOLS_ROOT/tmp/specs.txt"

verify_prefix() {
    local prefix=$1
    printf 'Checking runtime at %s\n' "$prefix"
    timeout --foreground --kill-after=2s 30s env PATH="$prefix/bin:/usr/bin:/bin" "$prefix/bin/python" -c 'import sys; assert sys.version_info[:2] == (3,12); print(sys.version)' || return 1
    timeout --foreground --kill-after=2s 30s env PATH="$prefix/bin:/usr/bin:/bin" "$prefix/bin/fastqc" --version || return 1
    timeout --foreground --kill-after=2s 30s env PATH="$prefix/bin:/usr/bin:/bin" "$prefix/bin/multiqc" --version || return 1
}

if [[ -s "$SELECTED" ]]; then
    IFS= read -r chosen < "$SELECTED"
    if ! verify_prefix "$chosen"; then
        printf 'Existing environment failed its runtime check; no environment selected.\n'
        : > "$SELECTED"
    fi
fi
if [[ ! -s "$SELECTED" && "$MODE" == inspect ]]; then
    printf 'No compatible QC environment found. Run install mode to create the project environment.\n'
    exit 0
fi

if [[ ! -s "$SELECTED" ]]; then
    [[ ! -e "$QC_PREFIX" ]] || { printf 'The project prefix already exists but is not ready: %s\nNo changes made to it. Review the log before repairing it.\n' "$QC_PREFIX"; exit 3; }
    SOLVER=classic
    if compgen -G "$MINIFORGE/conda-meta/conda-libmamba-solver-*.json" >/dev/null; then SOLVER=libmamba; fi
    printf 'Creating QC environment: %s (solver=%s)\n' "$QC_PREFIX" "$SOLVER"
    timeout --foreground --signal=TERM --kill-after=30s 45m "$TASK_CONDA_EXE" create \
        --yes --prefix "$QC_PREFIX" --solver "$SOLVER" \
        --override-channels \
        --channel https://conda.anaconda.org/conda-forge \
        --channel https://conda.anaconda.org/bioconda \
        --strict-channel-priority --no-default-packages "${SPECS[@]}"
    verify_prefix "$QC_PREFIX"
    printf '%s\n' "$QC_PREFIX" > "$SELECTED"
fi

IFS= read -r chosen < "$SELECTED"
META="$STATE_DIR/environment_metadata"
timeout --foreground --kill-after=2s 60s "$TASK_CONDA_EXE" list --prefix "$chosen" --explicit --md5 > "$META/packages-explicit-${STAMP}.txt"
timeout --foreground --kill-after=2s 60s "$TASK_CONDA_EXE" list --prefix "$chosen" --json > "$META/packages-${STAMP}.json"
{
    printf 'Prefix: %s\nChecked: %s\n' "$chosen" "$(date -Is)"
    verify_prefix "$chosen"
} > "$META/versions-${STAMP}.txt"
printf 'QC environment ready: %s\nSelection file: %s\n' "$chosen" "$SELECTED"
