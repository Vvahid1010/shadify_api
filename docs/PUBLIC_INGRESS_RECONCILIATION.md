# Public ingress source reconciliation

This recovers the selected artist-profile022/task8-v2 overlay into main source,
then adds API-owned public aliases for Authentication's generic opt-in guest proxy.
The per-file selected/candidate hashes distinguish recovered bytes from new merges.
Selected manifest:096acc6813f75cae287f79b67acecb5eafb75c71691eb447994117174455cf2f.
Git baseline:fba75b18bca4089887a107bcfe9928b1cd5289f1.

Public alias routing is /api/v1/public/proxy/{path} in Auth, configured /public
prefix and Shadify /api logical prefix, producing /api/public/{path}. Six exact
GET catalog entries retain Authentication-only peer admission and an8MiB response
bound. Only accept/cache-control/content-type/referrer-policy/x-content-type-options
are declared, including safe response sealing headers. No end-user identity is
invented. Private route identity checks and app-owned permissions remain unchanged.

Main's systemd credential ACL checks, video upload/signature/completion/studio paths,
MP4 codec validation/processing, and video variant semantics in022 are preserved.
The selected overlay's profile variants, public cover/publication visibility,
editorial, optional metadata and split-local-storage behavior are recovered together
because their repository/media imports and schema form the selected source closure.
Dependencies are unchanged: canonical shell0.4.6 and transport0.1.9. Projected mTLS
remains explicit OFF-only; managed UDS ON retains its replay admission. No new daemon.

Native packaging now includes the exact static receiver catalog and historical014
rollback file, and declares022 with completeness validation. Historical native
artifacts retain their existing validation. Source publication and package creation
never execute SQL or activate a service.

Qualification:71 focused tests pass, including actual native ON guest
request/response for all6 aliases and denial of private user access without an
assertion; ACL, MP4 framing, SDK/playback and package checks. Two foundation upload
fixture failures reproduce unchanged on main. Both complete profile-only and video-compatible migration histories pass in the
existing disposable Unix-socket PostgreSQL fixture with synthetic data; no active
database or user data was touched.019 now allows the exact016-plus018 function
hash55d683835bc7ec420deebf8e8175f15961980e7db9b1fa4a8b39b02681801191,
retaining the old selected hash. Real MP4 native-tool pipeline qualification remains
separate from framing/source-merge checks. Frontend25
focused tests and SSR build passed in the isolated frontend candidate.

Earlier thin-patch receipt is superseded: it omitted safe response headers from
the catalog. Use the reconciled source and new review receipt, not that older patch.
