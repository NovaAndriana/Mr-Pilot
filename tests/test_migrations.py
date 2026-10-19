"""golang-migrate checks on an MR diff."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mr_pilot.migrations import check  # noqa: E402

M = "core/database/migrations/"


def new(name, body="+CREATE TABLE IF NOT EXISTS x (id int);\n"):
    return {"new_path": M + name, "old_path": M + name, "new_file": True, "diff": "@@ -0,0 +1,1 @@\n" + body}


def rules(vs):
    return sorted((v["rule"], v["severity"]) for v in vs)


def test_idas_5413_case_renumber_and_edited_seed():
    """The real MR: duplicate versions renumbered, older seeds edited, new table + seed with ON CONFLICT."""
    diffs = [
        {"old_path": M + "20260918000000_add_table_document_action_history.up.sql",
         "new_path": M + "20260918000002_add_table_document_action_history.up.sql", "renamed_file": True, "diff": ""},
        {"old_path": M + "20260918000000_add_table_document_action_history.down.sql",
         "new_path": M + "20260918000002_add_table_document_action_history.down.sql", "renamed_file": True, "diff": ""},
        {"old_path": M + "20261002000000_seed_quota.up.sql", "new_path": M + "20261002000000_seed_quota.up.sql",
         "diff": "@@ -1,2 +1,2 @@\n-('ekyc', 'quota-ekyc-custom')\n+('ekyc', 'Kuota Sertifikat (E-KYC)')\n"},
        new("20261019000000_product_quota_item.up.sql",
            "+INSERT INTO product (code) VALUES ('QTA-STARTER-BUNDLE') ON CONFLICT (code) DO NOTHING;\n"),
        new("20261019000000_product_quota_item.down.sql", "+DROP TABLE IF EXISTS product_quota_item;\n"),
    ]
    vs = check(diffs)
    assert rules(vs) == [("migration-edited", "warning"), ("migration-renamed", "warning")], rules(vs)
    assert all(v["severity"] != "error" for v in vs), "tidak ada yang perlu dikomentari di GitLab"
    assert "20260918000000" in next(v for v in vs if v["rule"] == "migration-renamed")["message"]


def test_errors_missing_pair_and_duplicate_version():
    vs = check([new("20261020000000_a.up.sql"), new("20261020000000_b.up.sql"), new("20261020000000_b.down.sql")])
    assert ("migration-missing-pair", "error") in rules(vs) and ("migration-duplicate-version", "error") in rules(vs)
    pair = next(v for v in vs if v["rule"] == "migration-missing-pair")
    assert "20261020000000_a.down.sql" in pair["fix"] and pair["path"].endswith("_a.up.sql")


def test_seed_without_on_conflict_and_drop():
    vs = check([new("20261021000000_s.up.sql", "+INSERT INTO product (code) VALUES ('X');\n"
                                               "+ALTER TABLE product DROP COLUMN legacy_code;\n"),
                new("20261021000000_s.down.sql", "+DELETE FROM product WHERE code='X';\n")])
    assert rules(vs) == [("migration-drop", "warning"), ("migration-seed-not-idempotent", "warning")]
    assert check([new("20261022000000_t.up.sql", "+ALTER TABLE p DROP CONSTRAINT p_fk;\n"),
                  new("20261022000000_t.down.sql")]) == [], "DROP CONSTRAINT bukan hapus data"


def test_non_migration_files_ignored():
    assert check([{"new_path": "web/src/migrate.ts", "new_file": True, "diff": "+x\n"},
                  {"new_path": "docs/migrations.md", "diff": "+x\n"}]) == []
