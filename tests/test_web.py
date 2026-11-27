import re

from fastapi.testclient import TestClient

from .conftest import ADMIN_PASSWORD, UI, csrf_from


def test_pages_require_login(app):
    with TestClient(app) as c:
        for path in ["/", "/machines", "/settings", "/games/new"]:
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303, path
            assert r.headers["location"].startswith("/login")
        r = c.get("/settings", follow_redirects=False)
        assert r.headers["location"] == "/login?next=%2Fsettings"
        # htmx requests get an HX-Redirect instead of a 303.
        r = c.get("/settings", headers={"HX-Request": "true"}, follow_redirects=False)
        assert r.headers["hx-redirect"].startswith("/login")


def test_wrong_password_and_rate_limit(app, ctx):
    ctx.login_limiter.max_failures = 3
    with TestClient(app) as c:
        token = csrf_from(c.get("/login").text)
        for _ in range(3):
            r = c.post("/login", data={"csrf": token, "password": "wrong"})
            assert r.status_code == 401
        r = c.post("/login", data={"csrf": token, "password": ADMIN_PASSWORD})
        assert r.status_code == 429
        assert "Too many attempts" in r.text


def test_login_rejects_missing_csrf(app):
    with TestClient(app) as c:
        c.get("/login")
        r = c.post("/login", data={"password": ADMIN_PASSWORD})
        assert r.status_code == 403


