#!/usr/bin/env python3
"""Launch WES checksum/QC preparation inside the user's Ubuntu."""
import argparse, datetime, hashlib, json, os, pathlib, signal, subprocess, sys, time

SETUP = pathlib.Path(__file__).resolve().parent


def configured_path(variable, default):
    value = pathlib.Path(os.environ.get(variable, str(default))).expanduser()
    if not value.is_absolute():
        raise RuntimeError(variable + " must be an absolute path.")
    return value.resolve()


PROJECT = configured_path("WES_QC_PROJECT", pathlib.Path.home() / "wes_qc_project")
OUT = configured_path("WES_QC_OUTPUT", PROJECT / "results")
MANIFEST = configured_path("WES_QC_MANIFEST", PROJECT / "inputs.json")
STATUS = OUT / "launcher_status.json"
LOCK_DIR = PROJECT / "runtime" / "locks"
LOCK = LOCK_DIR / ("launcher-" + hashlib.sha256(str(OUT).encode()).hexdigest()[:16] + ".lock")
BASE_PY = pathlib.Path(sys.executable).resolve()
UTC = datetime.timezone.utc
ACTIVE_STATES = {"starting", "preparing_environment", "running_qc", "stopping"}


def now():
    return datetime.datetime.now(UTC).isoformat()


def write_json(path, obj):
    tmp = path.with_name(path.name + ".tmp." + str(os.getpid()))
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def process_stat(pid):
    raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
    return raw[raw.rfind(")") + 2:].split()


def process_start_ticks(pid):
    return process_stat(pid)[19]


def acquire():
    requirements()
    import fcntl
    OUT.mkdir(parents=True, exist_ok=True)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("The WES workflow is already running. Use status.")
    return handle


def lock_active():
    import fcntl
    OUT.mkdir(parents=True, exist_ok=True)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def requirements():
    for name in ("bootstrap.sh", "environment.yml", "run_qc.py"):
        if not (SETUP / name).is_file():
            raise RuntimeError("Missing prepared file: " + name)
    manifest = read_json(MANIFEST)
    raw_value = manifest.get("raw_root")
    if not isinstance(raw_value, str):
        raise RuntimeError("Set WES_QC_MANIFEST to a prepared private input manifest.")
    raw_root = pathlib.Path(raw_value).expanduser()
    if not raw_root.is_absolute() or not raw_root.is_dir():
        raise RuntimeError("The manifest raw_root is unavailable or not absolute.")
    raw_root = raw_root.resolve()
    if OUT == raw_root or OUT.is_relative_to(raw_root) or PROJECT == raw_root or PROJECT.is_relative_to(raw_root):
        raise RuntimeError("Keep project and result directories outside raw_root.")
    if not BASE_PY.is_file():
        raise RuntimeError("The bootstrap Python interpreter is unavailable.")


class WorkflowCancelled(Exception):
    pass


