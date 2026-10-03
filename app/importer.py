"""Idempotent, validating CSV import. CLI: python -m app.importer path/to/episodes.csv"""
import csv
import io
import sys
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert

from .models import Episode, SessionLocal

COLUMNS = ["episode_id", "robot_id", "task_name", "recorded_at", "duration_seconds", "operator_name", "quality"]
ROBOTS = {"arm-01", "arm-02", "arm-03", "mobile-01", "humanoid-01"}
QUALITIES = {"good", "usable", "bad"}
DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M")  # dd/mm, never mm/dd


def parse_dt(v: str) -> datetime:
    v = v[:-1] if v.endswith("Z") else v  # naive and Z timestamps are both treated as UTC
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError


def clean_row(raw: list[str]) -> tuple[dict | None, str | None]:
    if len(raw) != len(COLUMNS):
        return None, f"malformed row: expected {len(COLUMNS)} columns, got {len(raw)}"
    r = dict(zip(COLUMNS, (c.strip() for c in raw)))
    for col in COLUMNS:
        if not r[col]:
            return None, f"missing value: {col}"
    r["robot_id"] = r["robot_id"].lower()
    if r["robot_id"] not in ROBOTS:
        return None, f"unknown robot_id: {r['robot_id']}"
    r["task_name"] = " ".join(r["task_name"].lower().split())
    r["quality"] = r["quality"].lower()
    if r["quality"] not in QUALITIES:
        return None, f"invalid quality: {r['quality']}"
    if not r["duration_seconds"].isdigit() or int(r["duration_seconds"]) <= 0:
        return None, f"invalid duration_seconds: {r['duration_seconds']}"
    r["duration_seconds"] = int(r["duration_seconds"])
    try:
        r["recorded_at"] = parse_dt(r["recorded_at"])
    except ValueError:
        return None, f"invalid recorded_at: {r['recorded_at']}"
    return r, None


def import_csv(session, text: str) -> dict:
    reader = csv.reader(io.StringIO(text, newline=""))
    next(reader, None)  # header
    valid, seen, skipped, total = [], set(), [], 0
    for raw in reader:
        total += 1
        line = reader.line_num
        if not any(c.strip() for c in raw):
            skipped.append((line, "", "blank row"))
            continue
        row, err = clean_row(raw)
        if err:
            skipped.append((line, raw[0].strip() if raw else "", err))
        elif row["episode_id"] in seen:
            skipped.append((line, row["episode_id"], "duplicate in file: first occurrence kept"))
        else:
            seen.add(row["episode_id"])
            valid.append((line, row))
    inserted: set[str] = set()
    for i in range(0, len(valid), 5000):
        chunk = [r for _, r in valid[i : i + 5000]]
        stmt = insert(Episode).values(chunk).on_conflict_do_nothing(index_elements=["episode_id"])
        inserted |= set(session.scalars(stmt.returning(Episode.episode_id)))
    session.commit()
    for line, r in valid:
        if r["episode_id"] not in inserted:
            skipped.append((line, r["episode_id"], "already in database"))
    skipped.sort()
    return {
        "total_rows": total,
        "imported": len(inserted),
        "skipped": len(skipped),
        "skipped_by_reason": dict(Counter(s[2].split(":")[0] for s in skipped)),
        "details": [{"line": l, "episode_id": e, "reason": why} for l, e, why in skipped[:300]],
    }


if __name__ == "__main__":
    with SessionLocal() as s, open(sys.argv[1], newline="", encoding="utf-8-sig") as f:
        rep = import_csv(s, f.read())
    print({k: v for k, v in rep.items() if k != "details"})
    for d in rep["details"]:
        print(f"  line {d['line']}: {d['episode_id'] or '-'} -> {d['reason']}")
