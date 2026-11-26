from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.models import Blob, Version
from app.schemas import RetentionPolicy
from app.services.retention import VersionInfo, plan_retention

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def V(i, type="session", age=timedelta(0), pinned=False, tagged=False):
    return VersionInfo(id=f"v{i:04d}", type=type, created_at=NOW - age, pinned=pinned, tagged=tagged)


def plan(versions, head=None, now=NOW, tz=timezone.utc, **policy):
    return plan_retention(versions, RetentionPolicy(**policy), head, now, tz)


# ------------------------------------------------------------------ pure rules


def test_checkpoints_expire_after_keep_hours():
    vs = [V(1, "checkpoint", timedelta(hours=49)), V(2, "offline", timedelta(hours=50)),
          V(3, "pre_restore", timedelta(hours=72)), V(4, "checkpoint", timedelta(hours=47))]
    assert plan(vs, keep_newest=0) == {"v0001", "v0002", "v0003"}


def test_newest_n_are_protected_and_configurable():
    vs = [V(i, "checkpoint", timedelta(days=10 + i)) for i in range(8)]
    # Newest are the smallest ages: v0000..v0004 with keep_newest=5.
    assert plan(vs) == {"v0005", "v0006", "v0007"}
    assert plan(vs, keep_newest=2) == {f"v{i:04d}" for i in range(2, 8)}
    assert plan(vs, keep_newest=0) == {f"v{i:04d}" for i in range(8)}


def test_head_pinned_and_tagged_are_never_deleted():
    vs = [
        V(1, "checkpoint", timedelta(days=400)),
        V(2, "checkpoint", timedelta(days=400), pinned=True),
        V(3, "session", timedelta(days=4000), tagged=True),
        V(4, "session", timedelta(days=4000)),
    ]
    assert plan(vs, head="v0001", keep_newest=0) == {"v0004"}


def test_sessions_thin_to_daily_then_weekly_then_deleted():
    day = timedelta(days=1)
    vs = [
        # Within keep-all (14 d): every one kept.
        V(1, "session", 1 * day), V(2, "session", 1 * day + timedelta(hours=1)),
        # Daily window (14-90 d): 30 days ago, two on the same day; newest wins.
        V(3, "session", 30 * day), V(4, "manual", 30 * day + timedelta(hours=2)),
        V(5, "promoted", 31 * day),
        # Weekly window (90-365 d): same ISO week, newest wins.
        V(6, "session", 200 * day), V(7, "session", 201 * day),
        # Beyond 365 d: deleted.
        V(8, "session", 400 * day),
    ]
    week_of = lambda age: (NOW - age).isocalendar()[:2]  # noqa: E731
    assert week_of(200 * day) == week_of(201 * day)  # sanity: same ISO week
    assert plan(vs, keep_newest=0) == {"v0004", "v0007", "v0008"}


def test_null_means_no_limit():
    vs = [V(1, "checkpoint", timedelta(days=999)), V(2, "session", timedelta(days=999))]
    assert plan(vs, keep_newest=0, checkpoint_keep_hours=None, session_keep_all_days=None) == set()
    assert plan(vs, keep_newest=0, checkpoint_keep_hours=None, session_weekly_days=None) == set()


def test_max_versions_cap_deletes_oldest_unprotected():
    vs = [V(i, "session", timedelta(hours=i)) for i in range(10)]
    assert plan(vs, keep_newest=2, max_versions=4) == {f"v{i:04d}" for i in range(4, 10)}
    # Protected versions survive the cap even if that leaves more than max.
    vs[9] = V(9, "session", timedelta(hours=9), pinned=True)
    assert plan(vs, keep_newest=2, max_versions=2) == {f"v{i:04d}" for i in range(2, 9)}


def test_day_buckets_use_the_server_timezone():
    # 23:30 and 01:30 local in Port of Spain (UTC-4) on different local days,
    # but on the same UTC day.
    tz = ZoneInfo("America/Port_of_Spain")
    now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    a = VersionInfo("va", "session", datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc), False, False)   # Sep 30 23:30 local
    b = VersionInfo("vb", "session", datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc), False, False)   # Oct 1 01:30 local
    assert plan_retention([a, b], RetentionPolicy(keep_newest=0), None, now, timezone.utc) == {"va"}
    assert plan_retention([a, b], RetentionPolicy(keep_newest=0), None, now, tz) == set()


