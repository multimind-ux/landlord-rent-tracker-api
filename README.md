# Landlord Rent Tracker API

A FastAPI backend for a landlord managing **one building**: 5 self-contain rooms and a twin flat (2 units). Built with **FastAPI**, **SQLAlchemy**, **SQLite**, and **JWT authentication**.

## Features

- Seeded units on first startup: `Self-contain 1`–`5`, `Twin Flat A`, `Twin Flat B`
- One landlord account with a hashed password (PBKDF2-SHA256) and JWT login
- Protected CRUD for units, tenants, and rent records
- Creating a tenant marks their unit **occupied**; moving them out marks it **vacant**
- Payments update a rent record to **partial** or **paid**
- Dashboards for overdue rent, vacant units, and totals
- CORS enabled for `http://localhost:5173` (typical Vite frontend)

## Setup

```bash
cd landlord-rent-tracker-api

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

## Run

```bash
python main.py
```

or:

```bash
uvicorn main:app --reload
```

- API: <http://127.0.0.1:8000>
- Swagger docs: <http://127.0.0.1:8000/docs>

`rent_tracker.db` is created automatically, and the seven units are seeded on first start.

> For production, set a strong `SECRET_KEY` environment variable instead of the development default.

## Auth flow

1. Register the landlord **once** with `POST /auth/register`.
2. Log in with `POST /auth/login` to receive a JWT.
3. Click **Authorize** in `/docs` (or send `Authorization: Bearer <token>`) for every other endpoint.

Tokens expire after 2 hours.

## Endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| POST | `/auth/register` | Register the landlord (first run only) |
| POST | `/auth/login` | Get a JWT |
| POST/GET | `/units` | Create / list units |
| GET/PUT/DELETE | `/units/{id}` | Read / update / delete a unit |
| POST/GET | `/tenants` | Create / list tenants |
| GET/PUT/DELETE | `/tenants/{id}` | Read / update / delete a tenant |
| POST | `/tenants/{id}/move-out` | Move a tenant out and vacate the unit |
| POST/GET | `/rent-records` | Create / list rent records |
| GET/PUT/DELETE | `/rent-records/{id}` | Read / update / delete a rent record |
| POST | `/payments` | Record a payment (partial or full) |
| GET | `/dashboard/overdue` | Unpaid rent past its due date, with days overdue |
| GET | `/dashboard/vacant` | Vacant units |
| GET | `/dashboard/summary` | Expected, collected, outstanding, overdue tenants |

Errors: `404` when a unit/tenant/rent record is missing; `400` for duplicate registration, occupied units, invalid move-outs, and overpayments.

## Curl examples

Register (only works once per database):

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"name": "Landlord", "email": "landlord@example.com", "password": "secret123"}'
```

Log in and save the token:

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "landlord@example.com", "password": "secret123"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")
```

List the seeded units:

```bash
curl http://127.0.0.1:8000/units -H "Authorization: Bearer $TOKEN"
```

Create a tenant for unit 1 (marks it occupied):

```bash
curl -X POST http://127.0.0.1:8000/tenants \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "John Doe", "phone": "08012345678", "unit_id": 1}'
```

Create a rent record:

```bash
curl -X POST http://127.0.0.1:8000/rent-records \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": 1, "amount_due": 50000, "due_date": "2026-06-01"}'
```

Record a partial payment:

```bash
curl -X POST http://127.0.0.1:8000/payments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"rent_record_id": 1, "amount": 20000}'
```

Move the tenant out (unit becomes vacant):

```bash
curl -X POST http://127.0.0.1:8000/tenants/1/move-out \
  -H "Authorization: Bearer $TOKEN"
```

Dashboards:

```bash
curl http://127.0.0.1:8000/dashboard/overdue -H "Authorization: Bearer $TOKEN"
curl http://127.0.0.1:8000/dashboard/vacant -H "Authorization: Bearer $TOKEN"
curl http://127.0.0.1:8000/dashboard/summary -H "Authorization: Bearer $TOKEN"
```

## Tests

```bash
pytest -v
```

The tests use an in-memory SQLite database and cover login, tenant creation, payments, and overdue logic.
