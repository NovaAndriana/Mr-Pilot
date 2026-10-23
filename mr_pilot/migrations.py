"""Database-migration checks on an MR diff.

Two formats are recognised:
- golang-migrate: `<version>_<name>.up.sql` + `.down.sql`
- sql-migrate:    `<version>-<name>.sql` with `-- +migrate Up` / `-- +migrate Down` sections

Errors (posted to the MR) are the things that break a deploy:
- a new migration that is not idempotent (ADD COLUMN / CREATE TABLE without IF NOT EXISTS, INSERT without
  ON CONFLICT, ...): if the column or row is already in staging, e.g. added by hand, the migration fails, the
  database is left dirty and every later migration is stuck until someone forces the version;
- golang-migrate: a version older than the last migration on the target branch (silently skipped), a missing
  up/down pair, or a duplicate version;
- sql-migrate: a file without `-- +migrate Up`, a `$$` block without StatementBegin/End, CONCURRENTLY inside
  the migration transaction.
Everything else (renamed or edited old migrations, DROPs) is a heads-up for the lead on Telegram/dashboard."""
import re

from .standards import added_lines, violation

MIGRATION_DIR = re.compile(r"(^|/)(migrations?|migrate|db/migrat\w*)/", re.I)
_GM = re.compile(r"^(\d{3,20})_(.+?)\.(up|down)\.sql$", re.I)
_SM = re.compile(r"^(\d{3,20})-(.+?)\.sql$", re.I)
_SM_UP = re.compile(r"^\s*--\s*\+migrate\s+Up\b(.*)$", re.I)
_SM_DOWN = re.compile(r"^\s*--\s*\+migrate\s+Down\b", re.I)
_SM_BEGIN = re.compile(r"^\s*--\s*\+migrate\s+StatementBegin\b", re.I)
_INSERT = re.compile(r"^\s*INSERT\s+INTO\b", re.I)
_SAFE_INSERT = re.compile(r"ON\s+CONFLICT|WHERE\s+NOT\s+EXISTS", re.I)
_DROP = re.compile(r"^\s*(ALTER\s+TABLE\s+\S+\s+DROP\s+(COLUMN\s+)?(?!CONSTRAINT|DEFAULT|NOT\b)\S+|DROP\s+TABLE)\b", re.I)

