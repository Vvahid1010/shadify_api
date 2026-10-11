> CORRECTION: Runtime history contains 013_release_suggestions.sql and
> 014_artist_profile_media.sql. Retired 013_ingest_video.sql is NOT deployable.
> Use the merged UNAPPLIED 016_ingest_video_after_profile.sql plus the
> video-activation/ACTIVATION.md handoff. Preserve owner profile/runtime source.
> The earlier immutable media source zip and 013 discussion below are historical
> isolated-test evidence, not safe runtime overlays. Gate147/rev3 stays unchanged.

# Direct MP3 and MP4 uploads — source integration

Source status, 2026-10-10. No live migration, service activation, browser mutation,
credential/grant change or publication was performed by this work.

## Qualified MP3-only activation

The read-only archive on the Windows host is:
`C:\Users\Simer\Documents\Codex\2026-10-10\task\mp3-source-0b84c183ede8e53e.zip`.
SHA-256: `0b84c183ede8e53e7c6a6ab5cfdc51617f5bc888f50ccfc86f5583dfe5e43e97`.
It was captured before MP4 changes. Its `sha256.json` verifies all nine files.

Overlay those exact files into isolated copies of the qualified API/frontend
sources. Do not package the current mixed MP3/MP4 checkout for MP3 activation.
Preserve the owner's qualified public-artist/runtime changes.

- API/ingest production files: `shadify_api/media.py`, `shadify_api/media_tools.py`.
- Frontend production files: `src/domain/upload.ts`, `src/domain/studioMedia.ts`,
  `src/components/artist-studio/StudioUploads.vue`, `StudioMediaReview.vue`.
- Tests: `test_mp3_upload.py`, `test_direct_mp3_pipeline.py`, `mp3-upload.test.ts`.

Update the existing native API distribution used by API and ingest together;
refresh the byte listener if it shares that distribution/venv. Update the native
SSR frontend using the normal owner process. No MP3 migration, dependency,
security grant or protected setting change is needed. Reuse the already qualified
ClamAV/FFmpeg closures, pinned identities and complete signed definition set.

MP3 includes MPEG Layer III CBR/VBR, mono/stereo within existing sample-rate bounds,
32kbps or greater average source bitrate, bounded ID3v2.2/2.3/2.4 and ID3v1 tags,
and at most one fully validated attached JPEG/PNG. Other streams/layers, malformed
frames and trailing data are rejected. High is bounded by source bitrate and
320kbps; Low remains 128kbps. Source quality is marked lossy. Master bytes remain
unchanged. The probe consumes all packets; the original authorized 272.536-second
320kbps MP3 passed full validation, including its embedded cover.

Owner acceptance must upload the original MP3 directly, without WAV conversion,
through the normal authenticated browser flow. Capture the MPEG upload intent,
scan/validation/review/ready transitions, unchanged master checksum, private
output GET and real playback. Quarantine must remain inaccessible and sample
track audio must remain private. Record actual playback separately from source
validation and isolated tests. Accepted volume/rail/TV/player behavior remains
the regression boundary.

## Initial MP4 scope and migration

`013_ingest_video.sql` follows current 012. It extends existing asset/variant
checks and replaces three existing approval functions without new tables or
grants. It gives historical rows no approval. Video originals must be private,
draft and `video/mp4`. Ready video jobs require exactly approved `master` and
`video` variants. The master must retain the input MIME, size and SHA-256.

Do not apply retired 013 to the assigned live database; use merged candidate 016 only after runtime-owner coordination. It has
been exercised only in newly created disposable test schemas. Constraint changes
take normal PostgreSQL DDL locks, so coordinate with the existing runtime owner.

Initial source support is ISO MP4 with one H.264 8-bit yuv420p stream (Baseline,
Constrained Baseline, Main or High), one AAC-LC mono/stereo stream, even dimensions,
at most 1920 per dimension and 1920x1080 total pixels, and average frame rate at most 30fps. This
includes portrait dimensions within that pixel budget. MOV, silent files, extra
streams, other codecs/profiles and higher frame rates are rejected. Existing
100MiB input, 200MiB scan/scratch, 180-second CPU/wall and 2GiB memory maxima remain;
configured smaller limits still apply. Actual processing can time out within
those limits; a timeout never grants readiness.

