import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alisync_app import billing
from alisync_app.models import User, Shop
from alisync_app.subscriptions import plan_name


def active(plan="Growth", amount=14, status="ACTIVE"):
    return {"id": "gid://shopify/AppSubscription/1", "name": "AliSync " + plan,
            "status": status, "test": False, "lineItems": [{"plan": {"pricingDetails": {
                "interval": "EVERY_30_DAYS", "price": {"amount": str(amount), "currencyCode": "USD"}}}}]}


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        User.__table__.create(self.engine); Shop.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.user = User(email="billing@test.invalid", password_hash="unused", selected_plan="pro",
                         subscription_status="active_no_billing")
        self.db.add(self.user); self.db.flush()
        self.shop = Shop(user_id=self.user.id, shop_domain="test.myshopify.com", access_token="fake")
        self.db.add(self.shop); self.db.commit()
        self.subscriptions = []
        self.api = self.enterContext(patch.object(billing, "graphql", side_effect=self.graphql))
        self.settings = SimpleNamespace(BILLING_ENABLED=True, SHOPIFY_BILLING_TEST=False, SHOPIFY_CLIENT_ID="app")
        self.enterContext(patch.object(billing, "get_settings", return_value=self.settings))

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    def graphql(self, db, shop, query, variables=None):
        if "appSubscriptionCreate" in query:
            return {"appSubscriptionCreate": {"confirmationUrl": "https://test.myshopify.com/approve", "userErrors": []}}
        if "appSubscriptionCancel" in query:
            self.subscriptions = []
            return {"appSubscriptionCancel": {"userErrors": []}}
        return {"currentAppInstallation": {"activeSubscriptions": self.subscriptions}}

    def test_pending_charge_does_not_grant_access_and_decline_stays_free(self):
        self.assertEqual(plan_name(self.user), "free")
        result = billing.select_plan(self.db, self.user, self.shop, "growth")
        self.assertTrue(result["payment_required"])
        self.assertIn("confirmation_url", result)
        self.assertEqual(self.user.selected_plan, "free")
        variables = self.api.call_args.args[3]
        self.assertEqual(variables["items"][0]["plan"]["appRecurringPricingDetails"]["price"]["amount"], 14)
        self.assertFalse(variables["test"])
        billing.reconcile(self.db, self.user)
        self.assertEqual(plan_name(self.user), "free")

    def test_approval_declined_upgrade_and_cancel(self):
        self.subscriptions = [active()]
        billing.reconcile(self.db, self.user)
        self.assertEqual(plan_name(self.user), "growth")
        billing.select_plan(self.db, self.user, self.shop, "pro")
        billing.reconcile(self.db, self.user)
        self.assertEqual(plan_name(self.user), "growth")
        billing.select_plan(self.db, self.user, self.shop, "free")
        self.assertEqual(plan_name(self.user), "free")
        self.assertIsNone(self.shop.subscription_id)

    def test_reinstall_requires_new_approval(self):
        self.subscriptions = [active()]
        billing.reconcile(self.db, self.user)
        self.subscriptions = []
        result = billing.select_plan(self.db, self.user, self.shop, "growth")
        self.assertEqual(plan_name(self.user), "free")
        self.assertTrue(result["payment_required"])

    def test_wrong_price_inactive_and_test_charges_do_not_unlock_production(self):
        for subscription in (active(amount=1), active(status="PENDING"), active() | {"test": True}):
            self.subscriptions = [subscription]
            billing.reconcile(self.db, self.user)
            self.assertEqual(plan_name(self.user), "free")

    def test_disabled_billing_cannot_grant_paid_access(self):
        self.settings.BILLING_ENABLED = False
        with self.assertRaises(HTTPException):
            billing.select_plan(self.db, self.user, self.shop, "pro")
        self.assertEqual(plan_name(self.user), "free")

    def test_failed_creation_never_grants_access(self):
        self.api.side_effect = [
            {"currentAppInstallation": {"activeSubscriptions": []}},
            {"appSubscriptionCreate": {"confirmationUrl": None, "userErrors": [{"message": "Rejected"}]}}]
        with self.assertRaises(HTTPException) as error:
            billing.select_plan(self.db, self.user, self.shop, "pro")
        self.assertIn("Rejected", error.exception.detail)
        self.assertEqual(plan_name(self.user), "free")

    def test_local_test_charge_requires_approval_then_unlocks(self):
        self.settings.SHOPIFY_BILLING_TEST = True
        result = billing.select_plan(self.db, self.user, self.shop, "growth")
        self.assertTrue(self.api.call_args.args[3]["test"])
        self.assertTrue(result["payment_required"])
        self.assertEqual(plan_name(self.user), "free")
        self.subscriptions = [active() | {"test": True}]
        billing.reconcile(self.db, self.user)
        self.assertEqual(plan_name(self.user), "growth")

    def test_failed_cancellation_preserves_paid_access(self):
        self.api.side_effect = [
            {"currentAppInstallation": {"activeSubscriptions": [active()]}},
            {"appSubscriptionCancel": {"userErrors": [{"message": "Rejected"}]}}]
        with self.assertRaises(HTTPException):
            billing.select_plan(self.db, self.user, self.shop, "free")
        self.assertEqual(plan_name(self.user), "growth")


if __name__ == "__main__":
    unittest.main()
