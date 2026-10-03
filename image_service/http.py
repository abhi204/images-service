"""API Gateway boundary for the image lifecycle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import UTC, date, datetime

from botocore.exceptions import ClientError

from . import lifecycle, storage

MAX_BODY = 16 * 1024
MAX_CURSOR = 2048
ID_PATTERN = re.compile(r"^[\x21-\x7e]{1,128}$")


def _response(status: int, body: dict | None = None, headers: dict | None = None) -> dict:
    result = {"statusCode": status, "headers": {"Content-Type": "application/json", **(headers or {})}}
    if body is not None:
        result["body"] = json.dumps(body, separators=(",", ":"))
    return result


def _error(status: int, code: str, message: str, request_id: str) -> dict:
    return _response(status, {"code": code, "message": message, "request_id": request_id})


def _identity(event: dict, headers: dict) -> str:
    auth = event.get("requestContext", {}).get("authorizer") or {}
    owner = (
        (auth.get("jwt") or {}).get("claims", {}).get("sub")
        or (auth.get("claims") or {}).get("sub")
        or (auth.get("lambda") or {}).get("sub")
        or auth.get("principalId")
    )
    if not owner and os.getenv("APP_ENV") == "local":
        owner = headers.get("x-user-id")
    if not isinstance(owner, str) or not owner or len(owner.encode("utf-8")) > 256:
        raise lifecycle.ServiceError(401, "unauthorized", "Authentication is required")
    return owner


def _body(event: dict) -> dict:
    raw = event.get("body") or ""
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BODY:
        raise lifecycle.ServiceError(400, "invalid_request", "Request body is too large")
    try:
        if event.get("isBase64Encoded"):
            data = base64.b64decode(raw, validate=True)
            if len(data) > MAX_BODY:
                raise ValueError("body too large")
            raw = data.decode("utf-8")
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise lifecycle.ServiceError(400, "invalid_request", "Body must be a JSON object") from exc
    if not isinstance(value, dict):
        raise lifecycle.ServiceError(400, "invalid_request", "Body must be a JSON object")
    return value


def _upload_command(body: dict) -> dict:
    if set(body) - {"filename", "content_type", "size_bytes", "caption"}:
        raise lifecycle.ServiceError(400, "invalid_request", "Unknown upload field")
    filename = body.get("filename")
    media = body.get("content_type")
    size = body.get("size_bytes")
    caption = body.get("caption", "")
    if not isinstance(filename, str) or not filename or len(filename.encode("utf-8")) > 255 or "\x00" in filename:
        raise lifecycle.ServiceError(400, "invalid_request", "Invalid filename")
    if media not in storage.MEDIA_TYPES.values():
        raise lifecycle.ServiceError(400, "invalid_request", "Unsupported content type")
    if type(size) is not int or not 1 <= size <= storage.MAX_BYTES:
        raise lifecycle.ServiceError(400, "invalid_request", "Invalid image size")
    if not isinstance(caption, str) or len(caption) > 2000:
        raise lifecycle.ServiceError(400, "invalid_request", "Invalid caption")
    caption.encode("utf-8")
    return {"filename": filename, "content_type": media, "size_bytes": size, "caption": caption}


def _date(value: str, name: str) -> str:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            moment = datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
        else:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None or moment.utcoffset().total_seconds() != 0:
                raise ValueError("UTC required")
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, AttributeError) as exc:
        raise lifecycle.ServiceError(400, "invalid_request", f"Invalid {name}") from exc


def _cursor_encode(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(os.environ["CURSOR_SECRET"].encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).rstrip(b"=").decode()


def _cursor_decode(value: str, query: dict) -> str:
    if len(value) > MAX_CURSOR or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise lifecycle.ServiceError(400, "invalid_cursor", "Invalid cursor")
    try:
        blob = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        raw, signature = blob[:-32], blob[-32:]
        expected = hmac.new(os.environ["CURSOR_SECRET"].encode(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        data = json.loads(raw)
        if data["query"] != query or data["expires_at"] <= lifecycle.now_epoch():
            raise ValueError("scope or expiry")
        position = data["position"]
        if not isinstance(position, str) or len(position) > 100:
            raise ValueError("position")
        return position
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise lifecycle.ServiceError(400, "invalid_cursor", "Invalid cursor") from exc


def _list(params: dict) -> dict:
    if set(params) - {"owner_id", "date_from", "date_to", "limit", "cursor"}:
        raise lifecycle.ServiceError(400, "invalid_request", "Unknown query field")
    owner = params.get("owner_id")
    if owner is not None and (not isinstance(owner, str) or not owner or len(owner.encode()) > 256):
        raise lifecycle.ServiceError(400, "invalid_request", "Invalid owner ID")
    start = _date(params["date_from"], "date_from") if "date_from" in params else None
    end = _date(params["date_to"], "date_to") if "date_to" in params else None
    if start and end and start >= end:
        raise lifecycle.ServiceError(400, "invalid_request", "Date range is empty")
    raw_limit = params.get("limit", "25")
    if not isinstance(raw_limit, str) or not re.fullmatch(r"[1-9]\d{0,2}", raw_limit):
        raise lifecycle.ServiceError(400, "invalid_request", "Invalid limit")
    limit = int(raw_limit)
    if limit > 100:
        raise lifecycle.ServiceError(400, "invalid_request", "Limit exceeds 100")
    query = {"owner_id": owner, "date_from": start, "date_to": end, "limit": limit, "order": "ready_desc"}
    position = _cursor_decode(params["cursor"], query) if "cursor" in params else None
    items, next_position = lifecycle.list_ready(owner, start, end, limit, position)
    next_cursor = _cursor_encode({"query": query, "position": next_position,
                                  "expires_at": lifecycle.now_epoch() + 3600}) if next_position else None
    return {"items": items, "next_cursor": next_cursor}


def handler(event: dict, _context) -> dict:
    request_id = event.get("requestContext", {}).get("requestId") or str(uuid.uuid4())
    response = _handle(event, request_id)
    lifecycle.log_event("http", "response", request_id=request_id, status_code=response["statusCode"])
    return response


def _handle(event: dict, request_id: str) -> dict:
    method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod", "")
    path = event.get("rawPath") or event.get("path") or ""
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    params = event.get("queryStringParameters") or {}
    try:
        if path == "/images" and method == "POST":
            owner = _identity(event, headers)
            key = headers.get("idempotency-key")
            if not isinstance(key, str) or not ID_PATTERN.fullmatch(key):
                raise lifecycle.ServiceError(400, "invalid_request", "Valid Idempotency-Key header is required")
            result = lifecycle.initiate(owner, _upload_command(_body(event)), key)
            replay = result.pop("replay")
            return _response(200 if replay else 201, result)
        if path == "/images" and method == "GET":
            return _response(200, _list(params))
        match = re.fullmatch(r"/images/([^/]+)(?:/(complete|status|content))?", path)
        if not match:
            raise lifecycle.ServiceError(404, "not_found", "Route not found")
        try:
            image_id = str(uuid.UUID(match[1]))
        except ValueError as exc:
            raise lifecycle.ServiceError(400, "invalid_request", "Invalid image ID") from exc
        suffix = match[2]
        if method == "POST" and suffix == "complete":
            code, body = lifecycle.complete(_identity(event, headers), image_id)
            return _response(code, body)
        if method == "GET" and suffix == "status":
            return _response(200, lifecycle.status(_identity(event, headers), image_id))
        if method == "DELETE" and suffix is None:
            code, body = lifecycle.delete(_identity(event, headers), image_id)
            return _response(code, body)
        if method == "GET" and suffix is None:
            return _response(200, lifecycle.public_image(image_id))
        if method == "GET" and suffix == "content":
            disposition = params.get("disposition", "inline")
            if disposition not in {"inline", "attachment"} or set(params) - {"disposition"}:
                raise lifecycle.ServiceError(400, "invalid_request", "Invalid disposition")
            return _response(302, headers={"Location": lifecycle.content(image_id, disposition)})
        raise lifecycle.ServiceError(404, "not_found", "Route not found")
    except lifecycle.ServiceError as exc:
        return _error(exc.status, exc.code, exc.message, request_id)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        lifecycle.log_event("http", "dependency_error", request_id=request_id, error_code=code)
        if code in {"ProvisionedThroughputExceededException", "ThrottlingException", "TooManyRequestsException"}:
            return _error(429, "throttled", "Service is busy; retry", request_id)
        return _error(503, "dependency_unavailable", "Service is temporarily unavailable", request_id)
    except UnicodeError:
        return _error(400, "invalid_request", "Text must contain valid Unicode", request_id)
    except Exception as exc:
        lifecycle.log_event("http", "error", request_id=request_id, error_type=type(exc).__name__)
        return _error(503, "dependency_unavailable", "Service is temporarily unavailable", request_id)
