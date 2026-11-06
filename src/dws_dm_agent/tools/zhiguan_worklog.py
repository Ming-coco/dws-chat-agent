"""DWS Chat Agent 内置工具：把当天（或指定日期）在本地工作区实际做的事，填成智管工时草稿。

用法（由 Agent 调用，也可手工）：
    python -m dws_dm_agent.tools.zhiguan_worklog fill              # 今天
    python -m dws_dm_agent.tools.zhiguan_worklog fill --date 2026-11-03
    python -m dws_dm_agent.tools.zhiguan_worklog fill --date 2026-11-03 --date 2026-11-04
    python -m dws_dm_agent.tools.zhiguan_worklog fill --dry-run    # 只算不写

设计约束：
  * 只创建草稿（save_worklog_draft），**不提交**（submit 需要 worklog:submit，默认没有授权）。
  * 工时按「有改动的目录」凑到 default_daily_hours，四舍五入到 0.5 小时；不做平均分配。
  * 目录 → 智管填写对象的映射写在 state/zhiguan/config.json 的 mappings 里；
    未确认的目录（confirmed=false）一律记到「日常工作」，不去猜项目。
  * 幂等：同一日期已有草稿或已有正式工时时跳过，避免重复写。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_STATE_DIR = Path(__file__).resolve().parents[3] / "zhiguan_state"
CONFIG_NAME = "config.json"
TOKEN_NAME = "token.json"
LOG_NAME = "fill-log.jsonl"
HOLIDAY_CACHE = "holidays.json"
HOLIDAY_API = "https://timor.tech/api/holiday/year/%d"

# 首次运行如果本地还没有 token，就从这些位置导入（授权流程产出的临时文件）。
TOKEN_IMPORT_CANDIDATES = [
    Path(os.environ.get("TEMP", "")) / "dsh-MgYccB" / "zhiguan_mcp_token.json",
    Path(os.environ.get("TEMP", "")) / "zhiguan_mcp_token.json",
]


# --------------------------------------------------------------------------- utils


def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- MCP


class McpClient:
    def __init__(self, cfg: dict, state_dir: Path):
        self.cfg = cfg
        self.state_dir = state_dir
        self.base = cfg["mcp"]["base"]
        self.resource = cfg["mcp"]["resource"]
        self.client_id = cfg["mcp"]["client_id"]
        self.token_path = state_dir / cfg["mcp"].get("token_path", TOKEN_NAME)
        self.proto = "2025-06-18"
        self.session_id = None
        self.token = None

    # ---- oauth ----
    def load_token(self) -> dict:
        if not self.token_path.exists():
            self.import_token()
        tok = load_json(self.token_path, None)
        if not tok or not tok.get("access_token"):
            raise RuntimeError(
                "没有可用的智管 token（%s）。请先跑一次授权：\n"
                "  python -m dws_dm_agent.tools.zhiguan_auth" % self.token_path
            )
        self.token = tok
        return tok

    def import_token(self) -> bool:
        for cand in TOKEN_IMPORT_CANDIDATES:
            try:
                if cand.exists():
                    data = load_json(cand, None)
                    if data and data.get("access_token"):
                        save_json(self.token_path, data)
                        log("[token] 已从 %s 导入" % cand)
                        return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def refresh(self) -> dict:
        tok = self.token
        form = {
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": self.client_id,
            "resource": self.resource,
        }
        req = urllib.request.Request(
            self.base + "/api/work-hours/mcp/oauth/token",
            data=urllib.parse.urlencode(form).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as r:
            new = json.loads(r.read().decode())
        new["obtained_at"] = int(time.time())
        new.setdefault("client_id", self.client_id)
        new.setdefault("resource", self.resource)
        if "refresh_token" not in new:
            new["refresh_token"] = tok["refresh_token"]
        self.token = new
        save_json(self.token_path, new)
        return new

    def ensure_token(self) -> dict:
        tok = self.load_token()
        expires_in = tok.get("expires_in", 3600)
        if time.time() > tok.get("obtained_at", 0) + expires_in - 120:
            log("[token] 已过期，正在刷新…")
            return self.refresh()
        return tok

    # ---- rpc ----
    def _post(self, payload, session_id=None, proto=None):
        req = urllib.request.Request(
            self.resource, data=json.dumps(payload).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", "Bearer " + self.token["access_token"])
        if session_id:
            req.add_header("Mcp-Session-Id", session_id)
        if proto:
            req.add_header("MCP-Protocol-Version", proto)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")

    @staticmethod
    def _parse(raw, ctype):
        if "text/event-stream" in (ctype or ""):
            msgs = []
            for chunk in raw.split("\n\n"):
                data = [ln[5:].strip() for ln in chunk.splitlines() if ln.startswith("data:")]
                if data:
                    msgs.append("\n".join(data))
            return json.loads(msgs[-1]) if msgs else {}
        return json.loads(raw) if raw.strip() else {}

    def connect(self):
        self.ensure_token()
        status, headers, raw = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": self.proto,
                    "capabilities": {},
                    "clientInfo": {"name": "dws-chat-agent", "version": "1.0"},
                },
            }
        )
        if status == 401:
            self.refresh()
            return self.connect()
        if status != 200:
            raise RuntimeError("initialize 失败 HTTP %s: %s" % (status, raw[:300]))
        ctype = headers.get("Content-Type") or headers.get("content-type")
        self.session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        try:
            proto = (self._parse(raw, ctype).get("result") or {}).get("protocolVersion")
        except Exception:  # noqa: BLE001
            proto = None
        self.proto = proto or self.proto
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self.session_id,
            self.proto,
        )
        return self

    def call(self, name: str, arguments: dict, rid: int = 2) -> dict:
        status, headers, raw = self._post(
            {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            self.session_id,
            self.proto,
        )
        if status != 200:
            raise RuntimeError("tools/call %s 失败 HTTP %s: %s" % (name, status, raw[:400]))
        body = self._parse(raw, headers.get("Content-Type") or headers.get("content-type"))
        if body.get("error"):
            raise RuntimeError("tools/call %s 返回错误: %s" % (name, json.dumps(body["error"], ensure_ascii=False)))
        result = body.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            data = {"raw": text}
        data["_isError"] = bool(result.get("isError"))
        return data


# --------------------------------------------------------------------------- evidence


def run(cmd, cwd, timeout=60):
    try:
        out = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def git_repos(root: Path, max_depth: int = 3):
    """Find .git dirs under root (bounded depth, skip node_modules)."""
    repos = []
    root = Path(root)
    if not root.exists():
        return repos
    if (root / ".git").exists():
        repos.append(root)
    base_depth = len(root.parts)
    for dirpath, dirnames, _ in os.walk(root):
        cur = Path(dirpath)
        depth = len(cur.parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [
            d for d in dirnames
            if d not in (".git", "node_modules", ".venv", "__pycache__", "dist", "build", "target", ".idea")
        ]
        if (cur / ".git").exists() and cur != root:
            repos.append(cur)
    return repos


def git_activity(repo: Path, day: datetime) -> dict:
    since = day.replace(hour=0, minute=0, second=0).isoformat()
    until = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0).isoformat()
    out = run(
        ["git", "log", "--all", "--since=" + since, "--until=" + until,
         "--pretty=format:%ad|%s", "--date=format:%H:%M"],
        repo,
    )
    subjects, times = [], []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        stamp, _, subject = line.partition("|")
        subject = subject.strip()
        times.append(stamp)
        if subject and not subject.startswith("Merged notes"):
            subjects.append(subject)
    status = run(["git", "status", "--porcelain"], repo)
    dirty = len([l for l in status.splitlines() if l.strip()])
    return {"repo": repo.name, "commits": len(times), "subjects": subjects, "first": (times[-1] if times else None),
            "last": (times[0] if times else None), "dirty": dirty, "times": times}


def file_activity(root: Path, day: datetime, patterns, max_depth: int) -> dict:
    """Count files modified that day, and remember a few representative paths."""
    start = day.replace(hour=0, minute=0, second=0)
    end = start + timedelta(days=1)
    ex = [re.compile(p) for p in patterns]
    count = 0
    samples = []
    root = Path(root)
    if not root.exists():
        return {"files": 0, "samples": [], "first": None, "last": None}
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        if len(cur.parts) - base_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [
            d for d in dirnames
            if not any(p.search(os.sep + d + os.sep) for p in ex)
        ]
        for name in filenames:
            fp = cur / name
            if any(p.search(str(fp)) for p in ex):
                continue
            try:
                mtime = datetime.fromtimestamp(fp.stat().st_mtime)
            except OSError:
                continue
            if start <= mtime < end:
                count += 1
                if len(samples) < 6:
                    samples.append((mtime.strftime("%H:%M"), str(fp.relative_to(root))))
    return {"files": count, "samples": samples}


# --------------------------------------------------------------------------- calendar

LEAVE_WORDS = ("请假", "休假", "年假", "病假", "事假", "调休", "婚假", "产假", "陪产假", "丧假", "公休")


def dws_executable(cfg: dict | None = None) -> str:
    """找到 dws 命令行；允许在 config 里用 calendar.dws 覆盖。"""
    override = ((cfg or {}).get("calendar") or {}).get("dws") or os.environ.get("ZHIGUAN_DWS_CMD")
    if override and Path(override).exists():
        return override
    for name in ("dws.cmd", "dws.exe", "dws"):
        found = shutil.which(name)
        if found:
            return found
    cand = Path(os.environ.get("APPDATA", "")) / "npm" / "dws.cmd"
    return str(cand)


def _run_dws(args: list, timeout: int = 90):
    dws = dws_executable()
    cmd = [dws] + args
    if os.name == "nt" and dws.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log("[calendar] dws 调用失败（%s）" % type(exc).__name__)
        return None
    out = (proc.stdout or "").strip()
    start = out.find("{")
    if start < 0:
        return None
    try:
        return json.loads(out[start:])
    except ValueError:
        return None


def calendar_events(day: date, cfg: dict) -> list:
    """读钉钉日历当天日程。读不到就返回空列表，绝不因为日历而中断填工时。"""
    cal = cfg.get("calendar") or {}
    if not cal.get("enabled", True):
        return []
    payload = _run_dws([
        "calendar", "event", "list",
        "--start", "%sT00:00:00+08:00" % day.isoformat(),
        "--end", "%sT23:59:59+08:00" % day.isoformat(),
        "--format", "json",
    ])
    if not payload:
        log("[calendar] %s 没读到日程（dws 不可用或无返回）" % day)
        return []

    raw = []
    for holder in (payload, payload.get("result") or {}, payload.get("data") or {}):
        if isinstance(holder, dict) and isinstance(holder.get("events"), list):
            raw = holder["events"]
            break

    events = []
    for ev in raw:
        title = (ev.get("summary") or ev.get("title") or "").strip()
        if not title:
            continue
        self_attendee = None
        for a in ev.get("attendees") or []:
            if a.get("self"):
                self_attendee = a
                break
        if self_attendee and self_attendee.get("responseStatus") == "declined":
            continue
        st = ((ev.get("start") or {}).get("dateTime") or "").strip()
        en = ((ev.get("end") or {}).get("dateTime") or "").strip()
        hours = 0.0
        if st and en:
            try:
                hours = (datetime.fromisoformat(en) - datetime.fromisoformat(st)).total_seconds() / 3600.0
            except ValueError:
                hours = 0.0
        all_day = bool(ev.get("isAllDay")) or st.endswith("T00:00:00+08:00") and hours >= 20
        rooms = [r.get("roomName") for r in (ev.get("meetingRooms") or []) if r.get("roomName")]
        events.append({
            "title": title,
            "start": st,
            "end": en,
            "hours": round(hours, 2),
            "all_day": all_day,
            "is_leave": any(w in title for w in LEAVE_WORDS),
            "room": "、".join(rooms),
            "attendees": len(ev.get("attendees") or []),
            "organizer": ((ev.get("organizer") or {}).get("displayName") or "").strip(),
        })
    log("[calendar] %s 读到 %d 条日程" % (day, len(events)))
    return events


def match_mapping_by_title(title: str, cfg: dict):
    """按关键词把会议挂到项目上；没有关键词就不猜。"""
    for m in cfg.get("mappings") or []:
        for kw in (m.get("keywords") or [m.get("name", "")]):
            if kw and kw in title:
                return m.get("name")
    return None


def collect_day(cfg: dict, day: datetime) -> dict:
    """Gather per-mapping activity evidence for one calendar day."""
    patterns = cfg.get("exclude_patterns") or []
    depth = int(cfg.get("file_scan_depth", 4))
    evidence = {}
    for m in cfg.get("mappings") or []:
        root = Path(m["workspace"])
        if not root.exists():
            continue
        repos = git_repos(root, max_depth=3)
        git_bits = [git_activity(r, day) for r in repos]
        # 只有“当天有提交”的仓库才算当天工作；长期存在的 dirty 工作树不算
        git_bits = [g for g in git_bits if g["commits"]]
        files = file_activity(root, day, patterns, depth)
        docs = [parse_worklog_doc(p) for p in find_worklog_docs(root, day)]
        docs = [d for d in docs if d]
        # 噪声过滤：只有零星文件改动、既没提交也没手写日志的目录不算“今天在做这个”
        min_files = int(cfg.get("min_files_without_commits", 10))
        if not git_bits and not docs and files["files"] < min_files:
            continue
        if not git_bits and files["files"] == 0 and not docs:
            continue
        evidence[m["name"]] = {
            "mapping": m,
            "git": sorted(git_bits, key=lambda g: -g["commits"]),
            "files": files,
            "docs": docs,
            "commits": sum(g["commits"] for g in git_bits),
        }
    return evidence


# --------------------------------------------------------------------------- compose


SKIP_SUBJECT = re.compile(r"^(merge|Merged notes|Revert|chore: release)", re.IGNORECASE)


def clean_subject(s: str) -> str:
    s = re.sub(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF]+\s*", "", s)
    s = re.sub(r"^(feat|fix|docs|chore|ci|refactor|perf|test|style)\s*\(\s*([^)]*)\s*\)\s*[:：]\s*", r"[\2] ", s)
    s = re.sub(r"^(feat|fix|docs|chore|ci|refactor|perf|test|style)\s*[:：]\s*", "", s)
    return s.strip()


def find_worklog_docs(root: Path, day: datetime, max_depth: int = 3):
    """Locate the hand-written daily log markdown for `day` under one project root."""
    root = Path(root)
    if not root.exists():
        return []
    stamps = [day.strftime("%Y-%m-%d"), day.strftime("%Y%m%d"), day.strftime("%m-%d")]
    base_depth = len(root.parts)
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        if len(cur.parts) - base_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [
            d for d in dirnames
            if d not in (".git", "node_modules", ".venv", "__pycache__", "dist", "build", "target", ".dsh-work", ".dsh-tmp")
        ]
        for name in filenames:
            if not name.endswith(".md"):
                continue
            if any(s in name for s in stamps):
                hits.append(cur / name)
    return hits


def parse_worklog_doc(path: Path, max_len: int = 1200) -> dict:
    """Pull the useful summary lines out of a hand-written daily log."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    lines = [ln.rstrip() for ln in text.splitlines()]

    topic = ""
    project = ""
    headlines = []
    for ln in lines[:40]:
        s = ln.strip()
        if s.startswith("**主题**") or s.startswith("> **主题**"):
            topic = s.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif s.startswith("**项目**"):
            project = s.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif s.startswith(">") and not topic and len(s) > 10:
            topic = s.lstrip("> ").strip()

    mainline = ""
    for ln in lines:
        s = ln.strip()
        if s.startswith("| 主线") or s.startswith("| 当日主线"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 2:
                mainline = cells[1]
            break

    for ln in lines:
        s = ln.strip()
        if s.startswith("## "):
            title = s.lstrip("# ").strip()
            if re.match(r"^[一二三四五六七八九十]+、\s*(当日总览|今日目标|今日工作|今日产出|遗留|当日结果|结论|总结)", title):
                continue
            title = re.sub(r"^[一二三四五六七八九十]+、\s*", "", title)
            if re.search(r"(当日结果|遗留|结论|总结|总览)$", title):
                continue
            if title:
                headlines.append(title)

    # 关键结论：形如 “**问题**：……” 的要点行
    points = []
    skip_labels = {"项目", "主题", "验证", "仓库", "产物", "涉及文件", "改法"}
    for ln in lines:
        s = ln.strip()
        m = re.match(r"^\*\*([^*]{2,12})\*\*[：:]\s*(.+)$", s)
        if not m or m.group(1) in skip_labels:
            continue
        label = m.group(1)
        body = m.group(2).replace("`", "").strip()
        body = re.split(r"[。；;]", body)[0].strip()
        if len(body) < 12:
            continue
        if len(body) > 90:
            body = body[:90].rstrip("，,、 ") + "…"
        points.append("%s：%s" % (label, body))
        if len(points) >= 3:
            break

    return {"path": str(path), "topic": topic or project, "mainline": mainline,
            "headlines": headlines[:6], "points": points,
            "chars": len(text)}


def summarize(mapping_name: str, ev: dict, cfg: dict) -> str:
    limit = int(cfg.get("max_commit_subjects", 6))
    parts = []
    for doc in ev.get("docs") or []:
        topic = (doc.get("topic") or "").strip()
        mainline = (doc.get("mainline") or "").strip()
        heads = [h for h in (doc.get("headlines") or []) if h and not h.startswith("遗留")]
        points = [p.rstrip("，,、；; ") for p in (doc.get("points") or []) if p]
        if topic:
            parts.append("【%s】%s" % (mapping_name, topic.replace("**", "")))
        if mainline:
            parts.append("当日主线：%s。" % re.sub(r"\s+", " ", mainline)[:220])
        if heads:
            parts.append("推进内容：%s。" % "；".join(heads[:5]))
        if points:
            parts.append("要点：%s。" % "；".join(points[:3]))
        if parts:
            text = " ".join(parts).replace("**", "")
            max_len = int(cfg.get("max_content_len", 1800))
            return text[:max_len]

    seen = set()
    subjects = []
    for g in ev["git"]:
        for s in g["subjects"]:
            if SKIP_SUBJECT.match(s):
                continue
            c = clean_subject(s)
            key = c.lower()
            if not c or key in seen:
                continue
            seen.add(key)
            subjects.append(c)
            if len(subjects) >= limit:
                break
        if len(subjects) >= limit:
            break

    repo_names = [g["repo"] for g in ev["git"] if g["commits"]]
    if subjects:
        scope = "、".join(repo_names[:3])
        head = "在 %s 上推进：%s" % (scope, "；".join(subjects[:limit])) if scope else "推进：%s" % "；".join(subjects[:limit])
        parts.append(head + "。")
    elif repo_names:
        parts.append("处理 %s 的代码调整与联调验证。" % "、".join(repo_names[:3]))

    if ev["files"]["files"]:
        shown = "、".join(name for _, name in ev["files"]["samples"][:3])
        parts.append("同时完成 %d 个文件的修改与整理（如 %s）。" % (ev["files"]["files"], shown))

    text = " ".join(parts) if parts else "处理 %s 相关日常事务与沟通。" % mapping_name
    max_len = int(cfg.get("max_content_len", 1800))
    return text[:max_len]


def allocate_hours(evidence: dict, cfg: dict) -> dict:
    """Split the day's hours by commit count, never below min, rounded to step."""
    total = float(cfg.get("default_daily_hours", 7.5))
    step = float(cfg.get("hour_step", 0.5))
    floor = float(cfg.get("min_hours_per_entry", 0.5))

    names = list(evidence)
    if not names:
        return {}

    weights = {}
    for n in names:
        ev = evidence[n]
        weights[n] = max(
            1.0,
            ev["commits"] * 1.0
            + len(ev.get("docs") or []) * 4.0
            + min(ev["files"]["files"], 60) * 0.06,
        )

    wsum = sum(weights.values())
    raw = {n: total * weights[n] / wsum for n in names}
    rounded = {n: max(floor, round(raw[n] / step) * step) for n in names}

    # re-balance so the sum matches the daily total
    diff = round(total - sum(rounded.values()), 2)
    guard = 0
    while abs(diff) >= step / 2 and guard < 200:
        target = max(rounded, key=lambda n: rounded[n]) if diff < 0 else min(rounded, key=lambda n: rounded[n])
        if diff > 0:
            rounded[target] = round(rounded[target] + step, 2)
            diff = round(diff - step, 2)
        else:
            if rounded[target] - step < floor:
                break
            rounded[target] = round(rounded[target] - step, 2)
            diff = round(diff + step, 2)
        guard += 1
    return rounded


# --------------------------------------------------------------------------- fill


def _fetch_holiday_payload(year: int) -> dict:
    """从 timor.tech 取一年的安排，返回 {"YYYY-MM-DD": 是否上班}。失败抛异常。"""
    req = urllib.request.Request(HOLIDAY_API % year, headers={"User-Agent": "dws-chat-agent"})
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read().decode())
    table = {}
    for item in (payload.get("holiday") or {}).values():
        d = item.get("date")
        if d:
            table[d] = not bool(item.get("holiday"))
    return table


