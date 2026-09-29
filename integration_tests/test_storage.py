import asyncio
import os
import unittest
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from app.storage import (
    Customer, Job, StoredOrder, StoredPayment, accept_payment,
    claim_job, finish_job, sessions, utcnow,
)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.factory = sessions(os.environ["TEST_DATABASE_URL"])
        self.user = uuid4().int % (2 ** 62)
        async with self.factory.begin() as db:
            db.add(Customer(telegram_id=self.user))
        self.order = await self.new_order()

    async def asyncTearDown(self):
        async with self.factory.begin() as db:
            orders = select(StoredOrder.id).where(StoredOrder.telegram_id == self.user)
            from sqlalchemy import delete
            await db.execute(delete(Job).where(Job.order_id.in_(orders)))
            await db.execute(delete(StoredPayment).where(StoredPayment.order_id.in_(orders)))
            await db.execute(delete(StoredOrder).where(StoredOrder.telegram_id == self.user))
            await db.execute(delete(Customer).where(Customer.telegram_id == self.user))
        await self.engine.dispose()

    async def new_order(self):
        order_id = str(uuid4())
        async with self.factory.begin() as db:
            db.add(StoredOrder(id=order_id, telegram_id=self.user,
                request_key=str(uuid4()), amount=100, currency="XTR",
                snapshot={"days": 30, "devices": 3}, status="waiting_payment",
                created_at=utcnow()))
        return order_id

    async def pay(self, order_id=None, charge=None, amount=100):
        return await accept_payment(self.factory, order_id=order_id or self.order,
            telegram_id=self.user, provider="stars", charge_id=charge or self.order,
            amount=amount, currency="XTR")

    async def test_concurrent_duplicate(self):
        results = await asyncio.gather(*(self.pay() for _ in range(8)))
        self.assertEqual(results.count(True), 1)
        async with self.factory() as db:
            count = await db.scalar(select(func.count()).select_from(Job).where(Job.order_id == self.order))
            self.assertEqual(count, 1)
            order = await db.get(StoredOrder, self.order)
            self.assertEqual(order.status, "paid")

    async def test_mismatch_rolls_back(self):
        with self.assertRaises(ValueError):
            await self.pay(amount=99)
        async with self.factory() as db:
            self.assertEqual((await db.get(StoredOrder, self.order)).status, "waiting_payment")
            self.assertEqual(await db.scalar(select(func.count()).select_from(Job).where(Job.order_id == self.order)), 0)

    async def test_charge_cannot_pay_two_orders(self):
        await self.pay(charge="charge-" + self.order)
        other = await self.new_order()
        with self.assertRaises(IntegrityError):
            await self.pay(order_id=other, charge="charge-" + self.order)
        async with self.factory() as db:
            self.assertEqual((await db.get(StoredOrder, other)).status, "waiting_payment")
            self.assertEqual(await db.scalar(select(func.count()).select_from(Job).where(Job.order_id == other)), 0)

    async def test_restart_and_stale_lease(self):
        await self.pay()
        first = await claim_job(self.factory)
        self.assertIsNotNone(first)
        async with self.factory.begin() as db:
            await db.execute(update(Job).where(Job.id == first[0]).values(
                available_at=utcnow() - timedelta(seconds=1)))
        await self.engine.dispose()
        self.engine, self.factory = sessions(os.environ["TEST_DATABASE_URL"])
        second = await claim_job(self.factory)
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[2], second[2])
        self.assertFalse(await finish_job(self.factory, first[0], first[2]))
        self.assertTrue(await finish_job(self.factory, second[0], second[2]))

    async def test_exclusive_claim(self):
        await self.pay()
        claims = await asyncio.gather(claim_job(self.factory), claim_job(self.factory))
        self.assertEqual(sum(value is not None for value in claims), 1)
