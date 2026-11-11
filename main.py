"""Landlord Rent Tracker API - FastAPI + SQLAlchemy + SQLite.

One landlord manages one building with a twin flat (two units) and five
self-contain rooms. All unit, tenant and rent endpoints require a JWT.
"""

import hashlib
import hmac
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import jwt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SQLALCHEMY_DATABASE_URL = "sqlite:///./rent_tracker.db"
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 120

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
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


# Create the tables on startup (fine for a small demo project).
Base.metadata.create_all(bind=engine)


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


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------
class RegisterIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=120)
    password: str = Field(..., min_length=6, max_length=128)


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str


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


# ---------------------------------------------------------------------------
# App and auth routes
# ---------------------------------------------------------------------------
app = FastAPI(title="Landlord Rent Tracker API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    """Register the landlord. Only one account is allowed."""
    if db.query(User).count() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Landlord account already exists",
        )

    email = payload.email.strip().lower()
    user = User(
        name=payload.name,
        email=email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    """Log in with email and password to receive a JWT."""
    email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    return TokenOut(access_token=create_access_token(user))


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
