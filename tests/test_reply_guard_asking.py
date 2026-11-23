"""asking_back 判定的测试。

规则：向联系人索要权限、信息，或让他做决定，都不能发出去。
换成固定话术「稍等，正在同步小弈确认中」。

难点是别误伤——技术说明里「需要开通权限」是陈述事实，不是请求。
"""

from __future__ import annotations

import unittest

from dws_dm_agent.reply_guard import CONFIRM_REPLY, looks_like_asking_back, reply_problem


class AskingBackTests(unittest.TestCase):
    def test_asking_for_permission_is_rejected(self) -> None:
        # 「或者由有库权限的人照上面语句跑一次」——把执行推给对方
        text = "这台库对我这边打通，或者由有库权限的人照上面语句跑一次。"
        self.assertTrue(looks_like_asking_back(text))

    def test_asking_for_credentials_is_rejected(self) -> None:
        self.assertTrue(looks_like_asking_back("请提供一下数据库的账号和密码。"))

    def test_forcing_a_choice_is_rejected(self) -> None:
        self.assertTrue(
            looks_like_asking_back("是要改成先进材料，还是保留先进材料产业链？")
        )

    def test_requesting_an_action_is_rejected(self) -> None:
        self.assertTrue(looks_like_asking_back("麻烦你帮忙看一下这个页面的报错。"))

    def test_conditional_on_user_is_rejected(self) -> None:
        self.assertTrue(
            looks_like_asking_back("如果你把接口地址发我，我就可以直接查了。")
        )

    def test_plain_conclusion_passes(self) -> None:
        text = "构建已经通过，镜像推到了 harbor.example.com/team/gateway。"
        self.assertFalse(looks_like_asking_back(text))

    def test_stating_a_permission_requirement_passes(self) -> None:
        # 这是陈述，不是向对方要：没有主语「你/您」。
        # 早先的规则把「需要开通」也拦了，这是误伤。
        text = "这个接口需要开通数据库只读权限才能访问，当前账号没有该权限。"
        self.assertFalse(looks_like_asking_back(text))

    def test_stating_a_failure_passes(self) -> None:
        self.assertFalse(
            looks_like_asking_back("连不上，报错是 Connection REFUSED，端口 3306 超时。")
        )

    def test_the_fixed_reply_itself_passes(self) -> None:
        # 固定话术不能被自己拦下来
        self.assertFalse(looks_like_asking_back(CONFIRM_REPLY))

    def test_stating_completed_work_passes(self) -> None:
        self.assertFalse(
            looks_like_asking_back("SQL 已经执行，chain_name 从先进材料产业链改成了先进材料。")
        )

    def test_reply_problem_reports_asking_back(self) -> None:
        self.assertEqual(reply_problem("请提供一下数据库的账号和密码。"), "asking_back")


if __name__ == "__main__":
    unittest.main()
