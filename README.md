# MontyCloud image service

A Python service for original-image uploads, public browsing, downloads, and owner-authorized deletion. It uses API Gateway, Lambda, private S3 storage, and DynamoDB. An EventBridge scheduled rule triggers recovery of unfinished work every five minutes.

The image bytes go directly from the client to S3. Lambda validates one original at a time and publishes its metadata only after validation succeeds. All published images are public. Ownership controls upload completion, status, and deletion.

## Run locally

Prerequisites: Python 3.13 and Docker with Compose. Docker must be running and able to obtain the LocalStack and Lambda runtime images. The example shell commands also use `curl`.

```sh
make setup
make up
make compatibility
make bootstrap
make integration
make recovery
make limits
```

LocalStack is pinned to `4.14.0`. The local AWS endpoint is `http://localhost:4567`; port 4566 is left available for other projects. `make bootstrap` packages Linux dependencies, creates the AWS resources, and writes their names and the API base URL to `.local/api.json`. Repeating bootstrap updates the same resources and preserves the local cursor-signing secret.

Only the local endpoint is supported by the bootstrap and verification scripts. They explicitly use test credentials and reject external endpoints. They do not deploy to an AWS account.

The compatibility check verifies actual signed PUT requests, replay protection, placeholder races, Lambda invocation, and a scheduled invocation. Signature checks are enabled with `S3_SKIP_SIGNATURE_VALIDATION=0`. The script reports failures and unverified behavior rather than counting them as passes. See [compatibility evidence](docs/compatibility.md).

`make recovery` creates three local crash fixtures and waits for the actual five-minute schedule. It does not invoke the worker or recovery function manually. Allow up to seven minutes. `make limits` exercises real files at the 20 MiB and 25-million-pixel boundaries.

Run fast checks separately:

```sh
make lint
make test
```

`make down` stops this project's LocalStack container. It does not stop other projects or remove the Docker volume. Treat local emulated resources as disposable: persistence across restarts is not promised by this LocalStack configuration. Rerun bootstrap after restarting it. For an intentionally fresh environment, use `docker compose down --volumes` followed by the startup commands; this removes this project's local volume and data.

## Upload an image

The local API accepts `X-User-Id` as an explicit development identity. Use different values to demonstrate ownership. This is not a production authentication mechanism. Production must supply verified identity through the API Gateway authorizer and must not enable `APP_ENV=local`.

