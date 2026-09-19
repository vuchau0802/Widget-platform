import os
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

from migrate import run_migrations

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:dev@localhost:5433/widgets")


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    """Schema lives in migrations/ and is applied via run_migrations()."""
    run_migrations()


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


def get_or_create_tenant(supabase_user_id: str, email: str) -> int:
    """Every authenticated Supabase user maps to exactly one tenant row. Created
    automatically on first authenticated request — no separate signup step needed
    on our side, since Supabase already owns the actual account.
    ON CONFLICT DO NOTHING makes this safe under concurrent first requests."""
    conn = get_db()
    conn.execute(
        "INSERT INTO tenants (email, supabase_user_id) VALUES (%s, %s) ON CONFLICT (supabase_user_id) DO NOTHING",
        (email, supabase_user_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM tenants WHERE supabase_user_id = %s", (supabase_user_id,)
    ).fetchone()
    conn.close()
    return row["id"]


def create_widget(tenant_id: int, type_: str, title: str, fields: list, button_text: str) -> dict:
    conn = get_db()
    row = conn.execute(
        """INSERT INTO widgets (tenant_id, type, title, fields, button_text)
           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (tenant_id, type_, title, psycopg.types.json.Json(fields), button_text),
    ).fetchone()
    conn.commit()
    conn.close()
    return row


def list_widgets_for_tenant(tenant_id: int) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM widgets WHERE tenant_id = %s ORDER BY id", (tenant_id,)).fetchall()
    conn.close()
    return rows


def get_widget_for_tenant(widget_id: int, tenant_id: int):
    """Tenant isolation enforced at the query level: a widget belonging to a
    different tenant simply doesn't match this WHERE clause — it isn't just
    hidden by the UI, it's genuinely unreachable via this query."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM widgets WHERE id = %s AND tenant_id = %s", (widget_id, tenant_id)
    ).fetchone()
    conn.close()
    return row


def update_widget_for_tenant(widget_id: int, tenant_id: int, title=None, button_text=None):
    if title is None and button_text is None:
        return None
    conn = get_db()
    row = conn.execute(
        """UPDATE widgets
           SET title = COALESCE(%s, title),
               button_text = COALESCE(%s, button_text),
               bundle_version = bundle_version + 1
           WHERE id = %s AND tenant_id = %s RETURNING *""",
        (title, button_text, widget_id, tenant_id),
    ).fetchone()
    conn.commit()
    conn.close()
    return row


def delete_widget_for_tenant(widget_id: int, tenant_id: int) -> bool:
    conn = get_db()
    cursor = conn.execute(
        "DELETE FROM widgets WHERE id = %s AND tenant_id = %s", (widget_id, tenant_id)
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def count_submissions_for_widget(widget_id: int, tenant_id: int) -> int:
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM submissions WHERE widget_id = %s AND tenant_id = %s",
        (widget_id, tenant_id),
    ).fetchone()["count"]
    conn.close()
    return count


def get_widget_by_id(widget_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM widgets WHERE id = %s", (widget_id,)).fetchone()
    conn.close()
    return row


def insert_submission(widget_id: int, tenant_id: int, data: dict, ip: str, country, city, spam_flag: bool,
                      idempotency_key: str | None = None) -> tuple[dict, bool]:
    """Insert a submission. With an idempotency_key, a retried insert returns the
    existing row instead of creating a duplicate. Returns (row, deduplicated)."""
    conn = get_db()
    deduplicated = False

    if idempotency_key:
        row = conn.execute(
            """INSERT INTO submissions (widget_id, tenant_id, data, ip_address, country, city, spam_flag, idempotency_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
               RETURNING id, created_at""",
            (widget_id, tenant_id, psycopg.types.json.Json(data), ip, country, city, spam_flag, idempotency_key),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id, created_at FROM submissions WHERE tenant_id = %s AND idempotency_key = %s",
                (tenant_id, idempotency_key),
            ).fetchone()
            deduplicated = True
    else:
        row = conn.execute(
            """INSERT INTO submissions (widget_id, tenant_id, data, ip_address, country, city, spam_flag)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, created_at""",
            (widget_id, tenant_id, psycopg.types.json.Json(data), ip, country, city, spam_flag),
        ).fetchone()

    conn.commit()
    conn.close()
    return row, deduplicated


if __name__ == "__main__":
    init_db()
    seed_test_widget()