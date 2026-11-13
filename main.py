"""Landlord Rent Tracker API - FastAPI + SQLAlchemy + SQLite.

One landlord manages one building with a twin flat (two units) and five
self-contain rooms. All unit, tenant and rent endpoints require a JWT.
"""

import hashlib
import hmac
import logging
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import httpx
import jwt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./rent_tracker.db")

# SQLAlchemy defaults to psycopg2 for postgresql:// URLs; this project uses
# psycopg 3, so make the driver explicit. Neon's DATABASE_URL also uses the
# postgresql:// scheme.
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace(
        "postgres://", "postgresql+psycopg://", 1
    )
elif SQLALCHEMY_DATABASE_URL.startswith("postgresql://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace(
        "postgresql://", "postgresql+psycopg://", 1
    )

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 120

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    # Serverless functions can scale out quickly; avoid holding idle
    # connections open against the hosted database.
    engine = create_engine(SQLALCHEMY_DATABASE_URL, poolclass=NullPool)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    """Provide a database session and always close it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Enums and database models
# ---------------------------------------------------------------------------
class UnitType(str, Enum):
    self_contain = "self_contain"
    twin_flat = "twin_flat"


class UnitStatus(str, Enum):
    occupied = "occupied"
    vacant = "vacant"


class RentStatus(str, Enum):
    unpaid = "unpaid"
    partial = "partial"
    paid = "paid"


class User(Base):
    """The landlord account."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    must_change_password = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class Unit(Base):
    """A room/flat in the building."""

    __tablename__ = "units"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    type = Column(SAEnum(UnitType), nullable=False)
    rent_amount = Column(Float, nullable=False)
    status = Column(SAEnum(UnitStatus), nullable=False, default=UnitStatus.vacant)

    tenants = relationship("Tenant", back_populates="unit")


class Tenant(Base):
    """A tenant renting a unit. move_out_date is None while still active."""

    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    unit_id = Column(Integer, ForeignKey("units.id"), nullable=False)
    move_in_date = Column(Date, nullable=False, default=date.today)
    move_out_date = Column(Date, nullable=True)

    unit = relationship("Unit", back_populates="tenants")
    rent_records = relationship(
        "RentRecord", back_populates="tenant", cascade="all, delete-orphan"
    )


class RentRecord(Base):
    """Rent charged to a tenant for a period."""

    __tablename__ = "rent_records"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    amount_due = Column(Float, nullable=False)
    amount_paid = Column(Float, nullable=False, default=0.0)
    due_date = Column(Date, nullable=False)
    status = Column(SAEnum(RentStatus), nullable=False, default=RentStatus.unpaid)

    tenant = relationship("Tenant", back_populates="rent_records")
    payments = relationship(
        "Payment", back_populates="rent_record", cascade="all, delete-orphan"
    )


class Payment(Base):
    """A payment made against a rent record."""

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    rent_record_id = Column(Integer, ForeignKey("rent_records.id"), nullable=False)
    amount = Column(Float, nullable=False)
    date = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    rent_record = relationship("RentRecord", back_populates="payments")


class NotificationType(str, Enum):
    due_soon = "due_soon"
    due_today = "due_today"
    overdue = "overdue"


class Notification(Base):
    """An in-app rent reminder for the landlord."""

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(SAEnum(NotificationType), nullable=False, index=True)
    message = Column(String, nullable=False)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rent_record_id = Column(
        Integer,
        ForeignKey("rent_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_read = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    tenant = relationship("Tenant")
    rent_record = relationship("RentRecord")


class NotificationSettings(Base):
    """The landlord's reminder preferences (a single row)."""

    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, nullable=False)
    notify_days_before = Column(JSON, nullable=False, default=lambda: [7, 1])
    notify_on_due_date = Column(Boolean, nullable=False, default=True)
    overdue_reminder_every_days = Column(Integer, nullable=False, default=3)
    email_enabled = Column(Boolean, nullable=False, default=True)


# Create the tables on startup (fine for a small demo project).
Base.metadata.create_all(bind=engine)


def ensure_user_schema() -> None:
    """Add columns introduced after the first release to an existing users table."""
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "must_change_password" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE users "
                    "ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT TRUE"
                )
            )


