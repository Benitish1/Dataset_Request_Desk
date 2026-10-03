import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import bcrypt
import jwt
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request as HttpRequest, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from .importer import import_csv
from .models import Assignment, Episode, Request, SessionLocal, StatusHistory, User

SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")
OPS = ("operator", "admin")
Status = Literal["submitted", "in_progress", "delivered", "accepted", "rejected"]
# (from, to) -> roles allowed to perform that step
TRANSITIONS = {
    ("submitted", "in_progress"): OPS,
    ("in_progress", "delivered"): OPS,
    ("delivered", "accepted"): ("client",),
    ("delivered", "rejected"): ("client",),
    ("rejected", "in_progress"): OPS,
}
DUMMY_HASH = bcrypt.hashpw(b"x", bcrypt.gensalt()).decode()

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("desk")
app = FastAPI(title="Dataset Request Desk")


@app.middleware("http")
async def access_log(request: HttpRequest, call_next):
    t0, status = time.perf_counter(), 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    finally:
        log.info(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(), "method": request.method, "path": request.url.path,
            "status": status, "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            "user_id": getattr(request.state, "user_id", None)}))


def db():
    with SessionLocal() as s:
        yield s


bearer = HTTPBearer(auto_error=False)


def current_user(request: HttpRequest, cred: HTTPAuthorizationCredentials | None = Depends(bearer), s=Depends(db)):
    if not cred:
        raise HTTPException(401, "Not authenticated")
    try:
        sub = int(jwt.decode(cred.credentials, SECRET, algorithms=["HS256"])["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(401, "Invalid or expired token")
    user = s.get(User, sub)
    if not user or not user.active:  # deactivation takes effect immediately, not at token expiry
        raise HTTPException(401, "Account disabled")
    request.state.user_id = user.id
    return user


def require(*roles):
    def dep(u: User = Depends(current_user)):
        if u.role not in roles:
            raise HTTPException(403, "Your role cannot do this")
        return u
    return dep


@app.get("/health")
def health(s=Depends(db)):
    try:
        s.execute(text("select 1"))
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "database unavailable")


# ---------- auth & users ----------
class LoginIn(BaseModel):
    email: str
    password: str


def user_out(u: User):
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "organisation": u.organisation, "active": u.active}


@app.post("/api/auth/login")
def login(body: LoginIn, s=Depends(db)):
    u = s.scalar(select(User).where(User.email == body.email.strip().lower()))
    ok = bcrypt.checkpw(body.password.encode(), (u.password_hash if u else DUMMY_HASH).encode())
    if not (u and ok and u.active):
        raise HTTPException(401, "Wrong email or password")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    return {"token": jwt.encode({"sub": str(u.id), "exp": exp}, SECRET, algorithm="HS256"), "user": user_out(u)}


@app.get("/api/me")
def me(u: User = Depends(current_user)):
    return user_out(u)


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["client", "operator", "admin"]
    organisation: str | None = None


class UserPatch(BaseModel):
    role: Literal["client", "operator", "admin"] | None = None
    active: bool | None = None


@app.get("/api/users")
def list_users(_=Depends(require("admin")), s=Depends(db)):
    return [user_out(u) for u in s.scalars(select(User).order_by(User.id))]


@app.post("/api/users", status_code=201)
def create_user(body: UserIn, _=Depends(require("admin")), s=Depends(db)):
    email = body.email.strip().lower()
    if s.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Email already in use")
    u = User(email=email, name=body.name, role=body.role, organisation=body.organisation,
             password_hash=bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode())
    s.add(u)
    s.commit()
    return user_out(u)


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch, admin: User = Depends(require("admin")), s=Depends(db)):
    u = s.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    if u.id == admin.id and (body.active is False or (body.role and body.role != "admin")):
        raise HTTPException(409, "Admins cannot demote or deactivate themselves")
    if body.role:
        u.role = body.role
    if body.active is not None:
        u.active = body.active
    s.commit()
    return user_out(u)


# ---------- requests ----------
class RequestIn(BaseModel):
    task_name: str = Field(min_length=1, max_length=120)
    episodes_requested: int = Field(gt=0, le=100000)
    deadline: date
    notes: str = Field("", max_length=2000)


class TransitionIn(BaseModel):
    to: Status


class AssignIn(BaseModel):
    episode_ids: list[str] = Field(min_length=1, max_length=1000)


def req_out(r: Request, n: int):
    return {"id": r.id, "client": r.client.name, "client_id": r.client_id, "task_name": r.task_name,
            "episodes_requested": r.episodes_requested, "assigned": n, "deadline": r.deadline.isoformat(),
            "notes": r.notes, "status": r.status, "created_at": r.created_at.isoformat()}


