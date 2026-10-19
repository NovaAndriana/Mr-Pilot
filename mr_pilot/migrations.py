"""Database-migration checks on an MR diff (golang-migrate style: <version>_<name>.up.sql / .down.sql).

Only a missing pair or a duplicate version is an error (posted to GitLab). Everything else is a heads-up for
the lead on Telegram/dashboard: renamed or edited migrations, seeds that fail when the row already exists,
DROPs that break the running version during a rolling deploy, and the reminder that golang-migrate only runs
versions ABOVE the one already applied."""
import re

from .standards import added_lines, violation

MIGRATION_DIR = re.compile(r"(^|/)(migrations?|migrate|db/migrat\w*)/", re.I)
_FILE = re.compile(r"^(\d{3,14})_(.+?)\.(up|down)\.sql$", re.I)
_INSERT = re.compile(r"^\s*INSERT\s+INTO\b", re.I)
_SAFE_INSERT = re.compile(r"ON\s+CONFLICT|WHERE\s+NOT\s+EXISTS", re.I)
_DROP = re.compile(r"^\s*(ALTER\s+TABLE\s+\S+\s+DROP\s+(COLUMN\s+)?(?!CONSTRAINT|DEFAULT|NOT\b)\S+|DROP\s+TABLE)\b", re.I)


def _parts(path):
    m = _FILE.match(path.rsplit("/", 1)[-1])
    return (m.group(1), m.group(2), m.group(3).lower()) if m else None


def check(diffs):
    """-> list of violations (source "rule"), at most a handful."""
    out, new_files, versions = [], {}, {}
    for d in diffs or []:
        path = d.get("new_path") or d.get("old_path") or ""
        if not MIGRATION_DIR.search(path) or d.get("deleted_file"):
            continue
        p = _parts(path)
        if not p:
            continue
        ver, name, kind = p
        if d.get("new_file") or d.get("renamed_file"):
            new_files.setdefault((ver, name), set()).add(kind)
            versions.setdefault(ver, set()).add(name)
        old = _parts(d.get("old_path") or "") if d.get("renamed_file") else None
        if old and old[0] != ver and kind == "up":
            out.append(violation(
                "migration-renamed", "warning",
                f"Migrasi `{old[0]}_{old[1]}` dinomori ulang jadi `{ver}`.", path, None, snippet=path,
                why="golang-migrate mencatat versi per environment. Kalau versi lama sudah pernah jalan di salah satu "
                    "environment, file dengan nomor baru akan dijalankan lagi di sana.",
                fix="Pastikan versi lama belum pernah jalan di environment mana pun (cek `schema_migrations`), dan "
                    "file-nya aman dijalankan ulang (IF NOT EXISTS)."))
        if not d.get("new_file") and not d.get("renamed_file") and kind == "up":
            out.append(violation(
                "migration-edited", "warning",
                f"Migrasi lama `{ver}_{name}` diubah isinya.", path, None, snippet=path,
                why="Environment yang sudah menjalankan versi ini tidak akan menjalankannya lagi, jadi perubahannya "
                    "tidak sampai ke sana dan isi DB antar environment jadi beda.",
                fix="Kalau versi ini sudah pernah jalan di mana pun, buat migrasi baru untuk perubahannya."))
        if kind != "up":
            continue
        added = list(added_lines(d.get("diff")))
        text = "\n".join(t for _, t in added)
        for n, t in added:
            if _INSERT.match(t) and not _SAFE_INSERT.search(text):
                out.append(violation(
                    "migration-seed-not-idempotent", "warning",
                    f"Seed di `{ver}_{name}` memakai INSERT tanpa ON CONFLICT.", path, n, snippet=t,
                    why="Kalau datanya sudah ada (misalnya pernah dimasukkan manual), migrasi gagal dan status DB "
                        "jadi dirty, sehingga semua migrasi berikutnya macet.",
                    fix="Tambahkan `ON CONFLICT (...) DO NOTHING` / `DO UPDATE`, atau `WHERE NOT EXISTS`."))
                break
        for n, t in added:
            if _DROP.match(t):
                out.append(violation(
                    "migration-drop", "warning",
                    f"`{t.strip()[:80]}` di migrasi `{ver}`.", path, n, snippet=t,
                    why="Saat rolling deploy, pod versi lama masih jalan dan masih memakai kolom/tabel itu, jadi "
                        "request-nya error sampai semua pod baru siap. Datanya juga hilang permanen.",
                    fix="Hapus dalam dua tahap: rilis kode yang tidak lagi memakainya dulu, baru DROP di rilis berikutnya."))
                break
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
    for ver, names in sorted(versions.items()):
        if len(names) > 1:
            out.append(violation(
                "migration-duplicate-version", "error",
                f"Ada {len(names)} migrasi dengan nomor versi yang sama `{ver}`: {', '.join(sorted(names))}.", "", None,
                snippet=f"dup:{ver}",
                why="golang-migrate menolak seluruh folder kalau ada versi ganda, jadi tidak ada migrasi yang bisa jalan.",
                fix="Beri nomor versi yang berbeda pada salah satunya."))
    return out[:8]
