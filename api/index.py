"""Vercel entry point: prepare the database on cold start, then serve the FastAPI app."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import seed  # noqa: E402
from app.models import engine  # noqa: E402


def _prepare_db():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    # Several instances can cold-start at once; the advisory lock makes them take turns.
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(726461)"))
        try:
            command.upgrade(cfg, "head")
            seed.run()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(726461)"))


_prepare_db()

from app.main import app  # noqa: E402,F401
