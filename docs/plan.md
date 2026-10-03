# MontyCloud image service plan

This document records the scope, API contract, and design decisions used to build the image service. Measured results for the implementation are in [verification results](verification.md).

The service lets authenticated users upload original images, makes validated images publicly browsable, and lets owners delete them. Python, API Gateway, Lambda, S3, DynamoDB, and LocalStack satisfy the assignment's requested stack. An EventBridge scheduled rule supplies automatic recovery every five minutes. The compatibility test found that LocalStack 4.14.0 stores EventBridge Scheduler schedules without executing them, so the implementation uses scheduled rules for the same recovery behavior.

## Scope and limits

All ready images are public. Anonymous users can list images, read their public metadata, view them, and download them. Upload initiation requires a trusted user identity. Completion, status, and deletion are owner-only operations. Authentication comes from the surrounding platform; only the local development environment simulates identities. A client-supplied owner ID never establishes ownership.

Accept one JPEG, PNG, or WebP per upload. Preserve its original bytes. Reject animation, unsupported or malformed files, files exceeding 20 MiB, and images exceeding 25 million pixels. Verify contents rather than trusting a filename or declared media type.

Metadata includes the image ID, owner ID, caption, filename, timestamps, file properties, and internal lifecycle information. The two search filters are exact owner ID and the date the image became ready. Either filter can be omitted or combined with the other. Results are newest first and paginated.

Tags, tag discovery, private images, metadata editing, file replacement, a frontend, account registration, thumbnails, image transformations, and moderation are outside scope. User-name search and a user directory are also excluded.

The following defaults were agreed:

| Setting | Value |
| --- | --- |
| Maximum file size | 20 MiB, exactly 20,971,520 bytes |
| Maximum decoded dimensions | Width multiplied by height at most 25,000,000 pixels |
| Upload URL validity | 15 minutes from initiation |
| Completion deadline | 30 minutes from initiation |
| Initiation retry guarantee | At least 24 hours |
| Download URL validity | 60 seconds |
| Automatic recovery interval | Every five minutes |

A request that starts before URL expiration can continue afterward. Completion still has its own deadline. Accepted processing does not become an abandoned upload when the initiation deadline passes. See [S3 URL expiration behavior](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html).

## Client flow and API

1. The client requests an upload with filename, content type, declared size, caption, and an idempotency key.
2. The API creates the upload and returns its image ID, fixed deadlines, and a presigned S3 PUT URL with required headers.
3. The client sends the original bytes directly to S3.
4. The client requests completion. The API accepts durable validation work and returns a processing status.
5. A background worker validates the uploaded file. The owner polls status until it is ready, rejected, or failed.
6. Anyone can browse ready images and obtain a short-lived download redirect.
7. An owner can cancel or delete their image at any stage. Deletion returns 202, and status eventually reports deleted after content removal succeeds.

The HTTP contract is:

| Endpoint | Access | Response |
| --- | --- | --- |
| `POST /images` | Authenticated | 201 for a new upload; 200 for an idempotent replay with current status |
| `POST /images/{id}/complete` | Owner | 202 while processing; 200 when already ready |
| `GET /images/{id}/status` | Owner | 200 with lifecycle status and a safe error reason when relevant |
| `GET /images` | Public | 200 with ready items and an optional continuation cursor |
| `GET /images/{id}` | Public | 200 with ready metadata; otherwise 404 |
| `GET /images/{id}/content` | Public | 302 to a short-lived S3 URL after checking eligibility |
| `DELETE /images/{id}` | Owner | 202 while deletion is pending; 204 when already deleted |

`GET /images` accepts `owner_id`, `date_from`, `date_to`, `limit`, and `cursor`. Use an inclusive UTC start and exclusive UTC end. A September search uses September 1 as the start and October 1 as the end. The content endpoint accepts `disposition=inline` or `attachment`; default to inline.

Routine defaults: optional caption up to 2,000 characters; filename up to 255 UTF-8 bytes; idempotency key of 1–128 visible ASCII characters; page size 25 by default and 100 maximum. Bound request bodies and cursor sizes. A signed cursor expires after one hour and binds the filters, ordering, page size, and database position. Clients treat it as opaque.

Return errors as `{code, message, request_id}`. Use 400 for invalid request data, 401 for missing required identity, 404 for missing or inaccessible owner resources, 409 for incompatible state or idempotency input, 410 for an expired upload's completion request, 429 for throttling, and 503 for unresolved temporary dependency failures. Other terminal completion requests return 409 with the owner's current status. Status polling returns a normal 200 response for rejected or failed work. Asynchronous validation rejection is a status outcome, not a retroactive HTTP error.

