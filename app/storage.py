"""Transactional storage. Payment authentication belongs to the trusted adapter."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, JSON,
    String, UniqueConstraint, and_, or_, select,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))


class StoredOrder(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("amount > 0", name="order_positive_amount"),
        CheckConstraint("status IN ('waiting_payment','paid','provisioning','fulfilled',"
                        "'provisioning_failed','cancelled','expired','refunded')",
                        name="order_status"),
        UniqueConstraint("telegram_id", "request_key", name="order_request_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("customers.telegram_id"))
    request_key: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="waiting_payment")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoredPayment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "charge_id", name="payment_charge_identity"),
        UniqueConstraint("order_id", name="one_payment_per_order"),
        CheckConstraint("amount > 0", name="payment_positive_amount"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    provider: Mapped[str] = mapped_column(String(32))
    charge_id: Mapped[str] = mapped_column(String(256))
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "outbox_jobs"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="job_attempts"),
        CheckConstraint("status IN ('pending','running','done','failed')", name="job_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(64))


def sessions(url: str):
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("This release requires PostgreSQL with asyncpg")
    engine = create_async_engine(url, pool_pre_ping=True, echo=False)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def utcnow():
    return datetime.now(timezone.utc)


async def accept_payment(factory, *, order_id: str, telegram_id: int,
                         provider: str, charge_id: str, amount: int, currency: str) -> bool:
    """Only trusted adapters may call this. True means newly recorded, not provisioned."""
    if (not isinstance(provider, str) or not 1 <= len(provider) <= 32
            or not isinstance(charge_id, str) or not 1 <= len(charge_id) <= 256
            or type(amount) is not int or amount <= 0):
        raise ValueError("Invalid payment identity or amount")
    async with factory.begin() as db:
        order = await db.scalar(select(StoredOrder).where(
            StoredOrder.id == order_id).with_for_update())
        if order is None:
            raise ValueError("Unknown order")
        if (order.telegram_id, order.amount, order.currency) != (telegram_id, amount, currency):
            raise ValueError("Payment mismatch")
        previous = await db.scalar(select(StoredPayment).where(
            StoredPayment.order_id == order_id))
        if previous:
            if (previous.provider, previous.charge_id) != (provider, charge_id):
                raise ValueError("Different payment already recorded")
            return False
        if order.status != "waiting_payment":
            raise ValueError("Order is not payable")
        now = utcnow()
        db.add(StoredPayment(id=str(uuid4()), order_id=order_id, provider=provider,
                             charge_id=charge_id, amount=amount, currency=currency,
                             created_at=now))
        db.add(Job(id=str(uuid4()), order_id=order_id, status="pending",
                   attempts=0, available_at=now))
        order.status = "paid"
    return True


async def claim_job(factory, lease_seconds: int = 120, max_attempts: int = 12):
    if not 10 <= lease_seconds <= 3600 or not 1 <= max_attempts <= 100:
        raise ValueError("Invalid lease policy")
    now = utcnow()
    async with factory.begin() as db:
        job = await db.scalar(select(Job).where(
            Job.status.in_(("pending", "running")), Job.available_at <= now
        ).order_by(Job.available_at, Job.id).with_for_update(skip_locked=True).limit(1))
        if job is None:
            return None
        if job.attempts >= max_attempts:
            job.status = "failed"
            job.lease_token = None
            job.error_code = "attempts_exhausted"
            return None
        job.status = "running"
        job.attempts += 1
        job.lease_token = str(uuid4())
        job.available_at = now + timedelta(seconds=lease_seconds)
        return job.id, job.order_id, job.lease_token


async def finish_job(factory, job_id: str, token: str, *, retry_code: str | None = None):
    """Lease fencing protects DB updates, NOT external API effects."""
    if retry_code is not None and (not retry_code.isascii()
                                  or not retry_code.replace("_", "").isalnum()
                                  or len(retry_code) > 64):
        raise ValueError("Use a sanitized error code, not exception text")
    now = utcnow()
    async with factory.begin() as db:
        job = await db.scalar(select(Job).where(
            Job.id == job_id, Job.status == "running", Job.lease_token == token,
            Job.available_at > now,
        ).with_for_update())
        if job is None:
            return False
        job.status = "pending" if retry_code else "done"
        job.lease_token = None
        job.error_code = retry_code
        if retry_code:
            job.available_at = now + timedelta(seconds=min(3600, 2 ** min(job.attempts, 12)))
    return True
