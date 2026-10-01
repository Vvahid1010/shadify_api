# Initial API and R2 foundation

The 2026-10-01 implementation is source-only. It follows both media contracts;
it does not activate a local/public backend or claim that R2 works.

## Implemented behavior

FastAPI exposes process liveness, deliberately pending readiness, upload intent
and completion routes. Default uploads fail closed with 503 until a trusted
existing Authentication identity dependency and real MediaService are supplied.
No mock identity or in-memory repository is enabled in application source.
`create_app(service, identity)` is the integration seam; test identities and
in-memory metadata exist only in tests.

The service checks artist/track ownership against server-side metadata before
issuing a short-lived direct PUT authorization. The browser cannot choose keys,
owner, access class or publish state. Each intent creates a new UUID/key under
`originals/`. PUT signs content type, exact length and `If-None-Match: *` so
reusing an authorization cannot overwrite a successfully created original.
The browser sends the returned headers and a Blob body (the browser supplies
Content-Length). This conditional request must be proven on real R2 before
activation. URLs are sensitive bearer grants; do not log responses, URL queries
or storage exceptions in application, browser telemetry or gateway logs.

Only WAV/FLAC audio originals and JPEG/PNG artwork originals are accepted now.
The configured single-PUT cap defaults to 100 MiB and can be lowered. Large
video/multipart/resume is deferred and no video upload route is advertised;
it must be implemented before accepting large video. No file bytes are proxied
through the API upload endpoint or persisted on the application node.

Completion rechecks ownership/session expiry, reads object metadata and at most
4096 bytes for container magic, validates size and MIME, then conditionally
transitions upload_pending -> uploaded. Failed validation marks failed.
Uploaded stays private and draft, pending future processing; magic checks do
not prove complete decodability or safety. There is no processing/publish,
playback URL, protected-delivery authorization or entitlement endpoint.
All API responses use Cache-Control: no-store.

`PostgresMediaRepository` uses parameterized SQL and transaction-per-operation.
`migrations/001_media_foundation.sql` stores ownership/metadata/state only;
the migration was not applied. An assigned Shadify connection factory is supplied
by the integration owner. No artist ownership is accepted from the upload body.
Artist/track provisioning and ownership transfer are not exposed in this phase;
future ownership mutation must preserve/lock the ownership checks transactionally.
No live DB proof is implied by the local tests.

## Where to place credentials

Proposed protected local Shadify directory (to be coordinated/provisioned by the
shared runtime owner, not created by this implementation):

`/etc/chapary-local/shadify_api/storage.credentials.json`

Put the following JSON fields there privately, outside Git and browser bundles:

```json
{
  "provider": "r2",
  "endpoint_url": "https://<32-character-account-id>.r2.cloudflarestorage.com",
  "bucket": "<existing-private-development-bucket>",
  "access_key_id": "<R2-S3-access-key-id>",
  "secret_access_key": "<R2-S3-secret-access-key>",
  "upload_ttl_seconds": 300,
  "max_upload_bytes": 104857600
}
```

The endpoint uses the R2 S3 API, not `media.shadify.org` or an r2.dev URL.
Use the user's existing bucket-scoped Object Read/Write S3 credentials; no
account-wide administrative access is needed. Do not send secrets in chat.
The directory should be restricted to the assigned service owner. The file
must be a regular non-symlink file, mode 0600, owned by the API user (or root
only if the service can legitimately read it). Never chmod broadly to fix access.
No credential values were read, echoed, generated or transmitted during this work.

`load_storage_config(Path(...))` enforces file ownership/permissions and validates
the account-scoped HTTPS endpoint/TTL/size with redacted errors. Construct
`R2Storage(config)` explicitly server-side. Assemble the service with
`MediaService(PostgresMediaRepository(connect), R2Storage(config),
config.max_upload_bytes, config.upload_ttl_seconds)` so API limits and signed
authorization expiry use the same validated configuration. The app does not auto-discover AWS
credentials or silently enable cloud use when a file appears. The non-secret
`config_overlays.json` uses logical file references; resolved placement, database
credentials and canonical app-profile bindings remain shared-owner prerequisites.

## Cloudflare setup owned by the user

Keep the development R2 Standard bucket private; do not enable public/r2.dev
access. Configure bucket CORS for the exact approved browser origin such as
`https://dev.shadify.org`, method PUT, allowed Content-Type and If-None-Match.
Add no wildcard origin or credential access. Signed S3 uploads use the R2 S3
hostname; custom media domains do not carry S3 presigned upload URLs.
Do not attach the private-originals bucket wholesale to a public custom domain.

`media.shadify.org` remains the future controlled delivery layer for separately
derived published/public assets. Originals never become public by prefix alone.
User owns DNS, origin binding, WAF/rate limits, cache rules and monitoring.
No tunnel, credentials, Cloudflare resources or cache rules were changed.
No main-app Cache Everything rule, Worker or paid-media implementation was added.

## Simplest next verification

1. Coordinate Shadify's distinct Authentication proxy identity, canonical
   app-security transport/profile, private DB assignment, dedicated native venv,
   unit/port/ingress and credential-file placement with the parent/topology owner.
   Connect the existing shared identity verifier and PostgreSQL repository;
   review/apply the migration only to the assigned Shadify database.
2. User privately configures R2 and supplies the protected file above. After
   that explicit integration step, request one tiny supported upload under a
   legitimately owned track. Upload directly with the returned headers.
3. Verify R2 rejects overwrite/replay (412), wrong Content-Type/length/signature,
   expired grants and another owner's completion. Confirm browser CORS and
   bounded object verification; correct completion should return uploaded,
   private, draft. Keep original inaccessible without authorization.
4. Mark real R2/database/Auth proof only after those checks. Signed URL generation
   alone is local signing and proves no bucket/network/permission/CORS success.
   Domain/cache/abuse/delivery checks remain separate future work.

Reference provider behavior: [R2 presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/),
[boto3 example](https://developers.cloudflare.com/r2/examples/aws/boto3/),
[conditional R2 operations](https://developers.cloudflare.com/r2/api/s3/extensions/).
