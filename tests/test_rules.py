import pytest

from app.importer import import_csv
from app.models import Episode, SessionLocal
from sqlalchemy import func, select

CSV = (ROOT := __import__("pathlib").Path(__file__).resolve().parent.parent) / "seed" / "episodes.csv"


def new_req(api, h, n=1):
    r = api.post("/api/requests", headers=h, json={"task_name": "pick cup", "episodes_requested": n, "deadline": "2099-01-01"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def go(api, h, rid, to):
    return api.post(f"/api/requests/{rid}/transition", headers=h, json={"to": to})


def assign(api, h, rid, *ids):
    return api.post(f"/api/requests/{rid}/assignments", headers=h, json={"episode_ids": list(ids)})


# ---------- authorization ----------
def test_every_endpoint_requires_login(api):
    for method, path in [("get", "/api/requests"), ("get", "/api/episodes"), ("get", "/api/analytics?from=2026-01-01&to=2026-12-31"),
                         ("post", "/api/import/episodes"), ("get", "/api/users")]:
        assert getattr(api, method)(path).status_code == 401
    assert api.get("/health").status_code == 200


def test_client_cannot_use_operator_or_admin_endpoints(api, auth):
    h = auth("a")
    assert api.get("/api/episodes", headers=h).status_code == 403
    assert api.get("/api/analytics?from=2026-01-01&to=2026-12-31", headers=h).status_code == 403
    assert api.post("/api/import/episodes", headers=h, files={"file": ("x.csv", b"a")}).status_code == 403
    assert api.get("/api/users", headers=h).status_code == 403


def test_operator_cannot_manage_users_or_create_requests(api, auth):
    h = auth("ops")
    assert api.post("/api/users", headers=h, json={"email": "z@t.io", "name": "z", "password": "longenough1", "role": "admin"}).status_code == 403
    assert api.post("/api/requests", headers=h, json={"task_name": "x", "episodes_requested": 1, "deadline": "2099-01-01"}).status_code == 403


def test_client_only_sees_own_requests(api, auth):
    rid = new_req(api, auth("a"))
    assert [r["id"] for r in api.get("/api/requests", headers=auth("a")).json()] == [rid]
    assert api.get("/api/requests", headers=auth("b")).json() == []
    assert api.get(f"/api/requests/{rid}", headers=auth("b")).status_code == 404
    assert go(api, auth("b"), rid, "accepted").status_code == 404
    assert len(api.get("/api/requests", headers=auth("ops")).json()) == 1


def test_deactivated_user_loses_access_immediately(api, auth):
    h = auth("a")
    uid = next(u["id"] for u in api.get("/api/users", headers=auth("admin")).json() if u["email"] == "a@t.io")
    assert api.patch(f"/api/users/{uid}", headers=auth("admin"), json={"active": False}).status_code == 200
    assert api.get("/api/requests", headers=h).status_code == 401


# ---------- status transitions ----------
def test_full_workflow_with_rework_and_audit_trail(api, auth, add_episodes):
    add_episodes(("E1", "good"))
    rid = new_req(api, auth("a"))
    assert go(api, auth("ops"), rid, "in_progress").status_code == 200
    assert assign(api, auth("ops"), rid, "E1").status_code == 201
    assert go(api, auth("ops"), rid, "delivered").status_code == 200
    assert go(api, auth("a"), rid, "rejected").status_code == 200
    assert go(api, auth("ops"), rid, "in_progress").status_code == 200
    assert go(api, auth("ops"), rid, "delivered").status_code == 200
    assert go(api, auth("a"), rid, "accepted").status_code == 200
    hist = api.get(f"/api/requests/{rid}", headers=auth("a")).json()["history"]
    assert [h["to"] for h in hist] == ["submitted", "in_progress", "delivered", "rejected", "in_progress", "delivered", "accepted"]
    assert hist[0]["by"] == "a" and hist[1]["by"] == "ops" and hist[3]["by"] == "a"


@pytest.mark.parametrize("path", [["delivered"], ["accepted"], ["rejected"], ["in_progress", "accepted"], ["in_progress", "submitted"]])
def test_invalid_transitions_rejected(api, auth, path):
    rid = new_req(api, auth("a"))
    for step in path[:-1]:
        assert go(api, auth("ops"), rid, step).status_code == 200
    assert go(api, auth("ops"), rid, path[-1]).status_code == 409


def test_roles_own_their_steps(api, auth, add_episodes):
    add_episodes(("E1", "good"))
    rid = new_req(api, auth("a"))
    assert go(api, auth("a"), rid, "in_progress").status_code == 403       # client can't start work
    go(api, auth("ops"), rid, "in_progress")
    assign(api, auth("ops"), rid, "E1")
    assert go(api, auth("a"), rid, "delivered").status_code == 403         # client can't deliver
    go(api, auth("ops"), rid, "delivered")
    assert go(api, auth("ops"), rid, "accepted").status_code == 403        # operator can't accept
    assert go(api, auth("admin"), rid, "rejected").status_code == 403      # nor can admin


# ---------- assignment rules ----------
def test_only_good_or_usable_assignable(api, auth, add_episodes):
    add_episodes(("G", "good"), ("U", "usable"), ("B", "bad"))
    rid = new_req(api, auth("a"), 2)
    go(api, auth("ops"), rid, "in_progress")
    assert assign(api, auth("ops"), rid, "B").status_code == 422
    assert assign(api, auth("ops"), rid, "G", "U").status_code == 201
    assert assign(api, auth("ops"), rid, "NOPE").status_code == 404


def test_episode_belongs_to_one_request_at_a_time(api, auth, add_episodes):
    add_episodes(("G", "good"))
    r1, r2 = new_req(api, auth("a")), new_req(api, auth("b"))
    for r in (r1, r2):
        go(api, auth("ops"), r, "in_progress")
    assert assign(api, auth("ops"), r1, "G").status_code == 201
    assert assign(api, auth("ops"), r2, "G").status_code == 409
    api.delete(f"/api/requests/{r1}/assignments/G", headers=auth("ops"))   # released -> assignable again
    assert assign(api, auth("ops"), r2, "G").status_code == 201


def test_cannot_deliver_until_enough_episodes(api, auth, add_episodes):
    add_episodes(("G1", "good"), ("G2", "good"))
    rid = new_req(api, auth("a"), 2)
    go(api, auth("ops"), rid, "in_progress")
    assign(api, auth("ops"), rid, "G1")
    assert go(api, auth("ops"), rid, "delivered").status_code == 409
    assign(api, auth("ops"), rid, "G2")
    assert go(api, auth("ops"), rid, "delivered").status_code == 200


def test_assignment_requires_in_progress(api, auth, add_episodes):
    add_episodes(("G", "good"))
    rid = new_req(api, auth("a"))
    assert assign(api, auth("ops"), rid, "G").status_code == 409


# ---------- import ----------
def test_import_is_idempotent_and_reports_reasons(api):
    text = CSV.read_text()
    with SessionLocal() as s:
        first = import_csv(s, text)
    with SessionLocal() as s:
        second = import_csv(s, text)
        count = s.scalar(select(func.count()).select_from(Episode))
    assert first["imported"] > 100 and second["imported"] == 0 and count == first["imported"]
    assert second["skipped"] == first["skipped"] + first["imported"]
    reasons = " ".join(first["skipped_by_reason"])
    for expected in ("unknown robot_id", "malformed row", "duplicate in file", "invalid quality", "missing value", "invalid duration_seconds", "invalid recorded_at"):
        assert expected in reasons


def test_import_normalises_messy_values(api):
    csv_text = ("episode_id,robot_id,task_name,recorded_at,duration_seconds,operator_name,quality\n"
                "X1, ARM-01 ,  Pick   Cup ,14/08/2026 09:15,34,Kevin,GOOD\n"
                "X2,arm-02,pick cup,2026-08-14T09:20:00Z,40,Eric,usable\n")
    with SessionLocal() as s:
        assert import_csv(s, csv_text)["imported"] == 2
        e = s.scalar(select(Episode).where(Episode.episode_id == "X1"))
        assert (e.robot_id, e.task_name, e.quality) == ("arm-01", "pick cup", "good")
        assert e.recorded_at.isoformat().startswith("2026-08-14T09:15")


def test_analytics_runs_in_db(api, auth, add_episodes):
    add_episodes(("G1", "good", "fold towel"), ("G2", "good", "fold towel"), ("B1", "bad", "pick cup"))
    r = api.get("/api/analytics?from=2026-09-01&to=2026-09-01", headers=auth("ops")).json()
    assert r["episodes_per_day_per_robot"] == [{"day": "2026-09-01", "robot_id": "arm-01", "count": 3}]
    assert r["top_tasks_by_good_episodes"] == [{"task_name": "fold towel", "good_episodes": 2}]
