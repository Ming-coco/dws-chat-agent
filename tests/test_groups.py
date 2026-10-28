from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from dws_dm_agent import sandbox
from dws_dm_agent.config import groups_from_raw, load_settings
from dws_dm_agent.core import Contact, IncomingEvent, parse_dws_event
from dws_dm_agent.dashboard import AgentConfigStore
from dws_dm_agent.dws import DwsClient
from dws_dm_agent.service import (
    AgentService,
    _event_conversation_id,
    _mention_consume_command,
)


TZ = ZoneInfo("Asia/Shanghai")
SELF_OPEN_ID = "open-self"


def group_contact(
    *, conversation_id: str = "cid-group==", allowed: tuple[str, ...] = ()
) -> Contact:
    return Contact(
        alias="深圳租赁群",
        display_name="深圳租赁对接群",
        user_id="",
        open_dingtalk_id="",
        projects=("深圳租赁",),
        conversation_id=conversation_id,
        allowed_senders=allowed,
    )


def at_event_line(
    *,
    conversation_id: str = "cid-group==",
    sender_open_id: str = "open-someone",
    sender_name: str = "某同事",
    content: str = "@行弈 这个接口什么时候上线？",
    message_id: str = "message-1",
) -> str:
    # --flatten 之后事件就是一层扁平对象，字段名与 event schema 一致。
    return json.dumps(
        {
            "event_id": "event-1",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "sender": sender_name,
            "sender_open_dingtalk_id": sender_open_id,
            "content": content,
            "create_time": "2026-10-28 10:00:00",
        },
        ensure_ascii=False,
    )


class GroupIdentityTests(unittest.TestCase):
    def test_group_identity_is_namespaced_away_from_user_ids(self) -> None:
        # 群的 identity 必须以 group: 开头，否则可能撞上某个真实 userId。
        contact = group_contact()
        self.assertTrue(contact.is_group)
        self.assertEqual(contact.identity, "group:cid-group==")

    def test_direct_contact_is_not_a_group(self) -> None:
        contact = Contact("teammate", "Teammate", "u-1", "open-contact")
        self.assertFalse(contact.is_group)
        self.assertEqual(contact.identity, "u-1")

    def test_empty_allowed_senders_accepts_everyone_in_the_group(self) -> None:
        contact = group_contact(allowed=())
        self.assertTrue(contact.accepts_sender("open-anyone", "任意同事"))

    def test_allowed_senders_matches_open_id_or_display_name(self) -> None:
        contact = group_contact(allowed=("open-a", "叶锦洋"))
        self.assertTrue(contact.accepts_sender("open-a", "别人"))
        self.assertTrue(contact.accepts_sender("open-b", "叶锦洋"))
        self.assertFalse(contact.accepts_sender("open-c", "无关的人"))

    def test_direct_contacts_ignore_allowed_senders(self) -> None:
        contact = Contact("teammate", "Teammate", "u-1", "open-contact")
        self.assertTrue(contact.accepts_sender("open-stranger", "陌生人"))


class AtEventParsingTests(unittest.TestCase):
    def test_flattened_at_event_is_parsed_with_sender(self) -> None:
        event = parse_dws_event(
            at_event_line(), group_contact(), TZ, SELF_OPEN_ID
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.conversation_id, "cid-group==")
        self.assertEqual(event.sender_open_dingtalk_id, "open-someone")
        self.assertEqual(event.sender_name, "某同事")
        # 群聊里 contact 是整个群，发言人另存；sender_label 给提示词用。
        self.assertEqual(event.contact.alias, "深圳租赁群")
        self.assertEqual(event.sender_label, "某同事")

    def test_mention_in_another_group_is_ignored(self) -> None:
        event = parse_dws_event(
            at_event_line(conversation_id="cid-other=="),
            group_contact(),
            TZ,
            SELF_OPEN_ID,
        )
        self.assertIsNone(event)

    def test_own_message_never_re_triggers_the_agent(self) -> None:
        event = parse_dws_event(
            at_event_line(sender_open_id=SELF_OPEN_ID),
            group_contact(),
            TZ,
            SELF_OPEN_ID,
        )
        self.assertIsNone(event)

    def test_sender_outside_allowed_list_is_dropped(self) -> None:
        contact = group_contact(allowed=("open-boss",))
        event = parse_dws_event(
            at_event_line(sender_open_id="open-someone"),
            contact,
            TZ,
            SELF_OPEN_ID,
        )
        self.assertIsNone(event)

    def test_direct_message_parsing_still_filters_by_sender(self) -> None:
        contact = Contact("teammate", "Teammate", "u-1", "open-contact")
        line = json.dumps(
            {"data": {"content": "hi", "sender_open_dingtalk_id": "open-contact"}}
        )
        self.assertIsNotNone(parse_dws_event(line, contact, TZ, SELF_OPEN_ID))
        other = json.dumps(
            {"data": {"content": "hi", "sender_open_dingtalk_id": "open-stranger"}}
        )
        self.assertIsNone(parse_dws_event(other, contact, TZ, SELF_OPEN_ID))


