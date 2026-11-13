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
        # 测试一律不真的去读钉钉聊天，需要时显式打开
        "chat": {"enabled": False},
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


class ContentQualityTests(unittest.TestCase):
    """工时正文要写清楚「做了什么」，不能是「改了 30 个文件」这种废话。"""

    def ev(self, repos: dict, files=0, samples=None) -> dict:
        return {
            "mapping": SHENZHEN,
            "git": [{"repo": r, "commits": len(s), "subjects": s} for r, s in repos.items()],
            "commits": sum(len(s) for s in repos.values()),
            "files": {"files": files, "samples": [(("09:00", p) if isinstance(p, str) else p) for p in (samples or [])]},
            "docs": [],
        }

    def test_commits_are_grouped_by_repository(self):
        text = W.summarize("深圳租赁", self.ev({
            "zhike-cust-engine-cyl": ["[cyl] ChainNode 五个 mapper 迁移到 PostgreSQL"],
            "zhike-cust-engine-ls": ["[db] 修复 PostgreSQL 方言兼容问题"],
        }), base_cfg())
        self.assertIn("zhike-cust-engine-cyl：[cyl] ChainNode 五个 mapper 迁移到 PostgreSQL。", text)
        self.assertIn("zhike-cust-engine-ls：[db] 修复 PostgreSQL 方言兼容问题。", text)

    def test_file_count_is_not_mentioned_when_there_are_commits(self):
        text = W.summarize("深圳租赁", self.ev(
            {"repo-a": ["[db] 修复 PostgreSQL 方言兼容问题"]}, files=37,
            samples=["repo-a\\a.txt", "repo-a\\b.txt"],
        ), base_cfg())
        self.assertNotIn("37", text)
        self.assertNotIn("个文件", text)

    def test_file_count_is_used_only_when_nothing_was_committed(self):
        text = W.summarize("深圳租赁", self.ev({}, files=37,
                                              samples=["fk-bi-zyzl\\src\\main\\resources\\a.xml"]),
                           base_cfg())
        self.assertIn("37 个文件", text)

    def test_junk_commit_subjects_are_skipped(self):
        text = W.summarize("深圳租赁", self.ev({"repo-a": [
            "能跑", "韩哥你", "On dev: user-local-application.yml",
            "index on dev: c7af254", "Merge branch 'x' into 'dev'",
            "[db] 修复 PostgreSQL 方言兼容问题",
        ]}), base_cfg())
        for junk in ("能跑", "韩哥你", "On dev:", "index on", "Merge"):
            self.assertNotIn(junk, text)
        self.assertIn("PostgreSQL 方言兼容", text)

    def test_conventional_prefixes_are_stripped(self):
        text = W.summarize("深圳租赁", self.ev({"repo-a": [
            "build(docker): 补充 docker-entrypoint.sh",
        ]}), base_cfg())
        self.assertIn("[docker] 补充 docker-entrypoint.sh", text)
        self.assertNotIn("build(", text)

    def test_repositories_are_capped(self):
        repos = {"repo-%d" % i: ["[db] 修复第 %d 处方言问题" % i] for i in range(6)}
        text = W.summarize("深圳租赁", self.ev(repos), base_cfg(max_repos_in_content=2))
        self.assertIn("repo-0", text)
        self.assertNotIn("repo-3", text)

    def test_deep_paths_are_shortened(self):
        self.assertEqual(
            W._short_path(r"fk-bi-zyzl\src\main\resources\application.yml"),
            r"fk-bi-zyzl\…\application.yml",
        )
        self.assertEqual(W._short_path("pom.xml"), "pom.xml")

    def test_tool_artifacts_do_not_count_as_work_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = datetime(2026, 11, 10)
            stamp = day.replace(hour=10).timestamp()
            for rel in (".acl-recovery/backup.json", ".pg-audit/audit.ps1", "src/main/A.java"):
                fp = root / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text("x", encoding="utf-8")
                import os
                os.utime(fp, (stamp, stamp))
            found = W.file_activity(root, day, [], 4)
            self.assertEqual(found["files"], 1)


