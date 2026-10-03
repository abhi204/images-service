# LocalStack compatibility gate

Run at 2026-10-03T03:01:01.824271+00:00 against `http://localhost:4567` with LocalStack 4.14.0.
Resource prefix: `mc-compat-b342b0485b13`. The script uses dummy local credentials and refuses nonlocal endpoints.
The isolated compose must set `S3_SKIP_SIGNATURE_VALIDATION=0`;
LocalStack otherwise accepts changed and omitted signed headers.

Overall required protocol result: **PASS**. Unsigned GET access control remains an informational emulator limit.

| Check | Result | Observation | Time (ms) |
| --- | --- | --- | ---: |
| Private bucket setup | PASS | created mc-compat-b342b0485b13 | 167 |
| Bucket configuration | PASS | SSE=AES256; public block=True; versioning=off | 6 |
| Presigned required headers | PASS | signed headers: content-length, content-type, host, if-none-match | 1 |
| Conditional first PUT and retry | PASS | first HTTP 200; retry HTTP 412; original retained=True | 38 |
| Unsigned GET access control | UNVERIFIED | unsigned GET HTTP 200 | 2 |
| Altered content type | PASS | HTTP 403; object created=False | 7 |
| Omitted condition header | PASS | HTTP 403; object created=False | 3 |
| Omitted content type | PASS | HTTP 403; object created=False | 3 |
| Changed content length | PASS | HTTP 403; object created=False | 3 |
| Oversized body | PASS | HTTP 403; object created=False | 151 |
| Chunked transfer | PASS | HTTP 403; object created=False | 5 |
| Placeholder prevents replay | PASS | replay HTTP 412; stored bytes=0 | 5 |
| Concurrent upload and placeholder | PASS | upload HTTP 412; final stored bytes=0 | 7 |
| Lambda async configuration | PASS | retries=0; max age=300s | 0 |
| Lambda asynchronous execution | PASS | invoke HTTP 202; marker seen=True; delivery wait=2016 ms | 4384 |
| EventBridge minute-rule delivery | PASS | marker seen=True; delivery wait=60399 ms | 60526 |

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
