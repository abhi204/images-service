"""Exercise actual byte and pixel boundaries through the deployed local service."""

from __future__ import annotations

import io
import json
import struct
import time
import uuid
import zlib
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 20 * 1024 * 1024


def png(width, height):
    output = io.BytesIO()
    with Image.new("RGB", (width, height), (11, 91, 151)) as image:
        image.save(output, "PNG")
    return output.getvalue()


def padded_png(size):
    original = png(32, 24)
    payload = b"\x00" * (size - len(original) - 12)
    chunk_type = b"npAD"
    chunk = struct.pack(">I", len(payload)) + chunk_type + payload
    chunk += struct.pack(">I", zlib.crc32(chunk_type + payload))
    return original[:-12] + chunk + original[-12:]


def main():
    config = json.loads((ROOT / ".local/api.json").read_text())
    endpoint = urlparse(config["api_url"])
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Only the explicit localhost service is allowed")
    owner = "limits-" + uuid.uuid4().hex[:10]
    results = []
    cases = (
        ("exactly 20 MiB", lambda: padded_png(LIMIT), "ready"),
        ("exactly 25 million pixels", lambda: png(5000, 5000), "ready"),
        ("over 25 million pixels", lambda: png(5001, 5000), "rejected"),
    )
    for label, fixture, expected in cases:
        raw = fixture()
        start = time.monotonic()
        response = requests.post(
            config["api_url"] + "/images",
            headers={
                "X-User-Id": owner,
                "Idempotency-Key": uuid.uuid4().hex,
            },
            json={"filename": "limit.png", "content_type": "image/png", "size_bytes": len(raw)},
            timeout=35,
        )
        assert response.status_code == 201, response.status_code
        upload = response.json()
        response = requests.put(
            upload["upload_url"], headers=upload["upload_headers"], data=raw, timeout=40
        )
        assert response.status_code == 200, response.status_code
        image_id = upload["image_id"]
        path = config["api_url"] + "/images/" + image_id
        response = requests.post(path + "/complete", headers={"X-User-Id": owner}, timeout=35)
        assert response.status_code == 202, response.status_code
        outcome = None
        while time.monotonic() - start < 150:
            response = requests.get(path + "/status", headers={"X-User-Id": owner}, timeout=35)
            assert response.status_code == 200, response.status_code
            outcome = response.json()["status"]
            if outcome in {"ready", "rejected", "failed", "expired"}:
                break
            time.sleep(1)
        assert outcome == expected, f"{label}: expected {expected}, got {outcome}"
        result = {
            "case": label,
            "size_bytes": len(raw),
            "outcome": outcome,
            "end_to_end_seconds": round(time.monotonic() - start, 3),
        }
        results.append(result)
        print(json.dumps(result), flush=True)
        response = requests.delete(path, headers={"X-User-Id": owner}, timeout=35)
        assert response.status_code in {202, 204}, response.status_code
    (ROOT / ".local/limits-result.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
