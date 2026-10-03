import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
_base = urlsplit(os.environ.get("DATABASE_URL", "postgresql+psycopg2://postgres:12345@localhost:5432/desk"))
os.environ["DATABASE_URL"] = urlunsplit(_base._replace(path="/desk_test"))  # never touch the dev database

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

_admin = create_engine(urlunsplit(_base._replace(path="/postgres")), isolation_level="AUTOCOMMIT")
with _admin.connect() as c:
    if not c.scalar(text("select 1 from pg_database where datname='desk_test'")):
        c.execute(text("create database desk_test"))
cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "migrations"))
command.upgrade(cfg, "head")  # tests exercise the real migrations

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models import Episode, SessionLocal, User, engine  # noqa: E402
from app.seed import hash_pw  # noqa: E402

PW = "pw12345pw"


@pytest.fixture(autouse=True)
def clean():
    with engine.begin() as c:
        c.execute(text("TRUNCATE assignments, status_history, requests, episodes, users RESTART IDENTITY CASCADE"))
        pw = hash_pw(PW)
        for email, role in [("admin", "admin"), ("ops", "operator"), ("a", "client"), ("b", "client")]:
            c.execute(User.__table__.insert().values(email=f"{email}@t.io", name=email, role=role, password_hash=pw, active=True))
    yield


@pytest.fixture
def api():
    return TestClient(app)


@pytest.fixture
def auth(api):
    def _h(who):
        r = api.post("/api/auth/login", json={"email": f"{who}@t.io", "password": PW})
        assert r.status_code == 200
        return {"Authorization": f"Bearer {r.json()['token']}"}
    return _h


@pytest.fixture
def add_episodes():
    def _add(*specs):  # (episode_id, quality[, task])
        with SessionLocal() as s:
            for i, sp in enumerate(specs):
                s.add(Episode(episode_id=sp[0], robot_id="arm-01", task_name=sp[2] if len(sp) > 2 else "pick cup",
                              recorded_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc), duration_seconds=30, operator_name="X", quality=sp[1]))
            s.commit()
    return _add
