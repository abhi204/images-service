import io
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws
from PIL import Image

from image_service import http, lifecycle, storage

REAL_DISPATCH = lifecycle._dispatch


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("IMAGE_TABLE", "Images")
    monkeypatch.setenv("REQUEST_TABLE", "UploadRequests")
    monkeypatch.setenv("IMAGE_BUCKET", "images")
    monkeypatch.setenv("WORKER_FUNCTION", "worker")
    monkeypatch.setenv("CURSOR_SECRET", "test-secret")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_PUBLIC_ENDPOINT", raising=False)
    with mock_aws():
        dynamo = boto3.client("dynamodb", region_name="us-east-1")
        dynamo.create_table(
            TableName="Images",
            KeySchema=[{"AttributeName": "image_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "image_id", "AttributeType": "S"},
                {"AttributeName": "public_scope", "AttributeType": "S"},
                {"AttributeName": "owner_scope", "AttributeType": "S"},
                {"AttributeName": "gallery_sort", "AttributeType": "S"},
                {"AttributeName": "maintenance_scope", "AttributeType": "S"},
                {"AttributeName": "maintenance_sort", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "global_gallery",
                    "KeySchema": [
                        {"AttributeName": "public_scope", "KeyType": "HASH"},
                        {"AttributeName": "gallery_sort", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "owner_gallery",
                    "KeySchema": [
                        {"AttributeName": "owner_scope", "KeyType": "HASH"},
                        {"AttributeName": "gallery_sort", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "maintenance",
                    "KeySchema": [
                        {"AttributeName": "maintenance_scope", "KeyType": "HASH"},
                        {"AttributeName": "maintenance_sort", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamo.create_table(
            TableName="UploadRequests",
            KeySchema=[{"AttributeName": "request_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "request_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3").create_bucket(Bucket="images")
        monkeypatch.setattr(lifecycle, "_dispatch", lambda *_: None)
        yield


def event(method, path, body=None, headers=None, query=None):
    return {
        "requestContext": {"http": {"method": method}, "requestId": "req-1"},
        "rawPath": path,
        "headers": {"X-User-Id": "alice", **(headers or {})},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


def call(method, path, body=None, headers=None, query=None):
    result = http.handler(event(method, path, body, headers, query), None)
    return result["statusCode"], json.loads(result["body"]) if result.get("body") else None


def png(width=3, height=4):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(output, "PNG")
    return output.getvalue()


def initiate(key="one", data=None):
    data = data or png()
    command = {"filename": "photo.png", "content_type": "image/png", "size_bytes": len(data), "caption": "hello"}
    code, result = call("POST", "/images", command, {"Idempotency-Key": key})
    assert code == 201
    return result, data, command


def store(image_id, data):
    boto3.client("s3").put_object(Bucket="images", Key=f"originals/{image_id}", Body=data)


def test_initiation_replay_conflict_and_owner_boundary():
    created, data, command = initiate()
    code, replay = call("POST", "/images", command, {"Idempotency-Key": "one"})
    assert code == 200
    assert replay["image_id"] == created["image_id"]
    assert replay["completion_deadline"] == created["completion_deadline"]
    assert replay["upload_headers"] == {
        "Content-Type": "image/png",
        "Content-Length": str(len(data)),
        "If-None-Match": "*",
    }
    assert call("POST", "/images", {**command, "caption": "changed"}, {"Idempotency-Key": "one"})[0] == 409
    assert call("GET", f"/images/{created['image_id']}/status", headers={"X-User-Id": "bob"})[0] == 404
    assert call("GET", f"/images/{created['image_id']}")[0] == 404


def test_ready_delete_and_stale_worker_cannot_republish():
    created, data, command = initiate()
    image_id = created["image_id"]
    assert call("POST", f"/images/{image_id}/complete")[0] == 409
    store(image_id, data)
    assert call("POST", f"/images/{image_id}/complete")[0] == 202
    image = lifecycle._get(image_id)
    token = lifecycle._claim(image)
    assert token
    assert call("DELETE", f"/images/{image_id}")[0] == 202
    assert lifecycle._publish(image, token, storage.validate_original(data, "image/png")) is False
    lifecycle.run_work(image_id, "cleanup")
    assert call("GET", f"/images/{image_id}/status")[1]["status"] == "deleted"
    assert call("GET", f"/images/{image_id}")[0] == 404
    assert boto3.client("s3").get_object(Bucket="images", Key=f"originals/{image_id}")["Body"].read() == b""
    assert call("DELETE", f"/images/{image_id}")[0] == 204
    replay = call("POST", "/images", command, {"Idempotency-Key": "one"})[1]
    assert replay == {
        "image_id": image_id,
        "status": "deleted",
        "upload_expires_at": created["upload_expires_at"],
        "completion_deadline": created["completion_deadline"],
    }


def test_worker_rejects_mismatch_and_removes_content():
    created, _, _ = initiate()
    image_id = created["image_id"]
    store(image_id, b"not really a png")
    assert call("POST", f"/images/{image_id}/complete")[0] == 202
    lifecycle.run_work(image_id, "validate")
    assert call("GET", f"/images/{image_id}/status")[1]["status"] == "rejected"
    lifecycle.run_work(image_id, "cleanup")
    assert boto3.client("s3").get_object(Bucket="images", Key=f"originals/{image_id}")["Body"].read() == b""
    assert call("GET", f"/images/{image_id}")[0] == 404


def test_gallery_cursor_filters_and_authoritative_recheck():
    ids = []
    for index in range(3):
        created, data, _ = initiate(str(index))
        image_id = created["image_id"]
        store(image_id, data)
        call("POST", f"/images/{image_id}/complete")
        lifecycle.run_work(image_id, "validate")
        ids.append(image_id)
    code, page1 = call("GET", "/images", query={"limit": "2", "owner_id": "alice"})
    assert code == 200
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]
    code, page2 = call("GET", "/images", query={"limit": "2", "owner_id": "alice", "cursor": page1["next_cursor"]})
    assert code == 200
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None
    assert {item["image_id"] for item in page1["items"] + page2["items"]} == set(ids)
    assert call("GET", "/images", query={"limit": "1", "owner_id": "alice", "cursor": page1["next_cursor"]})[0] == 400
    assert call("GET", "/images", query={"date_from": "2099-01-01"})[1]["items"] == []
    newest = page1["items"][0]["image_id"]
    call("DELETE", f"/images/{newest}")
    assert newest not in {item["image_id"] for item in call("GET", "/images")[1]["items"]}


def test_image_validation_checks_pixels_animation_and_corruption():
    assert storage.validate_original(png(), "image/png").width == 3
    with pytest.raises(storage.InvalidImage):
        storage.validate_original(png(), "image/jpeg")
    with pytest.raises(storage.InvalidImage):
        storage.validate_original(png(5001, 5000), "image/png")
    animated = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        animated, "WEBP", save_all=True, append_images=[Image.new("RGB", (2, 2), "blue")]
    )
    with pytest.raises(storage.InvalidImage):
        storage.validate_original(animated.getvalue(), "image/webp")
    jpeg = io.BytesIO()
    Image.new("RGB", (3, 3), "red").save(jpeg, "JPEG")
    with pytest.raises(storage.InvalidImage):
        storage.validate_original(jpeg.getvalue()[:-12], "image/jpeg")


def test_auth_ignores_client_identity_outside_local(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    code, body = call(
        "POST", "/images", {"filename": "x", "content_type": "image/png", "size_bytes": 1}, {"Idempotency-Key": "key"}
    )
    assert code == 401
    assert body == {"code": "unauthorized", "message": "Authentication is required", "request_id": "req-1"}


def test_retry_and_recovery_bounds(monkeypatch):
    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    monkeypatch.setattr(storage, "read_original", lambda *_: (_ for _ in ()).throw(RuntimeError("temporary")))
    lifecycle.run_work(image_id, "validate")
    first = lifecycle._get(image_id)
    assert first["status"] == "processing" and first["attempt"] == 1
    assert first["next_check_at"] > lifecycle.now_epoch()
    dispatched = []
    monkeypatch.setattr(lifecycle, "_dispatch", lambda *args: dispatched.append(args))
    lifecycle.recover(now=int(first["next_check_at"]))
    assert dispatched == [(image_id, "validate")]
    assert lifecycle._get(image_id)["next_check_at"] > int(first["next_check_at"])


def test_cleanup_failure_retries_indefinitely(monkeypatch):
    created, _, _ = initiate()
    image_id = created["image_id"]
    call("DELETE", f"/images/{image_id}")
    monkeypatch.setattr(storage, "write_placeholder", lambda *_: (_ for _ in ()).throw(RuntimeError("S3 down")))
    lifecycle.run_work(image_id, "cleanup")
    image = lifecycle._get(image_id)
    assert image["status"] == "deleting" and image["cleanup_pending"] is True
    assert image["next_check_at"] > lifecycle.now_epoch()
    assert image["cleanup_attempt"] == 1


def test_concurrent_initiation_has_one_mapping_and_owner_scoped_keys():
    data = png()
    command = {"filename": "same.png", "content_type": "image/png", "size_bytes": len(data), "caption": ""}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(lifecycle.initiate, "alice", command, "shared") for _ in range(2)]
        results = [future.result() for future in futures]
    assert results[0]["image_id"] == results[1]["image_id"]
    assert sorted(result["replay"] for result in results) == [False, True]
    assert boto3.resource("dynamodb").Table("Images").scan()["Count"] == 1
    bob = lifecycle.initiate("bob", command, "shared")
    assert bob["image_id"] != results[0]["image_id"]
    assert boto3.resource("dynamodb").Table("UploadRequests").scan()["Count"] == 2


def test_replacement_lease_fences_old_worker():
    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    image = lifecycle._get(image_id)
    first = lifecycle._claim(image)
    assert first
    boto3.resource("dynamodb").Table("Images").update_item(
        Key={"image_id": image_id},
        UpdateExpression="SET lease_until=:past",
        ExpressionAttributeValues={":past": lifecycle.now_epoch() - 1},
    )
    second = lifecycle._claim(lifecycle._get(image_id))
    assert second and second != first
    facts = storage.validate_original(data, "image/png")
    assert lifecycle._publish(image, first, facts) is False
    assert lifecycle._publish(image, second, facts) is True
    assert lifecycle.public_image(image_id)["width"] == 3


def test_recovery_marks_exhausted_work_failed_and_cleanup_due():
    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    epoch = lifecycle.now_epoch()
    boto3.resource("dynamodb").Table("Images").update_item(
        Key={"image_id": image_id},
        UpdateExpression="SET attempt=:max, lease_until=:past, next_check_at=:due, maintenance_sort=:sort",
        ExpressionAttributeValues={
            ":max": 3,
            ":past": epoch - 1,
            ":due": epoch - 1,
            ":sort": f"{lifecycle.timestamp(epoch - 1)}#{image_id}",
        },
    )
    lifecycle.recover(now=epoch)
    image = lifecycle._get(image_id)
    assert image["status"] == "failed" and image["cleanup_pending"] is True
    assert image["error_reason"] == "Validation deadline or attempt limit reached"
    lifecycle.run_work(image_id, "cleanup")
    assert lifecycle._get(image_id)["status"] == "failed"


def test_dispatch_outage_leaves_durable_processing_for_next_recovery(monkeypatch):
    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    calls = []

    def fail_then_accept(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("invocation outage")

    monkeypatch.setattr(lifecycle, "_invoke", fail_then_accept)
    monkeypatch.setattr(lifecycle, "_dispatch", REAL_DISPATCH)
    assert call("POST", f"/images/{image_id}/complete")[0] == 202
    assert lifecycle._get(image_id)["status"] == "processing"
    due = int(lifecycle._get(image_id)["next_check_at"])
    lifecycle.recover(now=due)
    assert len(calls) == 2
    assert calls[-1] == (image_id, "validate")


def test_access_denied_fails_once_and_unknown_error_retries(monkeypatch):
    from botocore.exceptions import ClientError

    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
    monkeypatch.setattr(storage, "read_original", lambda *_: (_ for _ in ()).throw(denied))
    lifecycle.run_work(image_id, "validate")
    image = lifecycle._get(image_id)
    assert image["status"] == "failed" and image["attempt"] == 1
    assert image["error_reason"] == "Storage access was denied"


def test_short_download_retries_instead_of_rejecting(monkeypatch):
    data = png()
    stream = Mock()
    stream.read.return_value = data[:4]
    client = Mock()
    client.get_object.return_value = {"Body": stream, "ContentLength": len(data)}
    monkeypatch.setattr(storage, "_s3", lambda: client)
    with pytest.raises(OSError, match="ended before"):
        storage.read_original("images", "originals/id")
    stream.close.assert_called_once()
    stream.read.assert_called_once_with(storage.MAX_BYTES + 1)


def test_oversized_storage_object_is_rejected_without_retry(monkeypatch):
    stream = io.BytesIO(b"x" * (storage.MAX_BYTES + 1))
    client = Mock()
    client.get_object.return_value = {"Body": stream, "ContentLength": storage.MAX_BYTES + 100}
    monkeypatch.setattr(storage, "_s3", lambda: client)
    with pytest.raises(storage.InvalidImage, match="exceeds the 20 MiB"):
        storage.read_original("images", "originals/id")
    assert stream.closed


def test_cursor_expires_and_binds_filter_and_limit(monkeypatch):
    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    lifecycle.run_work(image_id, "validate")
    query = {"owner_id": "alice", "date_from": None, "date_to": None, "limit": 1, "order": "ready_desc"}
    token = http._cursor_encode(
        {
            "query": query,
            "position": lifecycle._get(image_id)["gallery_sort"],
            "expires_at": lifecycle.now_epoch() + 3600,
        }
    )
    assert call("GET", "/images", query={"limit": "1", "owner_id": "bob", "cursor": token})[0] == 400
    assert call("GET", "/images", query={"limit": "2", "owner_id": "alice", "cursor": token})[0] == 400
    monkeypatch.setattr(lifecycle, "now_epoch", lambda: 9_999_999_999)
    assert call("GET", "/images", query={"limit": "1", "owner_id": "alice", "cursor": token})[0] == 400


def test_gsi_partial_page_does_not_end_pagination(monkeypatch):
    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    lifecycle.run_work(image_id, "validate")
    table = lifecycle._table()
    original_query = table.query
    queries = 0

    def partial_query(**kwargs):
        nonlocal queries
        result = original_query(**kwargs)
        if queries == 0 and result["Items"]:
            result["LastEvaluatedKey"] = {"gallery_sort": result["Items"][-1]["gallery_sort"]}
        queries += 1
        return result

    monkeypatch.setattr(table, "query", partial_query)
    monkeypatch.setattr(lifecycle, "_table", lambda: table)
    items, cursor = lifecycle.list_ready(None, None, None, 10, None)
    assert [item["image_id"] for item in items] == [image_id]
    assert cursor is None
    assert queries == 2


def test_filtered_empty_page_has_cursor_after_500_candidates(monkeypatch):
    class FakeTable:
        calls = 0
        offset = 0

        def query(self, **kwargs):
            self.calls += 1
            amount = kwargs["Limit"]
            offset = self.offset
            self.offset += amount
            values = [
                {"image_id": f"gone-{i}", "gallery_sort": f"2026-01-01T00:00:00.{500 - i:06d}Z#{i:04d}"}
                for i in range(offset, offset + amount)
            ]
            return {"Items": values, "LastEvaluatedKey": {"image_id": values[-1]["image_id"]}}

    fake = FakeTable()
    monkeypatch.setattr(lifecycle, "_table", lambda: fake)
    monkeypatch.setattr(lifecycle, "_batch_get", lambda *_: {})
    items, next_position = lifecycle.list_ready(None, None, None, 25, None)
    assert items == []
    assert next_position == "2026-01-01T00:00:00.000002Z#0498"
    assert fake.offset == 500


def test_tied_ready_timestamps_use_uuid_tiebreaker():
    ids = []
    for index in range(3):
        created, data, _ = initiate(str(index))
        image_id = created["image_id"]
        store(image_id, data)
        call("POST", f"/images/{image_id}/complete")
        lifecycle.run_work(image_id, "validate")
        ids.append(image_id)
        tied = "2026-01-01T00:00:00.000000Z"
        boto3.resource("dynamodb").Table("Images").update_item(
            Key={"image_id": image_id},
            UpdateExpression="SET ready_at=:ready, gallery_sort=:sort",
            ExpressionAttributeValues={":ready": tied, ":sort": f"{tied}#{image_id}"},
        )
    seen = []
    cursor = None
    for _ in range(3):
        query = {"limit": "1"}
        if cursor:
            query["cursor"] = cursor
        code, body = call("GET", "/images", query=query)
        assert code == 200
        seen.extend(item["image_id"] for item in body["items"])
        cursor = body["next_cursor"]
    assert len(seen) == 3 and set(seen) == set(ids) and cursor is None


def test_expired_replay_loses_upload_permission_and_completion_returns_410(monkeypatch):
    created, data, command = initiate()
    image_id = created["image_id"]
    original_now = lifecycle.now_epoch()
    monkeypatch.setattr(lifecycle, "now_epoch", lambda: original_now + 901)
    code, replay = call("POST", "/images", command, {"Idempotency-Key": "one"})
    assert code == 200 and replay["image_id"] == image_id
    assert "upload_url" not in replay and replay["upload_expires_at"] == created["upload_expires_at"]
    store(image_id, data)
    monkeypatch.setattr(lifecycle, "now_epoch", lambda: original_now + 1801)
    assert call("POST", f"/images/{image_id}/complete")[0] == 410


def test_unknown_validation_failures_stop_after_three_claims(monkeypatch):
    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    clock = [lifecycle.now_epoch()]
    monkeypatch.setattr(lifecycle, "now_epoch", lambda: clock[0])
    monkeypatch.setattr(storage, "read_original", lambda *_: (_ for _ in ()).throw(OSError("transient")))
    for attempt in range(1, 4):
        lifecycle.run_work(image_id, "validate")
        image = lifecycle._get(image_id)
        assert int(image["attempt"]) == attempt
        if attempt < 3:
            assert image["status"] == "processing"
            clock[0] = int(image["retry_after"])
    assert image["status"] == "failed" and image["cleanup_pending"] is True


def test_cleanup_backoff_caps_at_one_hour(monkeypatch):
    created, _, _ = initiate()
    image_id = created["image_id"]
    call("DELETE", f"/images/{image_id}")
    monkeypatch.setattr(storage, "write_placeholder", lambda *_: (_ for _ in ()).throw(RuntimeError("down")))
    clock = [lifecycle.now_epoch()]
    monkeypatch.setattr(lifecycle, "now_epoch", lambda: clock[0])
    for attempt in range(12):
        before = clock[0]
        lifecycle.run_work(image_id, "cleanup")
        clock[0] = int(lifecycle._get(image_id)["cleanup_retry_after"])
        if attempt == 11:
            assert clock[0] - before == 3600
    image = lifecycle._get(image_id)
    assert image["status"] == "deleting" and image["cleanup_pending"] is True
    assert int(image["cleanup_attempt"]) == 12
    assert int(image["next_check_at"]) == clock[0]


def test_upload_size_boundary():
    body = {"filename": "big.png", "content_type": "image/png", "size_bytes": storage.MAX_BYTES}
    assert call("POST", "/images", body, {"Idempotency-Key": "largest"})[0] == 201
    body["size_bytes"] += 1
    assert call("POST", "/images", body, {"Idempotency-Key": "too-large"})[0] == 400


@pytest.mark.parametrize(
    "patch",
    [
        {"filename": ""},
        {"filename": "x" * 256},
        {"filename": "\ud800"},
        {"content_type": "text/plain"},
        {"size_bytes": True},
        {"size_bytes": 0},
        {"caption": "x" * 2001},
        {"caption": "\ud800"},
        {"unknown": True},
    ],
)
def test_invalid_upload_fields_are_client_errors(patch):
    command = {"filename": "sample.png", "content_type": "image/png", "size_bytes": 40}
    code, body = call("POST", "/images", {**command, **patch}, {"Idempotency-Key": "boundary"})
    assert code == 400
    assert body["code"] == "invalid_request"
    assert body["request_id"] == "req-1"


@pytest.mark.parametrize(
    "query",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "abc"},
        {"owner_id": ""},
        {"date_from": "yesterday"},
        {"date_from": "2026-01-01T00:00:00"},
        {"date_from": "2026-01-01T00:00:00+02:00"},
        {"date_from": "2026-02-01", "date_to": "2026-01-01"},
        {"unknown": "x"},
    ],
)
def test_invalid_gallery_parameters_are_client_errors(query):
    code, body = call("GET", "/images", query=query)
    assert code == 400
    assert body["code"] == "invalid_request"


@pytest.mark.parametrize("raw,encoded", [("not json", False), ("[]", False), ("###", True)])
def test_invalid_request_encoding_is_a_client_error(raw, encoded):
    request = event("POST", "/images", headers={"Idempotency-Key": "encoding"})
    request.update(body=raw, isBase64Encoded=encoded)
    response = http.handler(request, None)
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["code"] == "invalid_request"


@pytest.mark.parametrize(
    "aws_code,status_code",
    [
        ("ThrottlingException", 429),
        ("TooManyRequestsException", 429),
        ("AccessDeniedException", 503),
    ],
)
def test_dependency_errors_have_safe_http_responses(monkeypatch, aws_code, status_code):
    from botocore.exceptions import ClientError

    def fail(*_):
        raise ClientError({"Error": {"Code": aws_code, "Message": "private internal detail"}}, "Query")

    monkeypatch.setattr(lifecycle, "list_ready", fail)
    code, body = call("GET", "/images")
    assert code == status_code
    assert "private internal detail" not in json.dumps(body)
    assert body["request_id"] == "req-1"


def test_structured_request_log_excludes_upload_permission_and_caption(caplog):
    command = {
        "filename": "sample.png",
        "content_type": "image/png",
        "size_bytes": 40,
        "caption": "secret-caption-test",
    }
    code, body = call("POST", "/images", command, {"Idempotency-Key": "safe-logs"})
    assert code == 201
    messages = [record.getMessage() for record in caplog.records if record.name == "image_service.lifecycle"]
    records = [json.loads(message) for message in messages]
    assert {"operation": "http", "outcome": "response", "request_id": "req-1", "status_code": 201} in records
    assert "secret-caption-test" not in caplog.text
    assert body["upload_url"] not in caplog.text
    assert "X-Amz-Signature" not in caplog.text


def test_rest_authorizer_claims_override_forged_local_header(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    request = event(
        "POST", "/images",
        {"filename": "trusted.png", "content_type": "image/png", "size_bytes": 10},
        {"Idempotency-Key": "rest-claim", "X-User-Id": "forged-owner"},
    )
    request["requestContext"]["authorizer"] = {"claims": {"sub": "trusted-owner"}}
    response = http.handler(request, None)
    assert response["statusCode"] == 201
    image_id = json.loads(response["body"])["image_id"]
    assert lifecycle._get(image_id)["owner_id"] == "trusted-owner"


def test_gallery_batch_retries_partial_responses_and_preserves_index_order(monkeypatch):
    ids = []
    for index in range(3):
        created, data, _ = initiate(f"batch-{index}")
        image_id = created["image_id"]
        store(image_id, data)
        call("POST", f"/images/{image_id}/complete")
        lifecycle.run_work(image_id, "validate")
        ids.append(image_id)
    expected = sorted(ids, key=lambda image_id: lifecycle._get(image_id)["gallery_sort"], reverse=True)
    actual_resource = lifecycle._resource
    calls = []

    class PartialResource:
        def Table(self, name):
            return actual_resource().Table(name)

        def batch_get_item(self, RequestItems):
            calls.append(RequestItems)
            request = RequestItems["Images"]
            assert request["ConsistentRead"] is True
            assert len(request["Keys"]) <= 100
            if len(calls) == 1:
                first_two = {"Images": {"Keys": request["Keys"][:2], "ConsistentRead": True}}
                result = actual_resource().batch_get_item(RequestItems=first_two)
                return {
                    "Responses": {"Images": list(reversed(result["Responses"]["Images"]))},
                    "UnprocessedKeys": {"Images": {"Keys": request["Keys"][2:], "ConsistentRead": True}},
                }
            return actual_resource().batch_get_item(RequestItems=RequestItems)

    monkeypatch.setattr(lifecycle, "_resource", lambda: PartialResource())
    code, page = call("GET", "/images", query={"limit": "3"})
    assert code == 200
    assert [item["image_id"] for item in page["items"]] == expected
    assert len(calls) == 2


def test_gallery_exhausted_unprocessed_keys_returns_503(monkeypatch):
    created, data, _ = initiate()
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    lifecycle.run_work(image_id, "validate")
    actual_resource = lifecycle._resource
    calls = []

    class StalledResource:
        def Table(self, name):
            return actual_resource().Table(name)

        def batch_get_item(self, RequestItems):
            calls.append(RequestItems)
            return {"Responses": {}, "UnprocessedKeys": RequestItems}

    monkeypatch.setattr(lifecycle, "_resource", lambda: StalledResource())
    monkeypatch.setattr(lifecycle.time, "sleep", lambda *_: None)
    code, body = call("GET", "/images")
    assert code == 503 and body["code"] == "dependency_unavailable"
    assert len(calls) == 4
    assert all(call["Images"]["ConsistentRead"] is True for call in calls)


def test_repeated_delete_does_not_dispatch_again_or_bypass_cleanup_retry(monkeypatch):
    created, _, _ = initiate()
    image_id = created["image_id"]
    dispatched = []
    monkeypatch.setattr(lifecycle, "_dispatch", lambda *args: dispatched.append(args))
    assert call("DELETE", f"/images/{image_id}")[0] == 202
    assert call("DELETE", f"/images/{image_id}")[0] == 202
    assert dispatched == [(image_id, "cleanup")]
    clock = [lifecycle.now_epoch()]
    monkeypatch.setattr(lifecycle, "now_epoch", lambda: clock[0])
    writes = []

    def fail_write(*args):
        writes.append(args)
        raise OSError("unavailable")

    monkeypatch.setattr(storage, "write_placeholder", fail_write)
    lifecycle.run_work(image_id, "cleanup")
    image = lifecycle._get(image_id)
    assert len(writes) == 1 and image["cleanup_retry_after"] == clock[0] + 30
    assert lifecycle._reschedule(image, clock[0] + 300)
    lifecycle.run_work(image_id, "cleanup")
    assert len(writes) == 1
    clock[0] += 30
    lifecycle.run_work(image_id, "cleanup")
    assert len(writes) == 2


def test_date_lower_bound_does_not_hide_unprocessed_prefetched_candidates():
    recent = []
    for index in range(3):
        created, data, _ = initiate(f"recent-{index}")
        image_id = created["image_id"]
        store(image_id, data)
        call("POST", f"/images/{image_id}/complete")
        lifecycle.run_work(image_id, "validate")
        recent.append(image_id)
    created, data, _ = initiate("old")
    image_id = created["image_id"]
    store(image_id, data)
    call("POST", f"/images/{image_id}/complete")
    lifecycle.run_work(image_id, "validate")
    old_time = "2000-01-01T00:00:00.000000Z"
    boto3.resource("dynamodb").Table("Images").update_item(
        Key={"image_id": image_id},
        UpdateExpression="SET ready_at=:ready, gallery_sort=:sort",
        ExpressionAttributeValues={":ready": old_time, ":sort": f"{old_time}#{image_id}"},
    )
    seen = []
    cursor = None
    for _ in range(3):
        params = {"date_from": "2026-01-01", "limit": "1"}
        if cursor:
            params["cursor"] = cursor
        code, page = call("GET", "/images", query=params)
        assert code == 200
        seen.extend(item["image_id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert len(seen) == 3 and set(seen) == set(recent) and cursor is None
