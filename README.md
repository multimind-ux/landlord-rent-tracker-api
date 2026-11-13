# Landlord Rent Tracker API

A FastAPI backend for a landlord managing **one building**: 5 self-contain rooms and a twin flat (2 units). Built with **FastAPI**, **SQLAlchemy**, **SQLite/PostgreSQL**, and **JWT authentication**.

## Features

- Seeded units on first startup: `Self-contain 1`–`5`, `Twin Flat A`, `Twin Flat B`
- One landlord account, created on startup from `ADMIN_EMAIL`/`ADMIN_PASSWORD` when the database is empty
- PBKDF2-SHA256 password hashing, JWT login, and a forced first-login password change
- No public registration endpoint
- Protected CRUD for units, tenants, and rent records
- Creating a tenant marks their unit **occupied**; moving them out marks it **vacant**
- Payments update a rent record to **partial** or **paid**
- Dashboards for overdue rent, vacant units, and totals
- CORS enabled for the local Vite frontend and the deployed Vercel frontend

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

`rent_tracker.db` is created automatically, and the seven units are seeded on first start. If the database has no users, set `ADMIN_EMAIL` and `ADMIN_PASSWORD` before starting so the landlord account is created.

> For production, set a strong `SECRET_KEY` environment variable instead of the development default. Set `DATABASE_URL` to a PostgreSQL connection string to use a hosted database; otherwise the API falls back to SQLite.

## Production deployment

The API is deployed on Vercel with a Neon Postgres database:

- API: <https://landlord-rent-tracker-api.vercel.app>
- Swagger docs: <https://landlord-rent-tracker-api.vercel.app/docs>

Vercel project: `multimind/landlord-rent-tracker-api`

| Environment variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Neon Postgres connection string (local default: SQLite file) |
| `SECRET_KEY` | JWT signing secret |
| `ADMIN_EMAIL` | Email for the single landlord account created on first startup |
| `ADMIN_PASSWORD` | Initial password for that account; the user must change it on first login |

Deploy from this directory with:

```bash
vercel --prod
```

## Auth flow

1. On startup, if there are no users, the API creates the single landlord from `ADMIN_EMAIL` and `ADMIN_PASSWORD`. The password is hashed and never logged.
2. Log in with `POST /auth/login` to receive a JWT and a `must_change_password` flag.
3. While the flag is true, change the password with `POST /auth/change-password`.
4. Click **Authorize** in `/docs` (or send `Authorization: Bearer <token>`) for every other endpoint.

Tokens expire after 2 hours. There is no public registration.

## Endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| POST | `/auth/login` | Get a JWT and the `must_change_password` flag |
| POST | `/auth/change-password` | Change the logged-in landlord's password |
| POST/GET | `/units` | Create / list units |
| GET/PUT/DELETE | `/units/{id}` | Read / update / delete a unit |
| POST/GET | `/tenants` | Create / list tenants |
| GET/PUT/DELETE | `/tenants/{id}` | Read / update / delete a tenant |
| POST | `/tenants/{id}/move-out` | Move a tenant out and vacate the unit |
| POST/GET | `/rent-records` | Create / list rent records |
| GET/PUT/DELETE | `/rent-records/{id}` | Read / update / delete a rent record |
| POST/GET | `/payments` | Record a payment / list payment history |
| GET | `/dashboard/overdue` | Unpaid rent past its due date, with days overdue |
| GET | `/dashboard/vacant` | Vacant units |
| GET | `/dashboard/summary` | Expected, collected, outstanding, overdue tenants |

Errors: `404` when a unit/tenant/rent record is missing; `400` for occupied units, invalid move-outs, overpayments, and an incorrect current password when changing the password.

## Curl examples

Set the admin account before the first local start (or export these in your shell):

```bash
export ADMIN_EMAIL="admin@example.com"
export ADMIN_PASSWORD="replace-with-a-strong-password"
```

The account is created only when the users table is empty. Log in and save the token:

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@example.com", "password": "replace-with-a-strong-password"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")
```

Change the password after the first login (new passwords must be at least 8 characters):

```bash
curl -X POST http://127.0.0.1:8000/auth/change-password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"current_password": "replace-with-a-strong-password", "new_password": "my-new-secret"}'
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

The tests use an in-memory SQLite database and cover startup admin seeding, the forced-change flag, login, password changes, tenant creation, payments, and overdue logic.
