"""S3 permissions and original-image validation."""

from __future__ import annotations

import io
import os
import warnings
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from PIL import Image, UnidentifiedImageError

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 25_000_000
MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class InvalidImage(Exception):
    pass


@dataclass(frozen=True)
class ImageFacts:
    size: int
    width: int
    height: int
    format: str


def _s3(public: bool = False):
    endpoint = os.getenv("S3_PUBLIC_ENDPOINT" if public else "AWS_ENDPOINT_URL")
    return boto3.client("s3", endpoint_url=endpoint or None, config=Config(signature_version="s3v4"))


def upload_permission(bucket: str, key: str, content_type: str, size: int, expires: int) -> dict:
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(size),
        "If-None-Match": "*",
    }
    url = _s3(public=True).generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size,
            "IfNoneMatch": "*",
        },
        ExpiresIn=expires,
        HttpMethod="PUT",
    )
    return {"url": url, "headers": headers}


def download_permission(bucket: str, key: str, filename: str, disposition: str) -> str:
    safe = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;\r\n' else "_" for c in filename)
    safe = safe or "image"
    return _s3(public=True).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": f'{disposition}; filename="{safe}"',
        },
        ExpiresIn=60,
        HttpMethod="GET",
    )


def object_exists(bucket: str, key: str) -> bool:
    try:
        _s3().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def read_original(bucket: str, key: str) -> bytes:
    response = _s3().get_object(Bucket=bucket, Key=key)
    stream = response["Body"]
    try:
        data = stream.read(MAX_BYTES + 1)
    finally:
        stream.close()
    if len(data) > MAX_BYTES:
        raise InvalidImage("Image exceeds the 20 MiB limit")
    if len(data) < int(response["ContentLength"]):
        raise OSError("Storage download ended before the object was complete")
    if not data:
        raise InvalidImage("Image is empty")
    return data


def validate_original(data: bytes, declared_type: str) -> ImageFacts:
    if not data or len(data) > MAX_BYTES:
        raise InvalidImage("Image size is outside the allowed range")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                detected = image.format
                if detected not in MEDIA_TYPES or MEDIA_TYPES[detected] != declared_type:
                    raise InvalidImage("Image format differs from the declared content type")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                    raise InvalidImage("Animated images are unsupported")
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise InvalidImage("Image exceeds the pixel limit")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                if image.size != (width, height) or image.format != detected:
                    raise InvalidImage("Image changed during decoding")
    except (
        UnidentifiedImageError, OSError, ValueError, SyntaxError,
        Image.DecompressionBombError, Image.DecompressionBombWarning,
    ) as exc:
        raise InvalidImage("Image is corrupt or unsupported") from exc
    return ImageFacts(len(data), width, height, detected)


def write_placeholder(bucket: str, key: str) -> None:
    _s3().put_object(Bucket=bucket, Key=key, Body=b"", ContentType="application/octet-stream")
