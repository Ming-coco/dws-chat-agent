"""群聊 @ 回复的测试：只在被人 @ 时 @ 回去，且不误伤正文。"""

from __future__ import annotations

import unittest

from dws_dm_agent.mentions import (
    mention_prefix,
    should_mention,
    strip_bare_mentions,
)


class ShouldMentionTests(unittest.TestCase):
    def test_mentions_back_when_someone_mentioned_me(self) -> None:
        # 这就是需求本身：群里 @ 我提问，回复得 @ 回去。
        self.assertTrue(should_mention(True, is_console=False, is_group=True))

    def test_no_mention_for_plain_group_question(self) -> None:
        # 没 @ 我的普通提问不硬 @，否则每条回复都是红字提醒。
        self.assertFalse(should_mention(False, is_console=False, is_group=True))

    def test_console_group_never_mentions(self) -> None:
        # 控制台群里提问的人就是本人，@ 自己没意义。
        self.assertFalse(should_mention(True, is_console=True, is_group=True))

    def test_direct_message_never_mentions(self) -> None:
        self.assertFalse(should_mention(True, is_console=False, is_group=False))


class MentionPrefixTests(unittest.TestCase):
    def test_prefix_is_placeholder_only(self) -> None:
        # 钉钉会把 <@id> 渲染成「@显示名」，再拼一遍名字就重复了
        # （实测发出去是「@张三  张三 研究生研究明白了吗」）。
        prefix = mention_prefix("open-abc", "北海-韦振")
        self.assertEqual(prefix, "<@open-abc> ")
        self.assertNotIn("北海", prefix)

    def test_prefix_without_label(self) -> None:
        self.assertEqual(mention_prefix("open-abc", ""), "<@open-abc> ")

    def test_empty_id_yields_empty_prefix(self) -> None:
        # 没有 id 就无法真正 @ 到人，宁可不加，也不要加一个假 @。
        self.assertEqual(mention_prefix("", "北海"), "")
        self.assertEqual(mention_prefix("   ", "北海"), "")


class StripBareMentionsTests(unittest.TestCase):
    def test_strips_leading_bare_mention(self) -> None:
        self.assertEqual(strip_bare_mentions("@北海 这个我看了"), "这个我看了")

    def test_strips_mention_after_whitespace(self) -> None:
        self.assertEqual(
            strip_bare_mentions("先说结论。@北海 这个字段是空的"),
            "先说结论。这个字段是空的",
        )

    def test_keeps_text_without_mentions(self) -> None:
        text = "构建失败，原因是字段 ent_name 传了 null。"
        self.assertEqual(strip_bare_mentions(text), text)

    def test_does_not_touch_at_inside_id_placeholder(self) -> None:
        # 真正的占位符不能被剥掉，否则 @ 失效。
        text = "<@open-abc> 北海 这个我看了"
        self.assertIn("<@open-abc>", strip_bare_mentions(text))

    def test_all_mention_only_falls_back_to_original(self) -> None:
        # 整条正文只有一个 @，剥完什么都不剩就保留原文，避免发出空消息。
        self.assertEqual(strip_bare_mentions("@北海"), "@北海")


if __name__ == "__main__":
    unittest.main()