def assigned_count(s, rid):
    return s.scalar(select(func.count()).select_from(Assignment).where(Assignment.request_id == rid)) or 0


def visible_request(s, user: User, rid: int, lock=False) -> Request:
    r = s.get(Request, rid, with_for_update=lock)
    if not r or (user.role == "client" and r.client_id != user.id):
        raise HTTPException(404, "Request not found")  # 404, not 403: don't reveal other clients' ids
    return r


@app.post("/api/requests", status_code=201)
def create_request(body: RequestIn, u: User = Depends(require("client")), s=Depends(db)):
    if body.deadline < date.today():
        raise HTTPException(422, "Deadline cannot be in the past")
    r = Request(client_id=u.id, task_name=body.task_name.strip().lower(), episodes_requested=body.episodes_requested,
                deadline=body.deadline, notes=body.notes.strip(), status="submitted")
    s.add(r)
    s.flush()
    s.add(StatusHistory(request_id=r.id, from_status=None, to_status="submitted", actor_id=u.id))
    s.commit()
    s.refresh(r)
    return req_out(r, 0)


@app.get("/api/requests")
def list_requests(status: Status | None = None, u: User = Depends(current_user), s=Depends(db)):
    cnt = select(Assignment.request_id, func.count().label("n")).group_by(Assignment.request_id).subquery()
    q = select(Request, func.coalesce(cnt.c.n, 0)).outerjoin(cnt, cnt.c.request_id == Request.id)
    if u.role == "client":
        q = q.where(Request.client_id == u.id)
    if status:
        q = q.where(Request.status == status)
    return [req_out(r, n) for r, n in s.execute(q.order_by(Request.created_at.desc()).limit(500)).all()]


@app.get("/api/requests/{rid}")
def get_request(rid: int, u: User = Depends(current_user), s=Depends(db)):
    r = visible_request(s, u, rid)
    hist = s.execute(select(StatusHistory, User.name).join(User, User.id == StatusHistory.actor_id)
                     .where(StatusHistory.request_id == rid).order_by(StatusHistory.at, StatusHistory.id)).all()
    eps = s.scalars(select(Episode).join(Assignment, Assignment.episode_pk == Episode.id)
                    .where(Assignment.request_id == rid).order_by(Episode.episode_id)).all()
    out = req_out(r, len(eps))
    out["history"] = [{"from": h.from_status, "to": h.to_status, "by": name, "at": h.at.isoformat()} for h, name in hist]
    out["episodes"] = [{"episode_id": e.episode_id, "task_name": e.task_name, "quality": e.quality,
                        "robot_id": e.robot_id} for e in eps]
    return out


@app.post("/api/requests/{rid}/transition")
def transition(rid: int, body: TransitionIn, u: User = Depends(current_user), s=Depends(db)):
    r = visible_request(s, u, rid, lock=True)
    allowed = TRANSITIONS.get((r.status, body.to))
    if allowed is None:
        raise HTTPException(409, f"Cannot move from {r.status} to {body.to}")
    if u.role not in allowed:
        raise HTTPException(403, "Your role cannot perform this step")
    if body.to == "delivered":
        n = assigned_count(s, rid)
        if n < r.episodes_requested:
            raise HTTPException(409, f"Only {n} of {r.episodes_requested} episodes assigned")
    s.add(StatusHistory(request_id=rid, from_status=r.status, to_status=body.to, actor_id=u.id))
    r.status = body.to
    s.commit()
    return req_out(r, assigned_count(s, rid))


# ---------- assignments ----------
@app.post("/api/requests/{rid}/assignments", status_code=201)
def assign(rid: int, body: AssignIn, u: User = Depends(require(*OPS)), s=Depends(db)):
    r = visible_request(s, u, rid, lock=True)
    if r.status != "in_progress":
        raise HTTPException(409, "Episodes can only be assigned while the request is in progress")
    ids = list(dict.fromkeys(body.episode_ids))
    eps = s.scalars(select(Episode).where(Episode.episode_id.in_(ids))).all()
    missing = set(ids) - {e.episode_id for e in eps}
    if missing:
        raise HTTPException(404, f"Unknown episodes: {sorted(missing)[:5]}")
    bad = [e.episode_id for e in eps if e.quality not in ("good", "usable")]
    if bad:
        raise HTTPException(422, f"Only good or usable episodes can be assigned: {bad[:5]}")
    taken = s.scalars(select(Episode.episode_id).join(Assignment, Assignment.episode_pk == Episode.id)
                      .where(Episode.id.in_([e.id for e in eps]))).all()
    if taken:
        raise HTTPException(409, f"Already assigned to a request: {taken[:5]}")
    s.add_all(Assignment(request_id=rid, episode_pk=e.id, assigned_by=u.id) for e in eps)
    try:
        s.commit()
    except IntegrityError:  # concurrent assignment lost the race; the UNIQUE constraint is the real guard
        s.rollback()
        raise HTTPException(409, "An episode was just assigned elsewhere. Refresh and retry")
    return {"assigned": len(eps), "total": assigned_count(s, rid)}