Prepare a sample image and the request:

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from PIL import Image
Image.new('RGB', (32, 24), (30, 130, 180)).save('.local/sample.png')
Path('.local/upload-request.json').write_text(json.dumps({
    'filename': 'sample.png',
    'content_type': 'image/png',
    'size_bytes': Path('.local/sample.png').stat().st_size,
    'caption': 'Local demonstration',
}))
PY
API=$(.venv/bin/python -c 'import json; print(json.load(open(".local/api.json"))["api_url"])')
REQUEST_KEY=$(.venv/bin/python -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body -sS "$API/images" \
  -H 'X-User-Id: alice' -H "Idempotency-Key: $REQUEST_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @.local/upload-request.json > .local/upload.json
```

Send the file with the returned URL and exact headers. Do not log or share the URL; it grants temporary upload permission.

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
import requests
upload = json.loads(Path('.local/upload.json').read_text())
response = requests.put(upload['upload_url'], headers=upload['upload_headers'],
                        data=Path('.local/sample.png').read_bytes(), timeout=30)
assert response.status_code == 200, response.status_code
print('Uploaded bytes to S3')
PY
IMAGE_ID=$(.venv/bin/python -c 'import json; print(json.load(open(".local/upload.json"))["image_id"])')
curl --fail-with-body -sS -X POST "$API/images/$IMAGE_ID/complete" -H 'X-User-Id: alice'
curl --fail-with-body -sS "$API/images/$IMAGE_ID/status" -H 'X-User-Id: alice'
```

Completion returns `202` while validation runs. Poll status until it is `ready`, `rejected`, or `failed`. Retry initiation with the same key and identical input after a lost response; it returns the original image and deadlines. A deliberately new upload needs a new key. Reusing the URL after a successful PUT fails because the object already exists.

## Browse, download, and delete

After the image is ready, no identity header is required to read it:

```sh
curl --fail-with-body -sS "$API/images?owner_id=alice&limit=25"
curl --fail-with-body -sS "$API/images?owner_id=alice&date_from=2026-10-01&date_to=2026-11-01"
curl --fail-with-body -sS "$API/images/$IMAGE_ID"
curl --fail-with-body -sS -L "$API/images/$IMAGE_ID/content?disposition=attachment" -o .local/download.png
curl --fail-with-body -sS -X DELETE "$API/images/$IMAGE_ID" -H 'X-User-Id: alice'
curl --fail-with-body -sS "$API/images/$IMAGE_ID/status" -H 'X-User-Id: alice'
```

The date filter uses the time validation completed, with an inclusive start and exclusive end. Results are newest first. Preserve the filters and page size when sending `next_cursor` back as `cursor`. Continue until it is null, including after an empty page with a cursor. Index propagation is eventual, and pagination is not a frozen snapshot during concurrent changes.

Deletion returns `202` and immediately makes the image ineligible for public reads. Poll until `deleted`; repeating deletion then returns `204`. Completed deletion means the original bytes have been replaced by an empty object. A small ownership/status record and the empty S3 object remain so an old conditional upload cannot recreate the content. An already issued download may finish before replacement, and existing downloaded copies cannot be recalled.

The [OpenAPI specification](docs/openapi.yaml) defines the request and response shapes. Errors contain `code`, `message`, and `request_id`.

## Design decisions

- `Images` contains metadata and lifecycle state. `UploadRequests` maps an owner's idempotency key to the image it created. One transaction writes both records or neither.
- `Images` has two sparse gallery indexes for global and owner/date browsing. A third index orders unfinished work by its next check time. Request records expire after at least 24 hours; image deletion records do not expire.
- The image record is the durable work request. If a Lambda invocation is missed after completion is accepted, recovery can find and resubmit it. There is no SQS queue.
- Conditional writes and worker tokens prevent duplicate processing from publishing an image after deletion. An expired worker cannot publish over a newer attempt.
- Each worker attempt has a 120-second timeout and a 180-second ownership lease. Validation permits at most three claimed attempts within 30 minutes. Known invalid files are rejected immediately. Unexplained crashes get bounded retries. Cleanup keeps retrying with backoff because failed validation must not abandon the stored content.
- S3 is private, encrypted, and unversioned. Clients receive a signed conditional PUT for a unique key. Only the cleanup worker can overwrite that key with an empty object.

Limits are one nonanimated JPEG, PNG, or WebP, at most 20 MiB and 25 million pixels. Upload permission lasts 15 minutes, and completion must be requested within 30 minutes of initiation. Download URLs last 60 seconds. The originals are preserved without transformations or metadata stripping.

The global gallery and maintenance indexes each use a fixed partition value. This is a deliberate assignment tradeoff; sustained high traffic needs measurement and potentially sharding. LocalStack cannot establish production AWS capacity or complete IAM enforcement. In our check, an unsigned direct S3 GET returned the object despite the private-bucket configuration. The emulator must remain local; this result does not establish AWS bucket authorization. Signed PUT header enforcement and application ownership checks are verified separately. The limit checks exercise real files at the byte and pixel boundaries. They do not establish peak memory or throughput for every supported image.

There is no frontend, login system, private-image mode, tag search, metadata editing, thumbnail generation, or image replacement. See [the approved plan](docs/plan.md) for the full scope and failure contract.

## Review demonstration

1. Start from the documented local setup and run the compatibility checks.
2. Upload a sample, repeat its initiation request, and show the same image ID.
3. Complete validation, then list it anonymously and download identical bytes.
4. Show that a different user cannot read owner-only status or delete it.
5. Delete it and show that the old PUT cannot recreate the image.
6. Run the integration and recovery checks and explain their recorded results and limitations.

See [verification results](docs/verification.md) for the measured checks and remaining limits.

CI runs static checks and unit tests. The local integration scripts exercise the deployed API and emulated AWS services separately; a unit-test pass does not imply the deployment checks passed.
