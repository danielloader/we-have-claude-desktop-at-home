import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        sqlite_path=str(tmp_path / "test.db"),
        stub_lag_min_s=0.0,
        stub_lag_max_s=0.01,
        stub_tokens_per_s_min=5000,
        stub_tokens_per_s_max=5000,
        stub_stall_probability=0.0,
        telemetry_file=str(tmp_path / "telemetry.log"),
    )
    with TestClient(create_app(settings)) as c:
        yield c


def login(client, username, password):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def send(client, token, conv_id, content):
    """Post a message and collect the SSE events as (event, data) tuples."""
    events = []
    with client.stream(
        "POST", f"/api/conversations/{conv_id}/messages", json={"content": content}, headers=auth(token)
    ) as res:
        assert res.status_code == 200, res.text
        assert res.headers["content-type"].startswith("text/event-stream")
        event = None
        for line in res.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                events.append((event, json.loads(line[6:])))
    return events


def test_seeded_users_can_login(client):
    for u in ("admin", "alice", "bob"):
        login(client, u, u)
    res = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert res.status_code == 401


def test_me_reflects_jwt_claims(client):
    token = login(client, "admin", "admin")
    me = client.get("/api/auth/me", headers=auth(token)).json()
    assert me["username"] == "admin" and me["is_admin"] is True
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=auth("garbage")).status_code == 401


def test_chat_roundtrip_streams_and_persists(client):
    token = login(client, "alice", "alice")
    alice_id = client.get("/api/auth/me", headers=auth(token)).json()["id"]
    conv = client.post("/api/conversations", json={}, headers=auth(token)).json()

    events = send(client, token, conv["id"], "What is a fibre jacket?")
    names = [e for e, _ in events]
    assert names[0] == "user_message"
    assert names.index("thinking_start") < names.index("thinking_stop") < names.index("text_delta")
    assert names[-1] == "done"
    assert names.count("text_delta") > 10
    done = events[-1][1]
    assert done["output_tokens"] > 0 and done["message"]["role"] == "assistant"
    streamed = "".join(d["text"] for e, d in events if e == "text_delta")
    assert streamed == done["message"]["content"]
    assert "What is a fibre jacket?" in streamed

    detail = client.get(f"/api/conversations/{conv['id']}", headers=auth(token)).json()
    assert detail["conversation"]["title"] == "What is a fibre jacket?"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert all(m["user_id"] == alice_id for m in detail["messages"])
    assert detail["messages"][1]["thinking"]

    # A second turn sees the history, so the stub reports turn 3.
    events = send(client, token, conv["id"], "And a grommet?")
    assert "turn 3" in events[-1][1]["message"]["content"]


def test_users_are_isolated(client):
    alice, bob = login(client, "alice", "alice"), login(client, "bob", "bob")
    conv = client.post("/api/conversations", json={"title": "secret"}, headers=auth(alice)).json()
    send(client, alice, conv["id"], "hello")

    assert client.get("/api/conversations", headers=auth(bob)).json() == []
    assert client.get(f"/api/conversations/{conv['id']}", headers=auth(bob)).status_code == 404
    assert client.delete(f"/api/conversations/{conv['id']}", headers=auth(bob)).status_code == 404
    res = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth(bob))
    assert res.status_code == 404


def test_admin_reads_everyone_but_cannot_post_into_their_chats(client):
    alice, admin = login(client, "alice", "alice"), login(client, "admin", "admin")
    conv = client.post("/api/conversations", json={}, headers=auth(alice)).json()
    send(client, alice, conv["id"], "alice asks something")

    rows = client.get("/api/admin/conversations", headers=auth(admin)).json()
    assert [(r["username"], r["title"]) for r in rows] == [("alice", "alice asks something")]
    detail = client.get(f"/api/admin/conversations/{conv['id']}", headers=auth(admin)).json()
    assert detail["conversation"]["username"] == "alice"
    assert len(detail["messages"]) == 2

    # Admin's normal chat surface still only shows their own history.
    assert client.get("/api/conversations", headers=auth(admin)).json() == []
    res = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "x"}, headers=auth(admin))
    assert res.status_code == 404


