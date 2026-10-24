"""Sandbox portal: login wall, form validation, filing lands in the jsonl, confirmation shows the ref."""
import json

import pytest

from flock.portal import create_app

GOOD = {"channel": "doordash", "order_ids": "ORD-1001, ORD-1002", "amount": "2.50", "category": "commission",
        "merchant_ref": "F-0001", "message": "Charged 6.50 commission; contract says 20%."}


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    app.config["TESTING"] = True
    c = app.test_client()
    c.data_dir = tmp_path
    return c


def login(c, user="sandbox", pw="sandbox"):
    return c.post("/login", data={"username": user, "password": pw})


def test_pages_require_login(client):
    for path in ("/", "/disputes", "/disputes/new"):
        r = client.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_login_page_is_labeled_sandbox(client):
    assert b"SANDBOX" in client.get("/login").data


def test_wrong_password_rejected(client):
    r = login(client, pw="nope")
    assert r.status_code == 401 and b"Wrong username or password" in r.data
    assert client.get("/disputes/new").status_code == 302


def test_file_dispute_end_to_end(client):
    assert login(client).headers["Location"].endswith("/disputes/new")
    r = client.post("/disputes/new", data=GOOD)
    assert r.status_code == 302
    ref = r.headers["Location"].rsplit("/", 1)[-1]
    assert ref.startswith("SBX-")

    page = client.get(f"/disputes/{ref}").data.decode()
    assert f'id="portal-ref">{ref}<' in page and "$2.50" in page

    rows = [json.loads(x) for x in (client.data_dir / "disputes.jsonl").read_text().splitlines()]
    assert rows == [{**rows[0], "portal_ref": ref, "channel": "doordash", "order_ids": ["ORD-1001", "ORD-1002"],
                     "amount": 2.5, "category": "commission", "merchant_ref": "F-0001", "via": "portal"}]
    assert ref in client.get("/disputes").data.decode()


@pytest.mark.parametrize("field,value", [("channel", "acme"), ("order_ids", " "), ("amount", "-3"),
                                         ("amount", "abc"), ("category", "x"), ("message", "")])
def test_form_validation(client, field, value):
    login(client)
    r = client.post("/disputes/new", data={**GOOD, field: value})
    assert r.status_code == 400 and b"form-error" in r.data
    assert not (client.data_dir / "disputes.jsonl").exists()


def test_unknown_ref_404(client):
    login(client)
    assert client.get("/disputes/SBX-NOPE").status_code == 404
