# LocalStack compatibility gate

Run at 2026-10-04T05:41:51.141899+00:00 against `http://localhost:4567` with LocalStack 4.14.0.
Resource prefix: `mc-compat-f8f1a2e5c998`. The script uses dummy local credentials and refuses nonlocal endpoints.
The isolated compose must set `S3_SKIP_SIGNATURE_VALIDATION=0`;
LocalStack otherwise accepts changed and omitted signed headers.

Overall required protocol result: **PASS**. Unsigned GET access control remains an informational emulator limit.

| Check | Result | Observation | Time (ms) |
| --- | --- | --- | ---: |
| Private bucket setup | PASS | created mc-compat-f8f1a2e5c998 | 255 |
| Bucket configuration | PASS | SSE=AES256; public block=True; versioning=off | 9 |
| Presigned required headers | PASS | signed headers: content-length, content-type, host, if-none-match | 2 |
| Conditional first PUT and retry | PASS | first HTTP 200; retry HTTP 412; original retained=True | 64 |
| Unsigned GET access control | UNVERIFIED | unsigned GET HTTP 200 | 1 |
| Altered content type | PASS | HTTP 403; object created=False | 10 |
| Omitted condition header | PASS | HTTP 403; object created=False | 5 |
| Omitted content type | PASS | HTTP 403; object created=False | 4 |
| Changed content length | PASS | HTTP 403; object created=False | 4 |
| Oversized body | PASS | HTTP 403; object created=False | 178 |
| Chunked transfer | PASS | HTTP 403; object created=False | 6 |
| Placeholder prevents replay | PASS | replay HTTP 412; stored bytes=0 | 10 |
| Concurrent upload and placeholder | PASS | upload HTTP 200; final stored bytes=0 | 9 |
| Lambda async configuration | PASS | retries=0; max age=300s | 0 |
| Lambda asynchronous execution | PASS | invoke HTTP 202; marker seen=True; delivery wait=2020 ms | 22626 |
| EventBridge minute-rule delivery | PASS | marker seen=True; delivery wait=60386 ms | 60521 |

The S3 probes send actual HTTP PUT requests through a presigned URL.
The Lambda and EventBridge probes require an S3 marker written by executed function code,
so an accepted invocation alone cannot pass.
A schedule result is unverified when its target Lambda cannot become active.
Results reflect this LocalStack instance only. IAM policy enforcement and signature fidelity
require separate AWS verification before production use.

On the first run with LocalStack's default signature validation setting, the emulator accepted
altered and omitted signed headers, changed lengths, oversized bodies, and chunked transfer.
After the isolated compose enabled `S3_SKIP_SIGNATURE_VALIDATION=0`, each of those requests
returned HTTP 403 without creating an object. This configuration is required for the local upload gate.

The initial EventBridge Scheduler probe used a one-time schedule with a valid IAM role and
waited past the full one-minute delivery window. Its API call succeeded but no Lambda marker
appeared. The installed LocalStack 4.14.0 Scheduler provider delegates creation to Moto's
in-memory schedule store, and the installed provider has no delivery worker.
The required recovery trigger therefore uses a classic EventBridge scheduled rule.
Its probe registers a rule with Lambda permission, waits for a real minute tick,
checks the function's marker, and removes the rule afterward.

The unsigned GET probe checks access control with no Authorization header or query signature.
LocalStack's bucket configuration reads can pass while unauthenticated object access still succeeds;
that observation is marked UNVERIFIED for production access control.

Run again with `python scripts/compatibility.py --endpoint http://localhost:4567`.
Every run creates fresh resources with a unique `mc-compat` prefix;
no existing resources are deleted or changed.