class ConversationRoutingTests(unittest.TestCase):
    def test_conversation_id_is_read_from_flat_event(self) -> None:
        self.assertEqual(
            _event_conversation_id(at_event_line()), "cid-group=="
        )

    def test_conversation_id_is_read_from_nested_data(self) -> None:
        line = json.dumps(
            {"data": json.dumps({"openConversationId": "cid-nested=="})}
        )
        self.assertEqual(_event_conversation_id(line), "cid-nested==")

    def test_unparsable_line_yields_no_conversation(self) -> None:
        self.assertEqual(_event_conversation_id("not json"), "")
        self.assertEqual(_event_conversation_id(json.dumps([1, 2])), "")


class MentionCommandTests(unittest.TestCase):
    def test_subscription_has_no_target_and_uses_at_event(self) -> None:
        settings = type("S", (), {"raw": {"dws": {"binary": "dws"}}})()
        command = _mention_consume_command(settings)
        self.assertIn("user_im_message_receive_at", command)
        # at 事件不接受目标参数；多传会直接启动失败。
        self.assertNotIn("--user", command)
        self.assertNotIn("--group", command)
        self.assertIn("--flatten", command)


class GroupConfigTests(unittest.TestCase):
    def test_groups_are_parsed_into_group_contacts(self) -> None:
        groups = groups_from_raw(
            [
                {
                    "alias": "深圳租赁群",
                    "title": "深圳租赁对接群",
                    "open_conversation_id": "cid-a==",
                    "projects": ["深圳租赁", "深圳租赁"],
                    "allowed_senders": ["open-a", "open-a"],
                }
            ]
        )
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertTrue(group.is_group)
        self.assertEqual(group.conversation_id, "cid-a==")
        self.assertEqual(group.display_name, "深圳租赁对接群")
        self.assertEqual(group.projects, ("深圳租赁",))
        self.assertEqual(group.allowed_senders, ("open-a",))

    def test_group_without_conversation_id_is_dropped(self) -> None:
        self.assertEqual(
            groups_from_raw([{"alias": "空群", "open_conversation_id": ""}]), ()
        )

    def test_invalid_project_name_is_not_smuggled_into_the_path(self) -> None:
        groups = groups_from_raw(
            [
                {
                    "alias": "群",
                    "open_conversation_id": "cid==",
                    "projects": ["..", "a/b", 123, "ok"],
                }
            ]
        )
        self.assertEqual(groups[0].projects, ("ok",))