class UncommittedDayTests(unittest.TestCase):
    """只有 mtime 变化、没有提交的目录，不该被写成「改了 34 个文件」的工时条目。"""

    def test_day_with_only_file_changes_is_reported_as_no_commits(self):
        client = FakeClient(Path("."), eligible_targets())
        touched = {"mapping": ZHAOYIN, "git": [], "commits": 0,
                   "files": {"files": 34, "samples": []}, "docs": [], "dirs": ["招银"]}
        with patch.object(W, "calendar_events", return_value=[]), \
                patch.object(W, "collect_day", return_value={}), \
                patch.object(W, "collect_day", side_effect=lambda c, d, e=None, notes=None: (
                    notes.extend(["招银金租（34 个文件）"]) or {})):
            result = W.fill_day(client, base_cfg(), date(2026, 11, 9), dry_run=True)
        self.assertEqual(result["status"], "skipped_no_commits")
        self.assertIn("招银金租（34 个文件）", result["touched_only"])

    def test_collect_day_notes_directories_without_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "项目甲").mkdir()
            cfg = base_cfg(workspace_root=str(root), mappings=[], discovery={"enabled": True})
            notes = []
            with patch.object(W, "file_activity", return_value={"files": 30, "samples": []}):
                got = W.collect_day(cfg, datetime(2026, 11, 9), [], notes)
            self.assertEqual(got, {})
            self.assertEqual(notes, ["项目甲（30 个文件）"])


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


