from __future__ import annotations

import hashlib
import os
import re
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN_PASSWORD = "correct-horse-battery"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        admin_password=ADMIN_PASSWORD,
        secure_cookies=False,
        enable_scheduler=False,
        gc_grace_hours=0,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def ctx(app):
    return app.state.ctx


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


class UI:
    """A logged-in browser session."""

    def __init__(self, client: TestClient):
        self.client = client
        page = client.get("/login")
        self.csrf = csrf_from(page.text)
        r = client.post(
            "/login",
            data={"csrf": self.csrf, "password": ADMIN_PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        self.csrf = csrf_from(client.get("/").text)

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf", self.csrf)
        return self.client.post(url, data=data, **kw)

    def api(self, method, url, **kw):
        headers = kw.pop("headers", {})
        headers["X-CSRF-Token"] = self.csrf
        return self.client.request(method, url, headers=headers, **kw)


@pytest.fixture
def ui(app):
    with TestClient(app) as client:
        yield UI(client)


class Machine:
    def __init__(self, client: TestClient, machine_id: str, key: str, name: str):
        self.client = client
        self.id = machine_id
        self.key = key
        self.name = name
        self.headers = {"Authorization": f"Bearer {key}"}

    def request(self, method, url, **kw):
        headers = {**self.headers, **kw.pop("headers", {})}
        return self.client.request(method, url, headers=headers, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    # -- helpers mirroring the client sync flow --

    def upload(self, *contents: bytes) -> list[str]:
        hashes = [hashlib.sha256(c).hexdigest() for c in contents]
        missing = self.post("/api/v1/blobs/check", json={"hashes": hashes}).json()["missing"]
        for c, h in zip(contents, hashes):
            if h in missing:
                r = self.put(f"/api/v1/blobs/{h}", content=c)
                assert r.status_code in (200, 201), r.text
        return hashes

    def push(self, game_id, files: dict[str, bytes], parent=None, vtype="session", make_head=True,
             mtime="2026-11-20T18:00:00-04:00"):
        self.upload(*files.values())
        manifest = [
            {"path": p, "hash": hashlib.sha256(c).hexdigest(), "size": len(c), "mtime": mtime}
            for p, c in files.items()
        ]
        return self.post(
            f"/api/v1/games/{game_id}/versions",
            json={
                "parent_version_id": parent,
                "type": vtype,
                "make_head": make_head,
                "client_created_at": "2026-11-20T18:00:05-04:00",
                "files": manifest,
            },
        )


@pytest.fixture
def make_machine(app, ui):
    clients = []

    def _make(name="PC"):
        r = ui.api("POST", "/api/v1/machines", json={"name": name})
        assert r.status_code == 201, r.text
        body = r.json()
        client = TestClient(app)
        clients.append(client)
        return Machine(client, body["id"], body["api_key"], name)

    yield _make
    for c in clients:
        c.close()


@pytest.fixture
def pc(make_machine):
    return make_machine("Gaming PC")


@pytest.fixture
def laptop(make_machine):
    return make_machine("Laptop")


@pytest.fixture
def game(pc):
    r = pc.post(
        "/api/v1/games",
        json={
            "name": "Elden Ring",
            "roots": [
                {"alias": "saves", "path_template": "%APPDATA%\\EldenRing"},
                {"alias": "config", "path_template": "%LOCALAPPDATA%\\EldenRing"},
            ],
            "exe_names": ["eldenring.exe"],
            "excludes": ["**/*.tmp"],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def rand(n=2048) -> bytes:
    return os.urandom(n)