def load_holidays(state_dir: Path, year: int) -> dict:
    """节假日表：{"2026-10-01": False, "2026-10-10": True, ...}

    True  = 上班（含调休补班的周末）
    False = 不上班（法定假日）
    来源 timor.tech 的免费接口；按年缓存在 zhiguan_state/holidays.json。
    当年/次年的表超过 45 天会重新拉一次（新的放假安排通常在年底公布）；
    接口挂了就用缓存，缓存也没有就退回「周一到周五」。
    """
    path = Path(state_dir) / HOLIDAY_CACHE
    cache = load_json(path, {}) or {}
    raw = cache.get(str(year))
    if isinstance(raw, dict) and "days" in raw:
        fetched = raw.get("fetched_at") or ""
        fresh = False
        try:
            age = (datetime.now() - datetime.fromisoformat(fetched)).days
            fresh = year < datetime.now().year or age <= 45
        except (TypeError, ValueError):
            fresh = False
        if fresh and raw.get("days"):
            return raw["days"]
        table = raw.get("days") or {}
    elif isinstance(raw, dict):
        table = raw  # 兼容早期扁平格式
    else:
        table = {}

    try:
        fresh_table = _fetch_holiday_payload(year)
        if fresh_table:
            cache[str(year)] = {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "days": fresh_table,
            }
            try:
                save_json(path, cache)
            except OSError:
                pass
            log("[holiday] %d 年日历已更新（%d 条）" % (year, len(fresh_table)))
            return fresh_table
    except Exception as exc:  # noqa: BLE001
        log("[holiday] 取 %d 年节假日失败（%s），用本地缓存" % (year, type(exc).__name__))
    return table


