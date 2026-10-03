"""Run a small, bounded upload and browse workload against the local deployment.

This measures one emulator run, not production capacity. Percentiles use nearest rank:
the value at sorted index ceil(percentile * sample count) - 1.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests
from botocore.config import Config
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STAGES = (1, 3, 5)
WAVES = 3
REQUEST_TIMEOUT = (4, 20)
MAIN_SECONDS = 600
TOTAL_SECONDS = 720
WAVE_SECONDS = 90
DRAIN_SECONDS = 120
TERMINAL_FAILURES = {"failed", "expired", "deleted", "rejected"}


class WorkloadFailure(Exception):
    def __init__(self, operation: str, kind: str, status: int | None = None):
        super().__init__(f"{operation}: {kind}" + (f" ({status})" if status else ""))
        self.operation = operation
        self.kind = kind
        self.status = status

    def report(self) -> dict:
        return {"operation": self.operation, "error_class": self.kind, "http_status": self.status}


def local_http_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Only localhost HTTP endpoints are permitted")
    return value


def nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[math.ceil(percentile * len(ordered)) - 1], 3)


@dataclass
class UploadRecord:
    stage: int
    wave: int
    owner: str
    request_key: str
    fixture_side: int
    fixture_bytes: int
    initiation_attempted: bool = False
    image_id: str | None = None
    initiated_at: float | None = None
    complete_accepted_at: float | None = None
    ready_at: float | None = None
    result: str = "not_started"


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "setup"
        self.samples: dict[str, dict[str, list[dict]]] = {}

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def add(self, operation: str, seconds: float, outcome: str) -> None:
        with self._lock:
            self.samples.setdefault(self.phase, {}).setdefault(operation, []).append(
                {"seconds": seconds, "outcome": outcome}
            )

    def summary(self) -> dict:
        with self._lock:
            samples = {phase: {name: list(entries) for name, entries in operations.items()}
                       for phase, operations in self.samples.items()}
        return {
            phase: {
                name: {
                    "count": len(entries),
                    "failures": sum(entry["outcome"] != "ok" for entry in entries),
                    "outcomes": {key: sum(entry["outcome"] == key for entry in entries) for key in sorted({
                        entry["outcome"] for entry in entries
                    })},
                    "p50_ms": nearest_rank([entry["seconds"] * 1000 for entry in entries], 0.5),
                    "p95_ms": nearest_rank([entry["seconds"] * 1000 for entry in entries], 0.95),
                    "max_ms": round(max(entry["seconds"] for entry in entries) * 1000, 3),
                }
                for name, entries in sorted(operations.items())
            }
            for phase, operations in sorted(samples.items())
        }


class Client:
    def __init__(self, api_url: str, recorder: Recorder, deadline: float):
        self.base = local_http_url(api_url).rstrip("/")
        self.recorder = recorder
        self.deadline = deadline

    def request(self, operation: str, method: str, url: str, expected: int | tuple[int, ...],
                deadline: float | None = None, **kwargs):
        try:
            local_http_url(url)
        except ValueError as exc:
            raise WorkloadFailure(operation, "NonLocalURL") from exc
        remaining = min(self.deadline, deadline or self.deadline) - time.monotonic()
        if remaining <= 0:
            raise WorkloadFailure(operation, "DeadlineExceeded")
        start = time.monotonic()
        try:
            response = requests.request(
                method, url, timeout=(min(REQUEST_TIMEOUT[0], remaining),
                                     min(REQUEST_TIMEOUT[1], remaining)), allow_redirects=False, **kwargs
            )
        except requests.RequestException as exc:
            kind = type(exc).__name__
            self.recorder.add(operation, time.monotonic() - start, kind)
            raise WorkloadFailure(operation, kind) from exc
        expected_codes = (expected,) if isinstance(expected, int) else expected
        outcome = "ok" if response.status_code in expected_codes else f"HTTP{response.status_code}"
        self.recorder.add(operation, time.monotonic() - start, outcome)
        if outcome != "ok":
            raise WorkloadFailure(operation, "UnexpectedHTTP", response.status_code)
        return response

    def api(self, operation: str, method: str, path: str, expected: int | tuple[int, ...] = 200, **kwargs):
        return self.request(operation, method, self.base + path, expected, **kwargs)


def body(response, operation: str) -> dict:
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (ValueError, TypeError) as exc:
        raise WorkloadFailure(operation, "InvalidJSON") from exc


def fixture(pixels: int) -> bytes:
    rng = random.Random(pixels)
    raw = rng.randbytes(pixels * pixels * 3)
    output = io.BytesIO()
    Image.frombytes("RGB", (pixels, pixels), raw).save(output, "PNG")
    return output.getvalue()


def upload_command(record: UploadRecord) -> dict:
    return {
        "filename": f"load-{record.fixture_side}.png",
        "content_type": "image/png",
        "size_bytes": record.fixture_bytes,
        "caption": "Local bounded load fixture",
    }


def upload(client: Client, record: UploadRecord, raw: bytes, clock_start: float, deadline: float) -> None:
    command = upload_command(record)
    headers = {"X-User-Id": record.owner, "Idempotency-Key": record.request_key}
    record.initiation_attempted = True
    created = body(client.api("initiate", "POST", "/images", 201, deadline=deadline,
                              headers=headers, json=command), "initiate")
    try:
        record.image_id = created["image_id"]
        record.initiated_at = time.monotonic() - clock_start
        replay = body(client.api("initiate_replay", "POST", "/images", 200, deadline=deadline,
                                 headers=headers, json=command),
                      "initiate_replay")
        if any(replay[field] != created[field] for field in (
            "image_id", "upload_expires_at", "completion_deadline"
        )):
            raise WorkloadFailure("initiate_replay", "IdempotencyMismatch")
        client.request("signed_put", "PUT", created["upload_url"], 200, deadline=deadline,
                       data=raw, headers=created["upload_headers"])
        client.api("complete", "POST", f"/images/{record.image_id}/complete", 202, deadline=deadline,
                   headers={"X-User-Id": record.owner})
        record.complete_accepted_at = time.monotonic() - clock_start
        record.result = "complete_accepted"
    except KeyError as exc:
        raise WorkloadFailure("upload_response", "MissingField") from exc


def browse(client: Client, owner: str, operation: str, stop: threading.Event,
           failures: list[dict], lock: threading.Lock, deadline: float, counts: dict[str, int]) -> None:
    next_request = time.monotonic()
    while not stop.is_set():
        if stop.wait(max(0, next_request - time.monotonic())):
            break
        if time.monotonic() >= deadline:
            break
        try:
            params = {"limit": 5}
            if operation == "browse_owner":
                params["owner_id"] = owner
            body(client.api(operation, "GET", "/images", deadline=deadline, params=params), operation)
            with lock:
                counts[operation] += 1
        except WorkloadFailure as exc:
            with lock:
                failures.append(exc.report())
            stop.set()
            break
        next_request = max(next_request + 1, time.monotonic() + 1)


def stop_browsers(stop: threading.Event, threads: list[threading.Thread], errors: list[dict]) -> None:
    stop.set()
    for thread in threads:
        thread.join(timeout=sum(REQUEST_TIMEOUT) + 1)
        if thread.is_alive():
            failure = {"operation": "browser", "error_class": "ThreadDidNotStop"}
            if failure not in errors:
                errors.append(failure)


def status(client: Client, record: UploadRecord, deadline: float | None = None) -> str:
    result = body(client.api("status", "GET", f"/images/{record.image_id}/status",
                             deadline=deadline, headers={"X-User-Id": record.owner}), "status")
    try:
        return result["status"]
    except KeyError as exc:
        raise WorkloadFailure("status", "MissingField") from exc


def drain_ready(client: Client, records: list[UploadRecord], clock_start: float,
                deadline: float) -> tuple[int, float]:
    started = time.monotonic()
    pending = {record.image_id: record for record in records if record.complete_accepted_at is not None}
    first_pending: int | None = None
    last_notice = started
    while pending:
        if time.monotonic() >= deadline:
            raise WorkloadFailure("validation_drain", "DeadlineExceeded")
        for image_id, record in list(pending.items()):
            observed = status(client, record, deadline)
            if observed == "ready":
                record.ready_at = time.monotonic() - clock_start
                record.result = "ready"
                del pending[image_id]
            elif observed in TERMINAL_FAILURES:
                record.result = observed
                raise WorkloadFailure("validation_drain", f"Unexpected{observed.title()}")
        if first_pending is None:
            first_pending = len(pending)
        if pending:
            now = time.monotonic()
            if now - last_notice >= 30:
                print(f"Waiting for validation: {len(pending)} pending", flush=True)
                last_notice = now
            time.sleep(min(1, max(0, deadline - now)))
    return first_pending or 0, time.monotonic() - started


def gallery_ids(client: Client, owner: str, deadline: float) -> list[str]:
    ids: list[str] = []
    cursor = None
    for _ in range(8):
        params = {"owner_id": owner, "limit": 5}
        if cursor:
            params["cursor"] = cursor
        page = body(client.api("gallery_page", "GET", "/images", deadline=deadline, params=params),
                    "gallery_page")
        try:
            ids.extend(item["image_id"] for item in page["items"])
            cursor = page["next_cursor"]
        except (KeyError, TypeError) as exc:
            raise WorkloadFailure("gallery_page", "MissingField") from exc
        if not cursor:
            return ids
    raise WorkloadFailure("gallery_page", "PageLimitExceeded")


def verify_ready(client: Client, records: list[UploadRecord], fixtures: dict[int, bytes],
                 expected_ids: set[str], owner: str, deadline: float) -> dict:
    for record in records:
        if time.monotonic() >= deadline:
            raise WorkloadFailure("download", "DeadlineExceeded")
        response = client.api("content_redirect", "GET", f"/images/{record.image_id}/content", 302,
                              deadline=deadline)
        location = response.headers.get("Location", "")
        download = client.request("download", "GET", location, 200, deadline=deadline)
        if hashlib.sha256(download.content).digest() != hashlib.sha256(fixtures[record.fixture_side]).digest():
            raise WorkloadFailure("download", "HashMismatch")
    end = min(deadline, time.monotonic() + 20)
    while time.monotonic() < end:
        ids = gallery_ids(client, owner, deadline)
        if len(ids) == len(set(ids)) and set(ids) == expected_ids:
            return {"download_hashes_match": True, "owner_gallery_exact": True, "gallery_count": len(ids)}
        time.sleep(1)
    raise WorkloadFailure("gallery_page", "GalleryMismatch")


def cleanup(client: Client, records: list[UploadRecord], config: dict, deadline: float) -> dict:
    result = {"recorded_ids": 0, "recovered_ids": 0, "unresolved_uploads": 0,
              "deleted": 0, "zero_length_heads": 0, "errors": []}
    for record in records:
        if record.image_id or record.result != "failed" or not record.initiation_attempted:
            continue
        try:
            recovered = body(client.api("initiate_recovery", "POST", "/images", (200, 201),
                                        deadline=deadline,
                                        headers={"X-User-Id": record.owner,
                                                 "Idempotency-Key": record.request_key},
                                        json=upload_command(record)), "initiate_recovery")
            record.image_id = recovered["image_id"]
            result["recovered_ids"] += 1
        except WorkloadFailure as exc:
            result["errors"].append(exc.report())
        except (KeyError, TypeError) as exc:
            result["errors"].append({"operation": "initiate_recovery", "error_class": type(exc).__name__})
    unique = {record.image_id: record for record in records if record.image_id}
    result["recorded_ids"] = len(unique)
    result["unresolved_uploads"] = sum(record.result == "failed" and record.image_id is None
                                       for record in records)
    if result["unresolved_uploads"]:
        result["errors"].append({"operation": "cleanup", "error_class": "UnresolvedUpload",
                                 "count": result["unresolved_uploads"]})
    if not unique:
        return result
    aws = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3 = aws.client("s3", endpoint_url=config["endpoint_url"], config=Config(
        connect_timeout=4, read_timeout=10, retries={"total_max_attempts": 1}
    ))
    items = list(unique.items())
    for offset in range(0, len(items), 5):
        pending = set()
        for image_id, record in items[offset:offset + 5]:
            if time.monotonic() >= deadline:
                result["errors"].append({"operation": "cleanup", "error_class": "DeadlineExceeded"})
                break
            try:
                client.api("delete", "DELETE", f"/images/{image_id}", (202, 204), deadline=deadline,
                           headers={"X-User-Id": record.owner})
                pending.add(image_id)
            except WorkloadFailure as exc:
                result["errors"].append(exc.report())
        while pending and time.monotonic() < deadline:
            for image_id in list(pending):
                record = unique[image_id]
                try:
                    if status(client, record, deadline) != "deleted":
                        continue
                    result["deleted"] += 1
                    head = s3.head_object(Bucket=config["bucket_name"], Key="originals/" + image_id)
                    if head["ContentLength"] != 0:
                        raise WorkloadFailure("s3_head", "NonzeroLength")
                    result["zero_length_heads"] += 1
                    pending.remove(image_id)
                except WorkloadFailure as exc:
                    result["errors"].append(exc.report())
                    pending.remove(image_id)
                except Exception as exc:
                    result["errors"].append({"operation": "s3_head", "error_class": type(exc).__name__})
                    pending.remove(image_id)
            if pending:
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        if pending or time.monotonic() >= deadline:
            result["errors"].append({"operation": "cleanup", "error_class": "DeadlineExceeded",
                                     "remaining_ids": len(unique) - result["zero_length_heads"]})
            break
    return result


def run(config_path: Path, output_path: Path) -> int:
    run_id = uuid.uuid4().hex[:12]
    owner = "load-" + run_id
    started = time.monotonic()
    recorder = Recorder()
    records: list[UploadRecord] = []
    report = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "scope": "Local emulator observation; not production capacity proof",
        "workload": {"stage_concurrency": list(STAGES), "waves_per_stage": WAVES,
                     "upload_count": sum(STAGES) * WAVES, "browser_clients": 2,
                     "browser_max_requests_per_second_each": 1, "wave_validation_bound": "drain before next wave",
                     "main_stop_budget_seconds": MAIN_SECONDS, "overall_stop_budget_seconds": TOTAL_SECONDS,
                     "deadline_note": "In-flight transport calls may finish after a stop budget"},
        "stages": [], "request_latencies": {}, "uploads": [], "cleanup": None,
        "stop_reason": None, "passed": False,
    }
    config = None
    client = None
    try:
        config = json.loads(config_path.read_text())
        local_http_url(config["api_url"])
        local_http_url(config["endpoint_url"])
        client = Client(config["api_url"], recorder, started + TOTAL_SECONDS)
        fixtures = {size: fixture(size) for size in (256, 1024)}
        report["workload"]["fixtures"] = [
            {"width": size, "height": size, "pixel_count": size * size,
             "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for size, raw in fixtures.items()
        ]
        main_deadline = started + MAIN_SECONDS
        expected_ids: set[str] = set()
        for concurrency in STAGES:
            if time.monotonic() >= main_deadline:
                raise WorkloadFailure("stage", "DeadlineExceeded")
            print(f"Starting concurrency {concurrency} ({WAVES} bounded waves)", flush=True)
            recorder.set_phase(f"concurrency_{concurrency}")
            stage_start = time.monotonic()
            stage_records: list[UploadRecord] = []
            stage = {"concurrency": concurrency, "waves": WAVES, "uploaded": 0, "ready": 0,
                     "errors": [], "browser_requests": {"browse_global": 0, "browse_owner": 0},
                     "pending_on_first_poll_after_load_stop": None, "post_traffic_drain_seconds": None,
                     "correctness": {}}
            report["stages"].append(stage)
            lock = threading.Lock()
            stop = threading.Event()
            threads = [threading.Thread(target=browse, args=(client, owner, operation, stop,
                       stage["errors"], lock, main_deadline, stage["browser_requests"]), daemon=True)
                       for operation in ("browse_global", "browse_owner")]
            for thread in threads:
                thread.start()
            submission_seconds = 0.0
            try:
                for wave in range(WAVES):
                    if stage["errors"] or time.monotonic() >= main_deadline:
                        raise WorkloadFailure("stage", "StoppedBeforeNextWave")
                    wave_start = time.monotonic()
                    wave_records = []
                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        futures = []
                        for slot in range(concurrency):
                            size = (wave * concurrency + slot) % 2
                            pixels = (256, 1024)[size]
                            record = UploadRecord(concurrency, wave + 1, owner, uuid.uuid4().hex,
                                                  pixels, len(fixtures[pixels]))
                            records.append(record)
                            stage_records.append(record)
                            wave_records.append(record)
                            futures.append(pool.submit(upload, client, record, fixtures[pixels], started,
                                                       min(main_deadline, wave_start + WAVE_SECONDS)))
                        for future, record in zip(futures, wave_records, strict=True):
                            try:
                                future.result()
                            except WorkloadFailure as exc:
                                record.result = "failed"
                                stage["errors"].append(exc.report())
                            except Exception as exc:
                                record.result = "failed"
                                stage["errors"].append({"operation": "upload", "error_class": type(exc).__name__})
                    submission_seconds += time.monotonic() - wave_start
                    stage["uploaded"] = sum(record.complete_accepted_at is not None for record in stage_records)
                    if time.monotonic() - wave_start > WAVE_SECONDS:
                        stage["errors"].append({"operation": "wave", "error_class": "DeadlineExceeded"})
                    if stage["errors"]:
                        raise WorkloadFailure("stage", "WaveFailed")
                    if wave == WAVES - 1:
                        stop_browsers(stop, threads, stage["errors"])
                        if stage["errors"]:
                            raise WorkloadFailure("stage", "BrowseFailed")
                    drain_deadline = min(main_deadline, time.monotonic() + DRAIN_SECONDS)
                    pending, drain_seconds = drain_ready(client, wave_records, started, drain_deadline)
                    if wave == WAVES - 1:
                        stage["pending_on_first_poll_after_load_stop"] = pending
                        stage["post_traffic_drain_seconds"] = round(drain_seconds, 3)
                    if stage["errors"]:
                        raise WorkloadFailure("stage", "BrowseFailed")
                stage["ready"] = sum(record.ready_at is not None for record in stage_records)
                stage["validation_seconds"] = round(time.monotonic() - stage_start, 3)
                stage["correctness"] = verify_ready(client, stage_records, fixtures, expected_ids | {
                    record.image_id for record in stage_records
                }, owner, main_deadline)
                expected_ids.update(record.image_id for record in stage_records)
            finally:
                stop_browsers(stop, threads, stage["errors"])
                stage["stage_seconds"] = round(time.monotonic() - stage_start, 3)
                stage["submission_seconds"] = round(submission_seconds, 3)
                stage["uploaded"] = sum(record.complete_accepted_at is not None for record in stage_records)
                stage["ready"] = sum(record.ready_at is not None for record in stage_records)
                stage["completion_acceptances_per_second"] = round(
                    stage["uploaded"] / submission_seconds, 3) if submission_seconds else None
                denominator = stage.get("validation_seconds", stage["stage_seconds"])
                stage["ready_completions_per_second"] = round(
                    stage["ready"] / denominator, 3) if denominator else None
                stage["ready_throughput_denominator_seconds"] = denominator
                latencies = [(record.ready_at - record.complete_accepted_at) * 1000 for record in stage_records
                             if record.ready_at is not None and record.complete_accepted_at is not None]
                stage["completion_to_ready_ms"] = {
                    "count": len(latencies), "p50": nearest_rank(latencies, 0.5),
                    "p95": nearest_rank(latencies, 0.95),
                    "max": round(max(latencies), 3) if latencies else None,
                }
            print(f"Concurrency {concurrency}: {stage['ready']}/{len(stage_records)} ready; "
                  f"{stage['stage_seconds']}s", flush=True)
            if stage["errors"]:
                raise WorkloadFailure("stage", "BrowseFailed")
        report["stop_reason"] = "completed"
    except WorkloadFailure as exc:
        report["stop_reason"] = exc.report()
    except Exception as exc:
        report["stop_reason"] = {"operation": "setup_or_run", "error_class": type(exc).__name__}
    finally:
        if client is not None and config is not None:
            recorder.set_phase("cleanup")
            try:
                report["cleanup"] = cleanup(client, records, config, started + TOTAL_SECONDS)
            except Exception as exc:
                report["cleanup"] = {"recorded_ids": sum(record.image_id is not None for record in records),
                                     "deleted": 0, "zero_length_heads": 0,
                                     "errors": [{"operation": "cleanup", "error_class": type(exc).__name__}]}
        report["request_latencies"] = recorder.summary()
        report["uploads"] = [asdict(record) for record in records]
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        unique_ids = {record.image_id for record in records if record.image_id}
        report["correctness_totals"] = {"expected": sum(STAGES) * WAVES,
                                         "attempted": len(records), "unique_image_ids": len(unique_ids),
                                         "ready": sum(record.ready_at is not None for record in records)}
        report["passed"] = (report["stop_reason"] == "completed"
                            and len(records) == sum(STAGES) * WAVES
                            and len(unique_ids) == sum(STAGES) * WAVES
                            and all(record.ready_at is not None for record in records)
                            and all(not stage["errors"] and stage["correctness"].get("owner_gallery_exact")
                                    and stage["correctness"].get("download_hashes_match") for stage in report["stages"])
                            and report["cleanup"] is not None and not report["cleanup"]["errors"]
                            and report["cleanup"].get("unresolved_uploads") == 0
                            and report["cleanup"]["zero_length_heads"] == len(records)
                            and all(group["failures"] == 0 for phase in report["request_latencies"].values()
                                    for group in phase.values()))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Load result: {'PASS' if report['passed'] else 'FAIL'}; report at {output_path}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".local/api.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/load-result.json")
    args = parser.parse_args()
    raise SystemExit(run(args.config, args.output))
