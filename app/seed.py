import json
from pathlib import Path

import bcrypt
from sqlalchemy import select

from .importer import import_csv
from .models import SessionLocal, User

SEED = Path(__file__).resolve().parent.parent / "seed"


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


if __name__ == "__main__":
    with SessionLocal() as s:
        for u in json.loads((SEED / "users.json").read_text()):
            if not s.scalar(select(User).where(User.email == u["email"])):
                s.add(User(email=u["email"], name=u["name"], role=u["role"],
                           organisation=u.get("organisation"), password_hash=hash_pw(u["password"])))
        s.commit()
        rep = import_csv(s, (SEED / "episodes.csv").read_text(encoding="utf-8-sig"))
        print("seed import:", {k: v for k, v in rep.items() if k != "details"})