def workday(d: date, state_dir: Path | None = None) -> bool:
    """今天要不要填工时：调休补班算上班，法定假日不算。"""
    if state_dir is not None:
        table = load_holidays(state_dir, d.year)
        key = d.isoformat()
        if key in table:
            return table[key]
    return d.weekday() < 5


def known_draft_ids(state_dir: Path) -> set:
    """Draft ids this tool has created before (so --force only overwrites its own drafts)."""
    ids = set()
    path = Path(state_dir) / LOG_NAME
    if not path.exists():
        return ids
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("draftId"):
                ids.add(rec["draftId"])
    except OSError:
        pass
    return ids


def fill_day(client: McpClient, cfg: dict, day: date, dry_run: bool = False, force: bool = False) -> dict:
    day_dt = datetime(day.year, day.month, day.day)
    ctx = client.call("get_worklog_context", {"workDate": day.isoformat(), "page": 0, "size": 50})
    if ctx.get("_isError"):
        return {"date": day.isoformat(), "status": "context_error", "detail": ctx}

    existing = ctx.get("existingEntries") or []
    drafts = ctx.get("drafts") or []
    my_draft = None
    if force and drafts and not existing:
        ours = known_draft_ids(client.state_dir)
        for d in drafts:
            if d.get("id") in ours:
                my_draft = d
                break
        if my_draft is None:
            log("[force] 该日已有非本工具创建的草稿，不覆盖")
    used = float(ctx.get("existingHours") or 0) + sum(float(d.get("totalHours") or 0) for d in drafts)
    if (existing or drafts) and not my_draft:
        return {
            "date": day.isoformat(),
            "status": "skipped_already_filled",
            "existingEntries": len(existing),
            "drafts": len(drafts),
            "hours": used,
        }

    events = calendar_events(day, cfg)
    leave = [e for e in events if e["is_leave"]]
    skip_on_leave = (cfg.get("calendar") or {}).get("skip_on_leave", True)
    if leave and skip_on_leave and any(e["all_day"] or e["hours"] >= 4 for e in leave):
        return {
            "date": day.isoformat(),
            "status": "skipped_on_leave",
            "detail": "钉钉日历显示当天请假：%s" % "、".join(e["title"] for e in leave),
        }
    meetings = [e for e in events if not e["is_leave"] and not e["all_day"] and e["hours"] > 0]

    evidence = collect_day(cfg, day_dt)
    if not evidence:
        return {"date": day.isoformat(), "status": "skipped_no_activity"}

    # 配置为 SKIP 的目录（内部工具、调研归档）不参与工时分配
    active, skipped = {}, []
    for name, ev in evidence.items():
        if ev["mapping"].get("targetType") == "SKIP":
            skipped.append(name)
            continue
        active[name] = ev
    if skipped:
        log("[skip] %s 配置为不单独记工时，工时并入当天主项目" % "、".join(skipped))
    if not active:
        return {"date": day.isoformat(), "status": "skipped_no_content"}

    hours = allocate_hours(active, cfg)
    eligible = {(t.get("targetType"), t.get("id")) for t in ((ctx.get("eligibleTargets") or {}).get("items") or [])}

    entries = []
    owner = {}
    dropped_hours = 0.0
    for name, ev in active.items():
        m = ev["mapping"]
        ttype, tid = m["targetType"], m["targetId"]
        if (ttype, tid) not in eligible:
            log("[warn] %s 的映射 %s/%s 不在当日可填对象里，跳过（不记日常工作）" % (name, ttype, tid))
            dropped_hours += hours.get(name) or 0
            continue
        content = summarize(name, ev, cfg)
        if not content or len(content) < 8:
            log("[skip] %s 没有足够的工作内容，跳过" % name)
            dropped_hours += hours.get(name) or 0
            continue
        entry = {
            "targetType": ttype,
            "targetId": tid,
            "hours": hours.get(name),
            "content": content,
        }
        if ttype == "PROJECT":
            entry["projectId"] = tid
        owner[name] = entry
        entries.append(entry)

    if not entries:
        return {"date": day.isoformat(), "status": "skipped_no_content"}

    # 被跳过的工时并到当天最重的项目上
    if dropped_hours > 0:
        biggest = max(entries, key=lambda e: e["hours"] or 0)
        biggest["hours"] = round((biggest["hours"] or 0) + dropped_hours, 2)
        log("[hours] 把 %s 小时并入 %s" % (dropped_hours, biggest["targetId"]))

    # 会议：写进某个项目的内容里，不单独占工时条目（工时合计不变）。
    # 标题能按关键词挂到项目的，就写进那个项目；挂不上的，写进当天最重的项目。
    if meetings:
        matched_notes = defaultdict(list)
        loose_notes = []
        for e in meetings:
            owner_name = match_mapping_by_title(e["title"], cfg)
            note = "%s（%s–%s，%s 小时%s）" % (
                e["title"],
                e["start"][11:16],
                e["end"][11:16],
                ("%g" % e["hours"]),
                "，%s" % e["room"] if e.get("room") else "",
            )
            if owner_name and owner_name in owner:
                matched_notes[owner_name].append(note)
            else:
                loose_notes.append(note)

        for name, notes in matched_notes.items():
            target = owner[name]
            target["content"] = ("%s 参会：%s。" % (target["content"], "；".join(notes)))[
                : int(cfg.get("max_content_len", 1800))
            ]
            log("[calendar] %s 写入 %s" % ("；".join(notes), target["targetId"]))

        if loose_notes:
            biggest = max(entries, key=lambda e: e["hours"] or 0)
            biggest["content"] = ("%s 参会：%s。" % (biggest["content"], "；".join(loose_notes)))[
                : int(cfg.get("max_content_len", 1800))
            ]
            log("[calendar] %s 写入 %s" % ("；".join(loose_notes), biggest["targetId"]))

    total = round(sum(e["hours"] or 0 for e in entries), 2)
    result = {
        "date": day.isoformat(),
        "status": "dry_run" if dry_run else "draft_saved",
        "totalHours": total,
        "entries": entries,
    }
    if dry_run:
        return result

    payload = {"requestId": str(uuid.uuid4()), "workDate": day.isoformat(), "entries": entries}
    if my_draft:
        payload["draftId"] = my_draft.get("id")
        payload["expectedVersion"] = my_draft.get("version")
        log("[force] 覆盖已有草稿 %s（version=%s）" % (my_draft.get("id"), my_draft.get("version")))
    saved = client.call("save_worklog_draft", payload, rid=3)
    result.update(
        {
            "draftId": saved.get("draftId") or saved.get("id"),
            "version": saved.get("version"),
            "reviewUrl": saved.get("reviewUrl"),
            "issues": saved.get("issues") or [],
            "status": "draft_saved" if not saved.get("_isError") else "draft_failed",
        }
    )
    if saved.get("_isError"):
        result["detail"] = saved
    return result


