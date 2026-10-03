"""Exercise the deployed local API and verify stored bytes and access boundaries."""

from __future__ import annotations

import argparse
import io
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def run(config_path: Path) -> None:
    config = json.loads(config_path.read_text())
    for value in (config["endpoint_url"], config["api_url"]):
        url = urlparse(value)
        if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("Integration checks require an explicit localhost HTTP endpoint")
    base = config["api_url"].rstrip("/")
    run_id = uuid.uuid4().hex[:12]
    alice, bob = f"alice-{run_id}", f"bob-{run_id}"
    checks = []

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print(f"PASS {name}", flush=True)

    def api(method, path, *, owner=None, expected=200, key=None, **kwargs):
        headers = {}
        if owner:
            headers["X-User-Id"] = owner
        if key:
            headers["Idempotency-Key"] = key
        response = requests.request(
            method, base + path, headers=headers, timeout=35, allow_redirects=False, **kwargs
        )
        if response.status_code != expected:
            raise AssertionError(
                f"{method} {path}: expected {expected}, got {response.status_code}"
            )
        return response

    def data(format_name="PNG"):
        stream = io.BytesIO()
        Image.new("RGB", (19, 13), (16, 120, 200)).save(stream, format=format_name)
        return stream.getvalue()

    def initiate(owner, raw, key=None, content_type="image/png", filename="sample.png"):
        command = {
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(raw),
            "caption": "Integration fixture",
        }
        result = api(
            "POST", "/images", owner=owner, expected=201, key=key or uuid.uuid4().hex, json=command
        ).json()
        return result, command

    def put(upload, raw, expected=200):
        response = requests.put(
            upload["upload_url"], data=raw, headers=upload["upload_headers"], timeout=30
        )
        check(response.status_code == expected, f"S3 PUT returned {expected}")

    def wait_status(owner, image_id, wanted, timeout=150):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = api("GET", f"/images/{image_id}/status", owner=owner).json()
            if result["status"] == wanted:
                return result
            if result["status"] in {"failed", "expired", "deleted", "rejected"}:
                raise AssertionError(f"Expected {wanted}, got {result}")
            time.sleep(1)
        raise TimeoutError(f"Image did not reach {wanted} in {timeout}s")

    raw = data()
    api("POST", "/images", expected=401, key="unauthenticated", json={})
    check(True, "anonymous upload requires identity")
    request_key = uuid.uuid4().hex
    upload, command = initiate(alice, raw, request_key)
    image_id = upload["image_id"]
    replay = api("POST", "/images", owner=alice, key=request_key, json=command).json()
    check(replay["image_id"] == image_id, "initiation retry returns original image")
    check(
        replay["upload_expires_at"] == upload["upload_expires_at"],
        "retry does not extend the upload deadline",
    )
    api(
        "POST",
        "/images",
        owner=alice,
        key=request_key,
        expected=409,
        json={**command, "caption": "Different input"},
    )
    check(True, "same request key with changed input conflicts")
    api("GET", f"/images/{image_id}/status", owner=bob, expected=404)
    api("DELETE", f"/images/{image_id}", owner=bob, expected=404)
    api("POST", f"/images/{image_id}/complete", owner=bob, expected=404)
    api("GET", f"/images/{image_id}", expected=404)
    api("POST", f"/images/{image_id}/complete", owner=alice, expected=409)
    check(True, "ownership, unpublished reads, and missing-upload completion are enforced")
    put(upload, raw)
    put(upload, raw, expected=412)
    api("POST", f"/images/{image_id}/complete", owner=alice, expected=202)
    wait_status(alice, image_id, "ready")
    api("POST", f"/images/{image_id}/complete", owner=alice)
    public = api("GET", f"/images/{image_id}").json()
    check(
        public["owner_id"] == alice and public["width"] == 19 and public["height"] == 13,
        "validated metadata is publicly readable",
    )
    check(
        "s3_key" not in public and "upload_url" not in public,
        "public metadata excludes storage credentials and internal keys",
    )
    redirect = api("GET", f"/images/{image_id}/content?disposition=attachment", expected=302)
    download = requests.get(redirect.headers["Location"], timeout=30)
    check(
        download.status_code == 200 and download.content == raw,
        "download preserves the original bytes",
    )

    def upload_format(owner, format_name, media_type, filename):
        content = data(format_name)
        initiated, _ = initiate(owner, content, content_type=media_type, filename=filename)
        put(initiated, content)
        api("POST", f"/images/{initiated['image_id']}/complete", owner=owner, expected=202)
        wait_status(owner, initiated["image_id"], "ready")
        return initiated["image_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        alice_job = pool.submit(upload_format, alice, "JPEG", "image/jpeg", "photo.jpg")
        bob_job = pool.submit(upload_format, bob, "WEBP", "image/webp", "photo.webp")
        second_alice, bob_image = alice_job.result(), bob_job.result()
    check(True, "multiple owners can upload JPEG and WebP concurrently")
    end = time.monotonic() + 25
    while True:
        page = api("GET", "/images", params={"owner_id": alice, "limit": 1}).json()
        if page["items"] and page.get("next_cursor"):
            break
        if time.monotonic() >= end:
            raise AssertionError("Gallery index did not expose both images")
        time.sleep(1)
    first_id = page["items"][0]["image_id"]
    second = api(
        "GET", "/images", params={"owner_id": alice, "limit": 1, "cursor": page["next_cursor"]}
    ).json()
    check(
        {first_id, second["items"][0]["image_id"]} == {image_id, second_alice},
        "owner-filtered pagination returns each image without duplication",
    )
    api(
        "GET",
        "/images",
        expected=400,
        params={"owner_id": bob, "limit": 1, "cursor": page["next_cursor"]},
    )
    api("GET", "/images", expected=400, params={"cursor": "tampered"})
    check(True, "cursors reject tampering and changed filters")
    recent = api(
        "GET",
        "/images",
        params={"owner_id": alice, "date_from": "2000-01-01", "date_to": "2100-01-01"},
    ).json()
    check(
        {item["image_id"] for item in recent["items"]} == {image_id, second_alice},
        "owner and date filters combine",
    )
    empty = api("GET", "/images", params={"owner_id": alice, "date_to": "2000-01-01"}).json()
    check(empty["items"] == [], "date range excludes newer images")

    bad, _ = initiate(alice, b"not an image")
    put(bad, b"not an image")
    api("POST", f"/images/{bad['image_id']}/complete", owner=alice, expected=202)
    wait_status(alice, bad["image_id"], "rejected")
    api("GET", f"/images/{bad['image_id']}", expected=404)
    check(True, "invalid contents are rejected and never published")

    pending, _ = initiate(alice, raw)
    api("DELETE", f"/images/{pending['image_id']}", owner=alice, expected=202)
    wait_status(alice, pending["image_id"], "deleted")
    put(pending, raw, expected=412)
    check(True, "deleting an unused upload prevents later URL reuse")
    api("DELETE", f"/images/{image_id}", owner=alice, expected=202)
    api("GET", f"/images/{image_id}", expected=404)
    api("GET", f"/images/{image_id}/content", expected=404)
    wait_status(alice, image_id, "deleted")
    api("DELETE", f"/images/{image_id}", owner=alice, expected=204)
    put(upload, raw, expected=412)
    check(True, "deletion hides the image, completes, and is repeatable")

    aws = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
    )
    ddb = aws.resource("dynamodb", endpoint_url=config["endpoint_url"])
    record = ddb.Table(config["image_table"]).get_item(
        Key={"image_id": image_id}, ConsistentRead=True
    )["Item"]
    check(
        record["status"] == "deleted" and "caption" not in record and "filename" not in record,
        "deleted record retains status without original descriptive metadata",
    )
    result = {
        "checks": checks,
        "count": len(checks),
        "run_id": run_id,
        "retained_fixtures": [second_alice, bob_image, bad["image_id"], pending["image_id"]],
    }
    out = ROOT / ".local/integration-result.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Completed {len(checks)} checks; results in .local/integration-result.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".local/api.json")
    run(parser.parse_args().config)
