"""Pure order rules; adapters must authenticate payments and persist atomically."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from html import escape
import re


class Status(StrEnum):
    WAITING = "waiting_payment"
    PAID = "paid"
    PROVISIONING = "provisioning"
    FAILED = "provisioning_failed"
    FULFILLED = "fulfilled"


def positive_integer(value: str, maximum: int = 1000000) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ValueError("Expected ASCII integer")
    result = int(value)
    if not 1 <= result <= maximum:
        raise ValueError("Out of range")
    return result


def safe_text(value: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError("Invalid text length")
    return escape(value, quote=True)


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone required")
    return value


@dataclass(frozen=True)
class TariffSnapshot:
    tariff_id: str
    name: str
    amount: int
    currency: str
    days: int
    devices: int
    traffic_bytes: int
    squads: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (self.amount, self.days, self.devices):
            if type(value) is not int or value <= 0:
                raise ValueError("Invalid positive integer")
        if type(self.traffic_bytes) is not int or self.traffic_bytes < 0:
            raise ValueError("Invalid traffic limit")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("Invalid currency")
        if not self.tariff_id or not self.name or not self.squads:
            raise ValueError("Incomplete tariff")
        if not isinstance(self.squads, tuple) or not all(
            isinstance(squad, str) and squad for squad in self.squads
        ):
            raise ValueError("Invalid squads")


@dataclass(frozen=True)
class Payment:
    provider: str
    charge_id: str
    order_id: str
    telegram_id: int
    amount: int
    currency: str


@dataclass(frozen=True)
class Order:
    id: str
    telegram_id: int
    tariff: TariffSnapshot
    status: Status = Status.WAITING
    payment: Payment | None = None
    target_expiry: datetime | None = None


def record_payment(order: Order, payment: Payment) -> Order:
    """Input must come from an authenticated server-side payment adapter."""
    if not payment.provider or not payment.charge_id:
        raise ValueError("Missing payment identity")
    if (payment.order_id, payment.telegram_id, payment.amount, payment.currency) != (
        order.id, order.telegram_id, order.tariff.amount, order.tariff.currency
    ):
        raise ValueError("Payment mismatch")
    if order.payment is not None:
        if order.payment != payment:
            raise ValueError("Different payment already recorded")
        return order
    if order.status != Status.WAITING:
        raise ValueError("Order not payable")
    return replace(order, payment=payment, status=Status.PAID)


def plan_provisioning(order: Order, now: datetime, current_expiry: datetime | None) -> Order:
    """Persist the absolute target BEFORE calling the panel; reuse on retry."""
    aware(now)
    if order.status == Status.FULFILLED:
        return order
    if order.payment is None or order.status not in (
        Status.PAID, Status.PROVISIONING, Status.FAILED
    ):
        raise ValueError("Confirmed payment required")
    expiry = order.target_expiry
    if expiry is None:
        base = max(now, aware(current_expiry)) if current_expiry else now
        expiry = base + timedelta(days=order.tariff.days)
    return replace(order, status=Status.PROVISIONING, target_expiry=expiry)


def finish_provisioning(order: Order, observed_expiry: datetime) -> Order:
    if order.status == Status.FULFILLED:
        return order
    if order.status != Status.PROVISIONING or order.target_expiry is None:
        raise ValueError("Provisioning not started")
    if aware(observed_expiry) != order.target_expiry:
        raise ValueError("Panel result differs from persisted target")
    return replace(order, status=Status.FULFILLED)
