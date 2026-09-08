from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class WallboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "public" / "wallboard.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "public" / "wallboard.js").read_text(encoding="utf-8")

    def test_wallboard_uses_customer_scoped_monitoring_apis(self) -> None:
        for route in ("/api/session", "/api/config", "/api/latest", "/api/alarms", "/api/trends"):
            self.assertIn(route, self.script)
        self.assertNotIn("/api/read", self.script)
        self.assertIn('credentials: "same-origin"', self.script)

    def test_optional_data_endpoints_degrade_independently(self) -> None:
        self.assertIn("Promise.allSettled", self.script)
        self.assertIn('settledData(latestResult, "latest"', self.script)
        self.assertIn('settledData(alarmsResult, "alarms"', self.script)
        self.assertIn('settledData(trendsResult, "trends"', self.script)

    def test_untrusted_names_are_escaped_before_dynamic_html(self) -> None:
        self.assertIn("function escapeHtml", self.script)
        for field in ("room.name", "device.name", "action.threshold"):
            self.assertRegex(self.script, rf"escapeHtml\({re.escape(field)}")

    def test_production_page_has_no_scenario_preview_or_fake_live_values(self) -> None:
        self.assertNotIn("data-scenario-control", self.html)
        self.assertNotIn("原型演示", self.html)
        self.assertNotIn("78,640", self.html)
        self.assertIn('id="blockingState"', self.html)
        self.assertIn('id="dataCompleteness">—', self.html)

    def test_source_and_empty_room_states_are_explicit(self) -> None:
        for marker in ("sourceUnknownStamp", "sourceDemoStamp", "sourceRealStamp", "noDevicesInRoom"):
            self.assertIn(marker, self.script)


if __name__ == "__main__":
    unittest.main()
