import asyncio
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import digest  # noqa: E402


class JobAlertFilterTests(unittest.TestCase):
    def test_no_allowlist_disables_job_alert_search(self):
        self.assertIsNone(digest.job_alert_query([]))

    def test_query_uses_only_valid_exact_sender_addresses(self):
        query = digest.job_alert_query(["alerts@google.com", "jobs@apple.com", "not an address"])
        self.assertIn("from:alerts@google.com", query)
        self.assertIn("from:jobs@apple.com", query)
        self.assertNotIn("from:google.com", query)
        self.assertIn("subject:job", query)
        self.assertNotIn("\"Google\"", query)

    def test_alert_fetch_rechecks_exact_from_address(self):
        calls = []
        messages = {
            "trusted": {
                "id": "trusted", "internalDate": "1792100000000",
                "payload": {"headers": [
                    {"name": "From", "value": "Google Careers <alerts@google.com>"},
                    {"name": "Subject", "value": "New software engineer jobs"},
                ], "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Software Engineer, New York https://jobs.google.com/role/1").decode()}},
            },
            "spoofed": {
                "id": "spoofed", "internalDate": "1792100000001",
                "payload": {"headers": [
                    {"name": "From", "value": "alerts@google.com <random@untrusted.example>"},
                    {"name": "Subject", "value": "New software engineer jobs"},
                ], "mimeType": "text/plain", "body": {"data": ""}},
            },
        }

        async def get_json(url, _token, params):
            calls.append((url, params.copy()))
            if url.endswith("/messages"):
                if "subject:job" in params["q"]:
                    return 200, {"messages": [{"id": "trusted"}, {"id": "spoofed"}]}
                return 200, {"messages": []}
            return 200, messages[url.rsplit("/", 1)[-1]]

        items, errors = asyncio.run(digest.fetch_items(
            [("primary", "token")], get_json, ["alerts@google.com"]))
        self.assertEqual(errors, [])
        self.assertEqual([item["id"] for item in items], ["primary:trusted"])
        self.assertEqual(items[0]["kind"], "job_alert")
        self.assertIn("https://jobs.google.com/role/1", items[0]["body"])
        alert_queries = [params["q"] for url, params in calls if url.endswith("/messages") and "subject:job" in params["q"]]
        self.assertEqual(len(alert_queries), 1)
        self.assertIn("from:alerts@google.com", alert_queries[0])
        self.assertNotIn("from:untrusted.example", alert_queries[0])


if __name__ == "__main__":
    unittest.main()
