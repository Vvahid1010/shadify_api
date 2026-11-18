-- App-owned runtime access. Applied only through an approved migration plan.
-- Preserve historical privileges; add only current app-owned runtime relations.
BEGIN;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
public.artist_memberships,
public.artist_ownership_audit,
public.shadify_admin_grants,
public.shadify_admin_nomination_v1,
public.shadify_admin_nominations,
public.shadify_app_admin,
public.shadify_app_admin_audit,
public.shadify_app_admin_delegates,
public.shadify_artists,
public.shadify_discover_editors,
public.shadify_events,
public.shadify_ingest_approvals,
public.shadify_ingest_attempts,
public.shadify_ingest_jobs,
public.shadify_ingest_match_decisions,
public.shadify_ingest_release_proposals,
public.shadify_ingest_scans,
public.shadify_ingest_stages,
public.shadify_ingest_variants,
public.shadify_mb_cache,
public.shadify_mb_queue,
public.shadify_mb_rate,
public.shadify_media_assets,
public.shadify_moments,
public.shadify_nomination_control,
public.shadify_nomination_operations,
public.shadify_release_tracks,
public.shadify_releases,
public.shadify_tracks,
public.shadify_users,
public.shadify_videos TO "Shadify_api";
GRANT SELECT, UPDATE ON TABLE public.shadify_discover_state TO "Shadify_api";
GRANT SELECT ON TABLE public.shadify_nomination_operation_status TO "Shadify_api";
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.artist_manager_grants,
 public.auth_identity_projections TO "Shadify_api";
GRANT SELECT, INSERT ON TABLE public.shadify_artist_slugs,
 public.shadify_artist_slug_audit, public.shadify_primary_operations TO "Shadify_api";
GRANT SELECT, INSERT, UPDATE ON TABLE public.shadify_primary_selections TO "Shadify_api";
CREATE TABLE public.shadify_schema_revision (revision text NOT NULL PRIMARY KEY);
INSERT INTO public.shadify_schema_revision VALUES ('027_app_owned_runtime_access');
COMMIT;
