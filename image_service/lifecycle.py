"""Durable image lifecycle and conditional work claims."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Literal, TypedDict

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from . import storage

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
UPLOAD_SECONDS = 900
COMPLETE_SECONDS = 1800
LEASE_SECONDS = 180
MAX_ATTEMPTS = 3

State = Literal["uploading", "processing", "ready", "rejected", "failed", "expired", "deleting", "deleted"]


class ImageRecord(TypedDict, total=False):
    image_id: str
    owner_id: str
    status: State
    s3_key: str
    filename: str
    caption: str
    content_type: str
    declared_size: int
    size_bytes: int
    width: int
    height: int
    created_at: str
    upload_expires_at: int
    completion_deadline: int
    processing_deadline: int
    ready_at: str
    public_scope: str
    owner_scope: str
    gallery_sort: str
    maintenance_scope: str
    maintenance_sort: str
    next_check_at: int
    attempt: int
    work_token: str
    lease_until: int
    cleanup_pending: bool
    cleanup_attempt: int
    cleanup_retry_after: int
    retry_after: int
    error_reason: str


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def log_event(operation: str, outcome: str, **fields) -> None:
    LOG.info(json.dumps({"operation": operation, "outcome": outcome, **fields}, separators=(",", ":")))


def now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def timestamp(epoch: float | None = None) -> str:
    moment = datetime.fromtimestamp(epoch, UTC) if epoch is not None else datetime.now(UTC)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _endpoint():
    return os.getenv("AWS_ENDPOINT_URL") or None


def _resource():
    return boto3.resource("dynamodb", endpoint_url=_endpoint())


def _table():
    return _resource().Table(os.environ["IMAGE_TABLE"])


def _client():
    return boto3.client("dynamodb", endpoint_url=_endpoint())


def _serialize(item: dict) -> dict:
    serial = TypeSerializer()
    return {key: serial.serialize(value) for key, value in item.items()}


def _conditional(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] in {"ConditionalCheckFailedException", "TransactionCanceledException"}


def _get(image_id: str) -> ImageRecord | None:
    return _table().get_item(Key={"image_id": image_id}, ConsistentRead=True).get("Item")


def _owned(owner: str, image_id: str) -> ImageRecord:
    image = _get(image_id)
    if not image or image["owner_id"] != owner:
        raise ServiceError(404, "not_found", "Image not found")
    return image


def _request_key(owner: str, client_key: str) -> str:
    encoded = json.dumps([owner, client_key], ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fingerprint(command: dict) -> str:
    encoded = json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _invoke(image_id: str, operation: str) -> None:
    boto3.client("lambda", endpoint_url=_endpoint()).invoke(
        FunctionName=os.environ["WORKER_FUNCTION"],
        InvocationType="Event",
        Payload=json.dumps({"image_id": image_id, "operation": operation}).encode(),
    )


def _dispatch(image_id: str, operation: str) -> None:
    try:
        _invoke(image_id, operation)
    except Exception as exc:
        log_event("dispatch", "deferred", image_id=image_id, work=operation, error_type=type(exc).__name__)


def _replay(owner: str, request_key: str, fingerprint: str) -> dict:
    mapping = _resource().Table(os.environ["REQUEST_TABLE"]).get_item(
        Key={"request_key": request_key}, ConsistentRead=True
    ).get("Item")
    if not mapping:
        raise ServiceError(503, "dependency_unavailable", "Upload creation is being resolved; retry")
    if mapping["fingerprint"] != fingerprint:
        raise ServiceError(409, "idempotency_conflict", "Idempotency key was used with different input")
    image = _owned(owner, mapping["image_id"])
    return _initiation_view(image, replay=True, deadlines=mapping)


def _initiation_view(image: ImageRecord, replay: bool, deadlines: dict | None = None) -> dict:
    result = {
        "image_id": image["image_id"],
        "status": image["status"],
        "replay": replay,
    }
    source = deadlines or image
    result["upload_expires_at"] = timestamp(int(source["upload_expires_at"]))
    result["completion_deadline"] = timestamp(int(source["completion_deadline"]))
    if image["status"] != "uploading":
        return result
    remaining = max(0, int(image["upload_expires_at"]) - now_epoch() - 1)
    if image["status"] == "uploading" and remaining > 0:
        permission = storage.upload_permission(
            os.environ["IMAGE_BUCKET"], image["s3_key"], image["content_type"],
            int(image["declared_size"]), remaining,
        )
        result.update(upload_url=permission["url"], upload_headers=permission["headers"])
    return result


def initiate(owner: str, command: dict, client_key: str) -> dict:
    image_id = str(uuid.uuid4())
    epoch = now_epoch()
    request_key = _request_key(owner, client_key)
    fingerprint = _fingerprint(command)
    image: ImageRecord = {
        "image_id": image_id,
        "owner_id": owner,
        "status": "uploading",
        "s3_key": f"originals/{image_id}",
        "filename": command["filename"],
        "caption": command.get("caption", ""),
        "content_type": command["content_type"],
        "declared_size": command["size_bytes"],
        "created_at": timestamp(),
        "upload_expires_at": epoch + UPLOAD_SECONDS,
        "completion_deadline": epoch + COMPLETE_SECONDS,
        "maintenance_scope": "MAINT",
        "maintenance_sort": f"{timestamp(epoch + COMPLETE_SECONDS)}#{image_id}",
        "next_check_at": epoch + COMPLETE_SECONDS,
        "attempt": 0,
        "cleanup_pending": False,
    }
    mapping = {
        "request_key": request_key,
        "image_id": image_id,
        "fingerprint": fingerprint,
        "expires_at": epoch + 86400,
        "upload_expires_at": epoch + UPLOAD_SECONDS,
        "completion_deadline": epoch + COMPLETE_SECONDS,
    }
    try:
        _client().transact_write_items(TransactItems=[
            {"Put": {"TableName": os.environ["IMAGE_TABLE"], "Item": _serialize(image),
                     "ConditionExpression": "attribute_not_exists(image_id)"}},
            {"Put": {"TableName": os.environ["REQUEST_TABLE"], "Item": _serialize(mapping),
                     "ConditionExpression": "attribute_not_exists(request_key)"}},
        ])
    except ClientError as exc:
        if _conditional(exc):
            return _replay(owner, request_key, fingerprint)
        raise
    return _initiation_view(image, replay=False)


def _status_view(image: ImageRecord) -> dict:
    result = {"image_id": image["image_id"], "status": image["status"]}
    if image.get("error_reason"):
        result["error_reason"] = image["error_reason"]
    return result


def status(owner: str, image_id: str) -> dict:
    return _status_view(_owned(owner, image_id))


def complete(owner: str, image_id: str) -> tuple[int, dict]:
    image = _owned(owner, image_id)
    state = image["status"]
    if state == "ready":
        return 200, _status_view(image)
    if state == "processing":
        _dispatch(image_id, "validate")
        return 202, _status_view(image)
    if state == "expired":
        raise ServiceError(410, "upload_expired", "Completion deadline has passed")
    if state != "uploading":
        raise ServiceError(409, "invalid_state", f"Image is {state}")
    if now_epoch() >= int(image["completion_deadline"]):
        raise ServiceError(410, "upload_expired", "Completion deadline has passed")
    if not storage.object_exists(os.environ["IMAGE_BUCKET"], image["s3_key"]):
        raise ServiceError(409, "upload_missing", "Upload has not reached storage")
    epoch = now_epoch()
    deadline = epoch + COMPLETE_SECONDS
    try:
        _table().update_item(
            Key={"image_id": image_id},
            UpdateExpression=(
                "SET #s=:processing, processing_deadline=:deadline, next_check_at=:next, "
                "maintenance_scope=:maint, maintenance_sort=:sort"
            ),
            ConditionExpression="#s=:uploading AND owner_id=:owner AND completion_deadline>:now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":processing": "processing", ":deadline": deadline, ":next": epoch,
                ":maint": "MAINT", ":sort": f"{timestamp(epoch)}#{image_id}",
                ":uploading": "uploading", ":owner": owner, ":now": epoch,
            },
        )
    except ClientError as exc:
        if _conditional(exc):
            return complete(owner, image_id)
        raise
    _dispatch(image_id, "validate")
    return 202, {"image_id": image_id, "status": "processing"}


def _terminal(image: ImageRecord, state: State, reason: str = "") -> bool:
    epoch = now_epoch()
    try:
        _table().update_item(
            Key={"image_id": image["image_id"]},
            UpdateExpression=(
                "SET #s=:new, cleanup_pending=:yes, next_check_at=:next, maintenance_scope=:maint, "
                "maintenance_sort=:sort, error_reason=:reason "
                "REMOVE work_token, lease_until, public_scope, owner_scope, gallery_sort"
            ),
            ConditionExpression="#s=:old" + (" AND work_token=:token" if image.get("work_token") else ""),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":new": state, ":yes": True, ":next": epoch,
                ":maint": "MAINT", ":sort": f"{timestamp(epoch)}#{image['image_id']}",
                ":reason": reason, ":old": image["status"],
                **({":token": image["work_token"]} if image.get("work_token") else {}),
            },
        )
        return True
    except ClientError as exc:
        if _conditional(exc):
            return False
        raise


def delete(owner: str, image_id: str) -> tuple[int, dict | None]:
    image = _owned(owner, image_id)
    if image["status"] == "deleted":
        return 204, None
    if image["status"] == "deleting":
        return 202, _status_view(image)
    if not _terminal(image, "deleting"):
        return delete(owner, image_id)
    _dispatch(image_id, "cleanup")
    return 202, {"image_id": image_id, "status": "deleting"}


def public_image(image_id: str) -> dict:
    image = _get(image_id)
    if not image or image["status"] != "ready":
        raise ServiceError(404, "not_found", "Image not found")
    return _public_view(image)


def _public_view(image: ImageRecord) -> dict:
    result = {
        field: image[field] for field in (
            "image_id", "owner_id", "caption", "filename", "content_type",
            "size_bytes", "width", "height", "ready_at",
        )
    }
    for field in ("size_bytes", "width", "height"):
        result[field] = int(result[field])
    return result


def content(image_id: str, disposition: str) -> str:
    image = _get(image_id)
    if not image or image["status"] != "ready":
        raise ServiceError(404, "not_found", "Image not found")
    return storage.download_permission(
        os.environ["IMAGE_BUCKET"], image["s3_key"], image["filename"], disposition
    )


def _batch_get(image_ids: list[str]) -> dict[str, ImageRecord]:
    table_name = os.environ["IMAGE_TABLE"]
    pending = {table_name: {"Keys": [{"image_id": image_id} for image_id in image_ids], "ConsistentRead": True}}
    records: dict[str, ImageRecord] = {}
    for attempt in range(4):
        response = _resource().batch_get_item(RequestItems=pending)
        records.update({item["image_id"]: item for item in response.get("Responses", {}).get(table_name, [])})
        pending = response.get("UnprocessedKeys", {})
        if not pending or not pending.get(table_name, {}).get("Keys"):
            return records
        if attempt < 3:
            time.sleep(0.05 * 2 ** attempt)
    log_event("gallery", "unprocessed_keys", count=len(pending[table_name]["Keys"]))
    raise ServiceError(503, "dependency_unavailable", "Gallery is temporarily unavailable")


def list_ready(owner: str | None, start: str | None, end: str | None,
               limit: int, position: str | None) -> tuple[list[dict], str | None]:
    table = _table()
    index = "owner_gallery" if owner else "global_gallery"
    scope = "owner_scope" if owner else "public_scope"
    partition = owner or "PUBLIC"
    items: list[dict] = []
    examined = 0
    last = position
    exhausted = False
    while examined < 499 and len(items) < limit:
        upper = min(filter(None, (last, end)), default=None)
        condition = Key(scope).eq(partition)
        if upper:
            condition &= Key("gallery_sort").lt(upper)
        query_limit = min(100, 499 - examined, max(10, limit - len(items)))
        response = table.query(
            IndexName=index, KeyConditionExpression=condition,
            ScanIndexForward=False, Limit=query_limit,
        )
        candidates = response["Items"]
        if not candidates:
            exhausted = True
            break
        eligible = []
        for candidate in candidates:
            if start and candidate["gallery_sort"] < start:
                exhausted = True
                break
            eligible.append(candidate)
        current_by_id = _batch_get([candidate["image_id"] for candidate in eligible]) if eligible else {}
        for candidate in eligible:
            sort = candidate["gallery_sort"]
            last = sort
            examined += 1
            current = current_by_id.get(candidate["image_id"])
            if (current and current["status"] == "ready" and current.get("gallery_sort") == sort
                    and (owner is None or current["owner_id"] == owner)):
                items.append(_public_view(current))
            if len(items) >= limit or examined >= 499:
                break
        if len(items) >= limit:
            exhausted = False
        if exhausted or len(items) >= limit or examined >= 499:
            break
        if "LastEvaluatedKey" not in response:
            exhausted = True
            break
    if not last or exhausted:
        return items, None
    condition = Key(scope).eq(partition) & Key("gallery_sort").lt(min(filter(None, (last, end))))
    probe = table.query(IndexName=index, KeyConditionExpression=condition,
                        ScanIndexForward=False, Limit=1)["Items"]
    if not probe or (start and probe[0]["gallery_sort"] < start):
        return items, None
    return items, last


def _claim(image: ImageRecord) -> str | None:
    epoch = now_epoch()
    token = str(uuid.uuid4())
    if image["status"] != "processing":
        return None
    try:
        _table().update_item(
            Key={"image_id": image["image_id"]},
            UpdateExpression=(
                "SET work_token=:token, lease_until=:lease, next_check_at=:lease, "
                "maintenance_sort=:sort ADD attempt :one"
            ),
            ConditionExpression=(
                "#s=:processing AND attempt<:max AND processing_deadline>:now "
                "AND (attribute_not_exists(lease_until) OR lease_until<=:now) "
                "AND (attribute_not_exists(retry_after) OR retry_after<=:now)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":token": token, ":lease": epoch + LEASE_SECONDS,
                ":sort": f"{timestamp(epoch + LEASE_SECONDS)}#{image['image_id']}",
                ":one": 1, ":max": MAX_ATTEMPTS, ":now": epoch,
                ":processing": "processing",
            },
        )
        return token
    except ClientError as exc:
        if _conditional(exc):
            return None
        raise


def _publish(image: ImageRecord, token: str, facts: storage.ImageFacts) -> bool:
    ready_at = timestamp()
    epoch = now_epoch()
    try:
        _table().update_item(
            Key={"image_id": image["image_id"]},
            UpdateExpression=(
                "SET #s=:ready, ready_at=:ready_at, size_bytes=:size, width=:width, height=:height, "
                "public_scope=:public, owner_scope=:owner, gallery_sort=:sort "
                "REMOVE work_token, lease_until, retry_after, next_check_at, maintenance_scope, maintenance_sort"
            ),
            ConditionExpression=(
                "#s=:processing AND work_token=:token AND lease_until>:now "
                "AND processing_deadline>:now"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ready": "ready", ":ready_at": ready_at, ":size": facts.size,
                ":width": facts.width, ":height": facts.height, ":public": "PUBLIC",
                ":owner": image["owner_id"], ":sort": f"{ready_at}#{image['image_id']}",
                ":processing": "processing", ":token": token, ":now": epoch,
            },
        )
        return True
    except ClientError as exc:
        if _conditional(exc):
            return False
        raise


def _retry(image: ImageRecord, token: str, reason: str) -> None:
    epoch = now_epoch()
    attempt = int(image["attempt"])
    if attempt >= MAX_ATTEMPTS or epoch >= int(image["processing_deadline"]):
        current = _get(image["image_id"])
        if current and current.get("work_token") == token and _terminal(current, "failed", reason):
            _dispatch(image["image_id"], "cleanup")
        return
    delay = min(120, 10 * 2 ** (attempt - 1))
    try:
        _table().update_item(
            Key={"image_id": image["image_id"]},
            UpdateExpression=(
                "SET retry_after=:retry, next_check_at=:retry, maintenance_sort=:sort "
                "REMOVE work_token, lease_until"
            ),
            ConditionExpression="#s=:processing AND work_token=:token",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":retry": epoch + delay, ":sort": f"{timestamp(epoch + delay)}#{image['image_id']}",
                ":processing": "processing", ":token": token,
            },
        )
    except ClientError as exc:
        if not _conditional(exc):
            raise


def _validate(image: ImageRecord) -> None:
    token = _claim(image)
    if token is None:
        return
    current = _get(image["image_id"])
    if not current or current.get("work_token") != token:
        return
    try:
        data = storage.read_original(os.environ["IMAGE_BUCKET"], image["s3_key"])
        if len(data) != int(image["declared_size"]):
            raise storage.InvalidImage("Uploaded size differs from the declared size")
        facts = storage.validate_original(data, image["content_type"])
    except storage.InvalidImage as exc:
        if _terminal(current, "rejected", str(exc)):
            _dispatch(image["image_id"], "cleanup")
        return
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        log_event("validate", "storage_error", image_id=image["image_id"], error_code=code)
        if code in {"AccessDenied", "403"}:
            if _terminal(current, "failed", "Storage access was denied"):
                _dispatch(image["image_id"], "cleanup")
        else:
            _retry(current, token, "Validation failed after retries")
        return
    except Exception as exc:
        log_event("validate", "retry", image_id=image["image_id"], error_type=type(exc).__name__)
        _retry(current, token, "Validation failed after retries")
        return
    _publish(image, token, facts)


def _cleanup(image: ImageRecord) -> None:
    if image["status"] not in {"deleting", "rejected", "failed", "expired"} or not image.get("cleanup_pending"):
        return
    if int(image.get("cleanup_retry_after", 0)) > now_epoch():
        return
    try:
        storage.write_placeholder(os.environ["IMAGE_BUCKET"], image["s3_key"])
    except Exception as exc:
        log_event("cleanup", "retry", image_id=image["image_id"], error_type=type(exc).__name__)
        attempt = int(image.get("cleanup_attempt", 0)) + 1
        delay = min(3600, 30 * 2 ** min(attempt - 1, 7))
        epoch = now_epoch()
        try:
            _table().update_item(
                Key={"image_id": image["image_id"]},
                UpdateExpression=(
                    "SET cleanup_attempt=:attempt, cleanup_retry_after=:next, "
                    "next_check_at=:next, maintenance_sort=:sort"
                ),
                ConditionExpression="#s=:state AND cleanup_pending=:yes",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":attempt": attempt, ":next": epoch + delay,
                    ":sort": f"{timestamp(epoch + delay)}#{image['image_id']}",
                    ":state": image["status"], ":yes": True,
                },
            )
        except ClientError as exc:
            if not _conditional(exc):
                raise
        return
    if image["status"] == "deleting":
        update = (
            "SET #s=:deleted, cleanup_pending=:no "
            "REMOVE s3_key, filename, caption, content_type, declared_size, size_bytes, width, height, "
            "created_at, upload_expires_at, completion_deadline, processing_deadline, ready_at, "
            "error_reason, attempt, cleanup_attempt, cleanup_retry_after, next_check_at, maintenance_scope, "
            "maintenance_sort, work_token, lease_until, retry_after"
        )
    else:
        update = (
            "SET cleanup_pending=:no REMOVE cleanup_attempt, cleanup_retry_after, "
            "next_check_at, maintenance_scope, maintenance_sort"
        )
    try:
        _table().update_item(
            Key={"image_id": image["image_id"]},
            UpdateExpression=update,
            ConditionExpression="#s=:state AND cleanup_pending=:yes",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":state": image["status"], ":yes": True, ":no": False,
                **({":deleted": "deleted"} if image["status"] == "deleting" else {}),
            },
        )
    except ClientError as exc:
        if not _conditional(exc):
            raise


def run_work(image_id: str, operation: str) -> None:
    image = _get(image_id)
    if not image:
        return
    if operation == "validate":
        _validate(image)
    elif operation == "cleanup":
        _cleanup(image)
    else:
        raise ValueError("Unknown worker operation")


def _reschedule(image: ImageRecord, next_epoch: int) -> bool:
    try:
        _table().update_item(
            Key={"image_id": image["image_id"]},
            UpdateExpression="SET next_check_at=:next, maintenance_sort=:sort",
            ConditionExpression="#s=:state AND next_check_at=:old AND maintenance_sort=:oldsort",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":next": next_epoch, ":sort": f"{timestamp(next_epoch)}#{image['image_id']}",
                ":state": image["status"], ":old": int(image["next_check_at"]),
                ":oldsort": image["maintenance_sort"],
            },
        )
        return True
    except ClientError as exc:
        if _conditional(exc):
            return False
        raise


def recover(now: int | None = None, budget: int = 200, context=None) -> int:
    epoch = now if now is not None else now_epoch()
    response = _table().query(
        IndexName="maintenance",
        KeyConditionExpression=Key("maintenance_scope").eq("MAINT") &
        Key("maintenance_sort").lte(f"{timestamp(epoch)}#~"),
        Limit=min(budget, 200),
    )
    handled = 0
    for candidate in response["Items"]:
        if context and context.get_remaining_time_in_millis() < 5000:
            break
        image = _get(candidate["image_id"])
        if not image or image.get("maintenance_sort") != candidate["maintenance_sort"]:
            continue
        state = image["status"]
        if state == "uploading":
            if epoch >= int(image["completion_deadline"]) and _terminal(
                image, "expired", "Upload was not completed before its deadline"
            ):
                _dispatch(image["image_id"], "cleanup")
            continue
        if state == "processing":
            if epoch >= int(image["processing_deadline"]) or (
                int(image["attempt"]) >= MAX_ATTEMPTS and epoch >= int(image.get("lease_until", 0))
            ):
                if _terminal(image, "failed", "Validation deadline or attempt limit reached"):
                    _dispatch(image["image_id"], "cleanup")
                continue
            if int(image.get("lease_until", 0)) > epoch:
                _reschedule(image, int(image["lease_until"]))
                continue
            if int(image.get("retry_after", 0)) > epoch:
                _reschedule(image, int(image["retry_after"]))
                continue
            if _reschedule(image, epoch + 300):
                _dispatch(image["image_id"], "validate")
            handled += 1
            continue
        if state in {"deleting", "rejected", "failed", "expired"} and image.get("cleanup_pending"):
            if _reschedule(image, epoch + 300):
                _dispatch(image["image_id"], "cleanup")
            handled += 1
    return handled


def worker_handler(event: dict, context) -> None:
    image_id = str(uuid.UUID(event["image_id"]))
    operation = event["operation"]
    if operation not in {"validate", "cleanup"}:
        raise ValueError("Unknown worker operation")
    fields = {"image_id": image_id, "request_id": getattr(context, "aws_request_id", None)}
    log_event(operation, "started", **fields)
    try:
        run_work(image_id, operation)
    except Exception as exc:
        log_event(operation, "error", error_type=type(exc).__name__, **fields)
        raise
    log_event(operation, "finished", **fields)


def recovery_handler(_event: dict, context) -> dict:
    handled = recover(context=context)
    log_event("recovery", "finished", handled=handled, request_id=getattr(context, "aws_request_id", None))
    return {"handled": handled}