class GroupSettingsTests(unittest.TestCase):
    def _write_config(self, directory: Path, groups: list) -> Path:
        workspace = directory / "workspace"
        (workspace / "深圳租赁").mkdir(parents=True)
        config = directory / "config.json"
        config.write_text(
            json.dumps(
                {
                    "self": {"name": "本人", "open_dingtalk_id": SELF_OPEN_ID},
                    "contacts": [
                        {
                            "alias": "锦洋",
                            "display_name": "叶锦洋",
                            "user_id": "u-1",
                            "open_dingtalk_id": "open-1",
                        }
                    ],
                    "groups": groups,
                    "workspace_root": str(workspace),
                    "worktree_root": str(directory / "worktrees"),
                    "sandbox_root": str(directory / "sandboxes"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return config

    def test_targets_merges_contacts_and_groups(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self._write_config(
                directory,
                [
                    {
                        "alias": "深圳租赁群",
                        "open_conversation_id": "cid-a==",
                        "projects": ["深圳租赁"],
                    }
                ],
            )
            settings = load_settings(config, directory / "missing.env")
            self.assertEqual(len(settings.contacts), 1)
            self.assertEqual(len(settings.groups), 1)
            self.assertEqual(len(settings.targets), 2)

    def test_group_lookup_by_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self._write_config(
                directory,
                [
                    {
                        "alias": "深圳租赁群",
                        "open_conversation_id": "cid-a==",
                        "projects": ["深圳租赁"],
                    }
                ],
            )
            settings = load_settings(config, directory / "missing.env")
            found = settings.group_for_conversation("cid-a==")
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.alias, "深圳租赁群")
            self.assertIsNone(settings.group_for_conversation("cid-unknown=="))
            # contact_for 要能同时解析群身份，否则投递时找不到目标。
            self.assertIsNotNone(settings.contact_for("group:cid-a=="))

    def test_group_workspace_lives_inside_the_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self._write_config(
                directory,
                [
                    {
                        "alias": "深圳租赁群",
                        "open_conversation_id": "cid-a==",
                        "projects": ["深圳租赁"],
                    }
                ],
            )
            settings = load_settings(config, directory / "missing.env")
            group = settings.groups[0]
            workspace = settings.workspace_for(group)
            self.assertTrue(workspace.is_relative_to(settings.sandbox_root))
            self.assertEqual(workspace.name, "深圳租赁")


class GroupSendTests(unittest.TestCase):
    def _client(self) -> DwsClient:
        client = object.__new__(DwsClient)
        client.binary = "dws"
        client.profile = None
        client.ai_tag = True
        client.streaming_cards = False
        return client

    def test_group_send_targets_conversation_id(self) -> None:
        client = self._client()
        captured: list[list[str]] = []

        async def fake_command(arguments, **kwargs):
            captured.append(list(arguments))
            return {"result": {"openMessageId": "message-9"}}

        client._json_command = fake_command  # type: ignore[assignment]
        message_id, _ = asyncio.run(
            client.send(group_contact(), "在，马上发你", "uuid-1")
        )
        self.assertEqual(message_id, "message-9")
        self.assertIn("--conversation-id", captured[0])
        self.assertIn("cid-group==", captured[0])
        self.assertNotIn("--user", captured[0])
        self.assertNotIn("--open-dingtalk-id", captured[0])

    def test_group_reply_can_mention_the_asker(self) -> None:
        client = self._client()
        captured: list[list[str]] = []

        async def fake_command(arguments, **kwargs):
            captured.append(list(arguments))
            return {}

        client._json_command = fake_command  # type: ignore[assignment]
        asyncio.run(
            client.send(
                group_contact(),
                "在",
                "uuid-2",
                at_open_dingtalk_ids=["open-someone"],
            )
        )
        self.assertIn("--at-open-dingtalk-ids", captured[0])
        self.assertIn("open-someone", captured[0])

    def test_direct_send_still_uses_user_id(self) -> None:
        client = self._client()
        captured: list[list[str]] = []

        async def fake_command(arguments, **kwargs):
            captured.append(list(arguments))
            return {}

        client._json_command = fake_command  # type: ignore[assignment]
        contact = Contact("teammate", "Teammate", "u-1", "open-contact")
        asyncio.run(client.send(contact, "hi", "uuid-3"))
        self.assertIn("--user", captured[0])
        self.assertNotIn("--conversation-id", captured[0])

    def test_group_history_uses_conversation_id(self) -> None:
        client = self._client()
        client.settings = type("S", (), {"timezone": TZ, "raw": {"history_limit": 80}})()
        captured: list[list[str]] = []

        async def fake_command(arguments, **kwargs):
            captured.append(list(arguments))
            return {"result": {"messages": []}}

        client._retry_network_json_command = fake_command  # type: ignore[assignment]
        asyncio.run(client.history(group_contact()))
        self.assertIn("--conversation-id", captured[0])
        self.assertIn("cid-group==", captured[0])


class GroupDashboardMergeTests(unittest.TestCase):
    def _store(self, directory: Path) -> AgentConfigStore:
        config = directory / "config.json"
        config.write_text(
            json.dumps(
                {
                    "self": {"name": "本人", "open_dingtalk_id": SELF_OPEN_ID},
                    "contacts": [],
                    "workspace_root": str(directory),
                    "worktree_root": str(directory),
                    "agents": {},
                    "workflows": {"active": "", "presets": {}},
                }
            ),
            encoding="utf-8",
        )
        return AgentConfigStore(config, directory, contacts=[], groups=[])

    def test_merge_groups_normalizes_and_dedupes(self) -> None:
        merged = AgentConfigStore._merge_groups(
            [
                {
                    "alias": "群一",
                    "open_conversation_id": "cid-a==",
                    "projects": ["p1", "p1"],
                    "allowed_senders": ["open-a", " open-a "],
                }
            ],
            allow_empty=True,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["projects"], ["p1"])
        self.assertEqual(merged[0]["allowed_senders"], ["open-a"])
        # 没给显示名时回落到别名，避免面板上出现空白标题。
        self.assertEqual(merged[0]["display_name"], "群一")

    def test_duplicate_group_conversation_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AgentConfigStore._merge_groups(
                [
                    {"alias": "a", "open_conversation_id": "cid-x=="},
                    {"alias": "b", "open_conversation_id": "cid-x=="},
                ],
                allow_empty=True,
            )

    def test_group_without_conversation_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AgentConfigStore._merge_groups(
                [{"alias": "a", "open_conversation_id": ""}], allow_empty=True
            )

    def test_missing_project_directory_is_rejected_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            store = self._store(directory)
            with self.assertRaises(ValueError):
                store._validate_candidate(
                    {
                        "workflows": {"presets": {}},
                        "groups": [
                            {
                                "alias": "群",
                                "open_conversation_id": "cid-a==",
                                "projects": ["不存在的项目"],
                            }
                        ],
                    }
                )


class GroupSandboxTests(unittest.TestCase):
    def test_group_gets_its_own_project_view(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            workspace = directory / "workspace"
            (workspace / "深圳租赁").mkdir(parents=True)
            (workspace / "别的项目").mkdir()
            sandboxes = directory / "sandboxes"
            report = sandbox.rebuild(
                workspace,
                sandboxes,
                [("深圳租赁群", ("深圳租赁",))],
                apply=True,
            )
            self.assertTrue(report.healthy, report.as_dict())
            group_dir = sandboxes / sandbox.slugify_alias("深圳租赁群")
            visible = sorted(entry.name for entry in group_dir.iterdir())
            self.assertEqual(visible, ["深圳租赁"])


class GroupOnlyConfigurationTests(unittest.TestCase):
    # 只登记群、不登记单聊是合法配置；两类都空才该报错。

    def _settings(self, directory: Path, contacts: list, groups: list):
        workspace = directory / "workspace"
        workspace.mkdir(exist_ok=True)
        config = directory / "config.json"
        config.write_text(
            json.dumps(
                {
                    "self": {"name": "本人", "open_dingtalk_id": SELF_OPEN_ID},
                    "contacts": contacts,
                    "groups": groups,
                    "workspace_root": str(workspace),
                    "worktree_root": str(directory / "worktrees"),
                    "sandbox_root": str(directory / "sandboxes"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return load_settings(config, directory / "missing.env")

    def test_groups_alone_are_enough(self) -> None:
        from dws_dm_agent.service import _validate_configuration

        with tempfile.TemporaryDirectory() as raw:
            settings = self._settings(
                Path(raw),
                [],
                [{"alias": "群", "open_conversation_id": "cid-a=="}],
            )
            self.assertEqual(settings.contacts, ())
            errors = _validate_configuration(settings)
            self.assertFalse(
                [e for e in errors if "contacts" in e], errors
            )

    def test_both_empty_is_rejected(self) -> None:
        from dws_dm_agent.service import _validate_configuration

        with tempfile.TemporaryDirectory() as raw:
            settings = self._settings(Path(raw), [], [])
            self.assertTrue(_validate_configuration(settings))


class MentionRoutingTests(unittest.TestCase):
    """_parse_mention 是群聊的唯一入口，登记之外的会话必须被丢掉。"""

    def _service(self, groups):
        service = object.__new__(AgentService)
        service.settings = type(
            "S",
            (),
            {
                "groups": tuple(groups),
                "contacts": (),
                "targets": tuple(groups),
                "timezone": TZ,
                "self_open_id": SELF_OPEN_ID,
            },
        )()
        service._ignored_conversations = set()
        return service

    def test_registered_group_event_is_accepted(self) -> None:
        service = self._service([group_contact(conversation_id="cid-a==")])
        event = service._parse_mention(at_event_line(conversation_id="cid-a==").encode())
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.sender_open_dingtalk_id, "open-someone")

    def test_unregistered_conversation_is_dropped_and_noted_once(self) -> None:
        service = self._service([group_contact(conversation_id="cid-a==")])
        line = at_event_line(conversation_id="cid-stranger==").encode()
        self.assertIsNone(service._parse_mention(line))
        self.assertIsNone(service._parse_mention(line))
        # 只记一次，否则 145 人的群随便谁 @所有人 就能刷爆日志。
        self.assertEqual(service._ignored_conversations, {"cid-stranger=="})

    def test_own_mention_is_never_routed_back(self) -> None:
        service = self._service([group_contact(conversation_id="cid-a==")])
        line = at_event_line(conversation_id="cid-a==", sender_open_id=SELF_OPEN_ID)
        self.assertIsNone(service._parse_mention(line.encode()))

    def test_garbage_line_is_ignored(self) -> None:
        service = self._service([group_contact(conversation_id="cid-a==")])
        self.assertIsNone(service._parse_mention(b"not json"))


if __name__ == "__main__":
    unittest.main()
