BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

-- Running upgrade 0001 -> 9a9ed56149a0

CREATE EXTENSION IF NOT EXISTS vector;;

UPDATE alembic_version SET version_num='9a9ed56149a0' WHERE alembic_version.version_num = '0001';

-- Running upgrade 9a9ed56149a0 -> 72c808426f98

ALTER TABLE documents ADD COLUMN ts_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(filename, '') || ' ' || coalesce(extracted_text, ''))) STORED;

CREATE INDEX idx_documents_ts_vector ON documents USING gin (ts_vector);

UPDATE alembic_version SET version_num='72c808426f98' WHERE alembic_version.version_num = '9a9ed56149a0';

-- Running upgrade 72c808426f98 -> 97f28fa6d77c

