# Shadify Media Delivery, Cache & Security

## 1. Purpose

This document defines how Shadify should deliver music, music video, previews and artwork after the media has been stored.

Storage and delivery are intentionally separate concerns:

- docs/MEDIA_STORAGE_ARCHITECTURE.md defines where media bytes live;
- this document defines how users safely and efficiently receive those bytes.

The key architectural decision is to keep media delivery separate from the main Shadify application hostname.

## 2. Dedicated media hostname

The intended long-term media delivery hostname is:

    media.shadify.org

The main application hostname should not become the general media-cache surface.

Conceptually:

    app / web
        -> Shadify UI and normal page/session behavior

    API
        -> product data, authorization and business logic

    media.shadify.org
        -> music, video, previews, artwork and media delivery policy

The exact public application/API hostnames can evolve independently. The important invariant is that media delivery has a dedicated policy boundary.

## 3. Why media is separated

The dedicated media hostname is primarily about operational control, not branding.

It allows Shadify to:

- cache media aggressively without caching the whole application;
- keep HTML/session/API responses under different cache rules;
- apply media-specific WAF and rate limiting;
- isolate high-bandwidth traffic from normal application routing;
- handle free and paid media differently;
- reduce accidental caching of private/user-specific responses;
- tune CDN behavior specifically for HLS segments, audio, video, previews and artwork;
- evolve media authorization without redesigning the frontend route structure.

Do not use a global "Cache Everything" rule on the primary Shadify application as a substitute for this separation.

The accepted single host is media.shadify.org for versioned public audio, video,
artwork and previews. Configure it as an R2 Custom Domain on the separate public
derived-delivery bucket, not as a Cloudflare Tunnel hostname. Private originals/
drafts remain in their private bucket; never attach that bucket to the public
domain. The selected main VM app host is shadify.org; dev.shadify.org remains WSL.
User owns Cloudflare activation. No DNS, bucket, tunnel or public grant has been
created by the API source work.

## 4. Cache mental model

Upload and cache are different operations.

Uploading an object to R2 stores the durable copy. It does not mean every Cloudflare edge already has a cached copy.

Normal public delivery behaves conceptually like this:

    first request at an edge
        -> cache miss
        -> Cloudflare fetches from R2
        -> response can be cached

    later eligible requests at that edge
        -> cache hit
        -> response is served from Cloudflare cache
        -> no new R2 object read for that cached response

Therefore:

- R2 is the durable origin/object store;
- CDN cache is a temporary delivery copy close to users.

One million user requests do not necessarily mean one million R2 Class B reads when caching is effective.

## 5. Immutable delivery assets

Shadify should prefer versioned/immutable delivery objects.

Instead of overwriting:

    tracks/123/audio.m4a

prefer versioned/generated asset identity such as:

    delivery/.../{asset_id}/audio/...

If content changes, create a new asset/version.

Benefits:

- long cache lifetimes are safe;
- no stale-object ambiguity after replacement;
- cache invalidation becomes rare;
- clients and CDN nodes can strongly cache published media.

Original/master immutability is defined separately in MEDIA_STORAGE_ARCHITECTURE.md.

## 6. Public/free media path

For free/public media, the target path is:

    Internet
        ->
    Cloudflare DDoS protection
        ->
    WAF / abuse rules
        ->
    Rate limiting where useful
        ->
    CDN cache
        ->
    R2 only on cache miss

WAF means Web Application Firewall: request filtering at the web edge.

Rate limiting means restricting abnormal request frequency before traffic reaches the storage origin.

Publicly playable delivery assets may be cache-friendly, but originals remain private.

Typical public/cacheable assets include:

- free-track delivery segments/files;
- public previews;
- public artwork;
- public thumbnails/posters;
- public HLS manifests/segments when appropriate.

## 7. Paid/private full media path

Paid/private full content must not rely on secrecy of a permanent URL.

A URL such as:

    https://media.shadify.org/paid-song.m4a

must never by itself prove that the requester purchased the track.

Target model:

    Fan requests protected media
        ->
    shadify_api verifies Authentication + entitlement
        ->
    short-lived controlled media authorization
        ->
    protected media delivery layer
        ->
    Cloudflare cache/origin logic
        ->
    R2 only when needed

The exact mechanism may later use a Cloudflare Worker or another edge authorization approach.

Worker here means a small serverless program running at Cloudflare's edge.

Do not select the final Worker/edge-auth implementation before the paid-content backend phase. Preserve the boundary now.

## 8. Cache and authorization are separate concerns

Caching protected media does not mean making it public.

The protected delivery layer must first decide whether the request is authorized, then allow eligible cached/origin delivery.

Do not build a design where every authorized play forces the full object to be fetched again from R2 if a safe cache strategy can serve it.