# (pattern on one statement, short label, how to make it re-runnable)
_NOT_IDEMPOTENT = [
    (re.compile(r"^CREATE\s+(TEMP(ORARY)?\s+|UNLOGGED\s+)?TABLE\s+(?!IF\s+NOT\s+EXISTS)", re.I),
     "CREATE TABLE", "`CREATE TABLE IF NOT EXISTS ...`"),
    (re.compile(r"^CREATE\s+(UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(?!IF\s+NOT\s+EXISTS|CONCURRENTLY\b)\w", re.I),
     "CREATE INDEX", "`CREATE INDEX IF NOT EXISTS ...`"),
    (re.compile(r"^CREATE\s+(SEQUENCE|SCHEMA|EXTENSION)\s+(?!IF\s+NOT\s+EXISTS)", re.I),
     "CREATE {1}", "`CREATE {1} IF NOT EXISTS ...`"),
    (re.compile(r"^CREATE\s+TYPE\b", re.I),
     "CREATE TYPE", "bungkus dengan `DO $$ BEGIN CREATE TYPE ...; EXCEPTION WHEN duplicate_object THEN NULL; END $$;`"),
    (re.compile(r"^ALTER\s+TABLE\b.*\bADD\s+(COLUMN\s+)?(?!IF\s+NOT\s+EXISTS|CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|CHECK"
                r"|EXCLUDE|COLUMN\b)\w", re.I),
     "ADD COLUMN", "`ADD COLUMN IF NOT EXISTS ...`"),
    (re.compile(r"^ALTER\s+TABLE\b.*\bADD\s+CONSTRAINT\b", re.I),
     "ADD CONSTRAINT", "hapus dulu dengan `DROP CONSTRAINT IF EXISTS <nama>;` di baris sebelumnya"),
    (re.compile(r"^CREATE\s+TRIGGER\b", re.I),
     "CREATE TRIGGER", "`DROP TRIGGER IF EXISTS <nama> ON <tabel>;` sebelumnya, atau `CREATE OR REPLACE TRIGGER`"),
    (re.compile(r"^CREATE\s+(MATERIALIZED\s+)?VIEW\s+(?!IF\s+NOT\s+EXISTS)", re.I),
     "CREATE VIEW", "`CREATE OR REPLACE VIEW ...` (materialized: `IF NOT EXISTS`)"),
    (re.compile(r"^ALTER\s+TABLE\b.*\bRENAME\b", re.I),
     "RENAME", "bungkus dengan `DO $$ ... IF EXISTS (SELECT 1 FROM information_schema.columns ...) THEN ... END $$;`"),
    (re.compile(r"^DROP\s+(TABLE|INDEX|SEQUENCE|TYPE|VIEW|FUNCTION|TRIGGER|SCHEMA)\s+(CONCURRENTLY\s+)?(?!IF\s+EXISTS|CONCURRENTLY\b)",
                re.I),
     "DROP {1}", "`DROP {1} IF EXISTS ...`"),
    (re.compile(r"^ALTER\s+TABLE\b.*\bDROP\s+(COLUMN\s+)?(?!IF\s+EXISTS|DEFAULT|NOT\b|CONSTRAINT\b|COLUMN\b"
                r"|IDENTITY\b|EXPRESSION\b)\w", re.I),
     "DROP COLUMN", "`DROP COLUMN IF EXISTS ...`"),
    (re.compile(r"^ALTER\s+TABLE\b.*\bDROP\s+CONSTRAINT\s+(?!IF\s+EXISTS)", re.I),
     "DROP CONSTRAINT", "`DROP CONSTRAINT IF EXISTS ...`"),
]


def _kind(path):
    name = path.rsplit("/", 1)[-1]
    m = _GM.match(name)
    if m:
        return "gm", m.group(1), m.group(2), m.group(3).lower()
    m = _SM.match(name)
    if m:
        return "sm", m.group(1), m.group(2), "up"
    return None


def _parts(path):
    k = _kind(path)
    return (k[1], k[2], k[3]) if k and k[0] == "gm" else None


def _strip_comment(line):
    return re.sub(r"--.*$", "", line)


def statements(lines):
    """[(first line no, statement text)] from [(line no, text)]; `$$ ... $$` bodies are kept whole (and
    marked guarded: what's inside a DO block / function is the author's own logic)."""
    out, buf, start, in_dollar = [], [], None, False
    for n, raw in lines:
        t = raw if in_dollar else _strip_comment(raw)
        if not t.strip() and not buf:
            continue
        if start is None:
            start = n
        buf.append(t)
        if t.count("$$") % 2:
            in_dollar = not in_dollar
        if not in_dollar and t.rstrip().endswith(";"):
            out.append((start, " ".join(" ".join(buf).split())))
            buf, start = [], None
    if buf:
        out.append((start, " ".join(" ".join(buf).split())))
    return out


def not_idempotent(stmts):
    """[(line, statement, label, fix)] for statements that fail when run against a DB that already has the change."""
    found = []
    dropped = " ".join(s for _, s in stmts if re.match(r"^(ALTER\s+TABLE\b.*\bDROP\s+CONSTRAINT|DROP\s+TRIGGER)\s*"
                                                       r"(CONSTRAINT\s+)?IF\s+EXISTS", s, re.I)).lower()
    for n, st in stmts:
        if "$$" in st:
            continue
        guarded = re.search(r"\b(?:ADD\s+CONSTRAINT|CREATE\s+TRIGGER)\s+\"?(\w+)", st, re.I)
        if guarded and re.search(r"\bif\s+exists\s+\"?" + re.escape(guarded.group(1).lower()) + r"\b", dropped):
            continue  # dropped IF EXISTS just before: re-runnable
        if _INSERT.match(st):
            if not _SAFE_INSERT.search(st):
                found.append((n, st, "INSERT tanpa ON CONFLICT",
                              "`... ON CONFLICT (<kolom unik>) DO NOTHING` (atau `DO UPDATE`)"))
            continue
        for rx, label, fix in _NOT_IDEMPOTENT:
            m = rx.search(st)
            if m:
                word = (m.group(1) or "").upper() if "{1}" in label and m.lastindex else ""
                found.append((n, st, label.replace("{1}", word), fix.replace("{1}", word)))
                break
    return found