def test_open_redirect_is_blocked(app):
    with TestClient(app) as c:
        token = csrf_from(c.get("/login").text)
        r = c.post(
            "/login",
            data={"csrf": token, "password": ADMIN_PASSWORD, "next": "//evil.example/x"},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/"


def test_form_posts_need_csrf(ui):
    r = ui.client.post("/machines", data={"name": "x"})
    assert r.status_code == 403


def test_all_pages_render(ui, pc, game):
    gid = game["id"]
    v = pc.push(gid, {"saves/a": b"1"}).json()
    for path in ["/", f"/games/{gid}", f"/games/{gid}/settings", f"/versions/{v['id']}", "/machines", "/settings", "/games/new"]:
        r = ui.get(path)
        assert r.status_code == 200, path
    assert "Elden Ring" in ui.get("/").text


def test_create_game_from_form(ui):
    r = ui.post(
        "/games/new",
        {
            "name": "Hades II",
            "root_alias": ["saves", "profile"],
            "root_path": ["%SAVEDGAMES%\\Hades II", "%APPDATA%\\Hades"],
            "exe_names": "Hades2.exe\n\n",
            "excludes": "**/*.log\n**/*.tmp",
            "sync_mode": "manual",
            "retention_mode": "custom",
            "ret_keep_newest": "3",
            "ret_checkpoint_keep_hours": "24",
            "ret_session_keep_all_days": "7",
            "ret_session_daily_days": "",
            "ret_session_weekly_days": "",
            "ret_max_versions": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    gid = r.headers["location"].rsplit("/", 1)[1]
    game = ui.api("GET", f"/api/v1/games/{gid}").json()
    assert game["name"] == "Hades II"
    assert [x["alias"] for x in game["roots"]] == ["saves", "profile"]
    assert game["exe_names"] == ["Hades2.exe"]
    assert game["excludes"] == ["**/*.log", "**/*.tmp"]
    assert game["sync_mode"] == "manual"
    assert game["retention"] == {
        "keep_newest": 3,
        "checkpoint_keep_hours": 24,
        "session_keep_all_days": 7,
        "session_daily_days": None,
        "session_weekly_days": None,
        "max_versions": None,
    }


def test_create_game_form_shows_validation_errors(ui):
    r = ui.post("/games/new", {"name": "X", "root_alias": ["bad alias"], "root_path": ["p"]})
    assert r.status_code == 400
    assert "flash-error" in r.text
    assert 'value="X"' in r.text  # input preserved


def test_game_settings_and_overrides(ui, pc, game):
    gid = game["id"]
    r = ui.post(
        f"/games/{gid}/settings",
        {
            "name": "Elden Ring NG+",
            "root_alias": ["saves"],
            "root_path": ["%APPDATA%\\EldenRing"],
            "exe_names": "eldenring.exe",
            "excludes": "",
            "sync_mode": "auto",
            "retention_mode": "default",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    g = pc.get(f"/api/v1/games/{gid}").json()
    assert g["name"] == "Elden Ring NG+"
    assert [x["alias"] for x in g["roots"]] == ["saves"]

    r = ui.post(f"/games/{gid}/overrides", {f"ovr__saves__{pc.id}": "D:\\ER"}, follow_redirects=False)
    assert r.status_code == 303
    assert pc.get(f"/api/v1/games/{gid}").json()["roots"][0]["resolved_template"] == "D:\\ER"
    ui.post(f"/games/{gid}/overrides", {f"ovr__saves__{pc.id}": ""})
    assert pc.get(f"/api/v1/games/{gid}").json()["roots"][0]["resolved_template"] == "%APPDATA%\\EldenRing"


def test_delete_restore_and_purge_game(ui, pc, game):
    gid = game["id"]
    ui.post(f"/games/{gid}/delete")
    assert pc.get(f"/api/v1/games/{gid}").status_code == 404
    assert "Deleted games (1)" in ui.get("/").text
    ui.post(f"/games/{gid}/restore")
    assert pc.get(f"/api/v1/games/{gid}").status_code == 200
    ui.post(f"/games/{gid}/delete")
    r = ui.post(f"/games/{gid}/purge", {"confirm_name": "wrong"}, follow_redirects=True)
    assert "Type the game" in r.text
    ui.post(f"/games/{gid}/purge", {"confirm_name": "Elden Ring"})
    assert "Deleted games" not in ui.get("/").text


def test_timeline_filters_and_paging(ui, pc, game):
    gid = game["id"]
    parent = None
    for i in range(35):
        parent = pc.push(gid, {"saves/a": f"{i}".encode()}, parent=parent, vtype="checkpoint" if i % 5 else "session").json()["id"]
    page = ui.get(f"/games/{gid}").text
    assert page.count('class="version ') == 30
    assert "Load older versions" in page
    before = re.search(r"before=([0-9a-f-]+)", page).group(1)
    more = ui.get(f"/games/{gid}/versions?before={before}", headers={"HX-Request": "true"}).text
    assert more.count('class="version ') == 5
    sessions = ui.get(f"/games/{gid}?type=session").text
    assert sessions.count('class="version ') == 7


def test_version_htmx_actions(ui, pc, game):
    gid = game["id"]
    v1 = pc.push(gid, {"saves/a": b"1"}).json()
    v2 = pc.push(gid, {"saves/a": b"2"}, parent=v1["id"]).json()
    hx = {"HX-Request": "true", "X-CSRF-Token": ui.csrf}

    r = ui.client.post(f"/versions/{v1['id']}/pin", headers=hx)
    assert r.status_code == 200 and "Pinned" in r.text
    assert pc.get(f"/api/v1/versions/{v1['id']}").json()["pinned"] is True

    r = ui.client.post(f"/versions/{v1['id']}/meta", headers=hx, data={"tags": "boss, ng+", "note": "pre-Malenia"})
    assert "#boss" in r.text and "pre-Malenia" in r.text

    r = ui.client.post(f"/versions/{v1['id']}/promote", headers=hx)
    assert r.status_code == 204 and r.headers["hx-redirect"] == f"/games/{gid}"
    head = pc.get(f"/api/v1/games/{gid}").json()["head_version_id"]
    assert head not in (v1["id"], v2["id"])

    r = ui.client.post(f"/versions/{v2['id']}/delete", headers={**hx, "HX-Target": f"v-{v2['id']}"})
    assert r.status_code == 200 and r.text == ""
    r = ui.client.post(f"/versions/{head}/delete", headers=hx)
    assert r.status_code == 409
    assert r.headers.get("hx-retarget") == "#flash-area"


def test_machine_key_shown_once(ui):
    r = ui.post("/machines", {"name": "Steam Deck"})
    key = re.search(r'value="(sp_[^"]+)"', r.text).group(1)
    assert "shown only once" in r.text
    assert key not in ui.get("/machines").text
    with TestClient(ui.client.app) as c:
        me = c.get("/api/v1/machines/me", headers={"Authorization": f"Bearer {key}"}).json()
    assert me["name"] == "Steam Deck"
    m = re.search(r'action="/machines/([0-9a-f-]+)/revoke"', ui.get("/machines").text)
    ui.post(f"/machines/{m.group(1)}/revoke")
    assert "Revoked" in ui.get("/machines").text


def test_default_retention_settings(ui, ctx):
    from app.services import settings_store

    ui.post(
        "/settings/retention",
        {"ret_keep_newest": "7", "ret_checkpoint_keep_hours": "", "ret_session_keep_all_days": "30",
         "ret_session_daily_days": "120", "ret_session_weekly_days": "", "ret_max_versions": "500"},
    )
    with ctx.Session() as db:
        pol = settings_store.default_retention(db)
    assert pol.keep_newest == 7
    assert pol.checkpoint_keep_hours is None
    assert pol.session_keep_all_days == 30
    assert pol.max_versions == 500


def test_offsite_settings_and_run_now(ui, ctx, tmp_path):
    r = ui.post("/settings/offsite", {"enabled": "on", "remote": "nocolon"}, follow_redirects=True)
    assert "Remote must look like" in r.text
    ui.post("/settings/offsite", {"enabled": "on", "remote": "gdrive:sp"})
    from app.services import settings_store

    with ctx.Session() as db:
        assert settings_store.offsite(db) == {"enabled": True, "remote": "gdrive:sp"}

    r = ui.post("/settings/jobs/retention/run", follow_redirects=True)
    assert "started" in r.text or "already running" in r.text


def test_change_password_signs_out_other_sessions(app, ui):
    other = TestClient(app)
    other_ui = UI(other)
    assert other_ui.get("/settings", follow_redirects=False).status_code == 200

    r = ui.post("/settings/password", {"current": "nope", "new": "x" * 10, "confirm": "x" * 10}, follow_redirects=True)
    assert "Current password is wrong" in r.text
    r = ui.post("/settings/password", {"current": ADMIN_PASSWORD, "new": "short", "confirm": "short"}, follow_redirects=True)
    assert "at least 8" in r.text
    r = ui.post("/settings/password", {"current": ADMIN_PASSWORD, "new": "new-password-1", "confirm": "new-password-1"}, follow_redirects=True)
    assert "Password changed" in r.text

    # This session stays signed in; the other one is signed out.
    assert ui.get("/settings", follow_redirects=False).status_code == 200
    assert other.get("/settings", follow_redirects=False).status_code == 303
    other.close()


def test_logout(ui):
    ui.post("/logout")
    assert ui.get("/", follow_redirects=False).status_code == 303


def test_admin_password_reset_env(settings):
    from app.main import create_app

    create_app(settings)
    settings.admin_password = "recovered-password"
    settings.reset_admin_password = True
    app = create_app(settings)
    with TestClient(app) as c:
        token = csrf_from(c.get("/login").text)
        r = c.post("/login", data={"csrf": token, "password": "recovered-password"}, follow_redirects=False)
        assert r.status_code == 303


def test_game_settings_sync_machines_and_stats(ui, pc, laptop, game):
    gid = game["id"]
    base = {
        "name": "Elden Ring",
        "root_alias": ["saves"],
        "root_path": ["%APPDATA%\\EldenRing"],
        "sync_mode": "auto",
        "retention_mode": "default",
    }
    ui.post(f"/games/{gid}/settings", {**base, "sync_scope": "only", "sync_machine": [pc.id]})
    assert pc.get(f"/api/v1/games/{gid}").json()["sync_machines"] == [pc.id]
    assert "Gaming PC only" in ui.get("/").text
    page = ui.get(f"/games/{gid}/settings").text
    assert f'value="{pc.id}" checked' in page

    ui.post(f"/games/{gid}/settings", {**base, "sync_scope": "all", "sync_machine": [pc.id]})
    assert pc.get(f"/api/v1/games/{gid}").json()["sync_machines"] is None

    v1 = pc.push(gid, {"saves/a": b"x" * 1000}).json()
    pc.push(gid, {"saves/a": b"y" * 3000}, parent=v1["id"])
    page = ui.get(f"/games/{gid}").text
    assert "avg save 2.0 KB" in page
    assert "Make head" in page
    assert "syncs on All machines" in page
