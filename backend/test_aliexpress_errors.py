import unittest
from fastapi import HTTPException
from alisync_app.aliexpress import _check_error


class AliExpressErrorTests(unittest.TestCase):
    def test_supplier_detail_is_visible_without_raw_payload(self):
        with self.assertRaises(HTTPException) as caught:
            _check_error({"error_response": {"code": 27, "msg": "Invalid session",
                "sub_code": "session-expired", "sub_msg": "Please authorize again",
                "request_params": "sensitive"}})
        detail = caught.exception.detail
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Please authorize again", detail["message"])
        self.assertIn("session-expired", detail["message"])
        self.assertNotIn("sensitive", str(detail))
        self.assertNotIn("raw", detail)

    def test_generic_message_and_success(self):
        with self.assertRaises(HTTPException) as caught:
            _check_error({"error_response": {"code": 15, "msg": "Permission denied"}})
        self.assertIn("Permission denied", caught.exception.detail["message"])
        self.assertIsNone(_check_error({"result": {}}))


if __name__ == "__main__":
    unittest.main()
