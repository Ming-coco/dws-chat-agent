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
    "name": "客户 B",
    "workspace": r"D:\workspace\客户 B",
    "targetType": "PROJECT",
    "targetId": "249cc0b7-ae48-3a79-832b-d30f7d014e76",
    "keywords": ["客户 B", "中集"],
}
ZHAOYIN = {
    "name": "客户 A",
    "workspace": r"D:\workspace\客户 A",
    "targetType": "PROJECT",
    "targetId": "a114d835-e374-3bec-9ade-d5536cd5638c",
    "keywords": ["客户 A"],
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


def eligible_targets(extra: list | None = None) -> list:
    items = [
        {"targetType": m["targetType"], "id": m["targetId"], "name": m["name"]}
        for m in (SHENZHEN, ZHAOYIN)
    ]
    return items + list(extra or [])


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
        self.assertEqual(W.match_mapping_by_title("客户 A升级评审", cfg), "客户 A")
        self.assertEqual(W.match_mapping_by_title("中集舆情需求对齐", cfg), "客户 B")

    def test_unrelated_title_matches_nothing(self):
        self.assertIsNone(W.match_mapping_by_title("项目转型宣贯", base_cfg()))


class AllocateHoursTests(unittest.TestCase):
    def test_sums_to_daily_total(self):
        ev = {
            "客户 B": evidence("客户 B", SHENZHEN, 3, 20, "a"),
            "客户 A": evidence("客户 A", ZHAOYIN, 1, 5, "b"),
        }
        hours = W.allocate_hours(ev, base_cfg())
        self.assertAlmostEqual(sum(hours.values()), 7.5, places=2)
        self.assertGreater(hours["客户 B"], hours["客户 A"])

    def test_respects_hour_step(self):
        ev = {
            "客户 B": evidence("客户 B", SHENZHEN, 2, 10, "a"),
            "客户 A": evidence("客户 A", ZHAOYIN, 2, 10, "b"),
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
            "客户 B": evidence("客户 B", SHENZHEN, 1, 5, "舆情批量识别收口"),
            "客户 A": evidence("客户 A", ZHAOYIN, 3, 20, "JDK 17 升级线"),
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
        result, _ = self.run_fill([self.meeting("客户 A数据源评审")])
        target = next(e for e in result["entries"] if e["targetId"] == ZHAOYIN["targetId"])
        self.assertIn("参会：客户 A数据源评审", target["content"])

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
            "客户 B": evidence("客户 B", SHENZHEN, 1, 5, "a"),
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
            "客户 B": evidence("客户 B", SHENZHEN, 1, 5, "a"),
            "客户 A": evidence("客户 A", stranger, 3, 20, "b"),
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

    def test_hand_written_section_wins_over_the_auto_summary(self):
        doc = self.root / "2026-11-06_周五_工作日志.md"
        doc.write_text(
            "# 日志\n\n**主题**：自动摘要出来的主题\n\n"
            "## 一、今日工作\n\n"
            "**问题**：这条要点不该出现在工时正文里。\n\n"
            "## 九、工时内容\n\n"
            "第一段正文。\n"
            "第二段正文，含 ① ② 这样的编号。\n\n"
            "## 十、遗留\n\n"
            "遗留内容不该被带上。\n",
            encoding="utf-8",
        )
        parsed = W.parse_worklog_doc(doc)
        self.assertIn("第一段正文", parsed["content"])
        self.assertIn("第二段正文", parsed["content"])
        self.assertNotIn("遗留内容", parsed["content"])

    def test_hand_written_section_excludes_the_heading_itself(self):
        doc = self.root / "2026-11-06_周五_工作日志.md"
        doc.write_text("## 工时内容\n\n只有这一段。\n", encoding="utf-8")
        self.assertEqual(W.parse_worklog_doc(doc)["content"], "只有这一段。")

    def test_hand_written_heading_is_not_a_headline(self):
        doc = self.root / "2026-11-06_周五_工作日志.md"
        doc.write_text("## 一、工时内容\n\n正文。\n", encoding="utf-8")
        self.assertEqual(W.parse_worklog_doc(doc)["headlines"], [])

    def test_summarize_uses_hand_written_content_verbatim(self):
        ev = {"mapping": SHENZHEN, "git": [], "commits": 0,
              "files": {"files": 0, "samples": []},
              "docs": [{"topic": "主题", "mainline": "", "headlines": [],
                        "points": [], "content": "第一段。\n第二段。"}]}
        text = W.summarize("客户 B", ev, base_cfg())
        self.assertIn("第一段。", text)
        self.assertIn("第二段。", text)
        self.assertNotIn("\n", text)

    def test_summarize_falls_back_to_auto_summary_without_hand_written_content(self):
        ev = {"mapping": SHENZHEN, "git": [], "commits": 0,
              "files": {"files": 0, "samples": []},
              "docs": [{"topic": "主题甲", "mainline": "主线乙", "headlines": ["推进丙"],
                        "points": [], "content": ""}]}
        text = W.summarize("客户 B", ev, base_cfg())
        self.assertIn("主题甲", text)
        self.assertIn("主线乙", text)

    def test_trim_content_cuts_at_a_sentence_end(self):
        text = "第一句。" * 20 + "最后半句不该留"
        trimmed = W.trim_content(text, base_cfg(max_content_len=30))
        self.assertLessEqual(len(trimmed), 30)
        self.assertTrue(trimmed.endswith("。"))
        self.assertNotIn("最后半句", trimmed)

    def test_trim_content_leaves_short_text_alone(self):
        self.assertEqual(W.trim_content("短内容。", base_cfg()), "短内容。")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("客户 A", "新项目", ".hidden", "_scratch", "node_modules", "dbeaver统一导出sql"):
            (self.root / name).mkdir()
        (self.root / "afile.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **discovery):
        block = {"enabled": True, "ignore": ["dbeaver统一导出sql"]}
        block.update(discovery)
        return base_cfg(workspace_root=str(self.root), discovery=block)

    def names(self, cfg):
        return sorted(p.name for p in W.discover_dirs(cfg))

    def test_skips_hidden_platform_and_ignored_dirs(self):
        self.assertEqual(set(self.names(self.cfg())), {"新项目", "客户 A"})

    def test_disabled_discovery_yields_nothing(self):
        self.assertEqual(self.names(self.cfg(enabled=False)), [])

    def test_missing_root_is_tolerated(self):
        cfg = base_cfg(workspace_root=str(self.root / "nope"), discovery={"enabled": True})
        self.assertEqual(W.discover_dirs(cfg), [])

    def test_new_dir_matches_existing_mapping_by_keyword(self):
        self.assertEqual(W.match_mapping_for_dir("客户 A二期", base_cfg())["name"], "客户 A")

    def test_generic_dir_name_matches_nothing(self):
        self.assertIsNone(W.match_mapping_for_dir("标准部署版本", base_cfg()))

    def test_auto_match_uses_project_name(self):
        items = [{"targetType": "PROJECT", "id": "x", "name": "某平台企业视图部署服务"}]
        hit = W.auto_match("某平台部署", items, base_cfg())
        self.assertEqual(hit["targetId"], "x")
        self.assertTrue(hit["auto"])

    def test_auto_match_never_picks_daily(self):
        items = [{"targetType": "DAILY", "id": "d", "name": "日常工作"}]
        self.assertIsNone(W.auto_match("日常工作", items, base_cfg()))

    def test_auto_match_ignores_single_char_overlap(self):
        items = [{"targetType": "PROJECT", "id": "x", "name": "甲"}]
        self.assertIsNone(W.auto_match("甲乙丙", items, base_cfg()))

    def test_auto_match_bails_out_on_a_tie(self):
        items = [
            {"targetType": "PROJECT", "id": "a", "name": "区域产业链风控"},
            {"targetType": "PROJECT", "id": "b", "name": "区域产业链营销"},
        ]
        self.assertIsNone(W.auto_match("区域产业链", items, base_cfg()))

    def test_auto_match_prefers_exact_containment(self):
        items = [
            {"targetType": "PROJECT", "id": "a", "name": "无关项目"},
            {"targetType": "PROJECT", "id": "b", "customerName": "客户 A金融租赁有限公司"},
        ]
        self.assertEqual(W.auto_match("客户 A", items, base_cfg())["targetId"], "b")


FALLBACK = {
    "name": "技术产品架构V2.0",
    "targetType": "PRODUCT",
    "targetId": "77ba332d-b8bc-31b2-a030-55cd52016963",
}


class UnmappedDirectoryTests(unittest.TestCase):
    """当天只有「没匹配上项目」的目录在动时，挂兜底项目，不空跑。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.cfg = base_cfg(fallback_target=FALLBACK)
        self.ev = {
            "某个新目录": {
                "mapping": None,
                "git": [],
                "commits": 21,
                "files": {"files": 3, "samples": []},
                "docs": [],
                "dirs": ["某个新目录"],
            }
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_fill(self):
        client = FakeClient(self.state, eligible_targets(
            [{"targetType": FALLBACK["targetType"], "id": FALLBACK["targetId"], "name": FALLBACK["name"]}]
        ))
        with patch.object(W, "calendar_events", return_value=[]), \
                patch.object(W, "collect_day", return_value=self.ev):
            return W.fill_day(client, self.cfg, date(2026, 11, 4), dry_run=True)

    def test_unmatched_day_still_produces_a_draft_on_the_fallback(self):
        result = self.run_fill()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["targetId"], FALLBACK["targetId"])
        self.assertEqual(result["entries"][0]["hours"], 7.5)

    def test_fallback_day_names_the_unknown_directories(self):
        self.assertEqual(self.run_fill()["unmapped"], ["某个新目录"])
        self.assertEqual(self.run_fill()["fallback"], FALLBACK["name"])

    def test_without_a_fallback_the_day_is_left_empty(self):
        cfg = base_cfg()
        client = FakeClient(self.state, eligible_targets())
        with patch.object(W, "calendar_events", return_value=[]), \
                patch.object(W, "collect_day", return_value=self.ev):
            result = W.fill_day(client, cfg, date(2026, 11, 4), dry_run=True)
        self.assertEqual(result["status"], "skipped_no_content")
        self.assertEqual(result["unmapped"], ["某个新目录"])

    def test_merge_evidence_adds_up_the_directories(self):
        other = {
            "mapping": None, "git": [], "commits": 4,
            "files": {"files": 2, "samples": [("09:00", "a.py")]},
            "docs": [], "dirs": ["另一个目录"],
        }
        ev = dict(self.ev, 另一个目录=other)
        merged = W.merge_evidence(ev, ["某个新目录", "另一个目录"], FALLBACK)
        self.assertEqual(merged["commits"], 25)
        self.assertEqual(merged["files"]["files"], 5)
        self.assertEqual(merged["dirs"], ["某个新目录", "另一个目录"])
        self.assertTrue(merged["fallback"])

    def test_notification_says_where_the_hours_went(self):
        result = dict(self.run_fill(), status="draft_saved")
        subject, body = W.build_notification([result], base_cfg(
            notify={"enabled": True, "from": "a@b.com", "to": ["a@b.com"]},
            fallback_target=FALLBACK,
        ))
        self.assertIn("某个新目录", body)
        self.assertIn(FALLBACK["name"], body)
        self.assertIn("7.5", subject)


class NotificationTests(unittest.TestCase):
    def draft(self, **overrides):
        row = {
            "date": "2026-11-04",
            "status": "draft_saved",
            "totalHours": 7.5,
            "entries": [
                {"targetId": SHENZHEN["targetId"], "hours": 1.5, "content": "推进事项一。"},
                {"targetId": ZHAOYIN["targetId"], "hours": 6.0, "content": "推进事项二。"},
            ],
            "reviewUrl": "https://zhiguan.example/drafts/abc",
        }
        row.update(overrides)
        return row

    def cfg(self, **notify):
        block = {"enabled": True, "channel": "mail", "from": "a@b.com", "to": ["a@b.com"]}
        block.update(notify)
        return base_cfg(notify=block)

    def test_subject_carries_date_and_total(self):
        subject, body = W.build_notification([self.draft()], self.cfg())
        self.assertIn("2026-11-04", subject)
        self.assertIn("7.5", subject)
        self.assertIn("https://zhiguan.example/drafts/abc", body)

    def test_entry_names_resolve_from_mappings(self):
        _, body = W.build_notification([self.draft()], self.cfg())
        self.assertIn("客户 B", body)
        self.assertIn("客户 A", body)

    def test_paragraphs_are_separated_by_blank_lines(self):
        _, body = W.build_notification([self.draft()], self.cfg())
        self.assertIn("\n\n", body)
        self.assertNotIn("\n\n\n", body)

    def test_disabled_sends_nothing(self):
        self.assertIsNone(W.build_notification([self.draft()], self.cfg(enabled=False)))

    def test_draft_only_mode_skips_quiet_days(self):
        quiet = [self.draft(status="skipped_no_activity", totalHours=0, entries=[])]
        self.assertIsNone(W.build_notification(quiet, self.cfg(mode="draft_only")))

    def test_always_mode_reports_skipped_day(self):
        quiet = [self.draft(status="skipped_on_leave", totalHours=0, entries=[])]
        subject, body = W.build_notification(quiet, self.cfg(mode="always"))
        self.assertIn("请假", body)
        self.assertIn("未生成草稿", subject)

    def test_long_dashes_are_normalised(self):
        row = self.draft(entries=[
            {"targetId": SHENZHEN["targetId"], "hours": 1.0, "content": "会议（10:00\u201311:00）。"},
        ])
        _, body = W.build_notification([row], self.cfg())
        self.assertNotIn("\u2013", body)
        self.assertNotIn("\u2014", body)

    def test_send_mail_without_recipients_does_not_call_dws(self):
        cfg = base_cfg(notify={"enabled": True, "from": "a@b.com", "to": []})
        with patch.object(W, "_run_dws") as run:
            self.assertIsNone(W.send_mail(cfg, "s", "b"))
            run.assert_not_called()

    def test_send_mail_passes_expected_arguments(self):
        cfg = self.cfg(cc=["c@d.com"])
        reply = {"success": "true", "internetMessageId": "x"}
        with patch.object(W, "_run_dws", return_value=reply) as run:
            self.assertEqual(W.send_mail(cfg, "主题", "正文"), reply)
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["mail", "message", "send"])
        self.assertEqual(args[args.index("--to") + 1], "a@b.com")
        self.assertEqual(args[args.index("--cc") + 1], "c@d.com")
        self.assertEqual(args[args.index("--content") + 1], "正文")

    def test_cc_that_repeats_a_recipient_is_dropped(self):
        """同一个人两个邮箱各投一份，钉钉里就会出现两张一模一样的卡片。"""
        cfg = self.cfg(cc=["A@B.com"])
        with patch.object(W, "_run_dws", return_value={"success": "true"}) as run:
            W.send_mail(cfg, "s", "b")
        self.assertNotIn("--cc", run.call_args[0][0])

    def test_duplicate_addresses_inside_one_list_are_collapsed(self):
        self.assertEqual(W.unique_addresses(["a@b.com", "A@b.com ", ""]), ["a@b.com"])

    def test_cc_only_carries_addresses_not_already_in_to(self):
        self.assertEqual(
            W.unique_addresses(["a@b.com"]),
            ["a@b.com"],
        )
        cfg = self.cfg(cc=["a@b.com", "c@d.com"])
        with patch.object(W, "_run_dws", return_value={"success": "true"}) as run:
            W.send_mail(cfg, "s", "b")
        self.assertEqual(run.call_args[0][0][run.call_args[0][0].index("--cc") + 1], "c@d.com")

    def test_dws_command_prefers_node_when_js_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            js = Path(tmp) / "node_modules" / "dingtalk-workspace-cli" / "bin" / "dws.js"
            js.parent.mkdir(parents=True)
            js.write_text("", encoding="utf-8")
            with patch.object(W, "dws_executable", return_value=str(Path(tmp) / "dws.cmd")):
                with patch.object(W.shutil, "which", return_value="/usr/bin/node"):
                    cmd = W._dws_command(["mail", "message", "send"])
        self.assertEqual(cmd[0], "/usr/bin/node")
        self.assertTrue(cmd[1].endswith("dws.js"))
        self.assertEqual(cmd[2:], ["mail", "message", "send"])

    def test_dws_command_falls_back_to_cmd_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(W, "dws_executable", return_value=str(Path(tmp) / "dws.cmd")):
                with patch.object(W, "os") as fake_os:
                    fake_os.name = "nt"
                    with patch.object(W.shutil, "which", return_value=None):
                        cmd = W._dws_command(["a"])
        self.assertEqual(cmd[-1], "a")
        self.assertIn("dws.cmd", cmd[-2])


class NotificationDedupTests(unittest.TestCase):
    """同一天重跑但内容没变时，不能再发一封一模一样的邮件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.cfg = base_cfg(notify={
            "enabled": True, "channel": "mail", "from": "a@b.com", "to": ["a@b.com"],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def draft(self, hours=7.5):
        return {
            "date": "2026-11-04",
            "status": "draft_saved",
            "totalHours": hours,
            "entries": [{"targetId": SHENZHEN["targetId"], "hours": hours, "content": "推进事项。"}],
            "reviewUrl": "https://zhiguan.example/drafts/abc",
        }

    def send(self, results, renotify=False):
        reply = {"success": "true", "internetMessageId": "x"}
        with patch.object(W, "send_mail", return_value=reply) as fake:
            W.notify_mail(self.state, self.cfg, results, renotify=renotify)
        return fake

    def test_identical_rerun_sends_only_once(self):
        fake = self.send([self.draft()])
        again = self.send([self.draft()])
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(again.call_count, 0)

    def test_same_day_never_gets_a_second_mail_even_if_the_wording_changes(self):
        """重跑一次正文措辞会变（文件数、提交数），但一天只该收一封。"""
        first = self.send([self.draft(7.5)])
        second = self.send([self.draft(8.0)])
        self.assertEqual(first.call_count, 1)
        self.assertEqual(second.call_count, 0)

    def test_renotify_forces_the_mail_out_again(self):
        self.send([self.draft(7.5)])
        again = self.send([self.draft(8.0)], renotify=True)
        self.assertEqual(again.call_count, 1)

    def test_a_different_day_still_gets_its_own_mail(self):
        self.send([self.draft()])
        other = dict(self.draft(), date="2026-11-05")
        self.assertEqual(self.send([other]).call_count, 1)

    def test_fingerprint_is_recorded_in_the_log(self):
        self.send([self.draft()])
        rows = [json.loads(l) for l in (self.state / W.LOG_NAME).read_text(encoding="utf-8").splitlines() if l.strip()]
        notified = [r for r in rows if r.get("status") == "notified"]
        self.assertEqual(len(notified), 1)
        self.assertTrue(notified[0]["fingerprint"])

    def test_missing_log_means_nothing_was_sent_before(self):
        self.assertIsNone(W.last_notified_fingerprint(self.state))
        self.assertFalse(W.was_notified(self.state, "deadbeef"))

    def test_fingerprint_survives_an_unrelated_entry_in_between(self):
        self.send([self.draft()])
        W.append_log(self.state, {"at": "2026-11-05T17:40:00", "date": "2026-11-05", "status": "notified"})
        self.assertEqual(self.send([self.draft()]).call_count, 0)

    def test_failed_send_does_not_record_a_fingerprint(self):
        with patch.object(W, "send_mail", return_value=None):
            W.notify_mail(self.state, self.cfg, [self.draft()])
        self.assertIsNone(W.last_notified_fingerprint(self.state))


if __name__ == "__main__":
    unittest.main()
