import hashlib
import io
import zipfile

from fastapi.testclient import TestClient

from .conftest import rand


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------ auth


def test_health_needs_no_auth(app):
    with TestClient(app) as c:
        r = c.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_requests_without_or_with_bad_key_are_rejected(app):
    with TestClient(app) as c:
        r = c.get("/api/v1/games")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"
        r = c.get("/api/v1/games", headers={"Authorization": "Bearer sp_nope"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_api_key"


def test_revoked_key_stops_working(ui, pc):
    assert pc.get("/api/v1/games").status_code == 200
    assert ui.api("DELETE", f"/api/v1/machines/{pc.id}").status_code == 204
    assert pc.get("/api/v1/games").status_code == 401


def test_machine_management_is_ui_only(pc):
    assert pc.get("/api/v1/machines").status_code == 403
    assert pc.post("/api/v1/machines", json={"name": "x"}).status_code == 403
    assert pc.delete(f"/api/v1/machines/{pc.id}").status_code == 403


def test_cookie_api_calls_need_csrf_header(ui):
    r = ui.client.post("/api/v1/machines", json={"name": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"


def test_machines_me_reports_server_time_and_records_clock_skew(pc, ui):
    r = pc.get(
        "/api/v1/machines/me",
        # Laptop clock reads 10:00 in UTC-4 = 14:00Z; whatever "now" is, the
        # skew is computed in UTC so the timezone itself never counts as skew.
        headers={"X-Savepoint-Client-Time": "2026-01-01T10:00:00-04:00"},
    )
    body = r.json()
    assert body["id"] == pc.id and body["name"] == "Gaming PC"
    assert body["server_time"].endswith("Z")
    machines = ui.api("GET", "/api/v1/machines").json()["machines"]
    m = next(m for m in machines if m["id"] == pc.id)
    assert m["last_seen_at"] is not None
    assert m["clock_skew_ms"] is not None


def test_clock_skew_is_zero_for_correct_clock_in_any_timezone(pc, ui):
    from datetime import datetime, timedelta, timezone

    local = datetime.now(timezone(timedelta(hours=-4)))
    pc.get("/api/v1/machines/me", headers={"X-Savepoint-Client-Time": local.isoformat()})
    m = next(m for m in ui.api("GET", "/api/v1/machines").json()["machines"] if m["id"] == pc.id)
    assert abs(m["clock_skew_ms"]) < 5000


# ------------------------------------------------------------------ games


def test_game_crud_and_soft_delete(pc, game):
    gid = game["id"]
    assert game["sync_mode"] == "auto"
    assert game["retention"] is None
    assert [r["alias"] for r in game["roots"]] == ["saves", "config"]

    r = pc.patch(f"/api/v1/games/{gid}", json={"sync_mode": "manual", "exe_names": ["er.exe", "er.exe"]})
    assert r.status_code == 200
    assert r.json()["sync_mode"] == "manual"
    assert r.json()["exe_names"] == ["er.exe"]

    r = pc.patch(
        f"/api/v1/games/{gid}",
        json={"retention": {"keep_newest": 3, "checkpoint_keep_hours": 12}},
    )
    assert r.json()["retention"]["keep_newest"] == 3
    assert r.json()["retention"]["session_keep_all_days"] == 14  # default filled in

    r = pc.patch(f"/api/v1/games/{gid}", json={"retention": None})
    assert r.json()["retention"] is None

    assert pc.delete(f"/api/v1/games/{gid}").status_code == 204
    assert pc.get(f"/api/v1/games/{gid}").status_code == 404
    assert pc.get("/api/v1/games").json()["games"] == []


def test_game_validation(pc):
    r = pc.post("/api/v1/games", json={"name": "x", "roots": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    r = pc.post(
        "/api/v1/games",
        json={"name": "x", "roots": [{"alias": "a", "path_template": "p"}, {"alias": "A", "path_template": "q"}]},
    )
    assert r.status_code == 422
    r = pc.post("/api/v1/games", json={"name": "x", "roots": [{"alias": "bad/alias", "path_template": "p"}]})
    assert r.status_code == 422


def test_root_override_is_per_machine(pc, laptop, game, ui):
    gid = game["id"]
    r = laptop.put(
        f"/api/v1/games/{gid}/roots/saves/override", json={"path_template": "D:\\Saves\\ER"}
    )
    assert r.status_code == 200
    laptop_view = {g["id"]: g for g in laptop.get("/api/v1/games").json()["games"]}[gid]
    pc_view = {g["id"]: g for g in pc.get("/api/v1/games").json()["games"]}[gid]
    assert laptop_view["roots"][0]["resolved_template"] == "D:\\Saves\\ER"
    assert laptop_view["roots"][0]["path_template"] == "%APPDATA%\\EldenRing"
    assert pc_view["roots"][0]["resolved_template"] == "%APPDATA%\\EldenRing"

    # UI must name the machine.
    assert ui.api("PUT", f"/api/v1/games/{gid}/roots/saves/override", json={"path_template": "X"}).status_code == 400
    r = ui.api(
        "PUT",
        f"/api/v1/games/{gid}/roots/saves/override?machine_id={pc.id}",
        json={"path_template": "E:\\ER"},
    )
    assert r.json()["roots"][0]["resolved_template"] == "E:\\ER"

    # Clearing.
    laptop.put(f"/api/v1/games/{gid}/roots/saves/override", json={"path_template": None})
    laptop_view = laptop.get(f"/api/v1/games/{gid}").json()
    assert laptop_view["roots"][0]["resolved_template"] == "%APPDATA%\\EldenRing"

    assert laptop.put(f"/api/v1/games/{gid}/roots/nope/override", json={"path_template": "x"}).status_code == 404


def test_removing_a_root_drops_its_overrides(pc, game):
    gid = game["id"]
    pc.put(f"/api/v1/games/{gid}/roots/config/override", json={"path_template": "C:\\x"})
    r = pc.patch(f"/api/v1/games/{gid}", json={"roots": [{"alias": "saves", "path_template": "%APPDATA%\\ER2"}]})
    assert r.status_code == 200
    assert [x["alias"] for x in r.json()["roots"]] == ["saves"]
    # Re-adding the root doesn't resurrect the old override.
    r = pc.patch(
        f"/api/v1/games/{gid}",
        json={"roots": [{"alias": "saves", "path_template": "a"}, {"alias": "config", "path_template": "b"}]},
    )
    assert r.json()["roots"][1]["resolved_template"] == "b"


# ------------------------------------------------------------------ blobs


def test_blob_upload_check_and_download(pc):
    data = rand(300_000)
    h = sha(data)
    assert pc.post("/api/v1/blobs/check", json={"hashes": [h, h]}).json() == {"missing": [h]}
    r = pc.put(f"/api/v1/blobs/{h}", content=data)
    assert r.status_code == 201
    assert pc.post("/api/v1/blobs/check", json={"hashes": [h]}).json() == {"missing": []}
    r = pc.put(f"/api/v1/blobs/{h}", content=data)
    assert r.status_code == 200  # idempotent
    r = pc.get(f"/api/v1/blobs/{h}")
    assert r.status_code == 200
    assert r.content == data


def test_blob_is_stored_compressed(pc, ctx):
    data = b"A" * 100_000
    h = pc.upload(data)[0]
    path = ctx.blobs.path_for(h)
    assert path.exists()
    assert path.stat().st_size < 1000
    assert path.parent.name == h[2:4] and path.parent.parent.name == h[0:2]


def test_blob_hash_is_verified(pc, ctx):
    data = rand()
    wrong = sha(b"something else")
    r = pc.put(f"/api/v1/blobs/{wrong}", content=data)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "hash_mismatch"
    assert not ctx.blobs.exists(wrong)
    assert list(ctx.settings.tmp_dir.iterdir()) == []


def test_invalid_hash_rejected(pc):
    assert pc.put("/api/v1/blobs/NOTAHASH", content=b"x").status_code == 400
    assert pc.post("/api/v1/blobs/check", json={"hashes": ["abc"]}).status_code == 400
    assert pc.get(f"/api/v1/blobs/{sha(b'never')}").status_code == 404


def test_empty_blob(pc):
    h = pc.upload(b"")[0]
    assert pc.get(f"/api/v1/blobs/{h}").content == b""


# ------------------------------------------------------------------ versions


def test_full_push_flow(pc, game):
    gid = game["id"]
    r = pc.push(gid, {"saves/ER0000.sl2": rand(), "config/GraphicsConfig.xml": b"<x/>"})
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["unchanged"] is False
    assert v["is_head"] is True
    assert v["type"] == "session"
    assert v["machine_id"] == pc.id and v["machine_name"] == "Gaming PC"
    assert v["file_count"] == 2
    assert pc.get(f"/api/v1/games/{gid}").json()["head_version_id"] == v["id"]

    full = pc.get(f"/api/v1/versions/{v['id']}").json()
    assert sorted(f["path"] for f in full["files"]) == ["config/GraphicsConfig.xml", "saves/ER0000.sl2"]


def test_mtimes_are_normalized_to_utc(pc, game):
    r = pc.push(game["id"], {"saves/a": b"1"}, mtime="2026-11-20T18:00:00.123456-04:00")
    v = pc.get(f"/api/v1/versions/{r.json()['id']}").json()
    assert v["files"][0]["mtime"] == "2026-11-20T22:00:00.123456Z"
    assert v["latest_mtime"] == "2026-11-20T22:00:00.123456Z"
    assert v["client_created_at"] == "2026-11-20T22:00:05.000000Z"


def test_naive_mtimes_are_rejected(pc, game):
    pc.upload(b"1")
    r = pc.post(
        f"/api/v1/games/{game['id']}/versions",
        json={"type": "session", "files": [{"path": "saves/a", "hash": sha(b"1"), "size": 1, "mtime": "2026-11-20T18:00:00"}]},
    )
    assert r.status_code == 422


def test_missing_blobs_are_rejected(pc, game):
    present, absent = rand(), rand()
    pc.upload(present)
    r = pc.post(
        f"/api/v1/games/{game['id']}/versions",
        json={
            "type": "session",
            "files": [
                {"path": "saves/a", "hash": sha(present), "size": len(present), "mtime": "2026-11-20T18:00:00Z"},
                {"path": "saves/b", "hash": sha(absent), "size": len(absent), "mtime": "2026-11-20T18:00:00Z"},
            ],
        },
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "missing_blobs"
    assert err["missing"] == [sha(absent)]
    assert pc.get(f"/api/v1/games/{game['id']}/versions").json()["versions"] == []


def test_size_must_match_blob(pc, game):
    data = rand(100)
    pc.upload(data)
    r = pc.post(
        f"/api/v1/games/{game['id']}/versions",
        json={"type": "session", "files": [{"path": "saves/a", "hash": sha(data), "size": 99, "mtime": "2026-11-20T18:00:00Z"}]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "size_mismatch"


def test_manifest_path_validation(pc, game):
    gid = game["id"]
    for path, code in [
        ("saves\\a", "invalid_path"),
        ("/saves/a", "invalid_path"),
        ("saves", "invalid_path"),
        ("saves/../a", "invalid_path"),
        ("saves//a", "invalid_path"),
        ("other/a", "unknown_root"),
    ]:
        r = pc.push(gid, {path: b"x"})
        assert r.status_code == 400, path
        assert r.json()["error"]["code"] == code, path
    pc.upload(b"x")
    r = pc.post(
        f"/api/v1/games/{gid}/versions",
        json={
            "type": "session",
            "files": [
                {"path": "saves/A.sav", "hash": sha(b"x"), "size": 1, "mtime": "2026-11-20T18:00:00Z"},
                {"path": "saves/a.sav", "hash": sha(b"x"), "size": 1, "mtime": "2026-11-20T18:00:00Z"},
            ],
        },
    )
    assert r.json()["error"]["code"] == "duplicate_path"


def test_client_cannot_create_promoted_type(pc, game):
    r = pc.push(game["id"], {"saves/a": b"x"}, vtype="promoted")
    assert r.status_code == 422


def test_identical_manifest_is_deduplicated(pc, laptop, game):
    gid = game["id"]
    files = {"saves/a": rand(), "saves/b": rand()}
    v1 = pc.push(gid, files).json()
    # Same content, different mtimes and machine: still unchanged.
    r = laptop.push(gid, files, parent=v1["id"], mtime="2026-11-21T08:00:00Z", vtype="checkpoint")
    assert r.status_code == 200
    assert r.json()["unchanged"] is True
    assert r.json()["id"] == v1["id"]
    assert len(pc.get(f"/api/v1/games/{gid}/versions").json()["versions"]) == 1


def test_stale_parent_cannot_become_head(pc, laptop, game):
    gid = game["id"]
    v1 = pc.push(gid, {"saves/a": rand()}).json()
    v2 = pc.push(gid, {"saves/a": rand()}, parent=v1["id"]).json()
    # Laptop last synced v1 and doesn't know about v2.
    r = laptop.push(gid, {"saves/a": rand()}, parent=v1["id"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "head_moved"
    assert r.json()["error"]["head_version_id"] == v2["id"]
    # First version ever must have a null parent.
    r = laptop.push(gid, {"saves/a": rand()}, parent=None)
    assert r.status_code == 409


def test_conflict_flow_upload_without_head_then_promote(pc, laptop, game):
    gid = game["id"]
    v1 = pc.push(gid, {"saves/a": rand()}).json()
    v2 = pc.push(gid, {"saves/a": rand()}, parent=v1["id"]).json()

    local = laptop.push(gid, {"saves/a": rand()}, parent=v1["id"], vtype="offline", make_head=False)
    assert local.status_code == 201
    loser = local.json()
    assert loser["is_head"] is False
    assert pc.get(f"/api/v1/games/{gid}").json()["head_version_id"] == v2["id"]

    # Laptop decides local is newer and promotes it.
    r = laptop.post(f"/api/v1/versions/{loser['id']}/promote")
    assert r.status_code == 201
    promoted = r.json()
    assert promoted["type"] == "promoted"
    assert promoted["source_version_id"] == loser["id"]
    assert promoted["parent_version_id"] == v2["id"]
    assert promoted["is_head"] is True
    assert promoted["machine_id"] == laptop.id

    d = pc.get(f"/api/v1/versions/{promoted['id']}/diff?against={loser['id']}").json()
    assert d["added"] == d["changed"] == d["removed"] == []

    # Promoting the head again is a no-op.
    r = laptop.post(f"/api/v1/versions/{promoted['id']}/promote")
    assert r.status_code == 200 and r.json()["unchanged"] is True


def test_promote_older_version_rolls_back(pc, game):
    gid = game["id"]
    old = pc.push(gid, {"saves/a": b"old"}).json()
    new = pc.push(gid, {"saves/a": b"new"}, parent=old["id"]).json()
    r = pc.post(f"/api/v1/versions/{old['id']}/promote")
    head = r.json()
    assert pc.get(f"/api/v1/games/{gid}").json()["head_version_id"] == head["id"]
    files = pc.get(f"/api/v1/versions/{head['id']}").json()["files"]
    assert files[0]["hash"] == sha(b"old")
    # The next push from a client that pulled the promoted head works normally.
    r = pc.push(gid, {"saves/a": b"newer"}, parent=head["id"])
    assert r.status_code == 201
    assert new["id"] != r.json()["id"]


def test_diff(pc, game):
    gid = game["id"]
    v1 = pc.push(gid, {"saves/keep": b"k", "saves/change": b"1", "saves/remove": b"r"}).json()
    v2 = pc.push(gid, {"saves/keep": b"k", "saves/change": b"22", "saves/new": b"n"}, parent=v1["id"]).json()
    d = pc.get(f"/api/v1/versions/{v2['id']}/diff").json()  # defaults to previous version
    assert d["against_version_id"] == v1["id"]
    assert [f["path"] for f in d["added"]] == ["saves/new"]
    assert [f["path"] for f in d["removed"]] == ["saves/remove"]
    assert [(f["path"], f["old_size"], f["size"]) for f in d["changed"]] == [("saves/change", 1, 2)]
    assert d["unchanged_count"] == 1

    first = pc.get(f"/api/v1/versions/{v1['id']}/diff").json()
    assert first["against_version_id"] is None
    assert len(first["added"]) == 3


def test_download_zip(pc, game):
    files = {"saves/ER0000.sl2": rand(5000), "config/sub/dir/x.ini": b"[x]\n"}
    v = pc.push(game["id"], files).json()
    r = pc.get(f"/api/v1/versions/{v['id']}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "Elden%20Ring" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == sorted(files)
        for name, content in files.items():
            assert zf.read(name) == content


def test_version_listing_paging_and_filters(pc, game):
    gid = game["id"]
    parent = None
    ids = []
    for i in range(7):
        v = pc.push(gid, {"saves/a": f"{i}".encode()}, parent=parent, vtype="checkpoint" if i % 2 else "session").json()
        parent = v["id"]
        ids.append(v["id"])
    page1 = pc.get(f"/api/v1/games/{gid}/versions?limit=3").json()
    assert [v["id"] for v in page1["versions"]] == ids[::-1][:3]
    page2 = pc.get(f"/api/v1/games/{gid}/versions?limit=3&before={page1['next_before']}").json()
    assert [v["id"] for v in page2["versions"]] == ids[::-1][3:6]
    page3 = pc.get(f"/api/v1/games/{gid}/versions?limit=3&before={page2['next_before']}").json()
    assert [v["id"] for v in page3["versions"]] == ids[:1]
    assert page3["next_before"] is None

    sessions = pc.get(f"/api/v1/games/{gid}/versions?type=session").json()["versions"]
    assert {v["type"] for v in sessions} == {"session"} and len(sessions) == 4

    pc.patch(f"/api/v1/versions/{ids[2]}", json={"tags": ["boss", "boss", " ng+ "]})
    tagged = pc.get(f"/api/v1/games/{gid}/versions?tag=boss").json()["versions"]
    assert [v["id"] for v in tagged] == [ids[2]]
    assert tagged[0]["tags"] == ["boss", "ng+"]

    assert pc.get(f"/api/v1/games/{gid}/versions?type=bogus").status_code == 400


def test_patch_version(pc, game):
    v = pc.push(game["id"], {"saves/a": b"x"}).json()
    r = pc.patch(f"/api/v1/versions/{v['id']}", json={"pinned": True, "note": "  before final boss  "})
    assert r.json()["pinned"] is True
    assert r.json()["note"] == "before final boss"
    r = pc.patch(f"/api/v1/versions/{v['id']}", json={"tags": ["a"]})
    assert r.json()["pinned"] is True and r.json()["note"] == "before final boss"
    r = pc.patch(f"/api/v1/versions/{v['id']}", json={"note": None, "tags": []})
    assert r.json()["note"] is None and r.json()["tags"] == []


def test_delete_version_ui_only_and_not_head(pc, ui, game):
    gid = game["id"]
    v1 = pc.push(gid, {"saves/a": b"1"}).json()
    v2 = pc.push(gid, {"saves/a": b"2"}, parent=v1["id"]).json()
    assert pc.delete(f"/api/v1/versions/{v1['id']}").status_code == 403
    r = ui.api("DELETE", f"/api/v1/versions/{v2['id']}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "is_head"
    assert ui.api("DELETE", f"/api/v1/versions/{v1['id']}").status_code == 204
    assert pc.get(f"/api/v1/versions/{v1['id']}").status_code == 404


def test_versions_of_soft_deleted_game_are_kept(pc, game):
    gid = game["id"]
    v = pc.push(gid, {"saves/a": b"1"}).json()
    pc.delete(f"/api/v1/games/{gid}")
    assert pc.get(f"/api/v1/versions/{v['id']}").status_code == 200
    assert pc.push(gid, {"saves/a": b"2"}, parent=v["id"]).status_code == 404


# ------------------------------------------------------------------ heads


def test_heads_with_etag(pc, laptop, game):
    gid = game["id"]
    r = pc.get("/api/v1/heads")
    assert r.status_code == 200 and r.json() == {}
    v = pc.push(gid, {"saves/a": b"1"}).json()
    r = laptop.get("/api/v1/heads")
    body = r.json()
    assert body[gid]["version_id"] == v["id"]
    assert body[gid]["machine_id"] == pc.id
    etag = r.headers["etag"]

    r = laptop.get("/api/v1/heads", headers={"If-None-Match": etag})
    assert r.status_code == 304

    pc.push(gid, {"saves/a": b"2"}, parent=v["id"])
    r = laptop.get("/api/v1/heads", headers={"If-None-Match": etag})
    assert r.status_code == 200
    assert r.headers["etag"] != etag

    pc.delete(f"/api/v1/games/{gid}")
    assert laptop.get("/api/v1/heads").json() == {}


def test_error_shape_for_unknown_routes(pc):
    r = pc.get("/api/v1/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    r = pc.get("/api/v1/versions/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "not_found", "message": "Version not found"}}


# ------------------------------------------------------------------ per-game machine selection


def test_games_sync_on_all_machines_by_default(pc, laptop, game):
    assert game["sync_machines"] is None
    assert laptop.get(f"/api/v1/games/{game['id']}").json()["sync_enabled"] is True


def test_sync_machines_limits_which_machines_sync(pc, laptop, game):
    gid = game["id"]
    r = pc.patch(f"/api/v1/games/{gid}", json={"sync_machines": [pc.id]})
    assert r.status_code == 200
    assert r.json()["sync_machines"] == [pc.id]
    assert r.json()["sync_enabled"] is True
    assert laptop.get(f"/api/v1/games/{gid}").json()["sync_enabled"] is False
    # Still listed for the laptop, so its app can offer to turn sync on.
    assert gid in [g["id"] for g in laptop.get("/api/v1/games").json()["games"]]

    assert pc.push(gid, {"saves/a": b"pc"}).status_code == 201
    r = laptop.push(gid, {"saves/a": b"laptop"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "sync_disabled"

    assert pc.patch(f"/api/v1/games/{gid}", json={"sync_machines": None}).json()["sync_machines"] is None
    assert laptop.get(f"/api/v1/games/{gid}").json()["sync_enabled"] is True


def test_sync_machines_rejects_unknown_ids(pc, game):
    r = pc.patch(f"/api/v1/games/{game['id']}", json={"sync_machines": ["nope"]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unknown_machine"


def test_machine_can_toggle_its_own_sync(pc, laptop, make_machine, game):
    gid = game["id"]
    deck = make_machine("Steam Deck")
    # Turning off from "all machines" keeps every other active machine.
    r = laptop.put(f"/api/v1/games/{gid}/sync", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["sync_enabled"] is False
    assert sorted(r.json()["sync_machines"]) == sorted([pc.id, deck.id])
    # Turning back on adds just this machine.
    r = laptop.put(f"/api/v1/games/{gid}/sync", json={"enabled": True})
    assert sorted(r.json()["sync_machines"]) == sorted([pc.id, deck.id, laptop.id])
    assert r.json()["sync_enabled"] is True


def test_ui_toggles_sync_for_a_named_machine(ui, pc, laptop, game):
    gid = game["id"]
    assert ui.api("PUT", f"/api/v1/games/{gid}/sync", json={"enabled": False}).status_code == 400
    r = ui.api("PUT", f"/api/v1/games/{gid}/sync?machine_id={laptop.id}", json={"enabled": False})
    assert r.status_code == 200
    assert laptop.get(f"/api/v1/games/{gid}").json()["sync_enabled"] is False
