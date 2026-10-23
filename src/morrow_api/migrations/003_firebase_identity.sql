ALTER TABLE users ADD COLUMN firebase_uid text UNIQUE CHECK (length(firebase_uid) BETWEEN 1 AND 128);
ALTER TABLE users ADD COLUMN firebase_project_id text;
ALTER TABLE users ADD CONSTRAINT firebase_identity_pair CHECK ((firebase_uid IS NULL) = (firebase_project_id IS NULL));
ALTER TABLE auth_sessions ADD COLUMN firebase_auth_time bigint CHECK (firebase_auth_time > 0);
