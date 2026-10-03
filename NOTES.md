# NOTES

## 1. Design
**Model:** `users` (role, bcrypt hash, active) · `episodes` (unique `episode_id`) · `requests` (client, status) · `status_history` (from, to, actor, time: append-only audit trail) · `assignments` (`episode_pk` **UNIQUE**).
State lives in Postgres only; the API is stateless (JWT), so it can scale horizontally.

**Hard decisions**
1. *One-request-per-episode rule:* enforced by a UNIQUE constraint on `assignments.episode_pk`, with an app-level pre-check for friendly errors. A race between two operators then fails safely (409) instead of double-assigning.
2. *Import policy:* a row is rejected rather than "repaired" when repair would be guessing (unknown robot, missing operator, `45.5` or `-5` duration, `excellent` quality, unparseable date, wrong column count). Safe normalisation is applied: trim, lowercase robot/quality/task name (`"  Pick Cup "` → `pick cup`), `YYYY-MM-DD HH:MM:SS`, `Z` suffix, and `dd/mm/yyyy` (never mm/dd). Naive timestamps are treated as UTC. Duplicate ids inside a file: first valid row wins. Idempotency: `INSERT … ON CONFLICT DO NOTHING`, so a re-run imports 0 and reports "already in database".
3. *Who may do what:* only clients create requests (brief lists it under client). Accept/reject is client-only, even admins cannot, to keep the client's sign-off meaningful. Clients get 404 (not 403) for other clients' requests. Assignment is only allowed while `in_progress`; "delivered" needs `assigned >= requested` (checked under a row lock).

## 2. Left out / next with two days
No stretch item. No admin UI, pagination (lists cap at 500/100), rate limiting, password reset, refresh tokens, or CI caching. Next: SSE for live status, per-client email notifications, request-level audit view, an episode "export" job, e2e browser tests.

## 3. Something that went wrong
*(Fill in with your own experience: this section must be yours. Candidates to check against what really happened: Postgres not ready when the API started, which is why compose uses a healthcheck; timezone handling of `date_trunc`, which is why connections are pinned to UTC.)*

## 4. Security
Passwords: bcrypt (per-user salt), never stored plain; login does a dummy hash for unknown emails to blunt user enumeration. Tokens: 8 h HS256 JWT, but the user row is re-checked on every request, so deactivation is immediate. Validation: pydantic on every body, SQLAlchemy parameters (no string SQL), 20 MB import cap, UI escapes all dynamic text.
Worries: (1) JWT in `sessionStorage` is readable by any XSS, and a leaked/default `JWT_SECRET` lets someone mint admin tokens; move to httpOnly cookies + CSRF protection and a secrets manager. (2) No login throttling → credential stuffing; add rate limits/lockout. Also: the demo-account chips on the login page must be removed outside local use.

## 5. Scale
Analytics are pure SQL (`GROUP BY`, `percentile_cont`), supported by indexes on `(recorded_at, robot_id)` and `(task_name, quality)`. At 5 M episodes the range is index-filtered, but per-day/robot and top-task scans over a wide range still touch millions of rows → add pre-aggregated daily rollups (materialised view refreshed on import) and partition `episodes` by month. At 100× episodes the first thing to break is the picker (`ORDER BY episode_id LIMIT`, anti-join on assignments) → keyset pagination and a partial index on unassigned good/usable rows. At 10× users: the 500-row request list (needs pagination), per-request `bcrypt` cost on login, and the single API process (run several behind a load balancer; state is already external).

## 6. AI tooling
Built with Claude (Anthropic) generating the first draft of the backend, tests, UI and these notes from the brief. **I must review, run and be able to explain every line before submitting.** Nothing was executed against a live Postgres by the assistant, so run the tests first.
