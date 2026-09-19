"""Apply pending SQL migrations in order, tracking applied ones in schema_migrations."""

import glob
import os

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:dev@localhost:5433/widgets")
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def run_migrations(url: str | None = None) -> list[str]:
    conn = psycopg.connect(url or DATABASE_URL, row_factory=dict_row)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
    )
    conn.commit()

    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    pending = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))

    newly_applied = []
    for path in pending:
        version = os.path.basename(path)
        if version in applied:
            continue
        sql = open(path, encoding="utf-8").read()
        with conn.transaction():
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        newly_applied.append(version)
        print(f"applied {version}")

    # The earlier "SELECT version FROM schema_migrations" leaves an implicit open
    # transaction on the connection (autocommit is off), so `conn.transaction()`
    # only opens a savepoint on top of it. Without this commit, close() rolls
    # back everything the loop just ran.
    conn.commit()
    conn.close()
    return newly_applied


if __name__ == "__main__":
    run_migrations()