class CommitAuthorTests(unittest.TestCase):
    """工时只算自己的提交，别人的提交不能算进来。"""

    def test_my_commit_matches_by_name(self):
        self.assertTrue(W.is_my_commit(["hangyi"], "hangyi", "a@b.com"))

    def test_my_commit_matches_by_email(self):
        self.assertTrue(W.is_my_commit(["hangyi@yscredit.com"], "someone", "hangyi@yscredit.com"))

    def test_someone_else_is_not_mine(self):
        self.assertFalse(W.is_my_commit(["hangyi"], "jiangchenghao", "jch@yscredit.com"))

    def test_match_is_case_insensitive(self):
        self.assertTrue(W.is_my_commit(["HangYi"], "hangyi", ""))

    def test_configured_authors_win_over_the_repo_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = base_cfg(git_authors=["hangyi"])
            with patch.object(W, "git_identity", return_value=["someone@else.com"]):
                self.assertEqual(W.author_filters(cfg, repo), ["hangyi"])

    def test_repo_identity_is_used_when_nothing_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch.object(W, "git_identity", return_value=["me@corp.com", "me"]):
                self.assertEqual(W.author_filters(base_cfg(), repo), ["me@corp.com", "me"])

    def test_falls_back_to_the_known_author_as_a_last_resort(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch.object(W, "git_identity", return_value=[]):
                self.assertEqual(W.author_filters(base_cfg(), repo), [W.DEFAULT_GIT_AUTHOR])

    def test_git_activity_drops_other_peoples_commits(self):
        repo = Path("repo")
        log = (
            "09:00\x1fhangyi\x1fhangyi@yscredit.com\x1ffix(db): 修复 PostgreSQL 方言兼容问题\x1f\x1e"
            "10:00\x1fjiangchenghao\x1fjch@yscredit.com\x1f能跑\x1f\x1e"
        )
        with patch.object(W, "run", side_effect=lambda cmd, cwd, **kw: log if "log" in cmd else ""):
            got = W.git_activity(repo, datetime(2026, 11, 10), ["hangyi"])
        self.assertEqual(got["commits"], 1)
        self.assertEqual(got["subjects"], ["fix(db): 修复 PostgreSQL 方言兼容问题"])
        self.assertEqual(got["others"], ["jiangchenghao:能跑"])

    def test_git_activity_without_a_filter_keeps_everything(self):
        repo = Path("repo")
        log = (
            "09:00\x1fhangyi\x1fhangyi@yscredit.com\x1f修复方言\x1f\x1e"
            "10:00\x1fjiangchenghao\x1fjch@yscredit.com\x1f能跑\x1f\x1e"
        )
        with patch.object(W, "run", side_effect=lambda cmd, cwd, **kw: log if "log" in cmd else ""):
            got = W.git_activity(repo, datetime(2026, 11, 10))
        self.assertEqual(got["commits"], 2)

    def test_author_is_read_from_the_repository_config(self):
        repo = Path("repo")

        def fake(cmd, cwd, **kw):
            if cmd[1:3] == ["config", "--local"] and cmd[3] == "user.email":
                return "hangyi@yscredit.com\n"
            if cmd[1:3] == ["config", "--local"] and cmd[3] == "user.name":
                return "hangyi\n"
            return ""

        with patch.object(W, "run", side_effect=fake):
            self.assertEqual(W.git_identity(repo), ["hangyi@yscredit.com", "hangyi"])


class ChatSourceTests(unittest.TestCase):
    """钉钉聊天也能作为工作痕迹，但只写做了什么，不写是谁。"""

    def payload(self, messages: list) -> dict:
        by_conv = {}
        for m in messages:
            by_conv.setdefault(m.get("conversationTitle"), []).append(m)
        return {"result": {"conversationMessagesList": [
            {"messages": items} for items in by_conv.values()
        ]}}

    def msg(self, text, sender="行弈-吴铭威", conv="深圳租赁-技术开发群", at="10:00:00", single=False):
        return {"text": text, "content": text, "sender": sender, "conversationTitle": conv,
                "createTime": "2026-11-11 " + at, "singleChat": single, "sendType": "user"}

    def test_other_peoples_messages_are_ignored(self):
        payload = self.payload([
            self.msg("API_ZD_URL 生产配置是多少", sender="守仁-吴文斌"),
            self.msg("检查 API_ZD_URL 生产配置并更新"),
        ])
        with patch.object(W, "_run_dws", return_value=payload):
            got = W.chat_activity(date(2026, 11, 11), base_cfg(chat={"enabled": True, "me": ["行弈-吴铭威"]}))
        self.assertEqual(len(got), 1)
        self.assertIn("API_ZD_URL", got[0]["text"])

    def test_robot_messages_are_ignored(self):
        bot = dict(self.msg("流水线构建失败"), sendType="robot")
        with patch.object(W, "_run_dws", return_value=self.payload([bot])):
            got = W.chat_activity(date(2026, 11, 11), base_cfg(chat={"enabled": True, "me": ["行弈-吴铭威"]}))
        self.assertEqual(got, [])

    def test_short_acknowledgements_are_dropped(self):
        payload = self.payload([
            self.msg("好的"), self.msg("是的"), self.msg("收到，我看下"),
            self.msg("把 figure-engine 的 JDK17 升级好了"),
        ])
        with patch.object(W, "_run_dws", return_value=payload):
            got = W.chat_activity(date(2026, 11, 11), base_cfg(chat={"enabled": True, "me": ["行弈-吴铭威"]}))
        self.assertEqual(len(got), 1)
        self.assertIn("JDK17", got[0]["text"])

    def test_image_and_link_noise_is_stripped(self):
        text = "[图片消息](mediaId=@lQLPJw_KjWRPFlnNAW) https://x.example.com/a API_ZD_URL 要改"
        cleaned = W.clean_chat_text(text)
        self.assertNotIn("mediaId", cleaned)
        self.assertNotIn("https", cleaned)
        self.assertIn("API_ZD_URL", cleaned)

    def test_media_ids_do_not_leak_into_the_summary(self):
        payload = self.payload([self.msg("[文件消息](mediaId=$iwEcAqNwbmcDAQTRBGkF) long_text_F0A79DE5.txt")])
        with patch.object(W, "_run_dws", return_value=payload):
            got = W.chat_activity(date(2026, 11, 11), base_cfg(chat={"enabled": True, "me": ["行弈-吴铭威"]}))
        for m in got:
            self.assertNotIn("iwEcAq", m["text"])
            self.assertNotIn("lQLPJ", m["text"])

    def test_summary_names_no_people(self):
        items = [
            {"time": "10:00", "conversation": "邓南平", "single": True,
             "text": "招银测试账号可以用", "project": "", "group": ""},
            {"time": "11:00", "conversation": "深圳租赁-技术开发群", "single": False,
             "text": "API_ZD_URL 生产配置要更新", "project": "深圳租赁", "group": "深圳租赁-技术开发群"},
        ]
        text = W.summarize_chat(items)
        self.assertNotIn("邓南平", text)
        self.assertIn("深圳租赁", text)

    def test_summary_falls_back_to_the_group_when_no_technical_words(self):
        items = [{"time": "10:00", "conversation": "重庆-切库小群", "single": False,
                  "text": "那我更新到他们环境就好了吧", "project": "", "group": "重庆-切库小群"}]
        text = W.summarize_chat(items)
        self.assertTrue(text == "" or "重庆" in text or "讨论" in text)

    def test_chat_read_failure_never_breaks_the_run(self):
        with patch.object(W, "_run_dws", return_value=None):
            self.assertEqual(W.chat_activity(date(2026, 11, 11), base_cfg(chat={"enabled": True})), [])

    def test_disabled_chat_does_not_call_dws(self):
        with patch.object(W, "_run_dws") as run:
            self.assertEqual(W.chat_activity(date(2026, 11, 11), base_cfg()), [])
            run.assert_not_called()

    def test_group_title_maps_to_a_project(self):
        cfg = base_cfg(chat={"enabled": True, "me": ["行弈-吴铭威"]})
        cfg["mappings"] = [dict(SHENZHEN, keywords=["客户 B", "技术开发群"])]
        with patch.object(W, "_run_dws", return_value=self.payload([
            self.msg("中登数据接口的地址要确认", conv="客户 B-技术开发群"),
        ])):
            got = W.chat_activity(date(2026, 11, 11), cfg)
        self.assertEqual(got[0]["project"], SHENZHEN["name"])


class CommitHighlightTests(unittest.TestCase):
    """「跑批性能优化」要说清优化了什么、提升多少，不能只罗列。"""

    BODY = (
        "批量写：\n"
        "- 压测：10000 行 392.7s -> 9.1s（43x），SQL 条数 10000 -> 20。\n\n"
        "addEid：\n"
        "- 4 条 select max() 由每页重跑改为整批取一次快照\n"
        "  （1096 次 -> 4 次，1566ms -> 523ms）。\n"
    )

    def test_quantified_results_survive(self):
        got = W.commit_highlights(self.BODY)
        joined = " ".join(got)
        self.assertIn("392.7s", joined)
        self.assertIn("9.1s", joined)

    def test_trailing_measurement_line_is_kept(self):
        """数字单独折到下一行时，也要跟上一条并起来。"""
        got = W.commit_highlights("addEid：\n- 逐客户 findEid 改为每页一次\n  （12214 次 -> 245 次）。\n")
        self.assertTrue(any("12214" in h for h in got))

    def test_section_heading_is_attached(self):
        self.assertTrue(any(h.startswith("批量写：") for h in W.commit_highlights(self.BODY)))

    def test_lines_without_numbers_are_dropped(self):
        got = W.commit_highlights("批量写：\n- 新增 insertIntoTableBatch，拼 multi-values\n")
        self.assertEqual(got, [])

    def test_arrow_is_normalised(self):
        self.assertTrue(all("->" not in h for h in W.commit_highlights(self.BODY)))

    def test_highlight_count_is_capped(self):
        self.assertLessEqual(len(W.commit_highlights(self.BODY, limit=1)), 1)

    def test_summary_puts_the_gain_next_to_what_was_done(self):
        ev = {"mapping": {}, "git": [{"repo": "mr-injection", "commits": 1, "subjects": [],
                                      "details": [{"subject": "perf(fk-data): DataBIMonitorService 跑批性能优化",
                                                   "body": self.BODY}]}],
              "files": {"files": 0, "samples": []}, "docs": [], "commits": 1}
        text = W.summarize("招银金租", ev, base_cfg())
        self.assertIn("跑批性能优化", text)
        self.assertIn("392.7s", text)
        self.assertNotIn("\n", text)

    def test_summary_without_a_body_still_reads_cleanly(self):
        ev = {"mapping": {}, "git": [{"repo": "r", "commits": 1, "subjects": [],
                                      "details": [{"subject": "fix(db): 修复方言兼容", "body": ""}]}],
              "files": {"files": 0, "samples": []}, "docs": [], "commits": 1}
        self.assertIn("修复方言兼容", W.summarize("招银金租", ev, base_cfg()))


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