Public responses exclude S3 keys, presigned PUT URLs, internal work fields, and internal exception details. Initiation replay returns the same image and original deadlines, not new permission lasting another 15 minutes. After expiry, it returns the existing status without usable upload permission. A deliberate new upload requires a new idempotency key.

## Components and module boundaries

Use three deployed Lambda entrypoints:

- The API Lambda adapts HTTP requests and invokes lifecycle operations.
- The worker Lambda handles validation and content cleanup. API and recovery code invoke it asynchronously with an image ID and operation, never a client-selected S3 key.
- The recovery Lambda finds due work every five minutes and safely resubmits it or transitions exhausted work to failure.

An EventBridge scheduled rule supplies the recurring trigger. S3 stores the originals and retained empty placeholders. DynamoDB stores authoritative state. There is no SQS queue or per-image schedule.

Prefer Python 3.13, boto3, Pillow, and small Lambda handlers. Pin dependencies after the compatibility checks. Separate code by responsibility, with three substantive modules and thin entrypoints:

| Module responsibility | Operations and types it owns |
| --- | --- |
| HTTP boundary | Identity adaptation, request parsing, responses, and cursor encoding |
| Image lifecycle | Image states, initiation, completion, deletion, publication, work claims, recovery, and DynamoDB transactions |
| Image storage | Presigned requests, bounded S3 reads, decoded-image validation, and empty-placeholder writes |

The conceptual lifecycle API consists of `initiate(owner, command, key)`, `complete(owner, image_id)`, `status(owner, image_id)`, `delete(owner, image_id)`, `list_ready(query, cursor)`, `public_image(image_id)`, `run_work(image_id, operation)`, and `recover(now, budget)`. These are interface sketches, not code scaffolding. Keep AWS response dictionaries and HTTP event shapes at their boundaries.

## Storage and browsing

Use two DynamoDB tables, each with a string primary key:

| Table | Primary key | What each record stores |
| --- | --- | --- |
| `Images` | `image_id`, a server-generated UUID | Owner, metadata, lifecycle status, S3 location, and unfinished work |
| `UploadRequests` | `request_key`, a hash of an unambiguous encoding of owner ID plus client idempotency key | The image ID created by that request, its input fingerprint, fixed upload and completion deadlines, and the request record's expiry time |

For example, an `UploadRequests` record remembers that Alice's request `request789` created image `abc123`. The `Images` record with ID `abc123` describes that image. Here `abc123` is a shortened example; actual image IDs are UUIDs. Every upload has an immutable owner and a unique S3 key that is never reused.

Separate tables make the two responsibilities easier to inspect, debug, and explain. Our access patterns do not gain a query-performance benefit from combining these records. The tradeoff is one additional table to configure and manage. Record-type prefixes such as `IMAGE#` and `IDEMP#` are unnecessary because the table names distinguish the record types.

Create the `Images` record and its `UploadRequests` mapping in one conditional DynamoDB transaction across both tables. Both tables must be in the same AWS account and Region. Either both records are saved or neither is. Store a canonical fingerprint of initiation input in `UploadRequests`. A concurrent or repeated request reads the winning mapping consistently: matching input returns the same image; conflicting input returns 409. Do not rely solely on the SDK's shorter transaction retry-token window for the 24-hour application contract. [DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html)

Retain mappings in `UploadRequests` for at least 24 hours. Enable time-to-live expiration, or TTL, on that table so expired mappings can be removed afterward. Its deletion timing does not control API deadlines. If a mapping still exists after 24 hours, replay it rather than replacing it. Once retention ends and the mapping is gone, the same key might create a new upload. Clients must use new keys for deliberate new actions. Keep TTL disabled on `Images` so retained deletion records remain available. Empty S3 placeholders also remain stored.

Keep all three sparse indexes on `Images`. `UploadRequests` needs no secondary indexes because requests are looked up directly by their primary key.

| Index | Partition value | Ordering | Purpose |
| --- | --- | --- | --- |
| Global gallery | `PUBLIC` | `ready_at#image_id` | All ready images, optionally within a date range |
| Owner gallery | Owner ID | `ready_at#image_id` | One owner's ready images, optionally within a date range |
| Maintenance | `MAINT` | `next_check_at#image_id` | Only records with unfinished work that is due for checking |

The maintenance index is additional to the two browsing indexes already discussed. It avoids scanning all completed images on each recovery run. Do not shard these indexes initially; document the concentrated traffic of the global and maintenance keys.

Add browsing keys only when an image becomes ready. Remove them in the same state update that begins deletion. Keep maintenance keys only while validation, expiration handling, or cleanup needs attention. DynamoDB indexes only items that have their index-key attributes. [Sparse indexes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-indexes-general-sparse-indexes.html)

