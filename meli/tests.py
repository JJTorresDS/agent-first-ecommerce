import json

from django.test import Client, TestCase

from meli.models import MercadoLibreNotification


SAMPLE = {
    "_id": "5da8a1b24be30a49eb66c52a",
    "resource": "/orders/2195160686",
    "user_id": 468424240,
    "topic": "orders_v2",
    "application_id": 5503910054141466,
    "attempts": 1,
    "sent": "2019-10-30T16:19:20.129Z",
    "received": "2019-10-30T16:19:20.106Z",
}


class MercadoLibreWebhookTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def test_stores_an_orders_notification(self):
        response = self.client.post(
            "/meli/webhooks/",
            data=json.dumps(SAMPLE),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        row = MercadoLibreNotification.objects.get()
        self.assertEqual(row.notification_id, SAMPLE["_id"])
        self.assertEqual(row.topic, "orders_v2")
        self.assertEqual(row.resource, "/orders/2195160686")
        self.assertEqual(row.user_id, "468424240")
        self.assertEqual(row.payload["topic"], "orders_v2")
        self.assertIsNotNone(row.sent_at)
        self.assertIsNotNone(row.received_at)

    def test_repeated_id_is_stored_once(self):
        body = json.dumps(SAMPLE)
        self.client.post(
            "/meli/webhooks/",
            data=body,
            content_type="application/json",
        )
        second = self.client.post(
            "/meli/webhooks/",
            data=body,
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(MercadoLibreNotification.objects.count(), 1)

    def test_non_json_is_still_accepted(self):
        response = self.client.post(
            "/meli/webhooks/",
            data="not-json",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        row = MercadoLibreNotification.objects.get()
        self.assertEqual(row.payload, "not-json")
        self.assertEqual(row.topic, "")
        self.assertEqual(row.notification_id, "")
