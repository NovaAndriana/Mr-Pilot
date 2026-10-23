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
    assert rules(vs) == [("migration-drop", "warning"), ("migration-not-idempotent", "error")]
    v = next(x for x in vs if x["rule"] == "migration-not-idempotent")
    assert "ON CONFLICT" in v["fix"] and "DROP COLUMN IF EXISTS" in v["fix"] and v["line"] == 1
    assert check([new("20261022000000_t.up.sql", "+ALTER TABLE p DROP CONSTRAINT IF EXISTS p_fk;\n"),
                  new("20261022000000_t.down.sql")]) == [], "DROP CONSTRAINT bukan hapus data"
    # an old seed edited: still only a heads-up
    edited = {"new_path": M + "20261002000000_seed.up.sql", "old_path": M + "20261002000000_seed.up.sql",
              "diff": "@@ -1,1 +1,1 @@\n-x\n+INSERT INTO product (code) VALUES ('Y');\n"}
    assert ("migration-seed-not-idempotent", "warning") in rules(check([edited]))


def test_non_migration_files_ignored():
    assert check([{"new_path": "web/src/migrate.ts", "new_file": True, "diff": "+x\n"},
                  {"new_path": "docs/migrations.md", "diff": "+x\n"}]) == []


SM = "migration/"


def sm(name, body):
    return {"new_path": SM + name, "old_path": SM + name, "new_file": True,
            "diff": f"@@ -0,0 +1,{body.count(chr(10))} @@\n" + "".join("+" + x + "\n" for x in body.splitlines())}


def test_staging_failure_add_column_without_if_not_exists():
    """be-staging: `alter table companys add username` failed because the column was added by hand in staging."""
    vs = check([sm("20261020185200001-alter_company_add_username_passphrase.sql",
                   "-- +migrate Up\nalter table companys add username varchar(100);\n"
                   "alter table companys\n  add column if not exists passphrase text;\n"
                   "-- +migrate Down\nalter table companys drop column username;\n")])
    assert rules(vs) == [("migration-not-idempotent", "error")]
    v = vs[0]
    assert v["line"] == 2 and "ADD COLUMN IF NOT EXISTS" in v["fix"] and "passphrase" not in v["fix"]
    assert "dirty" in v["why"], "Down section is not checked"


def test_safe_migration_is_quiet():
    body = ("-- +migrate Up\nCREATE TABLE IF NOT EXISTS balance_reminder (id bigserial primary key);\n"
            "ALTER TABLE company ADD COLUMN IF NOT EXISTS invitation_name varchar(255);\n"
            "CREATE INDEX IF NOT EXISTS idx_balance_user ON balance (user_id);\n"
            "ALTER TABLE company DROP CONSTRAINT IF EXISTS company_code_uq;\n"
            "ALTER TABLE company ADD CONSTRAINT company_code_uq UNIQUE (code);\n"
            "INSERT INTO notification_template (code, body) VALUES ('X', 'y')\n  ON CONFLICT (code) DO NOTHING;\n"
            "-- +migrate StatementBegin\nDO $$ BEGIN\n  CREATE TYPE st AS ENUM ('a');\n"
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$;\n-- +migrate StatementEnd\n"
            "-- +migrate Down\nDROP TABLE balance_reminder;\n")
    assert check([sm("20261021000000001-safe.sql", body)]) == []


def test_sql_migrate_format_errors():
    vs = check([sm("20261021000000002-nomarker.sql", "CREATE TABLE IF NOT EXISTS a (id int);\n")])
    assert ("migration-sqlmigrate-format", "error") in rules(vs)
    vs = check([sm("20261021000000003-fn.sql", "-- +migrate Up\nCREATE OR REPLACE FUNCTION f() RETURNS trigger AS $$\n"
                                              "BEGIN NEW.x := 1; RETURN NEW; END; $$ LANGUAGE plpgsql;\n")])
    assert ("migration-sqlmigrate-statement", "error") in rules(vs)
    vs = check([sm("20261021000000004-idx.sql", "-- +migrate Up\nCREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (a);\n")])
    assert ("migration-concurrently-in-tx", "error") in rules(vs)
    ok = sm("20261021000000005-idx.sql", "-- +migrate Up notransaction\nCREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (a);\n")
    assert check([ok]) == []


def test_golang_migrate_concurrently_with_other_statements():
    vs = check([new("20261023000000_i.up.sql", "+CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (a);\n"
                                               "+ALTER TABLE t ADD COLUMN IF NOT EXISTS b int;\n"),
                new("20261023000000_i.down.sql")])
    assert rules(vs) == [("migration-concurrently-in-tx", "error")]


def test_version_older_than_target_branch_is_skipped_by_golang_migrate():
    existing = {M.rstrip("/"): ["20260916090000_a.up.sql", "20260916090000_a.down.sql",
                                "20261020000000_b.up.sql", "20261020000000_b.down.sql"]}
    diffs = [new("20261001000000_late.up.sql"), new("20261001000000_late.down.sql")]
    vs = check(diffs, existing, "staging")
    assert rules(vs) == [("migration-older-than-target", "error")]
    assert "`20261020000000`" in vs[0]["message"] and "`staging`" in vs[0]["message"]
    assert check([new("20261024000000_ok.up.sql"), new("20261024000000_ok.down.sql")], existing, "staging") == []
    # same version as a different file already on the branch
    dup = check([new("20261020000000_c.up.sql"), new("20261020000000_c.down.sql")], existing, "staging")
    assert ("migration-duplicate-version", "error") in rules(dup) and "sudah ada di `staging`" in dup[-1]["message"]
