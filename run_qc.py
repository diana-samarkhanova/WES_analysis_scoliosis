#!/usr/bin/env python3
"""Resumable, local-only raw WES checksum and QC stage. No read filtering or variant calling."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile

RUNNER_VERSION = "1.0.0"
FASTQC_ARGS = ["--threads", "1", "--memory", "1024", "--format", "fastq", "--noextract"]
MULTIQC_CONFIG = {
    "fn_clean_sample_names": False, "sample_names_replace": {}, "sample_names_rename": [],
    "ignore_samples": [], "no_version_check": True, "no_ai": True,
    "ai_summary": False, "ai_summary_full": False, "megaqc_upload": False,
    "make_data_dir": True, "data_format": "json", "report_readerrors": True,
}
STOP = threading.Event()
PROCESSES: set[subprocess.Popen] = set()
PROCESS_LOCK = threading.Lock()


class QcError(RuntimeError):
    pass


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def file_identity(path):
    path = Path(path)
    stat = path.stat()
    if not path.is_file():
        raise QcError(f"Not a regular input file: {path}")
    return {"path": str(path.resolve()), "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns,
            "device": stat.st_dev, "inode": stat.st_ino}


def task_key(entry):
    return f'{entry["sample_id"]}/L{entry["lane_label"]}/{entry["mate"]}'


def load_manifest(path):
    manifest = read_json(path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise QcError("Invalid or unsupported manifest schema.")
    files = manifest.get("files", [])
    if not isinstance(files, list) or len(files) != manifest.get("expected_files"):
        raise QcError("Input file count differs from manifest expected_files.")
    if not files:
        raise QcError("Manifest has no input files.")
    raw_root = Path(manifest["raw_root"]).resolve(strict=True)
    seen_paths, seen_keys, seen_names, samples, pairs = set(), set(), set(), set(), {}
    for entry in files:
        for field in ("sample_id", "batch", "lane_label", "mate", "path", "size_bytes", "expected_md5"):
            if field not in entry:
                raise QcError(f"Manifest entry missing {field}.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", entry["sample_id"]) or not re.fullmatch(r"[0-9]+", entry["lane_label"]):
            raise QcError("Unsafe sample ID or lane label.")
        if entry["mate"] not in ("R1", "R2"):
            raise QcError("Mate must be R1 or R2.")
        if not isinstance(entry["size_bytes"], int) or entry["size_bytes"] <= 0:
            raise QcError("Invalid expected input size.")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", entry["expected_md5"]):
            raise QcError("Invalid expected MD5.")
        entry["expected_md5"] = entry["expected_md5"].lower()
        p = Path(entry["path"])
        if not p.is_absolute() or "\n" in str(p) or "\r" in str(p):
            raise QcError("Input paths must be absolute and contain no newlines.")
        resolved = p.resolve(strict=True)
        if not resolved.is_relative_to(raw_root):
            raise QcError(f"Input is outside raw_root: {p}")
        if not re.search(r"\.(fastq|fq)\.gz$", p.name, re.I):
            raise QcError(f"Unexpected FASTQ suffix: {p}")
        if file_identity(p)["size_bytes"] != entry["size_bytes"]:
            raise QcError(f"Input size differs from expected manifest: {p}")
        key = task_key(entry)
        if str(resolved) in seen_paths or key in seen_keys or p.name in seen_names:
            raise QcError(f"Duplicate input path, task key or basename: {p}")
        seen_paths.add(str(resolved)); seen_keys.add(key); seen_names.add(p.name)
        samples.add(entry["sample_id"])
        pair = pairs.setdefault((entry["sample_id"], entry["lane_label"]), set())
        pair.add(entry["mate"])
    if len(samples) != manifest.get("expected_samples") or any(mates != {"R1", "R2"} for mates in pairs.values()):
        raise QcError("Sample count or R1/R2 pairing is incomplete.")
    return manifest


def verify_md5(entry, cached=None, progress=None):
    path = Path(entry["path"])
    before = file_identity(path)
    if before["size_bytes"] != entry["size_bytes"]:
        raise QcError(f"Input size changed: {path}")
    key = canonical_hash({"method": "compressed-file-md5-v1", "identity": before,
                          "expected_md5": entry["expected_md5"]})
    if (isinstance(cached, dict) and cached.get("cache_key") == key
            and cached.get("status") == "passed"
            and cached.get("observed_md5") == entry["expected_md5"]
            and cached.get("identity_after") == before):
        return dict(cached, reused=True)
    digest = hashlib.md5()
    count, last_progress = 0, 0.0
    with path.open("rb") as handle:
        while True:
            if STOP.is_set():
                raise InterruptedError("Checksum stage interrupted.")
            block = handle.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
            count += len(block)
            now = time.monotonic()
            if progress and now - last_progress > 5:
                progress(count, before["size_bytes"])
                last_progress = now
    after = file_identity(path)
    if before != after or count != before["size_bytes"]:
        raise QcError(f"Input changed while hashing: {path}")
    observed = digest.hexdigest()
    state = {"status": "passed" if observed == entry["expected_md5"] else "failed",
             "cache_key": key, "expected_md5": entry["expected_md5"], "observed_md5": observed,
             "identity_before": before, "identity_after": after, "bytes_read": count,
             "completed_at": utc_now(), "reused": False}
    return state


def parse_fastqc(zip_path, html_path, input_path):
    zip_path, html_path = Path(zip_path), Path(html_path)
    if not html_path.is_file() or html_path.stat().st_size == 0:
        raise QcError(f"Missing or empty FastQC HTML: {html_path}")
    html_prefix = html_path.read_bytes()[:8192].lower()
    if b"<html" not in html_prefix and b"<!doctype html" not in html_prefix:
        raise QcError("FastQC HTML is not recognisable HTML.")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            if archive.testzip() is not None:
                raise QcError("FastQC ZIP CRC validation failed.")
            data_members = [m for m in archive.namelist() if m.endswith("/fastqc_data.txt")]
            summaries = [m for m in archive.namelist() if m.endswith("/summary.txt")]
            if len(data_members) != 1 or len(summaries) != 1:
                raise QcError("Expected exactly one FastQC data and summary member.")
            data_info = archive.getinfo(data_members[0])
            if data_info.file_size > 100 * 1024 * 1024:
                raise QcError("Unexpectedly large FastQC data member.")
            text = archive.read(data_members[0]).decode("utf-8")
            summary = archive.read(summaries[0]).decode("utf-8")
    except (OSError, zipfile.BadZipFile, UnicodeError, KeyError) as exc:
        raise QcError(f"Invalid FastQC ZIP: {exc}") from exc
    if not text.startswith("##FastQC\t"):
        raise QcError("FastQC data has no version header.")
    current, statistics, module_status = None, {}, {}
    for line in text.splitlines():
        if line == ">>END_MODULE":
            current = None
        elif line.startswith(">>"):
            if current is not None:
                raise QcError("Unclosed FastQC module.")
            pieces = line[2:].split("\t")
            if len(pieces) != 2 or pieces[1] not in ("pass", "warn", "fail"):
                raise QcError("Invalid FastQC module header.")
            current, state = pieces
            module_status[current] = state
        elif current == "Basic Statistics" and line and not line.startswith("#"):
            pieces = line.split("\t", 1)
            if len(pieces) == 2:
                statistics[pieces[0]] = pieces[1]
    if current is not None or "Basic Statistics" not in module_status:
        raise QcError("Incomplete FastQC data.")
    expected_name = Path(input_path).name
    if statistics.get("Filename") != expected_name:
        raise QcError(f"FastQC filename mismatch: expected {expected_name}.")
    try:
        count = int(statistics["Total Sequences"])
    except (KeyError, ValueError) as exc:
        raise QcError("Missing/invalid FastQC total sequence count.") from exc
    if count <= 0:
        raise QcError("FastQC reports no sequences.")
    summary_status = {}
    for line in summary.splitlines():
        if not line:
            continue
        pieces = line.split("\t")
        if len(pieces) != 3 or pieces[2] != expected_name:
            raise QcError("FastQC summary filename/structure mismatch.")
        summary_status[pieces[1]] = pieces[0].lower()
    if summary_status != module_status:
        raise QcError("FastQC data and summary module statuses differ.")
    return {"filename": expected_name, "total_sequences": count, "basic_statistics": statistics,
            "module_status": module_status, "fastqc_version": text.splitlines()[0].split("\t", 1)[1],
            "zip_sha256": sha256_file(zip_path), "html_sha256": sha256_file(html_path)}


def validate_pair_counts(files, results):
    pairs = {}
    for entry in files:
        key = task_key(entry)
        pairs.setdefault((entry["sample_id"], entry["lane_label"]), {})[entry["mate"]] = results[key]["total_sequences"]
    rows = []
    for (sample, lane), mates in sorted(pairs.items()):
        if set(mates) != {"R1", "R2"} or mates["R1"] != mates["R2"]:
            raise QcError(f"R1/R2 read count mismatch: {sample} lane {lane}: {mates}")
        rows.append({"sample_id": sample, "lane_label": lane, "read_pairs": mates["R1"],
                     "counts_match": True, "full_read_id_order_verified": False})
    return rows


def run_command(command, log_path, env, cwd=None):
    if STOP.is_set():
        raise InterruptedError("QC interrupted.")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with Path(log_path).open("w", encoding="utf-8", newline="\n") as log:
        log.write(json.dumps({"command": command, "started_at": utc_now()}) + "\n")
        log.flush()
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                cwd=cwd, start_new_session=(os.name == "posix"))
        with PROCESS_LOCK:
            PROCESSES.add(proc)
        try:
            while proc.poll() is None:
                if STOP.wait(0.5):
                    terminate_process(proc)
                    raise InterruptedError("QC interrupted.")
            if proc.returncode != 0:
                raise QcError(f"Command failed with exit {proc.returncode}; see {log_path}")
        finally:
            with PROCESS_LOCK:
                PROCESSES.discard(proc)


def terminate_process(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=5)
    except ProcessLookupError:
        pass


def tool_text(command, env):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, timeout=30, check=False, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QcError(f"Tool check failed: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        raise QcError(f"Tool check exited {result.returncode}: {command[0]}: {result.stdout[:1500]}")
    return result.stdout.strip()


def environment_info(prefix, runtime_dir=None):
    prefix = Path(prefix).resolve(strict=True)
    env = os.environ.copy()
    env["PATH"] = str(prefix / "bin") + os.pathsep + "/usr/bin:/bin"
    if runtime_dir is not None:
        runtime_dir = Path(runtime_dir)
        for name in ("tmp", "cache", "config"):
            (runtime_dir / name).mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(runtime_dir / "tmp")
        env["XDG_CACHE_HOME"] = str(runtime_dir / "cache")
        env["XDG_CONFIG_HOME"] = str(runtime_dir / "config")
        env["MPLCONFIGDIR"] = str(runtime_dir / "cache" / "matplotlib")
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LC_ALL"] = "C.UTF-8"
    tools = {name: prefix / "bin" / name for name in ("fastqc", "multiqc", "java")}
    for name, path in tools.items():
        if not path.is_file() or not os.access(path, os.X_OK):
            raise QcError(f"Required tool missing from --env-prefix: {name}: {path}")
    fastqc_help = tool_text([str(tools["fastqc"]), "--help"], env)
    for flag in ("--threads", "--memory", "--format", "--noextract"):
        if flag not in fastqc_help:
            raise QcError(f"Installed FastQC does not advertise required option {flag}.")
    multiqc_help = tool_text([str(tools["multiqc"]), "--help"], env)
    for flag in ("--file-list", "--require-logs", "--no-version-check", "--fullnames"):
        if flag not in multiqc_help:
            raise QcError(f"Installed MultiQC does not advertise required option {flag}.")
    versions = {"fastqc": tool_text([str(tools["fastqc"]), "--version"], env),
                "multiqc": tool_text([str(tools["multiqc"]), "--version"], env),
                "java": tool_text([str(tools["java"]), "-version"], env)}
    packages = {p.name: sha256_file(p) for p in sorted((prefix / "conda-meta").glob("*.json"))}
    fingerprints = {name: {"path": str(path.resolve()), "sha256": sha256_file(path)}
                    for name, path in tools.items()}
    info = {"prefix": str(prefix), "versions": versions, "executables": fingerprints,
            "conda_metadata_sha256": canonical_hash(packages), "runner_version": RUNNER_VERSION,
            "runner_sha256": sha256_file(__file__)}
    return env, tools, info


def fastqc_one(entry, checksum, out_dir, tools, env, recipe):
    key = task_key(entry)
    task_dir = out_dir / "fastqc" / key
    task_dir.mkdir(parents=True, exist_ok=True)
    before = file_identity(entry["path"])
    if before != checksum["identity_after"]:
        raise QcError(f"Input changed since MD5 verification: {entry['path']}")
    cache_key = canonical_hash({"recipe": recipe, "identity": before, "md5": entry["expected_md5"]})
    state_path = task_dir / "completion.json"
    old = read_json(state_path)
    if isinstance(old, dict) and old.get("cache_key") == cache_key and old.get("status") == "passed":
        try:
            zip_path, html_path = Path(old["zip_path"]), Path(old["html_path"])
            if not zip_path.resolve().is_relative_to(task_dir.resolve()) or not html_path.resolve().is_relative_to(task_dir.resolve()):
                raise QcError("Cached report paths escaped task directory.")
            parsed = parse_fastqc(zip_path, html_path, entry["path"])
            if parsed != old["metrics"]:
                raise QcError("Cached report content differs.")
            if file_identity(entry["path"]) != before:
                raise QcError("Input changed while validating cached QC.")
            return dict(old, reused=True)
        except (OSError, ValueError, KeyError, QcError):
            pass
    attempt = task_dir / ("attempt_" + uuid.uuid4().hex)
    attempt.mkdir()
    command = [str(tools["fastqc"]), *FASTQC_ARGS, "--outdir", str(attempt), entry["path"]]
    run_command(command, attempt / "fastqc.log", env)
    after = file_identity(entry["path"])
    if after != before:
        raise QcError(f"Input changed during FastQC: {entry['path']}")
    zips, htmls = list(attempt.glob("*_fastqc.zip")), list(attempt.glob("*_fastqc.html"))
    if len(zips) != 1 or len(htmls) != 1:
        raise QcError(f"FastQC did not create exactly one ZIP and HTML: {attempt}")
    metrics = parse_fastqc(zips[0], htmls[0], entry["path"])
    result = {"status": "passed", "cache_key": cache_key, "task_key": key,
              "identity": after, "expected_md5": entry["expected_md5"],
              "zip_path": str(zips[0]), "html_path": str(htmls[0]),
              "metrics": metrics, "completed_at": utc_now(), "reused": False}
    atomic_json(state_path, result)
    return result


def normalize_header(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def validate_multiqc(directory, expected_results):
    directory = Path(directory)
    html_path = directory / "multiqc_report.html"
    data_dir = directory / "multiqc_data"
    if not html_path.is_file() or html_path.stat().st_size == 0:
        raise QcError("MultiQC HTML is missing or empty.")
    parsed = read_json(data_dir / "multiqc_fastqc.json")
    expected = {result["metrics"]["filename"]: result for result in expected_results}
    if not isinstance(parsed, dict) or set(parsed) != set(expected):
        missing = sorted(set(expected) - set(parsed or {}))
        extra = sorted(set(parsed or {}) - set(expected))
        raise QcError(f"MultiQC input/sample accounting differs; missing={missing[:10]}, extra={extra[:10]}.")
    for name, metrics in parsed.items():
        values = {normalize_header(k): v for k, v in metrics.items()}
        try:
            count = float(values["totalsequences"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QcError(f"MultiQC sequence count missing: {name}") from exc
        if count != expected[name]["metrics"]["total_sequences"]:
            raise QcError(f"MultiQC read count differs: {name}")
    source_path = data_dir / "multiqc_sources.json"
    sources = read_json(source_path)
    if not isinstance(sources, dict):
        raise QcError("MultiQC source audit JSON is missing or malformed.")
    observed_sources, observed_names = set(), set()
    for module, sections in sources.items():
        if not normalize_header(module).startswith("fastqc"):
            continue
        if not isinstance(sections, dict):
            raise QcError("Invalid MultiQC source-section structure.")
        for section, sample_sources in sections.items():
            if not isinstance(sample_sources, dict):
                raise QcError("Invalid MultiQC source-sample structure.")
            for sample_name, source in sample_sources.items():
                if not isinstance(source, str):
                    raise QcError("Invalid MultiQC source path.")
                observed_names.add(sample_name)
                observed_sources.add(str(Path(source).resolve()))
    if observed_names != set(expected):
        raise QcError("MultiQC source sample-name set differs from inputs.")
    expected_sources = {str(Path(r["zip_path"]).resolve()) for r in expected_results}
    if observed_sources != expected_sources:
        raise QcError("MultiQC source ZIP set differs from current validated FastQC files.")
    return {"validated_files": len(expected), "html_path": str(html_path),
            "html_sha256": sha256_file(html_path), "sources_sha256": sha256_file(source_path),
            "fastqc_json_sha256": sha256_file(data_dir / "multiqc_fastqc.json")}


def multiqc_all(results, out_dir, tools, env, info):
    root = out_dir / "multiqc"
    root.mkdir(exist_ok=True)
    attempt = root / ("attempt_" + uuid.uuid4().hex)
    attempt.mkdir()
    file_list = attempt / "fastqc_files.txt"
    file_list.write_text("".join(r["zip_path"] + "\n" for r in results), encoding="utf-8")
    config_path = attempt / "multiqc_config.json"
    config_path.write_text(json.dumps(MULTIQC_CONFIG, indent=2), encoding="utf-8")
    # JSON is a YAML subset; MultiQC accepts the explicit config file.
    command = [str(tools["multiqc"]), "--file-list", str(file_list), "--require-logs",
               "-m", "fastqc", "--no-version-check", "--fullnames", "--config",
               str(config_path), "--data-format", "json", "--outdir", str(attempt),
               "--filename", "multiqc_report.html"]
    # Filename naming normally produces multiqc_report_data; explicit config fixes canonical paths.
    cfg = dict(MULTIQC_CONFIG, output_fn_name="multiqc_report.html", data_dir_name="multiqc_data")
    config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    run_command(command, attempt / "multiqc.log", env, cwd=attempt)
    metrics = validate_multiqc(attempt, results)
    metrics.update({"status": "passed", "completed_at": utc_now(),
                    "recipe_sha256": canonical_hash({"environment": info, "config": cfg}),
                    "command": command})
    atomic_json(root / "completion.json", metrics)
    return metrics


def run(args):
    manifest = load_manifest(args.manifest)
    files = manifest["files"]
    out_dir = Path(args.out_dir).resolve()
    raw_root = Path(manifest["raw_root"]).resolve()
    if out_dir == raw_root or out_dir.is_relative_to(raw_root):
        raise QcError("--out-dir must be outside the raw input directory.")
    out_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "posix":
        raise QcError("The real QC runner must run inside Ubuntu/Linux.")
    import fcntl
    lock_dir = Path(args.lock_dir).resolve()
    if lock_dir == raw_root or lock_dir.is_relative_to(raw_root):
        raise QcError("--lock-dir must be outside the raw input directory.")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_name = "qc-" + hashlib.sha256(str(out_dir).encode("utf-8")).hexdigest()[:16] + ".lock"
    lock_handle = (lock_dir / lock_name).open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_handle.close()
        raise QcError("Another QC runner holds the output-directory lock.") from exc
    status = {"status": "running", "stage": "preflight", "started_at": utc_now(),
              "expected_files": len(files), "expected_samples": manifest["expected_samples"],
              "md5_completed": 0, "fastqc_completed": 0, "runner_version": RUNNER_VERSION}
    def update(**values):
        status.update(values, updated_at=utc_now())
        atomic_json(out_dir / "status.json", status)
    try:
        update()
        atomic_json(out_dir / "qc_summary.json", dict(status, note="Current run is not complete; previous per-file states may be reused."))
        env, tools, info = environment_info(args.env_prefix, out_dir / "runtime")
        atomic_json(out_dir / "environment.json", info)
        atomic_json(out_dir / "input_manifest_snapshot.json", manifest)
        recipe = {"environment": info, "fastqc_args": FASTQC_ARGS}
        checksums = {}
        update(stage="md5")
        for index, entry in enumerate(files, 1):
            key = task_key(entry)
            state_path = out_dir / "checksums" / key / "md5.json"
            update(current_file=entry["path"], bytes_hashed_current=0)
            state = verify_md5(entry, read_json(state_path),
                               lambda done, total: update(bytes_hashed_current=done, current_file_bytes=total))
            atomic_json(state_path, state)
            if state["status"] != "passed":
                raise QcError(f"MD5 mismatch for {entry['path']}; expected {state['expected_md5']}, observed {state['observed_md5']}.")
            checksums[key] = state
            update(md5_completed=index)
            print(f"MD5 {index}/{len(files)} {key} {'reused' if state['reused'] else 'passed'}", flush=True)
        update(stage="fastqc", current_file=None)
        results = {}
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        futures = {}
        try:
            for entry in files:
                future = executor.submit(fastqc_one, entry, checksums[task_key(entry)], out_dir, tools, env, recipe)
                futures[future] = entry
            for future in concurrent.futures.as_completed(futures):
                entry = futures[future]
                result = future.result()
                results[task_key(entry)] = result
                update(fastqc_completed=len(results), current_file=entry["path"])
                print(f"FastQC {len(results)}/{len(files)} {task_key(entry)}", flush=True)
        except BaseException:
            STOP.set()
            for future in futures:
                future.cancel()
            with PROCESS_LOCK:
                running = list(PROCESSES)
            for proc in running:
                terminate_process(proc)
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        update(stage="pair_counts")
        metrics = {key: result["metrics"] for key, result in results.items()}
        pairs = validate_pair_counts(files, metrics)
        atomic_json(out_dir / "pair_read_counts.json", pairs)
        update(stage="multiqc", current_file=None)
        ordered_results = [results[task_key(entry)] for entry in files]
        aggregate = multiqc_all(ordered_results, out_dir, tools, env, info)
        # Recheck all source identities before announcing overall success.
        for entry in files:
            if file_identity(entry["path"]) != checksums[task_key(entry)]["identity_after"]:
                raise QcError(f"Input changed before finalisation: {entry['path']}")
        findings = [{"task_key": key, "module": module, "finding": finding}
                    for key, result in results.items()
                    for module, finding in result["metrics"]["module_status"].items() if finding != "pass"]
        summary = {"status": "completed", "stage": "raw_checksum_and_qc", "completed_at": utc_now(),
                   "sample_count": manifest["expected_samples"], "fastq_files": len(files),
                   "file_pairs": len(pairs), "md5_passed": len(checksums),
                   "module_findings": findings, "module_findings_are_not_execution_failures": True,
                   "multiqc": aggregate, "per_file": ordered_results,
                   "limitations": ["No alignment or variant calling has been performed by this runner.",
                                   "No reads were trimmed, filtered or rewritten.",
                                   "R1/R2 counts were compared; full read-ID order was not verified.",
                                   "MD5 validates agreement with supplied checksums, not biological sample identity.",
                                   "Cached MD5 is reused only when recorded file identity and expected checksum match."]}
        atomic_json(out_dir / "qc_summary.json", summary)
        update(status="completed", stage="raw_checksum_and_qc", current_file=None,
               multiqc_report=aggregate["html_path"], module_findings=len(findings))
        print(f"RAW CHECKSUM + QC COMPLETE. Summary: {out_dir / 'qc_summary.json'}", flush=True)
        return 0
    except BaseException as exc:
        interrupted = isinstance(exc, (KeyboardInterrupt, InterruptedError))
        update(status="interrupted" if interrupted else "failed", error=str(exc), stage=status["stage"])
        atomic_json(out_dir / "qc_summary.json", dict(status, note="No complete current aggregate QC result."))
        if interrupted:
            print("Interrupted. Completed per-file results remain reusable.", file=sys.stderr)
            return 130
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        STOP.set()
        with PROCESS_LOCK:
            running = list(PROCESSES)
        for proc in running:
            terminate_process(proc)
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--env-prefix", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--lock-dir", default=str(Path(os.environ.get("WES_QC_PROJECT", str(Path.home() / "wes_qc_project"))).expanduser()
                                    / "runtime" / "locks"),
                        help="Linux filesystem directory for the single-run lock.")
    args = parser.parse_args()
    def stop_handler(signum, frame):
        # Repeated stop signals must not interrupt process cleanup.
        if STOP.is_set():
            return
        STOP.set()
        raise KeyboardInterrupt(f"Signal {signum}")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop_handler)
    try:
        return run(args)
    except (QcError, OSError, ValueError) as exc:
        print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

