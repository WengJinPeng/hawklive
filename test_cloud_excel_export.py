from __future__ import annotations

import asyncio
import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.worksheet._writer import ALL_TEMP_FILES

import cloud_api


ROOMS = [
    {"id": "room-a", "name": "CR1", "devices": [
        {"id": "device-a", "name": "Device 1"},
        {"id": "device-b", "name": "Device 2"},
    ]},
    {"id": "room-b", "name": "CR2", "devices": [
        {"id": "device-c", "name": "Device 3"},
    ]},
]
START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def reading(index: int, *, device: str = "device-a", alarm: bool = True) -> tuple:
    return (
        device, "Historical device name", START + timedelta(minutes=index),
        {"pm_0_3_um": index, "pm_0_5_um": index * 2},
        {"temperature": 20 + index, "humidity": 50 + index},
        "ALARM_ACTIVE" if alarm else "NORMAL", [],
    )


async def response_bytes(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


class CloudExcelExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows_by_room = {"room-a": [reading(0), reading(1, alarm=False)], "room-b": []}
        self.count_override = None
        self.queries = []
        self.db = MagicMock()
        self.db.execute.side_effect = self.execute
        self.connect_patch = patch("cloud_api.connect")
        self.connect = self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)
        self.connect.return_value.__enter__.return_value = self.db
        self.config_patch = patch("cloud_api.customer_configuration", return_value=ROOMS)
        self.configuration = self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def execute(self, query, params=()):
        query = " ".join(query.split())
        self.queries.append((query, params))
        result = MagicMock()
        if "FROM device_maintenance_periods" in query:
            result.fetchall.return_value = [
                ("device-a", "Calibration", START.timestamp(), (START + timedelta(minutes=1)).timestamp()),
            ]
        elif "FROM alarm_events" in query:
            result.fetchall.return_value = [("device-a", 3), ("device-b", 1)]
        elif "FROM readings" in query:
            rows = self.rows_by_room[params[1]]
            if "COUNT(*)" in query:
                result.fetchone.return_value = (
                    self.count_override if self.count_override is not None else len(rows),
                )
            else:
                result.fetchall.return_value = rows
        else:
            self.fail(f"Unexpected query: {query}")
        return result

    def export(self, **kwargs):
        response = cloud_api.customer_excel_export(
            locale=kwargs.pop("locale", "en-US"), user={"customer_id": "customer-a"}, **kwargs,
        )
        body = asyncio.run(response_bytes(response))
        workbook = load_workbook(io.BytesIO(body))
        self.addCleanup(workbook.close)
        return response, workbook

    def test_report_preserves_values_units_summary_maintenance_and_alarm_style(self) -> None:
        self.rows_by_room["room-a"].append(reading(2, device="device-b"))
        response, workbook = self.export()
        self.assertEqual(["Summary", "CR1", "CR2"], workbook.sheetnames)
        self.assertIn(".xlsx", response.headers["content-disposition"])
        self.assertEqual("en-US", response.headers["content-language"])
        sheet = workbook["CR1"]
        self.assertEqual("0.3 µm (particles/ft³)", sheet.cell(1, 3).value)
        self.assertEqual("Temperature (°C)", sheet.cell(1, 9).value)
        self.assertEqual(START.replace(tzinfo=None), sheet.cell(2, 1).value)
        self.assertEqual("Historical device name", sheet.cell(2, 2).value)
        self.assertEqual([0, 0, None, None, None, None, 20, 50], [sheet.cell(2, n).value for n in range(3, 11)])
        self.assertEqual("Alarm active", sheet.cell(2, 11).value)
        self.assertIn("During maintenance: Calibration", sheet.cell(2, 12).value)
        self.assertIsNone(sheet.cell(3, 12).value)  # Exclusive maintenance end.
        for row_number in (2, 4):
            self.assertTrue(all(sheet.cell(row_number, n).fill.fgColor.rgb == "00FDECEB" for n in range(1, 13)))
        self.assertTrue(all(sheet.cell(3, n).fill.patternType is None for n in range(1, 13)))
        self.assertTrue(all(cell.font.bold for cell in sheet[1]))
        self.assertEqual("A2", sheet.freeze_panes)
        self.assertEqual("A1:L4", sheet.auto_filter.ref)
        summaries = list(workbook["Summary"].values)[1:]
        first = summaries[0]
        self.assertEqual(("CR1", "Device 1", "0.3 µm", "particles/ft³", 0.5, 0, 1, 3, 2), first[:9])
        self.assertEqual((START.replace(tzinfo=None), (START + timedelta(minutes=1)).replace(tzinfo=None)), first[9:])
        self.assertEqual(8, len(summaries))
        self.configuration.assert_called_once_with("customer-a", include_disabled=True)

    def test_room_device_time_and_customer_filters_are_preserved(self) -> None:
        start = START.timestamp()
        end = (START + timedelta(days=31)).timestamp()
        _, workbook = self.export(start=start, end=end, cleanroom_ids="room-a", device_ids="device-a,device-b,device-a")
        self.assertEqual(["Summary", "CR1"], workbook.sheetnames)
        queries = [(q, p) for q, p in self.queries if "FROM readings" in q or "FROM alarm_events" in q]
        self.assertEqual(3, len(queries))
        for query, params in queries:
            self.assertIn("customer_id=%s", query)
            self.assertIn("cleanroom_id=%s", query)
            self.assertIn("device_id IN (%s,%s)", query)
            time_column = "started_at" if "FROM alarm_events" in query else "measured_at"
            self.assertIn(f"{time_column}>=to_timestamp(%s)", query)
            self.assertIn(f"{time_column}<=to_timestamp(%s)", query)
            self.assertEqual(("customer-a", "room-a", start, end, "device-a", "device-b"), params)
        maintenance_query = next(p for q, p in self.queries if "FROM device_maintenance_periods" in q)
        self.assertEqual("customer-a", maintenance_query[0])

    def test_chinese_report_preserves_business_names_and_styles(self) -> None:
        _, workbook = self.export(locale="zh-CN", cleanroom_ids="room-a")
        self.assertEqual(["汇总", "CR1"], workbook.sheetnames)
        sheet = workbook["CR1"]
        self.assertEqual("时间", sheet.cell(1, 1).value)
        self.assertEqual("0.3 µm（个/ft³）", sheet.cell(1, 3).value)
        self.assertEqual("Historical device name", sheet.cell(2, 2).value)
        self.assertEqual("报警中", sheet.cell(2, 11).value)
        self.assertIn("维护期间: Calibration", sheet.cell(2, 12).value)
        self.assertEqual("CR1", workbook["汇总"].cell(2, 1).value)

    def test_empty_rooms_and_unknown_scope_still_produce_valid_workbooks(self) -> None:
        _, workbook = self.export(cleanroom_ids="room-b")
        self.assertEqual(1, workbook["CR2"].max_row)
        self.assertEqual("A1:L1", workbook["CR2"].auto_filter.ref)
        _, unknown = self.export(cleanroom_ids="unknown-room")
        self.assertEqual(["Summary"], unknown.sheetnames)
        self.assertEqual(1, unknown["Summary"].max_row)

    def test_row_limit_remains_explicit_instead_of_truncating_export(self) -> None:
        existing_temporary_files = set(ALL_TEMP_FILES)
        self.count_override = 100001
        with self.assertRaises(HTTPException) as raised:
            self.export(cleanroom_ids="room-a")
        self.assertEqual(413, raised.exception.status_code)
        self.assertIn("100,000", raised.exception.detail)
        self.assertFalse(any("SELECT device_id,device_name" in query for query, _ in self.queries))
        self.assertEqual(existing_temporary_files, set(ALL_TEMP_FILES))

    def test_failure_after_one_room_cleans_up_all_partial_worksheets(self) -> None:
        existing_temporary_files = set(ALL_TEMP_FILES)
        execute = self.execute

        def fail_second_room(query, params=()):
            if "FROM readings" in query and params[1] == "room-b":
                raise RuntimeError("database unavailable")
            return execute(query, params)

        self.db.execute.side_effect = fail_second_room
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.export()
        self.assertEqual(existing_temporary_files, set(ALL_TEMP_FILES))

    def test_alarm_rows_do_not_repeatedly_rescan_worksheet_bounds(self) -> None:
        self.rows_by_room["room-a"] = [reading(i) for i in range(1000)]
        original = Worksheet.max_row.fget
        scans = []

        def tracked_max_row(sheet):
            scans.append(len(sheet._cells))
            return original(sheet)

        with patch.object(Worksheet, "max_row", property(tracked_max_row)):
            _, workbook = self.export(cleanroom_ids="room-a")
        self.assertLessEqual(len(scans), 2, f"Repeated full-sheet scans: {len(scans)}")
        sheet = workbook["CR1"]
        self.assertEqual(1001, sheet.max_row)
        self.assertEqual(999, sheet.cell(1001, 3).value)
        self.assertEqual("00FDECEB", sheet.cell(1001, 12).fill.fgColor.rgb)

    def test_anonymous_http_download_still_requires_authentication(self) -> None:
        response = TestClient(cloud_api.app).get("/api/history/export-xlsx")
        self.assertEqual(401, response.status_code)
        self.configuration.assert_not_called()

    def test_authenticated_http_request_downloads_the_selected_report(self) -> None:
        previous = cloud_api.app.dependency_overrides.copy()
        self.addCleanup(lambda: setattr(cloud_api.app, "dependency_overrides", previous))
        cloud_api.app.dependency_overrides[cloud_api.customer_user] = lambda: {"customer_id": "customer-a"}
        response = TestClient(cloud_api.app).get("/api/history/export-xlsx", params={
            "locale": "zh-CN", "cleanroom_ids": "room-a", "device_ids": "device-a",
            "start": START.timestamp(), "end": (START + timedelta(days=31)).timestamp(),
        })
        self.assertEqual(200, response.status_code)
        self.assertEqual("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", response.headers["content-type"])
        self.assertEqual("zh-CN", response.headers["content-language"])
        workbook = load_workbook(io.BytesIO(response.content))
        self.addCleanup(workbook.close)
        self.assertEqual(["汇总", "CR1"], workbook.sheetnames)
        self.assertEqual(3, workbook["CR1"].max_row)
        self.assertEqual("00FDECEB", workbook["CR1"].cell(2, 12).fill.fgColor.rgb)

    def test_device_selection_cannot_expand_the_requested_room(self) -> None:
        _, workbook = self.export(cleanroom_ids="room-a", device_ids="device-c")
        self.assertEqual(["Summary"], workbook.sheetnames)
        self.assertEqual([], self.queries)


if __name__ == "__main__":
    unittest.main()