def worker(lock_fd=None):
    pid = os.getpid()
    state = {"pid": pid, "started_at_utc": now(), "out_dir": str(OUT),
             "scope": "MD5 + FastQC + MultiQC only"}
    requested_signal = None

    def update(status, **extra):
        state.update(status=status, updated_at_utc=now(), **extra)
        write_json(STATUS, state)

    def interrupted(signum, frame):
        # Do not raise into subprocess.run/wait: that can kill the QC parent
        # before it has cleaned up its own FastQC/MultiQC process groups.
        nonlocal requested_signal
        requested_signal = signum

    def check_cancelled():
        if requested_signal is not None:
            raise WorkflowCancelled(f"Signal {requested_signal}")

    def run_stage(command, env, label):
        check_cancelled()
        update(state.get("status", "preparing_environment"), active_stage=label)
        proc = subprocess.Popen(command, cwd=SETUP, env=env)
        forwarded = False
        # Keep the launcher lock until the actual child exits, including cleanup.
        while True:
            if requested_signal is not None and not forwarded:
                update("stopping", active_stage=label)
                try:
                    # stop() already signalled the worker process group. Only
                    # forward to a surviving direct child, covering creation races
                    # without signalling every cleanup process a second time.
                    if proc.poll() is None:
                        proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
                forwarded = True
            try:
                code = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        check_cancelled()
        if code:
            raise subprocess.CalledProcessError(code, command)

    previous_handlers = {}
    try:
        if lock_fd is not None:
            os.fstat(lock_fd)
        state["process_start_ticks"] = process_start_ticks(pid)
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, interrupted)
        update("starting")
        requirements()
        check_cancelled()
        update("preparing_environment")
        env = os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE="1", LC_ALL="C.UTF-8",
                   WES_QC_PROJECT=str(PROJECT), WES_QC_OUTPUT=str(OUT),
                   WES_QC_MANIFEST=str(MANIFEST))
        run_stage(["bash", str(SETUP / "bootstrap.sh"), "inspect"], env, "inspect")
        selected = PROJECT / "setup" / "qc_env_path.txt"
        prefix_text = selected.read_text(encoding="utf-8").strip() if selected.exists() else ""
        if not prefix_text:
            check_cancelled()
            run_stage(["bash", str(SETUP / "bootstrap.sh"), "install"], env, "install")
            prefix_text = selected.read_text(encoding="utf-8").strip()
        check_cancelled()
        prefix = pathlib.Path(prefix_text)
        if not prefix_text or not prefix.is_absolute() or "\n" in prefix_text or not (prefix / "bin/python").is_file():
            raise RuntimeError("No valid QC environment was selected.")
        env["PATH"] = str(prefix / "bin") + ":/usr/bin:/bin"
        update("running_qc", environment_prefix=str(prefix))
        run_stage([str(prefix / "bin/python"), str(SETUP / "run_qc.py"), "--manifest",
                   str(MANIFEST), "--env-prefix", str(prefix),
                   "--out-dir", str(OUT), "--lock-dir", str(LOCK_DIR)], env, "qc")
        check_cancelled()
        update("complete", finished_at_utc=now())
        print("Full checksum/QC completed. Variant calling has not been run.", flush=True)
        return 0
    except (WorkflowCancelled, KeyboardInterrupt) as exc:
        update("interrupted", error=str(exc), finished_at_utc=now())
        print("Interrupted after child exit. Valid per-file results can be reused on restart.", flush=True)
        return 130
    except Exception as exc:
        update("failed", error=str(exc), finished_at_utc=now())
        print("Preparation/QC failed: " + str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def start():
    handle = acquire()
    proc = None
    log = None
    try:
        requirements()
        logs = OUT / "launcher_logs"
        logs.mkdir(exist_ok=True)
        stamp = datetime.datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + str(os.getpid())
        log = logs / (stamp + ".log")
        with log.open("ab", buffering=0) as stream:
            proc = subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()),
                     "_worker", "--lock-fd", str(handle.fileno())], cwd=SETUP, stdin=subprocess.DEVNULL,
                     stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
                     pass_fds=(handle.fileno(),))
        write_json(OUT / "last_launch.json", {"pid": proc.pid, "log": str(log), "started_at_utc": now()})
        time.sleep(0.8)
        code = proc.poll()
        if code is not None and code != 0:
            raise RuntimeError(f"Workflow exited early (code={code}). Read {log}")
        print("WES preparation/QC completed." if code == 0 else "WES preparation/QC started in the background.")
        print("Log: " + str(log))
        print("Progress: " + str(OUT / "status.json"))
        print("Keep the machine awake and all manifest input storage accessible.")
        return 0
    except Exception as exc:
        if proc is None or proc.poll() is not None:
            current = read_json(STATUS)
            if proc is None or current.get("pid") != proc.pid or current.get("status") in ACTIVE_STATES:
                write_json(STATUS, {"status": "start_failed", "updated_at_utc": now(),
                                   "error": str(exc), "log": str(log) if log else None,
                                   "pid": proc.pid if proc else None})
        else:
            print("The worker was launched, but launch bookkeeping failed; check status before retrying.",
                  file=sys.stderr)
        raise
    finally:
        # No explicit unlock: the inherited open-file description remains locked
        # until the worker exits after its current child has actually stopped.
        handle.close()


def status():
    active = lock_active()
    launcher = read_json(STATUS)
    stale = not active and launcher.get("status") in ACTIVE_STATES
    effective = ("not_running_previous_run_interrupted" if stale else
                 launcher.get("status", "starting_or_running" if active else "not_started"))
    if active and effective not in ACTIVE_STATES:
        effective = "starting_or_running"
    print(json.dumps({"process_lock_active": active, "effective_status": effective,
                      "stale_launcher_record": stale, "launcher": launcher,
                      "qc": read_json(OUT / "status.json"),
                      "last_launch": read_json(OUT / "last_launch.json")},
                     ensure_ascii=False, indent=2))
    return 0


def same_worker(state):
    try:
        pid = int(state["pid"])
        fields = process_stat(pid)
        if fields[0] == "Z" or fields[19] != state.get("process_start_ticks"):
            return False
        cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return (os.fsencode(str(pathlib.Path(__file__).resolve())) in cmdline
                and b"_worker" in cmdline and os.getpgid(pid) == pid and os.getsid(pid) == pid)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False


def stop():
    state = read_json(STATUS)
    if state.get("status") not in ACTIVE_STATES:
        raise RuntimeError("No running background workflow is recorded. Check status.")
    if not lock_active() or not same_worker(state):
        raise RuntimeError("The recorded workflow is not active or its identity cannot be verified. No process was signalled.")
    pid = int(state["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("The verified workflow exited before the stop signal was sent.")
        return 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not same_worker(state):
            if lock_active():
                print("The previous worker identity is no longer active or readable, but a launcher lock is still held. Stop completion is unconfirmed; check status before restarting.")
                return 2
            print("The verified WES worker has stopped and released its launcher lock. Completed per-file results remain.")
            return 0
        time.sleep(0.25)
    print("Stop requested, but the verified worker is still shutting down after 30 seconds. "
          "It has not been forcibly killed; check status and do not start another run yet.")
    return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="start", choices=["start", "run", "status", "stop", "_worker"])
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run this launcher inside Ubuntu WSL.")
    if args.mode == "start": return start()
    if args.mode == "status": return status()
    if args.mode == "stop": return stop()
    if args.mode == "_worker":
        if args.lock_fd is None:
            parser.error("_worker requires an inherited lock descriptor.")
        return worker(lock_fd=args.lock_fd)
    with acquire():
        return worker()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
