import json
import time

import pytest

from scripts.load_test import Recorder, UploadRecord, cleanup, local_http_url, nearest_rank, run


def test_local_http_url_rejects_nonlocal_and_credentials():
    assert local_http_url("http://127.0.0.1:4567/path") == "http://127.0.0.1:4567/path"
    for value in ("https://localhost:4567", "http://example.com", "http://localhost.evil",
                  "http://user@localhost:4567", "http://:secret@localhost:4567", "/relative/path"):
        with pytest.raises(ValueError):
            local_http_url(value)


def test_nearest_rank_and_stage_failure_samples():
    assert nearest_rank([], 0.95) is None
    assert nearest_rank([9, 1, 3, 5, 7], 0.5) == 5
    assert nearest_rank([9, 1, 3, 5, 7], 0.95) == 9
    recorder = Recorder()
    recorder.set_phase("concurrency_1")
    recorder.add("browse_global", 0.2, "ok")
    recorder.set_phase("concurrency_3")
    recorder.add("browse_global", 0.4, "Timeout")
    summary = recorder.summary()
    assert summary["concurrency_1"]["browse_global"]["failures"] == 0
    assert summary["concurrency_3"]["browse_global"]["failures"] == 1
    assert summary["concurrency_3"]["browse_global"]["p95_ms"] == 400


def test_invalid_endpoint_writes_failure_report_without_network(tmp_path):
    config = tmp_path / "config.json"
    output = tmp_path / "report.json"
    config.write_text(json.dumps({"api_url": "http://example.com", "endpoint_url": "http://localhost:4567"}))
    assert run(config, output) == 1
    report = json.loads(output.read_text())
    assert report["passed"] is False
    assert report["stop_reason"] == {"operation": "setup_or_run", "error_class": "ValueError"}
    assert report["uploads"] == []


def test_lost_initiation_response_is_reconciled_and_deleted(monkeypatch):
    record = UploadRecord(1, 1, "load-owner", "same-key", 256, 123,
                          initiation_attempted=True, result="failed")
    calls = []

    class Response:
        def __init__(self, value):
            self.value = value

        def json(self):
            return self.value

    class Client:
        def api(self, operation, method, path, expected=200, **kwargs):
            calls.append((operation, method, path, kwargs))
            if operation == "initiate_recovery":
                return Response({"image_id": "recovered-image"})
            if operation == "status":
                return Response({"status": "deleted"})
            return Response({})

    class S3:
        def head_object(self, **kwargs):
            assert kwargs["Key"] == "originals/recovered-image"
            return {"ContentLength": 0}

    class Session:
        def __init__(self, **kwargs):
            pass

        def client(self, *args, **kwargs):
            return S3()

    monkeypatch.setattr("scripts.load_test.boto3.Session", Session)
    result = cleanup(Client(), [record], {"endpoint_url": "http://localhost:4567", "bucket_name": "images"},
                     time.monotonic() + 10)
    replay = calls[0]
    assert replay[0:3] == ("initiate_recovery", "POST", "/images")
    assert replay[3]["headers"] == {"X-User-Id": "load-owner", "Idempotency-Key": "same-key"}
    assert replay[3]["json"]["size_bytes"] == 123
    assert record.image_id == "recovered-image" and record.result == "failed"
    assert result == {"recorded_ids": 1, "recovered_ids": 1, "unresolved_uploads": 0,
                      "deleted": 1, "zero_length_heads": 1, "errors": []}