def _up_lines(added, kind):
    """Lines of the Up part. sql-migrate: between `-- +migrate Up` and `-- +migrate Down`."""
    if kind != "sm":
        return added
    out, up = [], False
    for n, t in added:
        if _SM_UP.match(t):
            up = True
            continue
        if _SM_DOWN.match(t):
            up = False
            continue
        if up:
            out.append((n, t))
    return out


def _idempotent_violation(path, ver, name, found):
    items = []
    for n, st, label, fix in found[:5]:
        items.append(f"- baris {n}: `{st[:90]}{'…' if len(st) > 90 else ''}` → {fix}")
    more = f"\n- dan {len(found) - 5} lagi" if len(found) > 5 else ""
    labels = sorted({f[2] for f in found})
    return violation(
        "migration-not-idempotent", "error",
        f"Migration `{ver}_{name}` belum aman dijalankan ulang ({', '.join(labels)}).", path, found[0][0],
        snippet=f"idempotent:{ver}_{name}",
        why="Kalau perubahan ini sudah ada di database (misalnya kolomnya ditambah manual di staging, atau datanya "
            "sudah di-insert), migration gagal, status `schema_migrations` jadi dirty, dan semua migration "
            "berikutnya ikut macet sampai ada yang force versinya.",
        fix="\n".join(items) + more)


