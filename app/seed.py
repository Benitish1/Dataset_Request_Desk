import json
import os
from pathlib import Path

import bcrypt
from sqlalchemy import select

from .importer import import_csv
from .models import SessionLocal, User

SEED = Path(__file__).resolve().parent.parent / "seed"


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def seed_users(s):
    users = json.loads((SEED / "users.json").read_text())
    # In deployments, ADMIN_EMAIL / ADMIN_PASSWORD replace the public demo admin.
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_pw = os.getenv("ADMIN_PASSWORD", "")
    if admin_email and admin_pw:
        demo_admins = [u["email"] for u in users if u["role"] == "admin"]
        users = [u for u in users if u["role"] != "admin"]
        admin = s.scalar(select(User).where(User.email == admin_email))
        if not admin:
            s.add(User(email=admin_email, name="Admin", role="admin", password_hash=hash_pw(admin_pw)))
        elif not bcrypt.checkpw(admin_pw.encode(), admin.password_hash.encode()):
            admin.password_hash = hash_pw(admin_pw)  # rotate when the env var changes
        if admin:
            admin.role, admin.active = "admin", True
        for old in s.scalars(select(User).where(User.email.in_(demo_admins), User.email != admin_email)):
            old.active = False  # lock out admin@example.com if a previous deploy created it
    for u in users:
        if not s.scalar(select(User).where(User.email == u["email"])):
            s.add(User(email=u["email"], name=u["name"], role=u["role"],
                       organisation=u.get("organisation"), password_hash=hash_pw(u["password"])))
    s.commit()


def run():
    with SessionLocal() as s:
        seed_users(s)
        rep = import_csv(s, (SEED / "episodes.csv").read_text(encoding="utf-8-sig"))
        print("seed import:", {k: v for k, v in rep.items() if k != "details"})


if __name__ == "__main__":
    run()