@app.delete("/api/requests/{rid}/assignments/{episode_id}")
def unassign(rid: int, episode_id: str, u: User = Depends(require(*OPS)), s=Depends(db)):
    r = visible_request(s, u, rid, lock=True)
    if r.status != "in_progress":
        raise HTTPException(409, "Episodes can only be removed while the request is in progress")
    a = s.scalar(select(Assignment).join(Episode, Episode.id == Assignment.episode_pk)
                 .where(Assignment.request_id == rid, Episode.episode_id == episode_id))
    if not a:
        raise HTTPException(404, "Episode is not assigned to this request")
    s.delete(a)
    s.commit()
    return {"total": assigned_count(s, rid)}


# ---------- episodes, import, analytics ----------
@app.get("/api/meta/tasks")
def tasks(_=Depends(current_user), s=Depends(db)):
    return s.scalars(select(Episode.task_name).distinct().order_by(Episode.task_name)).all()


@app.get("/api/episodes")
def episodes(task_name: str | None = None, quality: Literal["good", "usable", "bad"] | None = None,
             available: bool = True, limit: int = Query(100, ge=1, le=500),
             _=Depends(require(*OPS)), s=Depends(db)):
    q = select(Episode).outerjoin(Assignment, Assignment.episode_pk == Episode.id)
    if task_name:
        q = q.where(Episode.task_name == task_name)
    q = q.where(Episode.quality == quality) if quality else q.where(Episode.quality.in_(("good", "usable")))
    if available:
        q = q.where(Assignment.id.is_(None))
    rows = s.scalars(q.order_by(Episode.episode_id).limit(limit)).all()
    return [{"episode_id": e.episode_id, "robot_id": e.robot_id, "task_name": e.task_name, "quality": e.quality,
             "duration_seconds": e.duration_seconds, "recorded_at": e.recorded_at.isoformat()} for e in rows]


@app.post("/api/import/episodes")
def import_episodes(file: UploadFile = File(...), _=Depends(require(*OPS)), s=Depends(db)):
    raw = file.file.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "File larger than 20 MB")
    try:
        return import_csv(s, raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise HTTPException(422, "File must be UTF-8 text")


@app.get("/api/analytics")
def analytics(date_from: date = Query(alias="from"), date_to: date = Query(alias="to"),
              _=Depends(require(*OPS)), s=Depends(db)):
    if date_to < date_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    lo = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc)
    hi = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    in_range = (Episode.recorded_at >= lo, Episode.recorded_at < hi)
    day = func.date_trunc("day", Episode.recorded_at)
    per_day = s.execute(select(day.label("d"), Episode.robot_id, func.count()).where(*in_range)
                        .group_by("d", Episode.robot_id).order_by("d", Episode.robot_id)).all()
    by_status = s.execute(select(Request.status, func.count()).where(Request.created_at >= lo, Request.created_at < hi)
                          .group_by(Request.status)).all()
    first_delivered = (select(StatusHistory.request_id, func.min(StatusHistory.at).label("at"))
                       .where(StatusHistory.to_status == "delivered").group_by(StatusHistory.request_id).subquery())
    median = s.scalar(select(func.percentile_cont(0.5).within_group(
        func.extract("epoch", first_delivered.c.at - Request.created_at)))
        .select_from(Request).join(first_delivered, first_delivered.c.request_id == Request.id)
        .where(Request.created_at >= lo, Request.created_at < hi))
    top = s.execute(select(Episode.task_name, func.count().label("n")).where(*in_range, Episode.quality == "good")
                    .group_by(Episode.task_name).order_by(func.count().desc(), Episode.task_name).limit(5)).all()
    return {"episodes_per_day_per_robot": [{"day": d.date().isoformat(), "robot_id": r, "count": c} for d, r, c in per_day],
            "requests_by_status": {st: c for st, c in by_status},
            "median_submit_to_deliver_seconds": float(median) if median is not None else None,
            "top_tasks_by_good_episodes": [{"task_name": t, "good_episodes": n} for t, n in top]}


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "static"), html=True), name="ui")
