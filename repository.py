import os
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:dev@localhost:5433/widgets")


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS widgets (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            fields JSONB NOT NULL,
            button_text TEXT DEFAULT 'Submit',
            bundle_version INTEGER DEFAULT 1,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_widgets_tenant ON widgets(tenant_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id SERIAL PRIMARY KEY,
            widget_id INTEGER NOT NULL REFERENCES widgets(id),
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            data JSONB NOT NULL,
            ip_address TEXT,
            country TEXT,
            city TEXT,
            spam_flag BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_widget ON submissions(widget_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_tenant ON submissions(tenant_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_created ON submissions(created_at)")

    conn.commit()
    conn.close()


def seed_test_widget():
    """Creates one tenant and one widget for Phase 2 testing, if they don't already exist."""
    conn = get_db()
    tenant = conn.execute("SELECT id FROM tenants WHERE email = %s", ("test@example.com",)).fetchone()
    if tenant is None:
        tenant = conn.execute(
            "INSERT INTO tenants (email) VALUES (%s) RETURNING id",
            ("test@example.com",),
        ).fetchone()
        conn.commit()
    tenant_id = tenant["id"]

    widget = conn.execute("SELECT id FROM widgets WHERE tenant_id = %s", (tenant_id,)).fetchone()
    if widget is None:
        widget = conn.execute(
            """INSERT INTO widgets (tenant_id, type, title, fields)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (tenant_id, "signup_form", "Test Signup Widget", '[{"name": "email", "label": "Email", "type": "email", "required": true}]'),
        ).fetchone()
        conn.commit()

    print(f"Seeded tenant_id={tenant_id}, widget_id={widget['id']}")
    conn.close()
    return tenant_id, widget["id"]


def get_widget_by_id(widget_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM widgets WHERE id = %s", (widget_id,)).fetchone()
    conn.close()
    return row


def insert_submission(widget_id: int, tenant_id: int, data: dict, ip: str, country, city, spam_flag: bool):
    conn = get_db()
    row = conn.execute(
        """INSERT INTO submissions (widget_id, tenant_id, data, ip_address, country, city, spam_flag)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, created_at""",
        (widget_id, tenant_id, psycopg.types.json.Json(data), ip, country, city, spam_flag),
    )
    result = row.fetchone()
    conn.commit()
    conn.close()
    return result


if __name__ == "__main__":
    init_db()
    seed_test_widget()