ensure_user_schema()


def seed_units(db: Session) -> None:
    """Seed the building's seven units once (5 self-contain + 2 twin flat)."""
    if db.query(Unit).count() > 0:
        return

    for number in range(1, 6):
        db.add(
            Unit(
                name=f"Self-contain {number}",
                type=UnitType.self_contain,
                rent_amount=50_000.0,
                status=UnitStatus.vacant,
            )
        )
    for letter in ("A", "B"):
        db.add(
            Unit(
                name=f"Twin Flat {letter}",
                type=UnitType.twin_flat,
                rent_amount=120_000.0,
                status=UnitStatus.vacant,
            )
        )
    db.commit()


# Seed the real database on import/startup.
with SessionLocal() as _db:
    seed_units(_db)


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256) and JWT helpers
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hash a password with a random salt."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), 200_000
    )
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a plain password against the stored hash."""
    try:
        _algorithm, salt, expected = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), 200_000
    )
    return hmac.compare_digest(digest.hex(), expected)


def seed_admin(db: Session) -> bool:
    """Create the single landlord from env vars when there are no users.

    Returns True when an account was created. The password is never logged.
    """
    if db.query(User).count() > 0:
        return False

    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        print(
            "No landlord account exists. Set ADMIN_EMAIL and ADMIN_PASSWORD "
            "before starting the API to create one."
        )
        return False

    db.add(
        User(
            name="Landlord",
            email=email,
            hashed_password=hash_password(password),
            must_change_password=True,
        )
    )
    db.flush()
    if db.query(NotificationSettings).count() == 0:
        db.add(NotificationSettings(email=email))
    db.commit()
    return True


def ensure_notification_settings(db: Session) -> None:
    """Backfill the single settings row for an already-seeded landlord."""
    if db.query(NotificationSettings).count() > 0:
        return

    user = db.query(User).order_by(User.id).first()
    email = (user.email if user else os.getenv("ADMIN_EMAIL", "")).strip()
    if not email:
        return

    db.add(NotificationSettings(email=email))
    db.commit()


# Seed the admin account on import/startup when the database has no users.
with SessionLocal() as _db:
    seed_admin(_db)
    ensure_notification_settings(_db)


def create_access_token(user: User) -> str:
    """Create a signed JWT for the landlord."""
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {"sub": str(user.id), "email": user.email, "exp": expires}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Dependency that validates the Bearer token and returns the landlord."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized

    try:
        payload = jwt.decode(
            credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM]
        )
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized

    user = db.get(User, user_id)
    if user is None:
        raise unauthorized
    return user


cron_bearer_scheme = HTTPBearer(auto_error=False)


def require_cron_secret(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        cron_bearer_scheme
    ),
) -> None:
    """Authorize the scheduled reminder job with CRON_SECRET, not a user JWT."""
    cron_secret = os.getenv("CRON_SECRET", "")
    if (
        not cron_secret
        or credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, cron_secret)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing cron secret",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class MessageOut(BaseModel):
    message: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    must_change_password: bool


class UnitCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: UnitType
    rent_amount: float = Field(..., ge=0)
    status: UnitStatus = UnitStatus.vacant


class UnitUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    type: UnitType | None = None
    rent_amount: float | None = Field(None, ge=0)
    status: UnitStatus | None = None


class UnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: UnitType
    rent_amount: float
    status: UnitStatus


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    phone: str = Field(..., min_length=1, max_length=30)
    unit_id: int
    move_in_date: date | None = None


class TenantUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    phone: str | None = Field(None, min_length=1, max_length=30)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    unit_id: int
    move_in_date: date
    move_out_date: date | None


class RentRecordCreate(BaseModel):
    tenant_id: int
    amount_due: float = Field(..., gt=0)
    due_date: date


class RentRecordUpdate(BaseModel):
    amount_due: float | None = Field(None, gt=0)
    due_date: date | None = None


class RentRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    amount_due: float
    amount_paid: float
    due_date: date
    status: RentStatus


class PaymentCreate(BaseModel):
    rent_record_id: int
    amount: float = Field(..., gt=0)


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rent_record_id: int
    amount: float
    date: datetime


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: NotificationType
    message: str
    tenant_id: int
    rent_record_id: int
    is_read: bool
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread_count: int


class ReadAllOut(BaseModel):
    updated: int


class NotificationSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    notify_days_before: list[int]
    notify_on_due_date: bool
    overdue_reminder_every_days: int
    email_enabled: bool


class NotificationSettingsUpdate(BaseModel):
    email: str | None = Field(None, min_length=3, max_length=200)
    notify_days_before: list[int] | None = None
    notify_on_due_date: bool | None = None
    overdue_reminder_every_days: int | None = Field(None, ge=1, le=365)
    email_enabled: bool | None = None


class ReminderResultOut(BaseModel):
    created: int
    email_sent: bool


class OverdueOut(BaseModel):
    rent_record_id: int
    tenant_id: int
    tenant_name: str
    unit_name: str
    amount_due: float
    amount_paid: float
    due_date: date
    days_overdue: int


class SummaryOut(BaseModel):
    total_expected: float
    total_collected: float
    total_outstanding: float
    overdue_tenants: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_unit_or_404(unit_id: int, db: Session) -> Unit:
    unit = db.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unit with id {unit_id} not found",
        )
    return unit


def get_tenant_or_404(tenant_id: int, db: Session) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant with id {tenant_id} not found",
        )
    return tenant


def get_rent_record_or_404(record_id: int, db: Session) -> RentRecord:
    record = db.get(RentRecord, record_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rent record with id {record_id} not found",
        )
    return record


def calculate_rent_status(amount_paid: float, amount_due: float) -> RentStatus:
    """Derive the rent status from the amounts paid and due."""
    if amount_paid <= 0:
        return RentStatus.unpaid
    if amount_paid < amount_due:
        return RentStatus.partial
    return RentStatus.paid


def overdue_records(db: Session) -> list[RentRecord]:
    """Unpaid/partial rent records whose due date has passed."""
    return (
        db.query(RentRecord)
        .filter(
            RentRecord.status != RentStatus.paid,
            RentRecord.due_date < date.today(),
        )
        .all()
    )


def get_or_create_notification_settings(db: Session) -> NotificationSettings:
    """Return the landlord's single settings row, creating it if needed."""
    settings = (
        db.query(NotificationSettings)
        .order_by(NotificationSettings.id)
        .first()
    )
    if settings is not None:
        return settings

    user = db.query(User).order_by(User.id).first()
    email = (user.email if user else os.getenv("ADMIN_EMAIL", "")).strip()
    settings = NotificationSettings(email=email)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def notification_created_on(
    db: Session,
    rent_record_id: int,
    notification_type: NotificationType,
    day: date,
) -> bool:
    """Whether this record already has this rule's notification for the day."""
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    return (
        db.query(Notification.id)
        .filter(
            Notification.rent_record_id == rent_record_id,
            Notification.type == notification_type,
            Notification.created_at >= start,
            Notification.created_at < end,
        )
        .first()
        is not None
    )


