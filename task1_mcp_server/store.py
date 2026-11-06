"""In-memory customer data standing in for a real backend."""

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel


class CustomerRecord(BaseModel):
    customer_id: str
    name: str
    email: str
    tier: str
    refundable_balance: float


class RefundReceipt(BaseModel):
    refund_id: str
    customer_id: str
    amount: float
    reason: str
    remaining_refundable_balance: float
    created_at: str


class CustomerNotFound(Exception):
    pass


class RefundExceedsBalance(Exception):
    pass


class CustomerStore:
    def __init__(self) -> None:
        self._customers = {
            "CUST-10001": CustomerRecord(
                customer_id="CUST-10001", name="Ava Patel", email="ava.patel@example.com",
                tier="gold", refundable_balance=500.00,
            ),
            "CUST-10002": CustomerRecord(
                customer_id="CUST-10002", name="Marcus Lee", email="marcus.lee@example.com",
                tier="standard", refundable_balance=120.50,
            ),
        }

    def get(self, customer_id: str) -> CustomerRecord:
        record = self._customers.get(customer_id)
        if record is None:
            raise CustomerNotFound(f"No customer with id {customer_id}")
        return record

    def refund(self, customer_id: str, amount: float, reason: str) -> RefundReceipt:
        record = self.get(customer_id)
        if amount > record.refundable_balance:
            raise RefundExceedsBalance(
                f"Refund of {amount:.2f} exceeds refundable balance of {record.refundable_balance:.2f}"
            )
        record.refundable_balance = round(record.refundable_balance - amount, 2)
        return RefundReceipt(
            refund_id=f"RF-{uuid.uuid4().hex[:12]}",
            customer_id=customer_id,
            amount=amount,
            reason=reason,
            remaining_refundable_balance=record.refundable_balance,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
