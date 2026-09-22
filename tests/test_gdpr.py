from repository import get_submissions_for_export, soft_delete_by_email
import psycopg
from psycopg.rows import dict_row

# Test export
print("=== GDPR Export (widget 1, tenant 1) ===")
rows = get_submissions_for_export(1, 1)
print("Found %d submissions" % len(rows))
for r in rows:
    print("  ID=%s, consent=%s, confirmed=%s, email=%s" % (
        r["id"], r["consent_given"], r["confirmed_at"] is not None, r["data"].get("email", "?")))

# Test delete by email
print()
print("=== GDPR Delete test@example.com from widget 1 ===")
count = soft_delete_by_email("test@example.com", 1, 1)
print("Deleted %d submissions" % count)

# Verify delete
print()
print("=== Verify deletion ===")
conn = psycopg.connect("postgresql://postgres:dev@127.0.0.1:5433/widgets", row_factory=dict_row)
rows = conn.execute("SELECT id, deleted_at IS NOT NULL as deleted, data FROM submissions WHERE id = 37").fetchall()
for r in rows:
    print("  ID=%s, deleted=%s, data=%s" % (r["id"], r["deleted"], r["data"]))
conn.close()