# Dataset Request Desk

Internal platform replacing the episode/request spreadsheets. FastAPI + PostgreSQL 16 + Alembic, with a dependency-free single-page UI served by the API. 

## Run it (needs Docker)
```bash
docker compose up --build
```
Open **http://localhost:8000**. On start the API container runs migrations, creates the seed users and imports `seed/episodes.csv` (safe to restart: both steps are idempotent).
Postgres is also exposed on `localhost:5433` (user `postgres`, password `12345`, db `desk`).

## Seed accounts (from `seed/users.json`; stored bcrypt-hashed)
| Role | Email | Password |
|---|---|---|
| admin | admin@example.com | admin123 |
| operator | ops1@example.com / ops2@example.com | ops123 |
| client | client-a@example.com / client-b@example.com | client123 |

## Tests
```bash
docker compose run --rm api pytest -q
```
Tests use a separate `desk_test` database (created automatically, built with the real migrations), so dev data is never touched. Without Docker: start any Postgres, set `DATABASE_URL`, run `pytest -q`.

## Useful commands
```bash
docker compose exec api python -m app.importer seed/episodes.csv   # CLI import with a printed report
python3 seed/generate_episodes.py 200000 > /tmp/big.csv            # then upload it in the Import tab
```
API docs: http://localhost:8000/docs · Health: `GET /health` · One JSON log line per request on stdout (`docker compose logs -f api`).

## Try the flow
1. Sign in as **Client A**, create a request (e.g. "pick cup", 3 episodes).
2. Sign in as **Operator**: open the request, *Start work*, tick 3 episodes in the picker, *Assign*, *Mark delivered*.
3. Back as **Client A**: accept or reject. Client B cannot see Client A's request.

## Scope notes
- Stretch item: **none chosen** (deliberately; correctness and tests first).
- Admin user management (create/deactivate/change role) exists in the API (`/api/users`, see `/docs`) but has no UI screen.
- Set a real `JWT_SECRET` outside local use.
