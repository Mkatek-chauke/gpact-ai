import json
import hashlib
import os


def database_url():
    if os.getenv("LOCAL_JSON_STORE", "").strip().lower() in {"1", "true", "yes"}:
        return None
    return (
        os.getenv("DATABASE_URL_POOLED")
        or os.getenv("DATABASE_URL")
        or os.getenv("NEON_DATABASE_URL")
    )


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
        with connection.cursor() as cursor:
            cursor.executemany(
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


def _actor_hash(principal_id):
    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()


def check_rate_limit(principal_id, action, limit, window_seconds):
    if not database_url():
        return None
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS government_rate_limits (
                actor_hash TEXT NOT NULL,
                action TEXT NOT NULL,
                window_started_at TIMESTAMPTZ NOT NULL,
                request_count INTEGER NOT NULL,
                PRIMARY KEY (actor_hash, action)
            )
            """
        )
        row = connection.execute(
            """
            INSERT INTO government_rate_limits
                (actor_hash, action, window_started_at, request_count)
            VALUES (%s, %s, now(), 1)
            ON CONFLICT (actor_hash, action) DO UPDATE SET
                window_started_at = CASE
                    WHEN government_rate_limits.window_started_at
                        <= now() - (%s * interval '1 second')
                    THEN now()
                    ELSE government_rate_limits.window_started_at
                END,
                request_count = CASE
                    WHEN government_rate_limits.window_started_at
                        <= now() - (%s * interval '1 second')
                    THEN 1
                    ELSE government_rate_limits.request_count + 1
                END
            RETURNING request_count
            """,
            (_actor_hash(principal_id), action, window_seconds, window_seconds),
        ).fetchone()
        return row["request_count"] <= limit


def record_audit_event(principal_id, event_type, resource_id=None, outcome="success"):
    if not database_url():
        return False
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS government_audit_events (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                actor_hash TEXT NOT NULL,
                event_type TEXT NOT NULL,
                resource_id TEXT,
                outcome TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            """
            INSERT INTO government_audit_events
                (actor_hash, event_type, resource_id, outcome)
            VALUES (%s, %s, %s, %s)
            """,
            (
                _actor_hash(principal_id),
                event_type[:80],
                resource_id[:200] if resource_id else None,
                outcome[:40],
            ),
        )
    return True