Likewise, do not bypass authorization merely to increase cache hit rate.

Security is authoritative; cache is an optimization.

## 9. DDoS and abuse-cost protection

Cloudflare sits in front of the media origin.

The defensive layers should be:

    attacker / abusive client
        ->
    Cloudflare DDoS protection
        ->
    WAF
        ->
    rate limiting / bot-abuse policy
        ->
    CDN cache
        ->
    R2

The purpose is to stop or absorb as much unwanted traffic as possible before it becomes an origin/storage read.

Important distinction:

- large volumetric attacks may be identified and mitigated as DDoS;
- slower or application-like abuse may not necessarily look like classic DDoS.

Therefore Shadify must not rely only on automatic DDoS classification.

Use media-specific WAF/rate-limit rules and cache policy so ordinary abusive traffic also has difficulty generating large volumes of billable R2 operations.

## 10. Cost-protection principles

The following rules are part of the cost architecture:

- never expose the private R2 bucket as an unmanaged public origin;
- route production media through the controlled Cloudflare media delivery layer;
- maximize safe cache hit rate for immutable public media;
- use long-lived cache policy for immutable/versioned assets where appropriate;
- avoid needless cache-busting query parameters;
- rate-limit obviously abnormal media access patterns;
- apply tighter controls to expensive video/media endpoints when justified;
- observe R2 Class A/Class B request counts and cache hit/miss behavior;
- revisit thresholds from real traffic, not guesses.

Do not hard-code current Cloudflare prices or free quotas into application behavior.

## 11. R2 development URL

Cloudflare's R2 development/public URL is not the intended production Shadify delivery surface.

Development-only R2 access may be useful for controlled testing, but production delivery should use the dedicated media hostname and the required Cloudflare security/cache layer.

Do not design production URLs around an R2 development hostname.

## 12. Upload path is separate

Artist uploads do not go through the public media delivery route.

Target upload flow remains:

    Artist browser
        ->
    shadify_api requests/authorizes upload
        ->
    short-lived signed upload authorization
        ->
    direct upload to private R2

This path is defined in MEDIA_STORAGE_ARCHITECTURE.md.

media.shadify.org is primarily a delivery boundary, not a general-purpose upload credential endpoint.

## 13. Free versus protected namespaces

The internal object layout may distinguish delivery classes such as:

    delivery/public/...
    previews/...
    delivery/protected/...

This can help policy clarity, but the exact key layout is not a security boundary by itself.

Security must come from Cloudflare/API authorization and private object access, not from folder names.

## 14. HLS and cache behavior

HLS means HTTP Live Streaming: media is delivered as a playlist plus smaller media segments.

For audio/video streaming, HLS can improve delivery and adaptive quality, but it also creates multiple object requests.

That makes cache effectiveness especially important:

- popular HLS segments should be served from edge/cache where possible;
- immutable segment names improve cacheability;
- manifests may need different cache lifetimes from media segments;
- the exact HLS cache policy should be finalized when the transcoding/player pipeline is implemented.

Do not prematurely optimize segment sizes or cache TTL values in the documentation phase.

TTL means Time To Live: how long a cached item may remain valid before revalidation/expiry.

## 15. Cookies and session separation

The media hostname should avoid unnecessary dependence on normal application cookies.

This reduces:

- needless request overhead;
- accidental coupling to UI session behavior;
- risk of caching user-specific application responses as media;
- complexity when media traffic scales independently.

Protected media authorization should use an explicit, purpose-built short-lived mechanism rather than blindly forwarding the full application session state to every static media object.

## 16. Initial implementation policy

During the current development phase:

- keep the R2 development bucket private;
- do not turn on broad public media access just to make frontend development easier;
- do not globally cache the main Shadify application;
- preserve media.shadify.org as the production-oriented delivery boundary;
- use local/mock media in the frontend until backend media delivery is explicitly implemented;
- defer paid-media Worker/edge authorization until entitlement/payment work begins.

## 17. Future production checklist

Before real public media launch, explicitly configure and validate:

- media.shadify.org custom domain;
- R2 origin/bucket binding;
- cache rules for artwork/audio/video/HLS;
- immutable asset strategy;
- WAF rules;
- rate-limit rules;
- DDoS protection posture;
- public/free asset policy;
- protected-media authorization;
- origin privacy;
- cache hit/miss observability;
- R2 request/cost monitoring;
- failure behavior when authorization or origin is unavailable.

## 18. Architecture invariant

The durable separation is:

    Shadify app = UI/session/product experience
    shadify_api = authority/ownership/entitlement
    media.shadify.org = controlled media delivery
    R2 = durable media storage
    Cloudflare CDN/cache = delivery optimization
    Cloudflare security = protection before origin

This lets Shadify control media cache, security and cost independently without forcing the entire main application domain into media-style caching.
