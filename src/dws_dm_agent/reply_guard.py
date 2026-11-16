"""模型最终正文的合法性校验。

模型偶尔会把「思考过程」当成最终正文交出来。实测过两种形态：

1. 英文排查独白：`Let me grep PanoramaServiceImpl for lz_ent / 链主.`
   `Do batch: ...`，整篇英文，直接被发到群里；
2. 只写了开场白就停：正文是 `I'll read the issue first.`，
   `stopReason=toolUse` 之后没有后续，半句话被当成回复发出去。

这两种都不是「内容不好」，而是「根本不是发给联系人的话」，必须在投递前
拦住。这里只做判断，不做修复——判无效后由调用方决定重算还是发兜底。
"""

from __future__ import annotations

import re

# 英文过程性独白的开头标记。只匹配句首或行首，避免把正常英文引用误判成独白；
# 中文正文里偶然出现一个英文词不会被误伤。
_ENGLISH_NARRATION_RE = re.compile(
    r"(?:^|(?<=[。！？\n]))\s*(?:"
    r"Let me|Let's|I'll|I will|I found|I see|I think|Now let me|"
    r"Let me check|Let me look|Let me grep|Let me try|"
    r"First,? I|Next,? I|Then,? I|"
    r"Based on my|Looking at the|Checking the|Searching the|"
    r"Do batch|Candidates also|Actually,?"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)

# 结尾像「还没说完」的英文，配合低中文占比时判为半截回复。
_ENGLISH_TAIL_RE = re.compile(
    r"(?:\bthe\b|\band\b|\bto\b|\bfor\b|\bwith\b|\bof\b|\bin\b|\.\.\.)\s*$",
    re.IGNORECASE,
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")

# 把推测当结论的措辞。中文里「应该是」「可能是」太常见，单独出现不足为凭，
# 所以只在「通篇没有一个可核对的证据锚点」时才判为推测。
_HEDGE_RE = re.compile(
    r"(?:应该|可能|大概|估计|似乎|好像|八成|多半|我猜|猜是|想必|说不定)"
)

# 证据锚点：出现任意一个，说明正文至少挂在一个可追查的事实上。
_EVIDENCE_RE = re.compile(
    r"(?:https?://|CQNSHCYDN-\d+|[A-Z][A-Z0-9_]{1,15}-\d{1,7}|"
    r"[A-Za-z_][A-Za-z0-9_]*\.(?:py|ts|js|java|go|json|md|sql|tsx|vue)\b|"
    r"`[^`]+`|第\s*\d+\s*行|process/\d+|\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2})"
)

# 判为无效的最低中文字符占比。面向中文同事的回复里，代码、路径、英文术语
# 都很常见，所以门槛定得低——真正的英文独白占比通常在 5% 以下。
MIN_CJK_RATIO = 0.15

# 短于此长度的中文回复不做英文独白判断：一句「收到」不需要分析。
MIN_CHECK_CHARS = 12


def cjk_ratio(text: str) -> float:
    """中文字符占非空白字符的比例。"""
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return 0.0
    return len(_CJK_RE.findall(stripped)) / len(stripped)


def looks_like_english_narration(text: str) -> bool:
    """判断正文是否是模型的英文过程独白，而不是给联系人看的答复。"""
    body = (text or "").strip()
    if len(body) < MIN_CHECK_CHARS:
        return False
    ratio = cjk_ratio(body)
    marked = bool(_ENGLISH_NARRATION_RE.search(body))
    if ratio >= MIN_CJK_RATIO:
        # 中文占主体：只有「带独白标记且中文明显偏少」才算异常。
        # 正常中文回复里引用一句英文错误信息不会被误伤。
        return marked and ratio < 0.3
    # 中文很少：只要有独白标记，或英文词明显占多数，就判为独白。
    if marked:
        return True
    return len(_LATIN_WORD_RE.findall(body)) >= 8


def looks_truncated(text: str) -> bool:
    """正文是否像被截断（以英文介词/连词结尾，且中文占比很低）。"""
    body = (text or "").strip()
    if len(body) < MIN_CHECK_CHARS:
        return False
    if cjk_ratio(body) >= MIN_CJK_RATIO:
        return False
    return bool(_ENGLISH_TAIL_RE.search(body))


def looks_like_speculation(text: str) -> bool:
    """正文是否在拿推测当结论，且通篇没有可核对的证据锚点。

    只靠「应该/可能」判断会误伤正常措辞（「可能是缓存问题，我查一下」）。
    所以要求两个条件同时成立：出现推测措辞，且正文里没有任何证据锚点
    （链接、单号、文件路径、行号、代码片段、带时间的引用）。有锚点说明
    它至少挂在某个可追查的事实上，交给对方判断即可。
    """
    body = (text or "").strip()
    if len(body) < MIN_CHECK_CHARS:
        return False
    if not _HEDGE_RE.search(body):
        return False
    return not _EVIDENCE_RE.search(body)


def reply_problem(text: str) -> str:
    """返回无效原因；正文可用时返回空串。

    原因字符串会写进审计库，方便按类型统计——不要改成自由文本。
    """
    body = (text or "").strip()
    if not body:
        return "empty"
    if looks_like_english_narration(body):
        return "english_narration"
    if looks_truncated(body):
        return "truncated"
    if looks_like_speculation(body):
        return "speculation"
    return ""