def test_non_admin_forbidden_from_admin_routes(client):
    alice = login(client, "alice", "alice")
    for path in ("/api/admin/users", "/api/admin/usage", "/api/admin/conversations"):
        assert client.get(path, headers=auth(alice)).status_code == 403


def test_admin_user_lifecycle(client):
    admin = login(client, "admin", "admin")
    me = client.get("/api/auth/me", headers=auth(admin)).json()

    res = client.post("/api/admin/users", json={"username": "carol", "password": "carol1"}, headers=auth(admin))
    assert res.status_code == 201
    carol = res.json()
    assert client.post("/api/admin/users", json={"username": "carol", "password": "x1234"}, headers=auth(admin)).status_code == 409
    login(client, "carol", "carol1")

    assert client.delete(f"/api/admin/users/{me['id']}", headers=auth(admin)).status_code == 400
    assert client.delete(f"/api/admin/users/{carol['id']}", headers=auth(admin)).status_code == 204
    assert client.post("/api/auth/login", json={"username": "carol", "password": "carol1"}).status_code == 401
    assert {u["username"] for u in client.get("/api/admin/users", headers=auth(admin)).json()} == {"admin", "alice", "bob"}


def test_usage_is_attributed_to_jwt_subject(client):
    alice, admin = login(client, "alice", "alice"), login(client, "admin", "admin")
    conv = client.post("/api/conversations", json={}, headers=auth(alice)).json()
    send(client, alice, conv["id"], "one")
    send(client, alice, conv["id"], "two")

    report = client.get("/api/admin/usage", headers=auth(admin)).json()
    by_name = {r["username"]: r for r in report["users"]}
    assert set(by_name) == {"admin", "alice", "bob"}
    alice_row = by_name["alice"]
    assert alice_row["messages"] == 2 and alice_row["used"] > 0
    assert alice_row["budget"] == 500_000 and alice_row["remaining"] == 500_000 - alice_row["used"]
    assert by_name["bob"]["used"] == 0
    assert report["totals"]["used"] == alice_row["used"] and report["totals"]["users"] == 3

    mine = client.get("/api/auth/me/usage", headers=auth(alice)).json()
    assert mine["used"] == alice_row["used"] and mine["period"] == "month"


def test_token_budget_is_enforced_and_editable(client):
    alice, admin = login(client, "alice", "alice"), login(client, "admin", "admin")
    alice_id = client.get("/api/auth/me", headers=auth(alice)).json()["id"]
    conv = client.post("/api/conversations", json={}, headers=auth(alice)).json()
    send(client, alice, conv["id"], "first")

    res = client.patch(f"/api/admin/users/{alice_id}", json={"token_budget": 1}, headers=auth(admin))
    assert res.status_code == 200 and res.json()["token_budget"] == 1
    res = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "again"}, headers=auth(alice))
    assert res.status_code == 429
    assert client.get("/api/auth/me/usage", headers=auth(alice)).json()["remaining"] == 0

    # null resets to the configured default
    res = client.patch(f"/api/admin/users/{alice_id}", json={"token_budget": None}, headers=auth(admin))
    assert res.json()["token_budget"] is None
    send(client, alice, conv["id"], "works again")


def test_info_reports_stub_provider(client):
    assert client.get("/api/info").json() == {
        "provider": "stub", "model": "stub-fixed-response", "chat_store": "sqlite", "telemetry": "file",
    }


def test_file_telemetry_records_request_span_and_metrics(client, tmp_path):
    alice = login(client, "alice", "alice")
    conv = client.post("/api/conversations", json={}, headers=auth(alice)).json()
    send(client, alice, conv["id"], "trace me")

    lines = (tmp_path / "telemetry.log").read_text().splitlines()
    txns = [l for l in lines if " txn " in l and "/api/conversations/{conversation_id}/messages" in l]
    assert len(txns) == 1 and "status=200" in txns[0] and "user=alice" in txns[0]
    txn_id = txns[0].split()[2]
    in_request = [l for l in lines if l.split()[2] == txn_id]
    names = {l.split()[3] for l in in_request}
    assert {"llm.stream", "store.append_message", "store.record_usage", "llm.output_tokens", "llm.time_to_first_token_ms"} <= names
    # Login does not create a user span before authentication, but the store call is traced.
    assert any(" span " in l and "store.get_by_username" in l for l in lines)