def last_overdue_notification(
    db: Session, rent_record_id: int
) -> Notification | None:
    """Return the most recent overdue notification for a rent record."""
    return (
        db.query(Notification)
        .filter(
            Notification.rent_record_id == rent_record_id,
            Notification.type == NotificationType.overdue,
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .first()
    )


def reminder_type_for(
    db: Session,
    record: RentRecord,
    settings: NotificationSettings,
    today: date,
) -> tuple[NotificationType | None, int]:
    """Return the matching rule and days overdue (0 for non-overdue rules)."""
    days_until_due = (record.due_date - today).days

    if days_until_due == 0 and settings.notify_on_due_date:
        return NotificationType.due_today, 0

    try:
        notify_days = {int(day) for day in (settings.notify_days_before or [])}
    except (TypeError, ValueError):
        notify_days = set()

    if days_until_due > 0 and days_until_due in notify_days:
        return NotificationType.due_soon, 0

    if days_until_due < 0:
        days_overdue = -days_until_due
        every = max(1, int(settings.overdue_reminder_every_days or 3))
        last = last_overdue_notification(db, record.id)
        # The first overdue reminder is sent as soon as the record is overdue;
        # later reminders repeat every `overdue_reminder_every_days` days.
        if last is None or (today - last.created_at.date()).days >= every:
            return NotificationType.overdue, days_overdue

    return None, 0


def build_notification_message(
    record: RentRecord,
    notification_type: NotificationType,
    outstanding: float,
    days_overdue: int,
    today: date,
) -> str:
    """Create a human-readable message for an in-app reminder."""
    tenant_name = record.tenant.name
    unit_name = record.tenant.unit.name
    amount = f"₦{outstanding:,.2f}"

    if notification_type == NotificationType.due_today:
        return (
            f"Rent for {tenant_name} ({unit_name}) is due today: {amount}."
        )
    if notification_type == NotificationType.due_soon:
        days = (record.due_date - today).days
        return (
            f"Rent for {tenant_name} ({unit_name}) is due in {days} "
            f"day{'s' if days != 1 else ''} on "
            f"{record.due_date.isoformat()}: {amount}."
        )
    return (
        f"Rent for {tenant_name} ({unit_name}) is {days_overdue} "
        f"day{'s' if days_overdue != 1 else ''} overdue "
        f"(due {record.due_date.isoformat()}): {amount}."
    )


def send_reminder_email(
    settings: NotificationSettings,
    details: list[dict],
    today: date,
) -> bool:
    """Send one summary email through Resend. Failures are logged, not raised."""
    if not settings.email_enabled or not details:
        return False

    api_key = os.getenv("RESEND_API_KEY", "").strip()
    email_from = os.getenv("EMAIL_FROM", "").strip()
    recipient = (settings.email or "").strip()
    if not api_key or not email_from or not recipient:
        logger.error(
            "Reminder email skipped: RESEND_API_KEY, EMAIL_FROM, or "
            "notification settings email is missing"
        )
        return False

    lines = [f"Rent reminders for {today.isoformat()}", ""]
    for detail in details:
        if detail["type"] == NotificationType.overdue:
            timing = (
                f"{detail['days_overdue']} day"
                f"{'s' if detail['days_overdue'] != 1 else ''} overdue"
            )
        elif detail["type"] == NotificationType.due_today:
            timing = "due today"
        else:
            timing = f"due {detail['due_date'].isoformat()}"

        lines.append(
            f"- {detail['tenant_name']} ({detail['unit_name']}) — "
            f"₦{detail['amount_owed']:,.2f} — {timing}"
        )

    payload = {
        "from": email_from,
        "to": [recipient],
        "subject": f"Rent reminders for {today.isoformat()}",
        "text": "\n".join(lines),
    }

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send reminder email via Resend")
        return False


# ---------------------------------------------------------------------------
# App and auth routes
# ---------------------------------------------------------------------------
app = FastAPI(title="Landlord Rent Tracker API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://landlord-rent-tracker-web.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/auth/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    """Log in with the admin-created email and password to receive a JWT."""
    email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    return TokenOut(
        access_token=create_access_token(user),
        must_change_password=bool(user.must_change_password),
    )


@app.post("/auth/change-password", response_model=MessageOut)
def change_password(
    payload: ChangePasswordIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change the password for the logged-in landlord."""
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current password",
        )

    user.hashed_password = hash_password(payload.new_password)
    user.must_change_password = False
    db.commit()
    return MessageOut(message="Password changed successfully")


# ---------------------------------------------------------------------------
# Units (protected)
# ---------------------------------------------------------------------------
@app.post("/units", response_model=UnitOut, status_code=status.HTTP_201_CREATED)
def create_unit(
    payload: UnitCreate,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Add a new unit."""
    unit = Unit(**payload.model_dump())
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


@app.get("/units", response_model=list[UnitOut])
def list_units(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List units with pagination."""
    return db.query(Unit).offset(skip).limit(limit).all()


@app.get("/units/{unit_id}", response_model=UnitOut)
def get_unit(
    unit_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Get one unit by id."""
    return get_unit_or_404(unit_id, db)


@app.put("/units/{unit_id}", response_model=UnitOut)
def update_unit(
    unit_id: int,
    payload: UnitUpdate,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Update a unit."""
    unit = get_unit_or_404(unit_id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(unit, field, value)
    db.commit()
    db.refresh(unit)
    return unit


@app.delete("/units/{unit_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_unit(
    unit_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Delete a unit. Units with tenants cannot be deleted."""
    unit = get_unit_or_404(unit_id, db)
    if unit.tenants:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a unit that has tenants",
        )
    db.delete(unit)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Tenants (protected)
# ---------------------------------------------------------------------------
@app.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Create a tenant and mark their unit as occupied."""
    unit = get_unit_or_404(payload.unit_id, db)
    if unit.status == UnitStatus.occupied:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unit {unit.name} is already occupied",
        )

    tenant = Tenant(
        name=payload.name,
        phone=payload.phone,
        unit_id=unit.id,
        move_in_date=payload.move_in_date or date.today(),
    )
    unit.status = UnitStatus.occupied
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@app.get("/tenants", response_model=list[TenantOut])
def list_tenants(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List tenants with pagination."""
    return db.query(Tenant).offset(skip).limit(limit).all()


@app.get("/tenants/{tenant_id}", response_model=TenantOut)
def get_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Get one tenant by id."""
    return get_tenant_or_404(tenant_id, db)


@app.put("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: int,
    payload: TenantUpdate,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Update a tenant's name or phone number."""
    tenant = get_tenant_or_404(tenant_id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tenant, field, value)
    db.commit()
    db.refresh(tenant)
    return tenant


@app.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Delete a tenant, vacating their unit and removing rent history."""
    tenant = get_tenant_or_404(tenant_id, db)
    if tenant.move_out_date is None:
        tenant.unit.status = UnitStatus.vacant
    db.delete(tenant)
    db.commit()
    return None


@app.post("/tenants/{tenant_id}/move-out", response_model=TenantOut)
def move_tenant_out(
    tenant_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Mark a tenant as moved out and free up their unit."""
    tenant = get_tenant_or_404(tenant_id, db)
    if tenant.move_out_date is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant has already moved out",
        )

    tenant.move_out_date = date.today()
    tenant.unit.status = UnitStatus.vacant
    db.commit()
    db.refresh(tenant)
    return tenant


# ---------------------------------------------------------------------------
# Rent records (protected)
# ---------------------------------------------------------------------------
@app.post(
    "/rent-records", response_model=RentRecordOut, status_code=status.HTTP_201_CREATED
)
def create_rent_record(
    payload: RentRecordCreate,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Create an unpaid rent record for a tenant."""
    get_tenant_or_404(payload.tenant_id, db)
    record = RentRecord(
        tenant_id=payload.tenant_id,
        amount_due=payload.amount_due,
        amount_paid=0.0,
        due_date=payload.due_date,
        status=RentStatus.unpaid,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@app.get("/rent-records", response_model=list[RentRecordOut])
def list_rent_records(
    tenant_id: int | None = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List rent records, optionally filtered by tenant."""
    query = db.query(RentRecord)
    if tenant_id is not None:
        query = query.filter(RentRecord.tenant_id == tenant_id)
    return query.offset(skip).limit(limit).all()


@app.get("/rent-records/{record_id}", response_model=RentRecordOut)
def get_rent_record(
    record_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Get one rent record by id."""
    return get_rent_record_or_404(record_id, db)


@app.put("/rent-records/{record_id}", response_model=RentRecordOut)
def update_rent_record(
    record_id: int,
    payload: RentRecordUpdate,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Update the amount due or due date of a rent record."""
    record = get_rent_record_or_404(record_id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(record, field, value)
    record.status = calculate_rent_status(record.amount_paid, record.amount_due)
    db.commit()
    db.refresh(record)
    return record


@app.delete("/rent-records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rent_record(
    record_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Delete a rent record and its payments."""
    record = get_rent_record_or_404(record_id, db)
    db.delete(record)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Payments (protected)
# ---------------------------------------------------------------------------
@app.get("/payments", response_model=list[PaymentOut])
def list_payments(
    rent_record_id: int | None = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List recorded payments (newest first), optionally for one rent record."""
    query = db.query(Payment)
    if rent_record_id is not None:
        query = query.filter(Payment.rent_record_id == rent_record_id)
    return query.order_by(Payment.date.desc()).offset(skip).limit(limit).all()


@app.post("/payments", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
def create_payment(
    payload: PaymentCreate,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Record a payment and update the rent record's status."""
    record = get_rent_record_or_404(payload.rent_record_id, db)

    outstanding = round(record.amount_due - record.amount_paid, 2)
    if payload.amount > outstanding + 0.001:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Payment exceeds outstanding balance of {outstanding:.2f}"
            ),
        )

    payment = Payment(rent_record_id=record.id, amount=payload.amount)
    record.amount_paid = round(record.amount_paid + payload.amount, 2)
    record.status = calculate_rent_status(record.amount_paid, record.amount_due)

    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


# ---------------------------------------------------------------------------
# Notifications (protected)
# ---------------------------------------------------------------------------
@app.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread: bool | None = None,
    is_read: bool | None = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List notifications newest first, optionally filtered by read state."""
    query = db.query(Notification)
    if unread is not None:
        query = query.filter(Notification.is_read == (not unread))
    elif is_read is not None:
        query = query.filter(Notification.is_read == is_read)

    return (
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@app.get("/notifications/unread-count", response_model=UnreadCountOut)
def notification_unread_count(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Return the number of unread notifications."""
    count = (
        db.query(Notification)
        .filter(Notification.is_read.is_(False))
        .count()
    )
    return UnreadCountOut(unread_count=count)


@app.post("/notifications/read-all", response_model=ReadAllOut)
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Mark every unread notification as read."""
    updated = (
        db.query(Notification)
        .filter(Notification.is_read.is_(False))
        .update({Notification.is_read: True}, synchronize_session=False)
    )
    db.commit()
    return ReadAllOut(updated=updated)


@app.post("/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Mark one notification as read."""
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification with id {notification_id} not found",
        )

    notification.is_read = True
    db.commit()
    db.refresh(notification)
    return notification


@app.get("/notifications/settings", response_model=NotificationSettingsOut)
def get_notification_settings(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Return the landlord's reminder settings."""
    return get_or_create_notification_settings(db)


@app.put("/notifications/settings", response_model=NotificationSettingsOut)
def update_notification_settings(
    payload: NotificationSettingsUpdate,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Update the landlord's reminder settings."""
    settings = get_or_create_notification_settings(db)
    values = {
        field: value
        for field, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }

    if "notify_days_before" in values:
        days = values["notify_days_before"]
        if any(day < 0 or day > 365 for day in days):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "notify_days_before must contain numbers between 0 and 365"
                ),
            )
        values["notify_days_before"] = list(dict.fromkeys(days))

    if "email" in values:
        email = values["email"].strip()
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="email must not be empty",
            )
        values["email"] = email

    for field, value in values.items():
        setattr(settings, field, value)

    db.commit()
    db.refresh(settings)
    return settings


# ---------------------------------------------------------------------------
# Scheduled reminder job (CRON_SECRET bearer token, not a user JWT)
# ---------------------------------------------------------------------------
@app.api_route(
    "/jobs/send-reminders",
    methods=["GET", "POST"],
    response_model=ReminderResultOut,
    dependencies=[Depends(require_cron_secret)],
)
def send_reminders(db: Session = Depends(get_db)):
    """Create due reminders and email one summary to the landlord.

    Vercel Cron sends GET requests; POST is kept as the documented job method.
    """
    today = datetime.now(timezone.utc).date()
    settings = get_or_create_notification_settings(db)
    records = (
        db.query(RentRecord)
        .filter(
            RentRecord.status.in_(
                [RentStatus.unpaid, RentStatus.partial]
            )
        )
        .all()
    )

    created_count = 0
    email_details: list[dict] = []

    for record in records:
        outstanding = round(record.amount_due - record.amount_paid, 2)
        if outstanding <= 0:
            continue

        notification_type, days_overdue = reminder_type_for(
            db, record, settings, today
        )
        if notification_type is None:
            continue
        if notification_created_on(
            db, record.id, notification_type, today
        ):
            continue

        notification = Notification(
            type=notification_type,
            message=build_notification_message(
                record,
                notification_type,
                outstanding,
                days_overdue,
                today,
            ),
            tenant_id=record.tenant_id,
            rent_record_id=record.id,
            is_read=False,
        )
        db.add(notification)
        created_count += 1
        email_details.append(
            {
                "type": notification_type,
                "tenant_name": record.tenant.name,
                "unit_name": record.tenant.unit.name,
                "amount_owed": outstanding,
                "due_date": record.due_date,
                "days_overdue": days_overdue,
            }
        )

    if created_count:
        # Commit notifications before attempting email, so an email failure
        # never loses the in-app reminders.
        db.commit()

    email_sent = False
    if created_count:
        try:
            email_sent = send_reminder_email(settings, email_details, today)
        except Exception:
            logger.exception("Unexpected error while sending reminder email")

    return ReminderResultOut(created=created_count, email_sent=email_sent)


# ---------------------------------------------------------------------------
# Dashboard (protected)
# ---------------------------------------------------------------------------
@app.get("/dashboard/overdue", response_model=list[OverdueOut])
def dashboard_overdue(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List unpaid rent past its due date, including days overdue."""
    today = date.today()
    return [
        OverdueOut(
            rent_record_id=record.id,
            tenant_id=record.tenant_id,
            tenant_name=record.tenant.name,
            unit_name=record.tenant.unit.name,
            amount_due=record.amount_due,
            amount_paid=record.amount_paid,
            due_date=record.due_date,
            days_overdue=(today - record.due_date).days,
        )
        for record in overdue_records(db)
    ]


@app.get("/dashboard/vacant", response_model=list[UnitOut])
def dashboard_vacant(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List all vacant units."""
    return db.query(Unit).filter(Unit.status == UnitStatus.vacant).all()


@app.get("/dashboard/summary", response_model=SummaryOut)
def dashboard_summary(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Totals for expected rent, collected rent and outstanding rent."""
    records = db.query(RentRecord).all()
    total_expected = round(sum(record.amount_due for record in records), 2)
    total_collected = round(sum(record.amount_paid for record in records), 2)
    total_outstanding = round(
        sum(max(record.amount_due - record.amount_paid, 0) for record in records), 2
    )
    overdue_tenants = len({record.tenant_id for record in overdue_records(db)})

    return SummaryOut(
        total_expected=total_expected,
        total_collected=total_collected,
        total_outstanding=total_outstanding,
        overdue_tenants=overdue_tenants,
    )


if __name__ == "__main__":
    # Run with: python main.py
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