def append_log(state_dir: Path, record: dict) -> None:
    path = state_dir / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="把本地工作痕迹填成智管工时草稿")
    ap.add_argument("action", choices=["fill", "context", "check"], nargs="?", default="fill")
    ap.add_argument("--date", action="append", default=None, help="YYYY-MM-DD，可重复；默认今天")
    ap.add_argument("--days-back", type=int, default=0, help="从今天往前补几天（跳过已填/周末）")
    ap.add_argument("--dry-run", action="store_true", help="只计算不写草稿")
    ap.add_argument("--force", action="store_true", help="已有草稿时覆盖重算（有已提交记录时不覆盖）")
    ap.add_argument("--state-dir", default=None)
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else DEFAULT_STATE_DIR
    cfg = load_json(state_dir / CONFIG_NAME, None)
    if cfg is None:
        raise SystemExit("找不到配置：%s" % (state_dir / CONFIG_NAME))

    client = McpClient(cfg, state_dir).connect()

    if args.action == "check":
        tok = client.ensure_token()
        log("token ok, scope=%s, 剩余 %d 分钟" % (
            tok.get("scope"), int(tok.get("obtained_at", 0) + tok.get("expires_in", 0) - time.time()) // 60))
        return 0

    if args.action == "context":
        day = args.date[0] if args.date else date.today().isoformat()
        ctx = client.call("get_worklog_context", {"workDate": day, "page": 0, "size": 50})
        print(json.dumps(ctx, ensure_ascii=False, indent=2)[:6000])
        return 0

    days = []
    if args.date:
        days = [datetime.strptime(d, "%Y-%m-%d").date() for d in args.date]
    else:
        today = date.today()
        for i in range(args.days_back, -1, -1):
            days.append(today - timedelta(days=i))

    results = []
    for day in days:
        if not workday(day, state_dir):
            log("[skip] %s 不是工作日（节假日/周末）" % day)
            continue
        res = fill_day(client, cfg, day, dry_run=args.dry_run, force=args.force)
        results.append(res)
        append_log(state_dir, {"at": datetime.now().isoformat(timespec="seconds"), **res})
        log(json.dumps(res, ensure_ascii=False))

    filled = [r for r in results if r.get("status") in ("draft_saved", "filled", "dry_run")]
    log("\n== 汇总：%d 天处理，%d 天生成草稿/预览" % (len(results), len(filled)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