# ------------------------------------------------------------------ job end-to-end


def _age_all(ctx, game_id, by: timedelta):
    with ctx.Session() as db:
        for v in db.scalars(select(Version).where(Version.game_id == game_id)):
            v.created_at = v.created_at - by
        db.commit()


def test_retention_job_deletes_versions_and_gc_frees_blobs(ctx, pc, game):
    gid = game["id"]
    parent = None
    blobs = []
    for i in range(8):
        content = f"checkpoint {i}".encode() * 100
        r = pc.push(gid, {"saves/a": content}, parent=parent, vtype="checkpoint").json()
        parent = r["id"]
        blobs.append(r)
    _age_all(ctx, gid, timedelta(days=3))

    run = ctx.jobs.run_retention()
    assert run.status == "ok", run.log
    assert "3 version(s) deleted" in run.summary
    remaining = pc.get(f"/api/v1/games/{gid}/versions").json()["versions"]
    assert len(remaining) == 5
    with ctx.Session() as db:
        assert db.scalar(select(Blob).where(Blob.hash.is_not(None)).limit(1)) is not None
        assert len(list(db.scalars(select(Blob)))) == 5
    # Blob files for the deleted versions are gone from disk.
    on_disk = set(ctx.blobs.iter_hashes())
    assert len(on_disk) == 5


def test_per_game_retention_overrides_default(ctx, pc, game):
    gid = game["id"]
    parent = None
    for i in range(4):
        parent = pc.push(gid, {"saves/a": f"{i}".encode()}, parent=parent, vtype="checkpoint").json()["id"]
    _age_all(ctx, gid, timedelta(days=3))
    pc.patch(f"/api/v1/games/{gid}", json={"retention": {"keep_newest": 10}})
    ctx.jobs.run_retention()
    assert len(pc.get(f"/api/v1/games/{gid}/versions").json()["versions"]) == 4
    pc.patch(f"/api/v1/games/{gid}", json={"retention": {"keep_newest": 1}})
    ctx.jobs.run_retention()
    assert len(pc.get(f"/api/v1/games/{gid}/versions").json()["versions"]) == 1


def test_gc_respects_grace_period_for_fresh_uploads(ctx, pc):
    ctx.settings.gc_grace_hours = 24
    h = pc.upload(b"uploaded but version not created yet")[0]
    ctx.jobs.run_retention()
    assert ctx.blobs.exists(h)
    with ctx.Session() as db:
        db.execute(update(Blob).values(created_at=datetime.now(timezone.utc) - timedelta(hours=25)))
        db.commit()
    ctx.jobs.run_retention()
    assert not ctx.blobs.exists(h)


def test_gc_keeps_blobs_shared_with_surviving_versions(ctx, pc, game):
    gid = game["id"]
    shared = b"shared content" * 50
    v1 = pc.push(gid, {"saves/a": shared, "saves/b": b"old"}, vtype="checkpoint").json()
    pc.push(gid, {"saves/a": shared, "saves/b": b"new"}, parent=v1["id"], vtype="checkpoint")
    _age_all(ctx, gid, timedelta(days=3))
    pc.patch(f"/api/v1/games/{gid}", json={"retention": {"keep_newest": 1}})
    ctx.jobs.run_retention()
    import hashlib

    assert ctx.blobs.exists(hashlib.sha256(shared).hexdigest())
    assert not ctx.blobs.exists(hashlib.sha256(b"old").hexdigest())


def test_gc_removes_purged_game_blobs(ctx, pc, ui, game):
    gid = game["id"]
    pc.push(gid, {"saves/a": b"purge me"})
    pc.delete(f"/api/v1/games/{gid}")
    r = ui.post(f"/games/{gid}/purge", {"confirm_name": "Elden Ring"}, follow_redirects=False)
    assert r.status_code == 303
    ctx.jobs.run_retention()
    assert list(ctx.blobs.iter_hashes()) == []


def test_retention_skips_while_running(ctx):
    lock = ctx.jobs._locks["retention"]
    lock.acquire()
    try:
        assert ctx.jobs.run_retention() is None
    finally:
        lock.release()
