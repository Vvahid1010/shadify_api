-- Staged source only. No role names, grants or runtime activation.
BEGIN;
ALTER TABLE shadify_artists
 ADD COLUMN primary_language varchar(64) NOT NULL DEFAULT 'und',
 ADD COLUMN localized jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(localized)='array' AND jsonb_array_length(localized)<=20),
 ADD COLUMN genre_ids jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(genre_ids)='array' AND jsonb_array_length(genre_ids)<=20),
 ADD COLUMN slug_changed_at timestamptz;
UPDATE shadify_artists SET localized=jsonb_build_array(jsonb_build_object('language','und','name',name,'bio',bio,'aliases','[]'::jsonb));
CREATE TABLE shadify_artist_slugs (
 slug varchar(64) PRIMARY KEY CHECK(slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'),
 artist_id uuid NOT NULL REFERENCES shadify_artists(id) DEFERRABLE INITIALLY DEFERRED,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(slug,artist_id)
);
INSERT INTO shadify_artist_slugs(slug,artist_id) SELECT slug,id FROM shadify_artists;
CREATE FUNCTION protect_artist_slug_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Artist slug history is permanent'; END $$;
CREATE TRIGGER artist_slug_history_immutable BEFORE UPDATE OR DELETE ON shadify_artist_slugs FOR EACH ROW EXECUTE FUNCTION protect_artist_slug_history();
CREATE FUNCTION register_artist_slug() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN RAISE EXCEPTION 'Artist UUID is immutable'; END IF;
 IF TG_OP='INSERT' OR NEW.slug IS DISTINCT FROM OLD.slug THEN
  IF NEW.slug IN ('admin','api','auth','studio','search','media','account','login','logout','signup','register','settings','support','help','privacy','terms','discover','artists','artist','track','release','playlist','tv','tvl','www','official','shadify') THEN RAISE EXCEPTION 'Reserved artist slug' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM shadify_artist_slugs WHERE slug=NEW.slug AND artist_id<>NEW.id) THEN RAISE EXCEPTION 'Artist slug unavailable' USING ERRCODE='23505'; END IF;
  INSERT INTO shadify_artist_slugs(slug,artist_id) VALUES(NEW.slug,NEW.id) ON CONFLICT DO NOTHING;
  -- A concurrent claimant can commit while the INSERT waits. Recheck the
  -- permanent registry after conflict resolution, even if the current slug moved.
  IF NOT EXISTS(SELECT 1 FROM shadify_artist_slugs WHERE slug=NEW.slug AND artist_id=NEW.id) THEN
   RAISE EXCEPTION 'Artist slug unavailable' USING ERRCODE='23505';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER register_artist_slug BEFORE INSERT OR UPDATE OF id,slug ON shadify_artists FOR EACH ROW EXECUTE FUNCTION register_artist_slug();
CREATE TABLE shadify_artist_slug_audit (
 id uuid PRIMARY KEY, artist_id uuid NOT NULL REFERENCES shadify_artists(id),
 actor_user_id uuid NOT NULL REFERENCES shadify_users(id), old_slug varchar(64) NOT NULL,
 new_slug varchar(64) NOT NULL, primary_override boolean NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(), CHECK(old_slug<>new_slug)
);
CREATE TRIGGER artist_slug_audit_immutable BEFORE UPDATE OR DELETE ON shadify_artist_slug_audit FOR EACH ROW EXECUTE FUNCTION protect_artist_slug_history();
-- Database-maintained index prevents stale/private projections after direct lifecycle writes.
CREATE FUNCTION normalize_artist_search(value text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
 SELECT trim(regexp_replace(regexp_replace(translate(casefold(normalize(value,NFKD) COLLATE pg_catalog.pg_unicode_fast),
 'يىك٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹','ییک01234567890123456789'),
 U&'[\0300-\036F\064B-\065F\0670\06D6-\06ED]','','g'),U&'[\200C\200D[:space:]]+',' ','g'))
$$;
ALTER TABLE shadify_artists ADD COLUMN normalized_names text[] NOT NULL DEFAULT '{}',
 ADD COLUMN search_names tsvector NOT NULL DEFAULT '', ADD COLUMN search_bio tsvector NOT NULL DEFAULT '';
CREATE FUNCTION index_artist_identity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE entry jsonb; names text[]:=ARRAY[normalize_artist_search(NEW.name)]; biographies text:=NEW.bio; alias_name text;
BEGIN
 FOR entry IN SELECT value FROM jsonb_array_elements(NEW.localized) LOOP
  names:=array_append(names,normalize_artist_search(entry->>'name'));
  biographies:=biographies||' '||coalesce(entry->>'bio','');
  FOR alias_name IN SELECT jsonb_array_elements_text(coalesce(entry->'aliases','[]')) LOOP names:=array_append(names,normalize_artist_search(alias_name)); END LOOP;
 END LOOP;
 NEW.normalized_names:=names;
 NEW.search_names:=to_tsvector('simple',array_to_string(names,' ')||' '||normalize_artist_search(NEW.genres::text||' '||NEW.genre_ids::text));
 NEW.search_bio:=to_tsvector('simple',normalize_artist_search(biographies)); RETURN NEW;
END $$;
CREATE TRIGGER index_artist_identity BEFORE INSERT OR UPDATE OF name,bio,localized,genres,genre_ids ON shadify_artists FOR EACH ROW EXECUTE FUNCTION index_artist_identity();
UPDATE shadify_artists SET name=name;
CREATE INDEX artist_names_search ON shadify_artists USING gin(search_names) WHERE publish_state='published';
CREATE INDEX artist_bio_search ON shadify_artists USING gin(search_bio) WHERE publish_state='published';
CREATE INDEX artist_exact_names_search ON shadify_artists USING gin(normalized_names) WHERE publish_state='published';
COMMIT;