GSI results are candidates, not authority. Re-read candidate image records consistently before exposing public metadata or issuing new download links. Bound list work to 500 index candidates per response and carry a continuation cursor even if deletion filtering produces a short or empty page. Advance the cursor past evaluated candidates without skipping unprocessed results. Preserve index order after any batch reads.

Pagination is not a frozen snapshot. Concurrent publication, deletion, and index propagation can change subsequent pages. A quiescent dataset must paginate without duplicates or omissions; do not promise that same property across all concurrent changes. GSI propagation is asynchronous. [GSI consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html)

## Upload validation and write protection

Use a private, encrypted, non-versioned S3 bucket with public access blocked. Disabling versioning is necessary for our chosen deletion semantics: replacing an object must not retain old image versions.

Presigned PUTs must bind the immutable key, exact declared content length, content type, and `If-None-Match: *`. Reject declared sizes outside 1–20,971,520 bytes before issuing permission. Enforce conditional client writes through the signing role and bucket policy. The worker's separate role may write empty placeholders unconditionally but must never grant that permission to clients.

The exact signed-length behavior is a compatibility gate. Its measured local results are recorded with the implementation. Check altered lengths, omitted headers, chunked requests, and oversized bodies. Verify the generated signature actually includes the required headers. S3 documents both Content-Length and conditional PUT, but that alone does not verify the selected SDK/client/emulator combination. [S3 PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)

The worker also checks actual size and media type, reads at most the file limit plus one byte, rejects excess pixels before full decoding, verifies the file, and fully decodes the image. Reject corrupt data, animation, and a declared format that differs from detected contents. Discard an incomplete download before retrying the entire read. Preserve the original bytes without re-encoding.

## Lifecycle and concurrent operations

The user-visible states are uploading, processing, ready, rejected, failed, expired, deleting, and deleted. Keep cleanup progress separate so a rejected image retains its useful rejection reason while its bytes are removed.

Every critical update checks the current state in DynamoDB. A validator can publish only from processing and with its own active work token. Deletion atomically makes the record ineligible for publication. A late validator cannot undo it. Publication, failure, and cleanup decisions must not rely on an earlier read followed by an unconditional update. [Conditional updates](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)

The image record is also the durable work request. Completion records processing and recovery information before trying to invoke Lambda. If invocation fails or the API crashes, the database still records unfinished work. A duplicate completion can request another delivery safely. After the durable transition succeeds, a dispatch failure need not turn acceptance into failure; recovery will attempt dispatch again.

Before accepting completion from uploading, check ownership, the completion deadline, and whether the S3 object exists. A missing object returns 409 and leaves the upload available for completion before its deadline. The worker performs content validation. The transition to processing remains conditional, so a concurrent deletion or expiration wins safely even if the object check already succeeded.

Worker defaults: 120-second Lambda timeout, 180-second exclusive work lease, at most three claimed validation attempts, and a 30-minute processing deadline measured from completion acceptance. A lease is a recorded time window during which one worker owns the attempt. Each claim receives a fresh token. Publication must match that token, so an older worker cannot overwrite a replacement attempt.

Reject known invalid input without another validation attempt. Known configuration failures, such as denied storage access, become failed and are logged for correction. A worker killed by a timeout or memory exhaustion may not record its cause, so recovery treats unexplained lost attempts as retryable only within the existing attempt and processing-time limits. Test representative images near the byte and pixel limits to choose memory and timeout settings. Do not add automatic routing to larger workers or resumable decoding for this assignment.

A transient failure records a retry time and releases the lease if possible. A crashed worker leaves a lease that eventually expires. Recovery checks the live record, retry time, lease, attempt count, and processing deadline before acting. Once attempts or processing time are exhausted, it conditionally records failed and schedules content cleanup. These are operational bounds under available services, not a guarantee of completion during an AWS outage.

