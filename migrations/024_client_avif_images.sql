-- Additive client AVIF intake; no runtime execution or audio/video policy changes.
BEGIN;
ALTER TABLE shadify_media_assets DROP CONSTRAINT shadify_profile_original_private;
ALTER TABLE shadify_media_assets ADD CONSTRAINT shadify_profile_original_private CHECK(
 asset_type NOT IN ('avatar_original','header_original') OR (access_class='private' AND publish_state='draft'
 AND mime IN ('image/png','image/jpeg','image/webp','image/avif') AND size<=10485760));
ALTER TABLE shadify_media_assets DROP CONSTRAINT shadify_profile_delivery_limits;
ALTER TABLE shadify_media_assets ADD CONSTRAINT shadify_profile_delivery_limits CHECK(asset_type NOT IN ('avatar_delivery','header_delivery') OR (mime IN ('image/png','image/jpeg','image/webp','image/avif') AND size<=10485760 AND access_class='free' AND publish_state='published'));
ALTER TABLE shadify_media_assets ADD CONSTRAINT shadify_client_avif_bound CHECK(
 mime<>'image/avif' OR asset_type NOT IN ('avatar_original','header_original','artwork_original') OR size<=1048576);
-- Preserve the installed audio/video/optional-metadata decision body verbatim.
-- Only the one existing profile-image MIME predicate may change.
DO $client_avif$
DECLARE definition text; needle text := 'source_asset.mime NOT IN (''image/png'',''image/jpeg'')';
BEGIN
 SELECT pg_get_functiondef(p.oid) INTO definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname=current_schema() AND p.proname='validate_shadify_ingest_decision' AND p.pronargs=0;
 IF definition IS NULL OR (length(definition)-length(replace(definition,needle,'')))/length(needle)<>1
 THEN RAISE EXCEPTION 'Exactly one existing profile MIME predicate required'; END IF;
 EXECUTE replace(definition,needle,'source_asset.mime NOT IN (''image/png'',''image/jpeg'',''image/webp'',''image/avif'')');
END $client_avif$;

DO $image_patch$
DECLARE definition text; needle text;
BEGIN
 SELECT pg_get_functiondef(p.oid) INTO STRICT definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() AND p.proname='require_shadify_pipeline_approval' AND p.pronargs=0;
 needle := $old0$(source_asset.asset_type NOT IN ('avatar_original','header_original') AND (target_asset.asset_type<>'artwork_delivery' OR output_role<>'cover'))))$old0$;
 IF (length(definition)-length(replace(definition,needle,'')))/length(needle)<>1 THEN RAISE EXCEPTION 'Exact image predicate required: require_shadify_pipeline_approval'; END IF;
 definition := replace(definition,needle,$new0$(source_asset.asset_type NOT IN ('avatar_original','header_original') AND (target_asset.asset_type<>'artwork_delivery' OR output_role NOT IN ('cover','cover_avif') OR (output_role='cover_avif' AND (source_asset.mime<>NEW.mime OR NEW.output_sha256<>NEW.input_sha256))))))$new0$);
 needle := $oldformat$NEW.mime<>CASE WHEN output_role IN ('cover_avif','cover_avif_small') THEN 'image/avif' ELSE 'image/png' END$oldformat$;
 IF (length(definition)-length(replace(definition,needle,'')))/length(needle)<>1 THEN RAISE EXCEPTION 'Exact image output MIME predicate required'; END IF;
 definition := replace(definition,needle,$newformat$(output_role IN ('cover_avif','cover_avif_small') AND NEW.mime NOT IN ('image/avif','image/webp','image/jpeg','image/png')) OR (output_role NOT IN ('cover_avif','cover_avif_small') AND NEW.mime<>'image/png')$newformat$);
 EXECUTE definition;
END $image_patch$;
DO $image_patch$
DECLARE definition text; needle text;
BEGIN
 SELECT pg_get_functiondef(p.oid) INTO STRICT definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() AND p.proname='require_shadify_pipeline_ready' AND p.pronargs=0;
 needle := $old0$ELSE ARRAY['master','cover'] END;$old0$;
 IF (length(definition)-length(replace(definition,needle,'')))/length(needle)<>1 THEN RAISE EXCEPTION 'Exact image predicate required: require_shadify_pipeline_ready'; END IF;
 definition := replace(definition,needle,$new0$WHEN kind='artwork_original' AND version='client-artwork-avif-v1' THEN ARRAY['master','cover','cover_avif'] ELSE ARRAY['master','cover'] END;$new0$);
 EXECUTE definition;
END $image_patch$;


COMMIT;
