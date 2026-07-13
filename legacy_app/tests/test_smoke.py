"""Basic smoke test for the legacy app. Run from legacy_app/ with:

    python -m unittest tests.test_smoke

Uses only the standard library + starlette's TestClient (already a FastAPI
dependency), so no extra packages are required.
"""
import unittest

import main as main_module
from starlette.testclient import TestClient


async def _noop_scrape():
    return None


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Skip the real (network + Playwright) scrape during the smoke test;
        # this only verifies the app boots and serves its existing DB/templates/static.
        main_module.run_all_scrapers = _noop_scrape
        cls.client = TestClient(main_module.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_home_page_loads(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("City Events", resp.text)

    def test_city_page_loads(self):
        resp = self.client.get("/city/bloomington-in")
        self.assertEqual(resp.status_code, 200)

    def test_unknown_city_returns_404(self):
        resp = self.client.get("/city/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_api_events_endpoint_returns_list(self):
        resp = self.client.get("/api/events")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), list)

    def test_static_mount_is_wired_up(self):
        resp = self.client.get("/static/does-not-exist.js")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