Configure Lambda asynchronous function-error retries to zero and maximum event age to five minutes initially. The application recovery policy owns counted validation attempts; AWS may still redeliver events or retry delivery failures. Duplicate deliveries remain safe. Validate this configuration in the local compatibility check. [Async configuration](https://docs.aws.amazon.com/lambda/latest/dg/invocation-async-configuring.html)

Recovery handles at most 200 due candidates per run initially. It advances a candidate's next recovery-check time conditionally before dispatch, so persistent failures do not monopolize every page. That check time schedules recovery, not permission for a newly dispatched worker to start. Worker claims use their separate lease and retry conditions. Active claims move the next check to lease expiry. If recovery fails after claiming dispatch, a later schedule can retry it. Index lag or service outages can delay recovery; conditional base-record checks prevent stale observations from authorizing destructive work.

## Deletion and cleanup

The owner requests deletion. The API marks deleting, removes gallery keys, records cleanup work, and returns 202. Only after the terminal state is durable may the worker replace the contents of the image's immutable S3 key with a zero-byte object. After that write succeeds, it marks deleted. An uncertain S3 response is safe to retry because the placeholder write is repeatable.

Do not use DeleteObject on these keys. Do not add a lifecycle rule that removes the placeholders. A conditional upload cannot create an object at an occupied key. If the original upload wins the race, the cleanup write replaces it; if the placeholder wins, the conditional upload must fail. This behavior needs explicit concurrent verification. [Conditional-write behavior](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)

Expired, rejected, and failed uploads receive the same content removal while retaining their owner-visible outcome. The terminal transition must win before cleanup starts. Otherwise, a stale cleanup observation could overwrite a valid image. Ready images never qualify for cleanup without an owner-authorized deletion transition.

Cleanup retains unfinished work until it succeeds, with retry delays capped at one hour and clear logs for repeated failures. It does not silently abandon deletion after the validation retry budget. Repeated deletion returns 202 while pending and 204 after the placeholder is confirmed. Retain minimal ownership and status information, not unnecessary captions, filenames, or previous processing details, after deletion.

Deletion removes stored image content, not every storage record. Empty objects and small deletion records remain. Previously issued GETs may return the original before replacement, or an empty response afterward. In-flight downloads and existing copies cannot be recalled. State checks prevent new links after deletion is observed; concurrent requests already authorized can finish.

## Verification and implementation sequence

The implementation followed these verification gates in order.

1. Establish a clean local repository and prove the critical LocalStack behavior in a narrow compatibility check: signed conditional PUT, exact byte-bound enforcement, simultaneous upload and placeholder writes, private-bucket configuration, asynchronous Lambda invocation, and EventBridge scheduled-rule delivery. Prefer the existing pinned LocalStack community release if it supports the required behavior. If a required feature is unsupported, report the specific gap and revisit the version or design. Do not replace an integration check with a mock while claiming equivalence. Do not deploy to real AWS without separate authorization.
2. Configure both DynamoDB tables, TTL on `UploadRequests`, and the three indexes on `Images`. Give each Lambda access to the tables and operations it needs. Implement initiation and the lifecycle with DynamoDB conditions and transactions. Verify that a failed transaction leaves neither a new image nor a new request mapping. Verify matching and conflicting retries, owner isolation, expiration, duplicate claims, timeout recovery, and deletion/publication races before adding all routes.
3. Implement bounded image validation and placeholder cleanup. Test valid JPEG/PNG/WebP, exact size and pixel boundaries, corrupt data, animation, mismatched declarations, interrupted reads, and S3 failures.
4. Connect the API, background worker, and recovery schedule. Inject failures after state writes and before invocation, after an S3 write but before confirmation, and during cleanup. Confirm jobs remain recoverable and terminal states cannot be revived.
5. Implement owner/date queries, signed cursors, authoritative state rechecks, metadata reads, and download redirects. Test combined filters, timestamp ties, empty pages with continuation, invalid cursors, index lag, and exact downloaded bytes.
6. Run the complete LocalStack workflow from a fresh setup. Test multiple users and concurrent uploads/deletions. Run the five-minute schedule at least once and force a missed-job recovery. Record actual results and limitations.
7. Prepare the README, OpenAPI specification, setup/reset instructions, architecture explanation, test matrix, failure behavior, known limitations, and a short live-review demonstration. Add CI for suitable static and unit checks.

Use structured logs with request IDs, image IDs, operation, outcome, and safe error codes. Do not log credentials, presigned URLs, image contents, or captions. The first load checks establish bounded-resource behavior and concurrent correctness; no production capacity claim follows from LocalStack alone.

## Implementation status

The service, local deployment, tests, and review instructions are implemented. The separate `Images` and `UploadRequests` tables, fixed global index, periodic recovery latency, retained empty objects, and eventual list consistency are deliberate tradeoffs.

Environment and protocol compatibility were checked with actual requests. LocalStack results do not establish production capacity or full AWS IAM enforcement. In the local check, an unsigned direct S3 GET succeeded despite the private-bucket configuration. Keep the emulator bound to localhost and record this access-control limitation separately from signed PUT enforcement and application ownership checks.

For completed checks and remaining limits, see [verification results](verification.md). The repository includes a runnable LocalStack demonstration; it has not been deployed to a real AWS account.