def check(diffs, existing=None, target_branch=""):
    """-> list of violations (source "rule"), at most a handful.
    existing: {migration dir: [file names on the target branch]} for the version-order check (optional)."""
    out, new_files, versions = [], {}, {}
    mine = set()
    for d in diffs or []:
        path = d.get("new_path") or d.get("old_path") or ""
        if MIGRATION_DIR.search(path) and not d.get("deleted_file"):
            mine.add(path.rsplit("/", 1)[-1])
    for d in diffs or []:
        path = d.get("new_path") or d.get("old_path") or ""
        if not MIGRATION_DIR.search(path) or d.get("deleted_file"):
            continue
        k = _kind(path)
        if not k:
            continue
        fmt, ver, name, kind = k
        is_new = bool(d.get("new_file") or d.get("renamed_file"))
        if is_new and fmt == "gm":
            new_files.setdefault((ver, name), set()).add(kind)
        if is_new:
            versions.setdefault(ver, set()).add(name)
        old = _kind(d.get("old_path") or "") if d.get("renamed_file") else None
        if old and old[1] != ver and kind == "up":
            out.append(violation(
                "migration-renamed", "warning",
                f"Migrasi `{old[1]}_{old[2]}` dinomori ulang jadi `{ver}`.", path, None, snippet=path,
                why="Versi migrasi dicatat per environment. Kalau versi lama sudah pernah jalan di salah satu "
                    "environment, file dengan nomor baru akan dijalankan lagi di sana.",
                fix="Pastikan versi lama belum pernah jalan di environment mana pun (cek `schema_migrations`), dan "
                    "file-nya aman dijalankan ulang (IF NOT EXISTS)."))
        if not is_new and kind == "up":
            out.append(violation(
                "migration-edited", "warning",
                f"Migrasi lama `{ver}_{name}` diubah isinya.", path, None, snippet=path,
                why="Environment yang sudah menjalankan versi ini tidak akan menjalankannya lagi, jadi perubahannya "
                    "tidak sampai ke sana dan isi DB antar environment jadi beda.",
                fix="Kalau versi ini sudah pernah jalan di mana pun, buat migrasi baru untuk perubahannya."))
        if kind != "up":
            continue
        added = list(added_lines(d.get("diff")))
        if fmt == "sm":
            out += _sql_migrate_format(path, ver, name, added, is_new)
        up = _up_lines(added, fmt)
        stmts = statements(up)
        if fmt == "gm" and len(stmts) > 1 and any(re.search(r"\bCONCURRENTLY\b", s, re.I) for _, s in stmts):
            n = next(n for n, s in stmts if re.search(r"\bCONCURRENTLY\b", s, re.I))
            out.append(violation(
                "migration-concurrently-in-tx", "error",
                f"`CONCURRENTLY` di `{ver}_{name}` digabung dengan statement lain dalam satu file.", path, n,
                snippet=f"concurrently:{ver}_{name}",
                why="File dengan beberapa statement dijalankan sebagai satu transaksi, sedangkan "
                    "`CREATE INDEX CONCURRENTLY` tidak boleh di dalam transaksi, jadi migration gagal dan DB dirty.",
                fix="Pindahkan `CREATE INDEX CONCURRENTLY` ke file migration sendiri yang hanya berisi statement itu."))
        if is_new:
            found = not_idempotent(stmts)
            if found:
                out.append(_idempotent_violation(path, ver, name, found))
        else:
            for n, t in added:
                if _INSERT.match(t) and not _SAFE_INSERT.search("\n".join(x for _, x in added)):
                    out.append(violation(
                        "migration-seed-not-idempotent", "warning",
                        f"Seed di `{ver}_{name}` memakai INSERT tanpa ON CONFLICT.", path, n, snippet=t,
                        why="Kalau datanya sudah ada (misalnya pernah dimasukkan manual), migrasi gagal dan status DB "
                            "jadi dirty, sehingga semua migrasi berikutnya macet.",
                        fix="Tambahkan `ON CONFLICT (...) DO NOTHING` / `DO UPDATE`, atau `WHERE NOT EXISTS`."))
                    break
        for n, t in up:
            if _DROP.match(t):
                out.append(violation(
                    "migration-drop", "warning",
                    f"`{t.strip()[:80]}` di migrasi `{ver}`.", path, n, snippet=t,
                    why="Saat rolling deploy, pod versi lama masih jalan dan masih memakai kolom/tabel itu, jadi "
                        "request-nya error sampai semua pod baru siap. Datanya juga hilang permanen.",
                    fix="Hapus dalam dua tahap: rilis kode yang tidak lagi memakainya dulu, baru DROP di rilis berikutnya."))
                break
        if is_new and fmt == "gm" and existing:
            out += _older_than_target(path, ver, name, existing, mine, target_branch)
    for (ver, name), kinds in sorted(new_files.items()):
        missing = {"up", "down"} - kinds
        if missing and len(kinds) == 1:
            have = next(iter(kinds))
            out.append(violation(
                "migration-missing-pair", "error",
                f"Migrasi `{ver}_{name}` hanya punya file `.{have}.sql`, pasangan `.{missing.pop()}.sql`-nya tidak ada.",
                next((d.get("new_path") for d in diffs if (d.get("new_path") or "").endswith(f"{ver}_{name}.{have}.sql")), ""),
                None, snippet=f"{ver}_{name}",
                why="Tanpa pasangan up/down, rollback tidak bisa dilakukan dan golang-migrate bisa gagal membaca folder.",
                fix=f"Tambahkan file `{ver}_{name}.{'down' if have == 'up' else 'up'}.sql`."))
    taken = {}
    for names in (existing or {}).values():
        for f in names:
            if f in mine:
                continue
            k = _kind(f)
            if k:
                taken.setdefault(k[1], set()).add(k[2])
    for ver, names in sorted(versions.items()):
        clash = names | taken.get(ver, set())
        if len(clash) > 1:
            where = f" (sudah ada di `{target_branch}`: {', '.join(sorted(taken[ver]))})" if taken.get(ver) else ""
            out.append(violation(
                "migration-duplicate-version", "error",
                f"Ada {len(clash)} migrasi dengan nomor versi yang sama `{ver}`: {', '.join(sorted(clash))}{where}.",
                "", None, snippet=f"dup:{ver}",
                why="Tool migrasi menolak folder yang punya versi ganda, jadi tidak ada migrasi yang bisa jalan.",
                fix="Beri nomor versi yang berbeda (timestamp terbaru) pada migrasi di MR ini."))
    return out[:8]


