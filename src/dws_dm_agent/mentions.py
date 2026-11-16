"""群聊回复里的 @ 提及。

需求很具体：群里有人 **@ 我** 提问题，我回复时把 **他** @ 回去。
普通提问、控制台群、单聊都不 @——硬 @ 只会让每条回复都变成红字提醒。

dws 发 markdown 时的硬性要求：正文里必须出现 `<@openDingTalkId>` 占位符，
同时用 `--at-open-dingtalk-ids` 传同一个 id，@ 才会真正触发通知。只写
「@张三」四个字不会通知任何人，看起来 @ 了其实没 @。
"""

from __future__ import annotations

import re

# 群里常见的 @ 显示名写法：「@北海-韦振」「@北海 看下」。出现在句首、空白后
# 或中文标点后都算提及；不匹配邮箱里的 @（前面紧贴字母数字）。
_BARE_MENTION_RE = re.compile(
    r"(?:^|(?<=[\s。！？；：，、\n])|(?<=[.!?;:,]))@[^\s@，,。.：:；;！!？?、]{1,32}"
)

# 中文标点后面不该有空格（删 @ 时容易留下）。
_SPACE_AFTER_CJK_PUNCT = re.compile(r"([。！？；：，、])\s+")
_DOUBLE_SPACE_RE = re.compile(r"[ \t]{2,}")


def should_mention(event_mentions_self: bool, is_console: bool, is_group: bool) -> bool:
    """是否要在回复里 @ 对方。"""
    if is_console:
        # 控制台群里提问的人就是本人，@ 自己没有意义。
        return False
    return bool(event_mentions_self and is_group)


def mention_prefix(open_dingtalk_id: str, name: str = "") -> str:
    """构造 @ 前缀。id 为空时返回空串——没有 id 就无法真正 @ 到人。

    只返回占位符，后面**不再拼名字**：钉钉会把 `<@id>` 自己渲染成
    「@显示名」，再写一遍名字就会变成「@张三 张三」（实测过）。
    name 参数只为调用方日志保留，不参与拼接。
    """
    id_value = (open_dingtalk_id or "").strip()
    if not id_value:
        return ""
    return f"<@{id_value}> "


def strip_bare_mentions(text: str) -> str:
    """去掉正文里模型手写的 `@某人` 字面量。

    模型常写「@圣明 这个我看下」，但那串文字不会通知任何人。真正需要 @ 的
    人由 mention_prefix 插入；其余字面提及留着只会让人以为通知到了。
    """
    body = text or ""
    if "@" not in body:
        return body
    cleaned = _BARE_MENTION_RE.sub("", body)
    # 删掉 @ 之后可能留下双空格；中文标点后的空格直接去掉，英文句子保留一个。
    cleaned = _SPACE_AFTER_CJK_PUNCT.sub(r"\1", cleaned)
    cleaned = _DOUBLE_SPACE_RE.sub(" ", cleaned)
    lines = [line.lstrip() for line in cleaned.split("\n")]
    result = "\n".join(lines).strip()
    # 整条正文只有一个 @ 时剥完为空，此时保留原文，避免发出空消息。
    return result or body
