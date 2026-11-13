"""Tests for the Landlord Rent Tracker API: auth, tenants, payments, overdue."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from main import (
    Base,
    User,
    app,
    get_db,
    hash_password,
    seed_admin,
    seed_units,
    verify_password,
)

TEST_EMAIL = "landlord@example.com"
TEST_PASSWORD = "secret123"

# Separate in-memory database so tests never touch rent_tracker.db.
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def client():
    """Fresh database (with seeded units and landlord) and test client."""
    Base.metadata.create_all(bind=engine)
    with TestingSessionLocal() as db:
        seed_units(db)
        db.add(
            User(
                name="Landlord",
                email=TEST_EMAIL,
                hashed_password=hash_password(TEST_PASSWORD),
                must_change_password=False,
            )
        )
        db.commit()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def auth_headers(client, email=TEST_EMAIL, password=TEST_PASSWORD):
    """Log in and return Authorization headers."""
    response = client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def create_tenant(client, headers, unit_id=1, name="John Doe"):
    """Create an active tenant for a unit."""
    return client.post(
        "/tenants",
        json={"name": name, "phone": "08012345678", "unit_id": unit_id},
        headers=headers,
    )


def create_rent_record(client, headers, tenant_id, amount_due=1000.0, due_date=None):
    """Create a rent record, defaulting to a due date five days from now."""
    return client.post(
        "/rent-records",
        json={
            "tenant_id": tenant_id,
            "amount_due": amount_due,
            "due_date": (due_date or date.today() + timedelta(days=5)).isoformat(),
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_login_and_protected_routes(client):
    # Protected route rejects anonymous requests.
    assert client.get("/units").status_code == 401

    # Wrong password is rejected, correct password returns a JWT.
    wrong = client.post(
        "/auth/login",
        json={"email": TEST_EMAIL, "password": "wrong-password"},
    )
    assert wrong.status_code == 401

    login = client.post(
        "/auth/login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"
    assert login.json()["access_token"]
    assert login.json()["must_change_password"] is False

    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    units = client.get("/units", headers=headers)
    assert units.status_code == 200
    assert len(units.json()) == 7  # 5 self-contain + 2 twin flats

    # A garbage token is rejected.
    bad = client.get("/units", headers={"Authorization": "Bearer not-a-token"})
    assert bad.status_code == 401


def test_register_endpoint_is_removed(client):
    response = client.post(
        "/auth/register",
        json={
            "name": "Another",
            "email": "another@example.com",
            "password": "secret123",
        },
    )
    assert response.status_code == 404


def test_startup_seed_creates_admin(client, monkeypatch, capsys):
    with TestingSessionLocal() as db:
        db.query(User).delete()
        db.commit()

        monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("ADMIN_PASSWORD", "startup-secret")

        assert seed_admin(db) is True
        user = db.query(User).one()
        assert user.email == "admin@example.com"
        assert user.must_change_password is True
        assert verify_password("startup-secret", user.hashed_password)

        # It must not create a second account on the next startup.
        assert seed_admin(db) is False
        assert db.query(User).count() == 1

    captured = capsys.readouterr()
    assert "startup-secret" not in captured.out


def test_login_returns_must_change_password_flag(client):
    with TestingSessionLocal() as db:
        user = db.query(User).filter(User.email == TEST_EMAIL).one()
        user.must_change_password = True
        db.commit()

    login = client.post(
        "/auth/login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True


def test_change_password_rejects_wrong_current_password(client):
    headers = auth_headers(client)
    response = client.post(
        "/auth/change-password",
        json={
            "current_password": "wrong-password",
            "new_password": "brand-new-secret",
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Current password is incorrect"

    # The old password still works and the flag was not changed.
    login = client.post(
        "/auth/login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert login.status_code == 200
    assert login.json()["must_change_password"] is False


def test_change_password_success(client):
    headers = auth_headers(client)
    response = client.post(
        "/auth/change-password",
        json={
            "current_password": TEST_PASSWORD,
            "new_password": "brand-new-secret",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Password changed successfully"

    old_login = client.post(
        "/auth/login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/auth/login",
        json={"email": TEST_EMAIL, "password": "brand-new-secret"},
    )
    assert new_login.status_code == 200
    assert new_login.json()["must_change_password"] is False


def test_change_password_requires_login(client):
    response = client.post(
        "/auth/change-password",
        json={
            "current_password": TEST_PASSWORD,
            "new_password": "brand-new-secret",
        },
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Tenants and units
# ---------------------------------------------------------------------------
def test_tenant_creation_marks_unit_occupied(client):
    headers = auth_headers(client)

    response = create_tenant(client, headers, unit_id=1)
    assert response.status_code == 201
    tenant = response.json()
    assert tenant["unit_id"] == 1
    assert tenant["move_out_date"] is None

    unit = client.get("/units/1", headers=headers).json()
    assert unit["status"] == "occupied"

    # The same unit cannot take a second active tenant.
    second = create_tenant(client, headers, unit_id=1, name="Jane Doe")
    assert second.status_code == 400

    # Six of seven units remain vacant.
    assert len(client.get("/dashboard/vacant", headers=headers).json()) == 6

    # Moving out frees the unit.
    move_out = client.post(f"/tenants/{tenant['id']}/move-out", headers=headers)
    assert move_out.status_code == 200
    assert move_out.json()["move_out_date"] == date.today().isoformat()

    unit = client.get("/units/1", headers=headers).json()
    assert unit["status"] == "vacant"
    assert len(client.get("/dashboard/vacant", headers=headers).json()) == 7

    # Moving out twice is a bad request.
    assert client.post(f"/tenants/{tenant['id']}/move-out", headers=headers).status_code == 400


def test_tenant_and_unit_not_found(client):
    headers = auth_headers(client)

    assert client.get("/units/999", headers=headers).status_code == 404
    assert client.get("/tenants/999", headers=headers).status_code == 404
    assert create_tenant(client, headers, unit_id=999).status_code == 404
    assert client.post("/tenants/999/move-out", headers=headers).status_code == 404


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
def test_payment_updates_rent_status(client):
    headers = auth_headers(client)
    tenant = create_tenant(client, headers, unit_id=1).json()
    record = create_rent_record(client, headers, tenant["id"], amount_due=1000.0).json()
    assert record["status"] == "unpaid"
    assert record["amount_paid"] == 0.0

    # Partial payment.
    partial = client.post(
        "/payments",
        json={"rent_record_id": record["id"], "amount": 400.0},
        headers=headers,
    )
    assert partial.status_code == 201
    assert partial.json()["amount"] == 400.0

    current = client.get(f"/rent-records/{record['id']}", headers=headers).json()
    assert current["amount_paid"] == 400.0
    assert current["status"] == "partial"

    # Remaining balance clears the record.
    final = client.post(
        "/payments",
        json={"rent_record_id": record["id"], "amount": 600.0},
        headers=headers,
    )
    assert final.status_code == 201

    current = client.get(f"/rent-records/{record['id']}", headers=headers).json()
    assert current["amount_paid"] == 1000.0
    assert current["status"] == "paid"

    # Overpayment and missing records are rejected.
    overpay = client.post(
        "/payments",
        json={"rent_record_id": record["id"], "amount": 1.0},
        headers=headers,
    )
    assert overpay.status_code == 400
    assert client.post(
        "/payments",
        json={"rent_record_id": 999, "amount": 10.0},
        headers=headers,
    ).status_code == 404

    # Payment history is listed newest first and can be filtered.
    history = client.get("/payments", headers=headers)
    assert history.status_code == 200
    assert len(history.json()) == 2
    assert {payment["amount"] for payment in history.json()} == {400.0, 600.0}

    filtered = client.get(
        "/payments", params={"rent_record_id": record["id"]}, headers=headers
    )
    assert len(filtered.json()) == 2


# ---------------------------------------------------------------------------
# Overdue dashboard and summary
# ---------------------------------------------------------------------------
def test_overdue_dashboard_and_summary(client):
    headers = auth_headers(client)
    tenant = create_tenant(client, headers, unit_id=1, name="John Doe").json()
    due = date.today() - timedelta(days=3)
    record = create_rent_record(
        client, headers, tenant["id"], amount_due=50_000.0, due_date=due
    ).json()

    overdue = client.get("/dashboard/overdue", headers=headers)
    assert overdue.status_code == 200
    rows = overdue.json()
    assert len(rows) == 1
    assert rows[0]["rent_record_id"] == record["id"]
    assert rows[0]["tenant_name"] == "John Doe"
    assert rows[0]["unit_name"] == "Self-contain 1"
    assert rows[0]["days_overdue"] == 3

    summary = client.get("/dashboard/summary", headers=headers).json()
    assert summary == {
        "total_expected": 50_000.0,
        "total_collected": 0.0,
        "total_outstanding": 50_000.0,
        "overdue_tenants": 1,
    }

    # A partial payment keeps the record overdue but updates the totals.
    client.post(
        "/payments",
        json={"rent_record_id": record["id"], "amount": 20_000.0},
        headers=headers,
    )
    summary = client.get("/dashboard/summary", headers=headers).json()
    assert summary["total_collected"] == 20_000.0
    assert summary["total_outstanding"] == 30_000.0
    assert summary["overdue_tenants"] == 1
    assert len(client.get("/dashboard/overdue", headers=headers).json()) == 1

    # Paying the rest clears it from the overdue list.
    client.post(
        "/payments",
        json={"rent_record_id": record["id"], "amount": 30_000.0},
        headers=headers,
    )
    assert client.get("/dashboard/overdue", headers=headers).json() == []
    summary = client.get("/dashboard/summary", headers=headers).json()
    assert summary["total_collected"] == 50_000.0
    assert summary["total_outstanding"] == 0.0
    assert summary["overdue_tenants"] == 0


def test_future_rent_is_not_overdue(client):
    headers = auth_headers(client)
    tenant = create_tenant(client, headers, unit_id=1).json()
    create_rent_record(
        client, headers, tenant["id"], due_date=date.today() + timedelta(days=5)
    )

    assert client.get("/dashboard/overdue", headers=headers).json() == []
    summary = client.get("/dashboard/summary", headers=headers).json()
    assert summary["overdue_tenants"] == 0
    assert summary["total_expected"] == 1000.0
