from alembic import context
from sqlalchemy import create_engine

from app.models import DATABASE_URL

engine = create_engine(DATABASE_URL)
with engine.connect() as conn:
    context.configure(connection=conn)
    with context.begin_transaction():
        context.run_migrations()
