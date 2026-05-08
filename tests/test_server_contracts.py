import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["APP_ENV"] = "test"

import server  # noqa: E402


class ServerContractsTest(unittest.TestCase):
    def base_payload(self):
        return {
            "origin": "Bengaluru, India",
            "destination": "Goa, India",
            "startDate": "2026-06-01",
            "endDate": "2026-06-04",
            "travelers": 2,
            "budget": "moderate",
            "pace": "balanced",
            "travelMode": "driving",
            "interests": ["food", "beaches"],
            "constraints": ["vegetarian meals"],
        }

    def test_validate_trip_payload_normalizes_mode_and_days(self):
        payload = server.validate_trip_payload(self.base_payload())
        self.assertEqual(payload["travelMode"], "DRIVE")
        self.assertEqual(payload["days"], 4)
        self.assertEqual(payload["travelers"], 2)

    def test_fallback_plan_matches_trip_length(self):
        payload = server.validate_trip_payload(self.base_payload())
        plan = server.fallback_plan(payload)
        self.assertEqual(len(plan["dailyPlan"]), 4)
        self.assertIn("Goa", plan["title"])
        self.assertTrue(plan["liveAdjustments"])

    def test_fallback_route_has_metrics(self):
        payload = server.validate_trip_payload(self.base_payload())
        route = server.fallback_route(payload)
        self.assertEqual(route["source"], "demo-route")
        self.assertGreater(route["distanceMeters"], 0)
        self.assertIn("estimate", route["distanceText"])

    def test_google_duration_parser(self):
        self.assertEqual(server.parse_google_duration("165s"), 165)
        self.assertEqual(server.parse_google_duration("12.5s"), 12)
        self.assertEqual(server.parse_google_duration("bad"), 0)


if __name__ == "__main__":
    unittest.main()
