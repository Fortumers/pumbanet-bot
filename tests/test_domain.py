import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from app.domain import (
    Order, Payment, Status, TariffSnapshot, finish_provisioning,
    plan_provisioning, positive_integer, record_payment, safe_text,
)


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.order = Order("o1", 123, TariffSnapshot(
            "standard", "Standard", 100, "XTR", 30, 3, 0, ("squad-id",)
        ))
        self.payment = Payment("stars", "charge-1", "o1", 123, 100, "XTR")

    def test_payment_repeat(self):
        paid = record_payment(self.order, self.payment)
        self.assertEqual(record_payment(paid, self.payment), paid)

    def test_wrong_payment_fields(self):
        for fields in ({"amount": 99}, {"currency": "RUB"},
                       {"telegram_id": 456}, {"order_id": "other"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                record_payment(self.order, replace(self.payment, **fields))

    def test_different_charge_rejected(self):
        paid = record_payment(self.order, self.payment)
        with self.assertRaises(ValueError):
            record_payment(paid, replace(self.payment, charge_id="charge-2"))

    def test_unpaid_cannot_provision(self):
        with self.assertRaises(ValueError):
            plan_provisioning(self.order, self.now, None)

    def test_renewal_and_retry(self):
        paid = record_payment(self.order, self.payment)
        existing = self.now + timedelta(days=10)
        planned = plan_provisioning(paid, self.now, existing)
        self.assertEqual(planned.target_expiry, self.now + timedelta(days=40))
        retry = plan_provisioning(planned, self.now + timedelta(days=1), planned.target_expiry)
        self.assertEqual(retry.target_expiry, planned.target_expiry)
        done = finish_provisioning(retry, planned.target_expiry)
        self.assertEqual(done.status, Status.FULFILLED)
        self.assertEqual(plan_provisioning(done, self.now, None), done)

    def test_expired_renewal(self):
        paid = record_payment(self.order, self.payment)
        planned = plan_provisioning(paid, self.now, self.now - timedelta(days=1))
        self.assertEqual(planned.target_expiry, self.now + timedelta(days=30))

    def test_panel_result_verified(self):
        paid = record_payment(self.order, self.payment)
        planned = plan_provisioning(paid, self.now, None)
        with self.assertRaises(ValueError):
            finish_provisioning(planned, self.now)

    def test_invalid_numbers(self):
        for value in ("²", "١٢", "", "-1", "0", "9" * 200, "photo", "$√π÷§£∆✓₽&#/"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                positive_integer(value)
        self.assertEqual(positive_integer("12"), 12)

    def test_text_preserved(self):
        self.assertEqual(safe_text("<b>текст</b>"), "&lt;b&gt;текст&lt;/b&gt;")
        self.assertEqual(safe_text("../../etc/passwd"), "../../etc/passwd")
        self.assertEqual(safe_text("$√π÷§£∆✓₽&#/"), "$√π÷§£∆✓₽&amp;#/")
        with self.assertRaises(ValueError):
            safe_text("x" * 2001)

    def test_naive_datetime_rejected(self):
        paid = record_payment(self.order, self.payment)
        with self.assertRaises(ValueError):
            plan_provisioning(paid, datetime(2026, 1, 1), None)


if __name__ == "__main__":
    unittest.main()
