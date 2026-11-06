from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dws_dm_agent.tools import zhiguan_worklog as W


SHENZHEN = {
    "name": "深圳租赁",
    "workspace": r"D:\workspace\深圳租赁",
    "targetType": "PROJECT",
    "targetId": "249cc0b7-ae48-3a79-832b-d30f7d014e76",
    "keywords": ["深圳租赁", "中集"],
}
ZHAOYIN = {
    "name": "招银金租",
    "workspace": r"D:\workspace\招银",
    "targetType": "PROJECT",
    "targetId": "a114d835-e374-3bec-9ade-d5536cd5638c",
    "keywords": ["招银"],
}


def base_cfg(**overrides) -> dict:
    cfg = {
        "default_daily_hours": 7.5,
        "hour_step": 0.5,
        "min_hours_per_entry": 0.5,
        "max_content_len": 1800,
        "max_commit_subjects": 6,
        "min_files_without_commits": 10,
        "on_unmapped": "skip",
        "calendar": {"enabled": True, "skip_on_leave": True},
        "mappings": [SHENZHEN, ZHAOYIN],
    }
    cfg.update(overrides)
    return cfg


def evidence(name: str, mapping: dict, commits: int, files: int, topic: str) -> dict:
    return {
        "mapping": mapping,
        "git": [],
        "commits": commits,
        "files": {"files": files, "samples": []},
        "docs": [{
            "topic": topic,
            "mainline": "主线内容",
            "headlines": ["推进事项一", "推进事项二"],
            "points": ["要点内容"],
        }],
    }


class FakeClient:
    """Only answers get_worklog_context; anything else is a test bug."""

    def __init__(self, state_dir: Path, targets: list):
        self.state_dir = Path(state_dir)
        self.targets = targets
        self.saved = []

    def call(self, name, arguments, rid=2):
        if name == "get_worklog_context":
            return {
                "existingEntries": [],
                "drafts": [],
                "existingHours": 0,
                "eligibleTargets": {"items": self.targets},
            }
        if name == "save_worklog_draft":
            self.saved.append(arguments)
            return {"id": "draft-1", "version": 0, "issues": [], "reviewUrl": "u"}
        raise AssertionError("unexpected tool call: %s" % name)


def eligible_targets() -> list:
    return [
        {"targetType": m["targetType"], "id": m["targetId"], "name": m["name"]}
        for m in (SHENZHEN, ZHAOYIN)
    ]


class HolidayTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        days = {
            "2026-10-01": False,  # 国庆，不上班
            "2026-10-09": True,   # 普通工作日
            "2026-10-10": True,   # 国庆后补班的周六
            "2026-09-20": True,   # 中秋前补班的周日
            "2026-09-26": False,  # 普通周六不写表里
        }
        (self.state / W.HOLIDAY_CACHE).write_text(
            json.dumps({
                "2026": {
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "days": days,
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_holiday_is_not_a_workday(self):
        self.assertFalse(W.workday(date(2026, 10, 1), self.state))

    def test_makeup_saturday_is_a_workday(self):
        self.assertTrue(W.workday(date(2026, 10, 10), self.state))

    def test_makeup_sunday_is_a_workday(self):
        self.assertTrue(W.workday(date(2026, 9, 20), self.state))

    def test_plain_weekend_is_not_a_workday(self):
        self.assertFalse(W.workday(date(2026, 11, 7), self.state))

    def test_plain_weekday_is_a_workday(self):
        self.assertTrue(W.workday(date(2026, 10, 9), self.state))

    def test_without_state_dir_falls_back_to_weekday(self):
        self.assertTrue(W.workday(date(2026, 11, 5), None))
        self.assertFalse(W.workday(date(2026, 11, 7), None))

    def test_flat_legacy_cache_format_is_accepted(self):
        (self.state / W.HOLIDAY_CACHE).write_text(
            json.dumps({"2026": {"2026-10-01": False, "2026-10-10": True}}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertFalse(W.workday(date(2026, 10, 1), self.state))
        self.assertTrue(W.workday(date(2026, 10, 10), self.state))

    def test_stale_cache_for_current_year_is_refetched(self):
        (self.state / W.HOLIDAY_CACHE).write_text(
            json.dumps({
                "2026": {
                    "fetched_at": "2026-01-01T00:00:00",
                    "days": {"2026-10-01": False, "2026-10-10": True},
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        fresh = {"2026-10-01": False, "2026-10-10": True, "2026-10-11": True}
        with patch.object(W, "_fetch_holiday_payload", return_value=fresh) as mocked:
            table = W.load_holidays(self.state, 2026)
        mocked.assert_called_once_with(2026)
        self.assertEqual(table, fresh)


class CalendarParseTests(unittest.TestCase):
    def payload(self):
        return {
            "success": True,
            "result": {
                "hasMore": False,
                "events": [{
                    "id": "e1",
                    "summary": "项目转型宣贯",
                    "isAllDay": False,
                    "start": {"dateTime": "2026-11-04T10:00:00+08:00"},
                    "end": {"dateTime": "2026-11-04T11:00:00+08:00"},
                    "attendees": [{"displayName": "我", "self": True, "responseStatus": "accepted"}],
                    "organizer": {"displayName": "思源-滕菁"},
                    "meetingRooms": [{"roomName": "湘湖"}],
                }],
            },
        }

    def test_parses_meeting_fields(self):
        with patch.object(W, "_run_dws", return_value=self.payload()):
            events = W.calendar_events(date(2026, 11, 4), base_cfg())
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["title"], "项目转型宣贯")
        self.assertEqual(ev["hours"], 1.0)
        self.assertEqual(ev["room"], "湘湖")
        self.assertFalse(ev["all_day"])
        self.assertFalse(ev["is_leave"])

    def test_leave_event_is_flagged(self):
        payload = self.payload()
        payload["result"]["events"][0].update({
            "summary": "请假中",
            "isAllDay": True,
            "start": {"dateTime": "2026-11-02T00:00:00+08:00"},
            "end": {"dateTime": "2026-11-02T23:59:00+08:00"},
        })
        with patch.object(W, "_run_dws", return_value=payload):
            events = W.calendar_events(date(2026, 11, 2), base_cfg())
        self.assertTrue(events[0]["is_leave"])
        self.assertTrue(events[0]["all_day"])

    def test_declined_event_is_dropped(self):
        payload = self.payload()
        payload["result"]["events"][0]["attendees"][0]["responseStatus"] = "declined"
        with patch.object(W, "_run_dws", return_value=payload):
            self.assertEqual(W.calendar_events(date(2026, 11, 4), base_cfg()), [])

    def test_missing_dws_output_degrades_to_empty(self):
        with patch.object(W, "_run_dws", return_value=None):
            self.assertEqual(W.calendar_events(date(2026, 11, 4), base_cfg()), [])

    def test_disabled_calendar_skips_dws(self):
        cfg = base_cfg(calendar={"enabled": False})
        with patch.object(W, "_run_dws", side_effect=AssertionError("should not call dws")):
            self.assertEqual(W.calendar_events(date(2026, 11, 4), cfg), [])


class MappingMatchTests(unittest.TestCase):
    def test_matches_by_keyword(self):
        cfg = base_cfg()
        self.assertEqual(W.match_mapping_by_title("招银升级评审", cfg), "招银金租")
        self.assertEqual(W.match_mapping_by_title("中集舆情需求对齐", cfg), "深圳租赁")

    def test_unrelated_title_matches_nothing(self):
        self.assertIsNone(W.match_mapping_by_title("项目转型宣贯", base_cfg()))


class AllocateHoursTests(unittest.TestCase):
    def test_sums_to_daily_total(self):
        ev = {
            "深圳租赁": evidence("深圳租赁", SHENZHEN, 3, 20, "a"),
            "招银金租": evidence("招银金租", ZHAOYIN, 1, 5, "b"),
        }
        hours = W.allocate_hours(ev, base_cfg())
        self.assertAlmostEqual(sum(hours.values()), 7.5, places=2)
        self.assertGreater(hours["深圳租赁"], hours["招银金租"])

    def test_respects_hour_step(self):
        ev = {
            "深圳租赁": evidence("深圳租赁", SHENZHEN, 2, 10, "a"),
            "招银金租": evidence("招银金租", ZHAOYIN, 2, 10, "b"),
        }
        hours = W.allocate_hours(ev, base_cfg())
        for value in hours.values():
            self.assertAlmostEqual((value * 2) % 1, 0, places=6)

    def test_empty_evidence_returns_empty(self):
        self.assertEqual(W.allocate_hours({}, base_cfg()), {})


class FillDayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.cfg = base_cfg()
        self.ev = {
            "深圳租赁": evidence("深圳租赁", SHENZHEN, 1, 5, "舆情批量识别收口"),
            "招银金租": evidence("招银金租", ZHAOYIN, 3, 20, "JDK 17 升级线"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_fill(self, meetings, ev=None):
        client = FakeClient(self.state, eligible_targets())
        with patch.object(W, "calendar_events", return_value=meetings), \
                patch.object(W, "collect_day", return_value=self.ev if ev is None else ev):
            return W.fill_day(client, self.cfg, date(2026, 11, 4), dry_run=True), client

    def meeting(self, title, hours=1.0, **extra):
        ev = {
            "title": title,
            "start": "2026-11-04T10:00:00+08:00",
            "end": "2026-11-04T11:00:00+08:00",
            "hours": hours,
            "all_day": False,
            "is_leave": False,
            "room": "湘湖",
            "attendees": 5,
            "organizer": "思源-滕菁",
        }
        ev.update(extra)
        return ev

    def test_meeting_does_not_change_hours(self):
        without, _ = self.run_fill([])
        with_meeting, _ = self.run_fill([self.meeting("项目转型宣贯")])
        self.assertAlmostEqual(without["totalHours"], 7.5, places=2)
        self.assertAlmostEqual(with_meeting["totalHours"], 7.5, places=2)
        self.assertEqual(
            {e["targetId"]: e["hours"] for e in without["entries"]},
            {e["targetId"]: e["hours"] for e in with_meeting["entries"]},
        )

    def test_meeting_is_appended_to_content(self):
        result, _ = self.run_fill([self.meeting("项目转型宣贯")])
        joined = " ".join(e["content"] for e in result["entries"])
        self.assertIn("参会：项目转型宣贯", joined)
        self.assertIn("10:00–11:00", joined)
        self.assertIn("湘湖", joined)

    def test_meeting_matches_project_by_keyword(self):
        result, _ = self.run_fill([self.meeting("招银数据源评审")])
        target = next(e for e in result["entries"] if e["targetId"] == ZHAOYIN["targetId"])
        self.assertIn("参会：招银数据源评审", target["content"])

    def test_unmapped_meeting_goes_to_biggest_entry(self):
        result, _ = self.run_fill([self.meeting("项目转型宣贯")])
        biggest = max(result["entries"], key=lambda e: e["hours"])
        self.assertIn("参会：项目转型宣贯", biggest["content"])

    def test_content_stays_within_limit(self):
        cfg = base_cfg(max_content_len=120)
        client = FakeClient(self.state, eligible_targets())
        with patch.object(W, "calendar_events", return_value=[self.meeting("项目转型宣贯")]), \
                patch.object(W, "collect_day", return_value=self.ev):
            result = W.fill_day(client, cfg, date(2026, 11, 4), dry_run=True)
        for entry in result["entries"]:
            self.assertLessEqual(len(entry["content"]), 120)

    def test_leave_day_is_skipped(self):
        client = FakeClient(self.state, eligible_targets())
        leave = self.meeting("请假中", hours=24.0, all_day=True, is_leave=True)
        with patch.object(W, "calendar_events", return_value=[leave]), \
                patch.object(W, "collect_day", return_value=self.ev):
            result = W.fill_day(client, self.cfg, date(2026, 11, 2), dry_run=True)
        self.assertEqual(result["status"], "skipped_on_leave")

    def test_short_leave_does_not_skip_the_day(self):
        client = FakeClient(self.state, eligible_targets())
        half = self.meeting("调休 1 小时", hours=1.0, is_leave=True)
        with patch.object(W, "calendar_events", return_value=[half]), \
                patch.object(W, "collect_day", return_value=self.ev):
            result = W.fill_day(client, self.cfg, date(2026, 11, 4), dry_run=True)
        self.assertEqual(result["status"], "dry_run")

    def test_all_day_meeting_is_not_counted_as_participation(self):
        result, _ = self.run_fill([self.meeting("全员大会", hours=8.0, all_day=True)])
        self.assertNotIn("参会：", " ".join(e["content"] for e in result["entries"]))

    def test_skip_mapping_is_left_out(self):
        skip = dict(ZHAOYIN, name="内部工具", targetType="SKIP", targetId=None)
        cfg = base_cfg(mappings=[SHENZHEN, skip])
        ev = {
            "深圳租赁": evidence("深圳租赁", SHENZHEN, 1, 5, "a"),
            "内部工具": evidence("内部工具", skip, 5, 40, "b"),
        }
        client = FakeClient(self.state, eligible_targets())
        with patch.object(W, "calendar_events", return_value=[]), \
                patch.object(W, "collect_day", return_value=ev):
            result = W.fill_day(client, cfg, date(2026, 11, 4), dry_run=True)
        self.assertEqual([e["targetId"] for e in result["entries"]], [SHENZHEN["targetId"]])
        self.assertAlmostEqual(result["totalHours"], 7.5, places=2)

    def test_unmapped_target_is_not_written_as_daily(self):
        stranger = dict(ZHAOYIN, targetId="ffffffff-0000-0000-0000-000000000000")
        cfg = base_cfg(mappings=[SHENZHEN, stranger])
        ev = {
            "深圳租赁": evidence("深圳租赁", SHENZHEN, 1, 5, "a"),
            "招银金租": evidence("招银金租", stranger, 3, 20, "b"),
        }
        client = FakeClient(self.state, eligible_targets())
        with patch.object(W, "calendar_events", return_value=[]), \
                patch.object(W, "collect_day", return_value=ev):
            result = W.fill_day(client, cfg, date(2026, 11, 4), dry_run=True)
        self.assertNotIn("DAILY", {e["targetType"] for e in result["entries"]})
        self.assertAlmostEqual(result["totalHours"], 7.5, places=2)


class ParseWorklogDocTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_topic_and_points(self):
        doc = self.root / "2026-11-04_周三_工作日志.md"
        doc.write_text(
            "# 日志\n\n**主题**：舆情批量识别收口\n\n| 主线 | 全选冻结方案落地 |\n\n"
            "## 今日工作\n\n"
            "**需求**：补全选冻结提示并支持批量回滚，覆盖历史数据。\n\n"
            "**修复**：去重逻辑改为按风险等级判断，避免误吞升级信号。\n",
            encoding="utf-8",
        )
        parsed = W.parse_worklog_doc(doc)
        self.assertIn("舆情批量识别收口", parsed["topic"])
        self.assertIn("全选冻结方案落地", parsed["mainline"])
        self.assertEqual(parsed["headlines"], ["今日工作"])
        joined = " ".join(parsed["points"])
        self.assertIn("批量回滚", joined)
        self.assertIn("风险等级", joined)

    def test_list_prefixed_points_are_not_collected(self):
        """真实日志的要点行是顶格写的；带 - 前缀的行当前不会被采集。"""
        doc = self.root / "2026-11-04_周三_工作日志.md"
        doc.write_text(
            "**主题**：x\n\n- **需求**：补全选冻结提示并支持批量回滚，覆盖历史数据。\n",
            encoding="utf-8",
        )
        self.assertEqual(W.parse_worklog_doc(doc)["points"], [])

    def test_short_point_bodies_are_dropped(self):
        doc = self.root / "2026-11-04_周三_工作日志.md"
        doc.write_text("**主题**：x\n\n**需求**：短。\n", encoding="utf-8")
        self.assertEqual(W.parse_worklog_doc(doc)["points"], [])

    def test_reports_no_docs_for_other_day(self):
        doc = self.root / "2026-11-04_周三_工作日志.md"
        doc.write_text("**主题**：x\n", encoding="utf-8")
        self.assertEqual(W.find_worklog_docs(self.root, datetime(2026, 11, 5)), [])


if __name__ == "__main__":
    unittest.main()