def _sql_migrate_format(path, ver, name, added, is_new):
    text = [t for _, t in added]
    out = []
    up = next(((n, t) for n, t in added if _SM_UP.match(t)), None)
    if is_new and not up:
        out.append(violation(
            "migration-sqlmigrate-format", "error",
            f"File `{ver}-{name}.sql` tidak punya penanda `-- +migrate Up`.", path, added[0][0] if added else None,
            snippet=f"format:{ver}-{name}",
            why="sql-migrate hanya menjalankan statement di bawah `-- +migrate Up`. Tanpa penanda itu file dianggap "
                "kosong, tercatat sudah jalan, tapi perubahannya tidak pernah masuk ke database.",
            fix="Tambahkan `-- +migrate Up` di baris pertama, dan `-- +migrate Down` sebelum statement rollback."))
    if any("$$" in t for t in text) and not any(_SM_BEGIN.match(t) for t in text):
        n = next(n for n, t in added if "$$" in t)
        out.append(violation(
            "migration-sqlmigrate-statement", "error",
            f"Blok `$$` di `{ver}-{name}.sql` tidak dibungkus `StatementBegin` / `StatementEnd`.", path, n,
            snippet=f"stmt:{ver}-{name}",
            why="sql-migrate memecah file per titik koma, jadi isi function/DO block terpotong di tengah dan "
                "migration gagal dengan syntax error.",
            fix="Bungkus bloknya dengan `-- +migrate StatementBegin` dan `-- +migrate StatementEnd`."))
    if up and "notransaction" not in up[1].lower() and any(re.search(r"\bCONCURRENTLY\b", t, re.I) for t in text):
        n = next(n for n, t in added if re.search(r"\bCONCURRENTLY\b", t, re.I))
        out.append(violation(
            "migration-concurrently-in-tx", "error",
            f"`CONCURRENTLY` di `{ver}-{name}.sql` dijalankan di dalam transaksi.", path, n,
            snippet=f"concurrently:{ver}-{name}",
            why="sql-migrate membungkus setiap file dengan transaksi, sedangkan `CREATE INDEX CONCURRENTLY` tidak "
                "boleh di dalam transaksi, jadi migration gagal.",
            fix="Ganti penandanya jadi `-- +migrate Up notransaction` dan taruh index ini di file sendiri."))
    return out


def _older_than_target(path, ver, name, existing, mine, target_branch):
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    names = existing.get(folder) or []
    vers = [k[1] for f in names if f not in mine for k in [_kind(f)] if k and k[0] == "gm"]
    if not vers:
        return []
    last = max(vers, key=int)
    if int(ver) >= int(last):
        return []
    return [violation(
        "migration-older-than-target", "error",
        f"Versi `{ver}` lebih kecil dari migration terakhir di `{target_branch or 'branch tujuan'}` (`{last}`).",
        path, None, snippet=f"order:{ver}_{name}",
        why="golang-migrate hanya menjalankan versi di atas versi yang sudah tercatat di `schema_migrations`. "
            f"Di environment yang sudah sampai `{last}`, file ini dilewati tanpa error, jadi perubahannya tidak "
            "pernah masuk ke database.",
        fix=f"Ganti nomor versinya (file `.up.sql` dan `.down.sql`) dengan timestamp yang lebih baru dari `{last}`, "
            "misalnya buat ulang dengan `migrate create -ext sql -dir <folder> <nama>`.")]