Standard tail-moov and fast-start inputs use bwrap's existing read-only descriptor
data mount at `/scratch/input`. The descriptor is consumed and closed before
payload execution. Host paths and credentials are not mounted; the existing
networkless namespace, tool closure and prlimit controls remain. MOV external
references are disabled. Both streams are fully decoded into a null sink with
bounded progress. Processing happens once through the same manifest/approval
pipeline, producing sanitized fragmented MP4 H.264 High/4.1 + AAC-LC stereo48kHz
128kbps without B-frame timing delay, which is revalidated. No on-demand transcode or alternate pipeline exists.

The video delivery asset uses existing technical-ready `free/published` metadata
required by video attachment. This is not content publication or public byte
delivery: the normal video record remains draft and private access is checked.
No video content is automatically created or published by upload completion.

## Runtime-owner frontend/player integration

Media source changes prepare existing `StudioUploads.vue` and `StudioMediaReview.vue`
on a track. Do not add a second player. The UI remains gated until the owner adds
these server availability hints to the existing artist availability object:

- `original_video_upload: true` after merged API, 016 and ingest qualification.
- `private_video_playback: true` only with the shared-engine integration below.

The shared player's managed queue currently assumes audio. In `stores/player.ts`,
the owner should use `managed.mediaKind ?? 'audio'` for managed `media.kind`, and
`entry.mediaKind ?? 'audio'` for `mode`/`queueMode` in `playManaged`. `ManagedTrack`
now carries that optional field and `managedPrivateVideo` projects a stable track
and approved private asset through the same access model. Preserve existing
queue continuity, source renewal, volume, persistent layout and TV behavior;
qualify the video element with a small synthetic fixture, not user content.

Ensure StudioWorkspace still mounts the existing upload/review components for
tracks. For catalog attachment, use the existing authorized video create/patch
API with `track_id` and the approved **video variant** `delivery_asset_id`.
`POST /api/artists/{artist}/videos` defaults to draft. Never attach a video master
to the track's audio `original_asset_id`; both frontend and API reject that use.
Keep publication a separate explicit operation. Artist routes, Workspace,
Authentication, runtime and shared-player code were not edited by this task.

## Rollback and evidence

Failed migration transactions roll back atomically. After 013 commits, retain
the forward schema if video rows exist; restoring old enum/role checks would
conflict with those rows. Do not delete ingest history or assets as rollback.
For application rollback, stop new video intake, keep pending video jobs private,
and retain a compatible worker until those jobs settle or a forward fix is ready.
An older audio-only worker cannot safely process pending video jobs. MP3/audio
and artwork semantics remain compatible under 013.

Real synthetic MP4 acceptance passed HTTP upload, real ClamAV, native decoding,
transcoding, normal owner review, immutable master/private output retrieval,
draft video attachment and wrong-kind audio attachment rejection. Test identities
and MusicBrainz no-match responses are fixtures, not live Auth/provider proof.
Seven MP4 framing/codec/boundary tests cover tail-moov/fast-start and actual
Baseline/Main/High profiles; five MP3 tests and five prior tool/container tests
pass after the MP4 extension. Three disposable 013 compatibility tests preserve
audio/artwork approval, existing worker-role scope and expired-attempt rejection. Twenty-seven frontend upload/review tests and scoped
lint pass. Source-only Vue typecheck passes with generated Quasar/Vite declarations;
the normal project typecheck still includes unrelated binary public `.ts` streams.

`/tmp` was full. Tests use the ignored main-filesystem directory
`shadify_api/.build-tools/fmt` through `TMPDIR`; no shared cleanup or tmpfs resizing
was performed. Runtime activation still needs owner-controlled browser proof.
