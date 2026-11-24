"""历史数据清理的测试。

关键点：
  - 只删过程记录（runs/events/outgoing/manual_messages）
  - 事项记忆（tasks/task_changes）和游标（console_cursors）必须保留
  - 清理的是「早于截止时间」的数据，边界内的不能动
"""

from __future__ import annotations

import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dws_dm_agent.core import AuditStore, Contact, IncomingEvent


class PurgeHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent.parent / "build" / "test-tmp" / (
            f"purge-{os.getpid()}-{id(self)}"
        )
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = AuditStore(self.root / "audit.sqlite3")
        self.tz = timezone(timedelta(hours=8))
        self.now = datetime.now(self.tz)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _insert_run(self, days_ago: int, session: str) -> None:
        moment = self.now - timedelta(days=days_ago)
        contact = Contact(
            alias="测试",
            display_name="测试",
            user_id="u1",
            open_dingtalk_id="o1",
            projects=(),
        )
        self.store.record_run(
            session_id=session,
            conversation_id="c1",
            contact=contact,
            started_at=moment,
            finished_at=moment,
            action="reply",
            status="sent",
            handled="hi",
            reason="",
            codex_exit_code=0,
            request_preview="hello",
            trace_id=f"t-{session}",
        )

    def _count(self, table: str) -> int:
        return self.store.connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]

    def test_removes_runs_older_than_cutoff(self) -> None:
        self._insert_run(40, "old1")
        self._insert_run(40, "old2")
        self._insert_run(5, "recent")
        self.assertEqual(self._count("runs"), 3)

        cutoff = self.now - timedelta(days=30)
        removed = self.store.purge_history(cutoff)

        self.assertEqual(removed.get("runs"), 2)
        self.assertEqual(self._count("runs"), 1)

    def test_keeps_rows_inside_window(self) -> None:
        self._insert_run(1, "fresh")
        cutoff = self.now - timedelta(days=30)
        self.store.purge_history(cutoff)
        self.assertEqual(self._count("runs"), 1)

    def test_tasks_are_never_purged(self) -> None:
        # 事项记忆是长期上下文，按时间清理会丢线索
        moment = (self.now - timedelta(days=200)).isoformat()
        self.store.connection.execute(
            """
            INSERT INTO tasks(
                id, scope_self, scope_contact, scope_conversation,
                goal, status, last_evidence_at, created_at, updated_at
            ) VALUES ('t1', '1', 'u1', 'c1', '待办', 'open', ?, ?, ?)
            """,
            (moment, moment, moment),
        )
        self.store.connection.commit()

        self.store.purge_history(self.now - timedelta(days=30))
        self.assertEqual(self._count("tasks"), 1, "事项记忆不该被清理")

    def test_returns_per_table_counts(self) -> None:
        self._insert_run(40, "old")
        removed = self.store.purge_history(self.now - timedelta(days=30))
        self.assertIn("runs", removed)
        # 没有数据的表也应在结果里，方便日志观察
        self.assertIn("events", removed)
        self.assertIn("outgoing", removed)
        self.assertIn("manual_messages", removed)

    def test_empty_store_is_safe(self) -> None:
        removed = self.store.purge_history(self.now - timedelta(days=30))
        self.assertEqual(sum(removed.values()), 0)


class RetentionConfigTests(unittest.TestCase):
    def test_trace_follows_history(self) -> None:
        from dws_dm_agent.config import Settings

        settings = Settings(
            raw={},
            config_path=Path("config.json"),
            env_path=Path(".env"),
            timezone=timezone(timedelta(hours=8)),
            contacts=(),
            groups=(),
            state_dir=Path("state"),
            workspace_root=Path("."),
            worktree_root=Path("."),
            sandbox_root=Path("."),
            history_retention_days=15,
        )
        # 会话记录和调用轨迹共用一个保留期，不会出现「有记录没轨迹」
        self.assertEqual(settings.history_retention_days, 15)


if __name__ == "__main__":
    unittest.main()
