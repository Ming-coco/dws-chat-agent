from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from dws_dm_agent import sandbox


class ProjectNameTests(unittest.TestCase):
    def test_plain_single_segment_names_are_accepted(self) -> None:
        for name in ("proj-a", "项目一", "a.b", "a b", "_x"):
            self.assertEqual(sandbox.normalize_project_name(name), name)

    def test_path_traversal_and_separators_are_rejected(self) -> None:
        for name in ("", "   ", ".", "..", "a/b", "a\\b", "../x", "C:", "a:b"):
            self.assertIsNone(sandbox.normalize_project_name(name), name)

    def test_windows_reserved_and_control_characters_are_rejected(self) -> None:
        for name in ("CON", "con", "PRN", "COM1", "LPT9", "a\x00b", "a\nb"):
            self.assertIsNone(sandbox.normalize_project_name(name), repr(name))

    def test_non_string_input_is_rejected_instead_of_stringified(self) -> None:
        # str(["a"]) 会得到 "['a']"，看着像合法目录名，其实是配置写错了。
        self.assertIsNone(sandbox.normalize_project_name(123))
        self.assertIsNone(sandbox.normalize_project_name(None))
        self.assertIsNone(sandbox.normalize_project_name(["a"]))


class AliasSlugTests(unittest.TestCase):
    def test_unsafe_alias_characters_never_reach_the_directory_name(self) -> None:
        slug = sandbox.slugify_alias("..\\..\\Windows")
        self.assertNotIn("\\", slug)
        self.assertNotIn("/", slug)
        self.assertFalse(slug.startswith("."))
        self.assertEqual(Path(slug).name, slug)

    def test_slug_is_stable_and_distinct_per_alias(self) -> None:
        self.assertEqual(sandbox.slugify_alias("锦洋"), sandbox.slugify_alias("锦洋"))
        self.assertNotEqual(sandbox.slugify_alias("锦洋"), sandbox.slugify_alias("楚芸"))

    def test_empty_and_reserved_aliases_still_produce_a_directory_name(self) -> None:
        for alias in ("", "   ", "CON", "..", "/"):
            slug = sandbox.slugify_alias(alias)
            self.assertTrue(slug)
            self.assertEqual(Path(slug).name, slug)


class RebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="sandbox-test-"))
        self.workspace = self.root / "workspace"
        self.boxes = self.root / "boxes"
        for name in ("alpha", "beta", "gamma"):
            (self.workspace / name).mkdir(parents=True)
            (self.workspace / name / "README.md").write_text(name, encoding="utf-8")

    def tearDown(self) -> None:
        # 只摘链接再删目录树，确保测试本身不会顺着链接删到工作区。
        if self.boxes.is_dir():
            for contact_dir in self.boxes.iterdir():
                if contact_dir.is_dir():
                    for entry in contact_dir.iterdir():
                        if sandbox.is_link(entry):
                            sandbox.remove_link(entry)
        shutil.rmtree(self.root, ignore_errors=True)

    def test_dry_run_reports_absent_links_without_touching_disk(self) -> None:
        report = sandbox.rebuild(
            self.workspace, self.boxes, [("甲", ("alpha",))], apply=False
        )
        self.assertTrue(report.healthy)
        self.assertEqual([link.state for link in report.contacts[0].links], [sandbox.LINK_ABSENT])
        self.assertFalse(self.boxes.exists())

    def test_apply_mounts_only_the_assigned_projects(self) -> None:
        report = sandbox.rebuild(
            self.workspace,
            self.boxes,
            [("甲", ("alpha",)), ("乙", ("beta", "gamma"))],
            apply=True,
        )
        self.assertTrue(report.healthy)
        first = sandbox.contact_sandbox_dir(self.boxes, "甲")
        second = sandbox.contact_sandbox_dir(self.boxes, "乙")
        self.assertEqual(sorted(p.name for p in first.iterdir()), ["alpha"])
        self.assertEqual(sorted(p.name for p in second.iterdir()), ["beta", "gamma"])
        # 每个联系人只能看到自己的项目，看不到别人的。
        self.assertNotIn("beta", [p.name for p in first.iterdir()])

    def test_writing_through_the_link_reaches_the_real_directory(self) -> None:
        sandbox.rebuild(
            self.workspace, self.boxes, [("甲", ("alpha",))], apply=True
        )
        mounted = sandbox.contact_sandbox_dir(self.boxes, "甲") / "alpha" / "new.txt"
        mounted.write_text("hello", encoding="utf-8")
        self.assertEqual(
            (self.workspace / "alpha" / "new.txt").read_text(encoding="utf-8"), "hello"
        )

    def test_second_run_is_idempotent(self) -> None:
        assignments = [("甲", ("alpha", "beta"))]
        sandbox.rebuild(self.workspace, self.boxes, assignments, apply=True)
        report = sandbox.rebuild(self.workspace, self.boxes, assignments, apply=True)
        self.assertTrue(report.healthy)
        self.assertEqual(
            [link.state for link in report.contacts[0].links],
            [sandbox.LINK_OK, sandbox.LINK_OK],
        )

    def test_project_removed_from_assignment_is_unlinked_but_kept_on_disk(self) -> None:
        sandbox.rebuild(
            self.workspace, self.boxes, [("甲", ("alpha", "beta"))], apply=True
        )
        report = sandbox.rebuild(
            self.workspace, self.boxes, [("甲", ("alpha",))], apply=True
        )
        self.assertTrue(report.healthy)
        directory = sandbox.contact_sandbox_dir(self.boxes, "甲")
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ["alpha"])
        # 目标项目目录本体必须原样存在。
        self.assertTrue((self.workspace / "beta" / "README.md").is_file())

    def test_missing_target_is_reported_and_not_replaced_by_anything(self) -> None:
        report = sandbox.rebuild(
            self.workspace, self.boxes, [("甲", ("alpha", "ghost"))], apply=True
        )
        self.assertFalse(report.healthy)
        self.assertTrue(any("ghost" in item for item in report.errors), report.errors)
        directory = sandbox.contact_sandbox_dir(self.boxes, "甲")
        self.assertFalse(os.path.lexists(directory / "ghost"))

    def test_invalid_project_name_is_reported_not_silently_skipped(self) -> None:
        report = sandbox.rebuild(
            self.workspace, self.boxes, [("甲", ("..",))], apply=True
        )
        self.assertFalse(report.healthy)
        self.assertEqual(report.contacts[0].links[0].state, sandbox.LINK_INVALID)

    def test_unexpected_real_directory_in_sandbox_is_preserved(self) -> None:
        directory = sandbox.contact_sandbox_dir(self.boxes, "甲")
        (directory / "mine").mkdir(parents=True)
        (directory / "mine" / "keep.txt").write_text("keep", encoding="utf-8")
        report = sandbox.rebuild(
            self.workspace, self.boxes, [("甲", ("alpha",))], apply=True
        )
        self.assertTrue(report.healthy)
        self.assertTrue((directory / "mine" / "keep.txt").is_file())

    def test_contact_without_projects_gets_an_empty_directory_and_a_note(self) -> None:
        report = sandbox.rebuild(
            self.workspace, self.boxes, [("甲", ())], apply=True
        )
        self.assertTrue(report.healthy)
        self.assertTrue(any("甲" in note for note in report.notes), report.notes)
        directory = sandbox.contact_sandbox_dir(self.boxes, "甲")
        self.assertEqual(list(directory.iterdir()), [])

    def test_primary_workspace_points_at_the_first_project(self) -> None:
        workspace = sandbox.primary_workspace(
            self.boxes, self.workspace, "甲", ("alpha", "beta")
        )
        self.assertEqual(workspace.name, "alpha")
        self.assertEqual(
            workspace,
            sandbox.contact_sandbox_dir(self.boxes, "甲") / "alpha",
        )

    def test_primary_workspace_falls_back_to_workspace_root(self) -> None:
        self.assertEqual(
            sandbox.primary_workspace(self.boxes, self.workspace, "甲", ()),
            self.workspace,
        )
        self.assertEqual(
            sandbox.primary_workspace(self.boxes, self.workspace, "甲", ("..",)),
            self.workspace,
        )


class LinkSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="sandbox-link-"))
        self.target = self.root / "target"
        self.target.mkdir()
        (self.target / "PRECIOUS.txt").write_text("do not delete", encoding="utf-8")
        self.boxes = self.root / "boxes"

    def tearDown(self) -> None:
        if self.boxes.is_dir():
            for entry in self.boxes.iterdir():
                if sandbox.is_link(entry):
                    sandbox.remove_link(entry)
        shutil.rmtree(self.root, ignore_errors=True)

    def test_remove_link_keeps_the_target_contents(self) -> None:
        sandbox.rebuild(self.target.parent, self.boxes, [("甲", ("target",))], apply=True)
        link = sandbox.contact_sandbox_dir(self.boxes, "甲") / "target"
        self.assertTrue(sandbox.is_link(link))
        self.assertTrue(sandbox.remove_link(link))
        self.assertFalse(os.path.lexists(link))
        self.assertTrue((self.target / "PRECIOUS.txt").is_file())

    def test_remove_link_refuses_plain_directories(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.assertFalse(sandbox.remove_link(plain))
        self.assertTrue(plain.is_dir())

    def test_stale_link_is_rebuilt_to_the_correct_target(self) -> None:
        sandbox.rebuild(
            self.root, self.boxes, [("甲", ("target",))], apply=True
        )
        link = sandbox.contact_sandbox_dir(self.boxes, "甲") / "target"
        # 假装项目被改名：链接还在，目标不在了。
        moved = self.root / "renamed"
        sandbox.remove_link(link)
        self.target.rename(moved)
        report = sandbox.rebuild(
            self.root, self.boxes, [("甲", ("target",))], apply=True
        )
        self.assertFalse(report.healthy)
        self.assertEqual(
            report.errors[0].split("：")[0], "甲 / target"
        )
        # 恢复目录后重建应当成功。
        moved.rename(self.target)
        report = sandbox.rebuild(
            self.root, self.boxes, [("甲", ("target",))], apply=True
        )
        self.assertTrue(report.healthy, report.errors)


if __name__ == "__main__":
    unittest.main()
