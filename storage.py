import json
import os


def database_url():
    return os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL")


def _connect():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("DATABASE_URL is set, but the psycopg driver is not installed.") from exc
    return psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=10)


def load_documents():
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS government_documents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                classification TEXT NOT NULL,
                status TEXT NOT NULL,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS government_documents_classification_idx "
            "ON government_documents (classification)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS government_documents_title_idx "
            "ON government_documents (title)"
        )
        rows = connection.execute(
            "SELECT data FROM government_documents ORDER BY title, id"
        ).fetchall()
        return [row["data"] for row in rows]


def save_documents(documents):
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS government_documents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                classification TEXT NOT NULL,
                status TEXT NOT NULL,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO government_documents (id, title, classification, status, data, updated_at)
            VALUES (%(id)s, %(title)s, %(classification)s, %(status)s, %(data)s::jsonb, now())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                classification = EXCLUDED.classification,
                status = EXCLUDED.status,
                data = EXCLUDED.data,
                updated_at = now()
            """,
            [
                {
                    "id": document["id"],
                    "title": document["title"],
                    "classification": document["classification"],
                    "status": document["status"],
                    "data": json.dumps(document, ensure_ascii=False),
                }
                for document in documents
            ],
        )
