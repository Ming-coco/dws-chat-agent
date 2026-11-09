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
import hashlib
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


def _dws_command(args: list) -> list:
    """构造 dws 调用命令。

    Windows 上的 dws.cmd 会把带换行的参数截断（邮件正文只留住第一行），
    所以只要能找到 node 和 dws.js，就直接用 node 调用，绕开 cmd.exe。
    """
    dws = dws_executable()
    js = Path(dws).parent / "node_modules" / "dingtalk-workspace-cli" / "bin" / "dws.js"
    if js.is_file():
        node = shutil.which("node") or shutil.which("node.exe")
        if node:
            return [node, str(js)] + args
    cmd = [dws] + args
    if os.name == "nt" and dws.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    return cmd


def _run_dws(args: list, timeout: int = 90):
    cmd = _dws_command(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log("[dws] 调用失败（%s）" % type(exc).__name__)
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


PLATFORM_DIRS = {
    ".git", "node_modules", ".venv", "__pycache__", "dist", "build", "target",
    ".idea", ".vscode", "logs", "log", "state", "tmp", ".dsh-work", ".dsh-tmp",
}


def _squash(text) -> str:
    """归一化目录名/项目名，去掉空白与标点，便于互相包含判断。"""
    return re.sub(r"[\s\-_（）()【】\[\]·．.,，。/\\]+", "", str(text or "")).lower()


def workspace_root(cfg: dict) -> Path:
    """项目目录的父目录；新项目就是这里新出现的一级子目录。"""
    return Path((cfg.get("workspace_root") or cfg.get("workspace") or r"D:\workspace"))


def discover_dirs(cfg: dict) -> list:
    """动态列出 workspace_root 下的一级子目录（跳过隐藏目录与平台目录）。

    目录会不断增加，所以项目清单不能写死在 mappings 里；这里每跑一次就重新枚举。
    """
    if (cfg.get("discovery") or {}).get("enabled") is False:
        return []
    root = workspace_root(cfg)
    if not root.is_dir():
        return []
    ignore = {_squash(x) for x in ((cfg.get("discovery") or {}).get("ignore") or [])}
    out = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith((".", "_", "~$")):
            continue
        if child.name.lower() in PLATFORM_DIRS or _squash(child.name) in ignore:
            continue
        out.append(child)
    return out


def _path_key(path) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).lower()


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度；目录名很短，O(n*m) 足够。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def auto_match(name: str, eligible: list, cfg: dict):
    """把新目录按名字匹配到智管可填对象；匹配不到或并列就返回 None，绝不硬猜。"""
    if not eligible:
        return None
    target = _squash(name)
    if len(target) < 2:
        return None
    need = int((cfg.get("discovery") or {}).get("match_min_chars", 3))
    scored = []
    for item in eligible:
        if item.get("targetType") == "DAILY":
            continue
        score = 0
        for field in ("name", "customerName", "code"):
            token = _squash(item.get(field))
            if len(token) < 2:
                continue
            if target in token:
                # 目录名被项目名整段包含，最强信号
                score = max(score, len(target) + 100)
            elif len(token) >= need and token in target:
                score = max(score, len(token) + 100)
            else:
                shared = _lcs_len(target, token)
                if shared >= need:
                    score = max(score, shared)
        if score:
            scored.append((score, item))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        # 两个项目打分并列，说明这个目录名说明不了归属，交给人来定
        return None
    item = scored[0][1]
    return {
        "name": name,
        "targetType": item.get("targetType"),
        "targetId": item.get("id"),
        "auto": True,
        "note": "按目录名自动匹配：%s" % (item.get("name") or item.get("code") or item.get("id")),
    }


def collect_day(cfg: dict, day: datetime, eligible: list | None = None) -> dict:
    """Gather per-project activity evidence for one calendar day.

    目录来源是动态的：先算 config.mappings 里显式配置的目录（它们的 targetId 是人工确认过的），
    再把 workspace_root 下新出现、且有当天活动的目录一并纳入；新目录优先按已有映射的名字/关键词
    归一，其次按智管可填对象的名字自动匹配，都匹配不上的标记为未归属，交给上层并入主项目并在通知里点名。
    """
    patterns = cfg.get("exclude_patterns") or []
    depth = int(cfg.get("file_scan_depth", 4))
    min_files = int(cfg.get("min_files_without_commits", 10))

    sources = []
    seen = set()
    for m in cfg.get("mappings") or []:
        root = Path(m["workspace"])
        seen.add(_path_key(root))
        sources.append((m.get("name") or root.name, m, root))

    for root in discover_dirs(cfg):
        key = _path_key(root)
        if key in seen:
            continue
        seen.add(key)
        mapping = match_mapping_for_dir(root.name, cfg) or auto_match(root.name, eligible or [], cfg)
        sources.append((root.name, mapping, root))

    evidence = {}
    for name, mapping, root in sources:
        if not root.exists():
            continue
        repos = git_repos(root, max_depth=3)
        git_bits = [git_activity(r, day) for r in repos]
        # 只有“当天有提交”的仓库才算当天工作；长期存在的 dirty 工作树不算
        git_bits = [g for g in git_bits if g["commits"]]
        files = file_activity(root, day, patterns, depth)
        docs = [parse_worklog_doc(
            p,
            max_points=int(cfg.get("max_worklog_points", 3)),
            max_point_len=int(cfg.get("max_worklog_point_len", 90)),
        ) for p in find_worklog_docs(root, day)]
        docs = [d for d in docs if d]
        # 噪声过滤：只有零星文件改动、既没提交也没手写日志的目录不算“今天在做这个”
        if not git_bits and not docs and files["files"] < min_files:
            continue
        if not git_bits and files["files"] == 0 and not docs:
            continue

        key = (mapping or {}).get("name") or name
        bit = {
            "mapping": mapping,
            "git": sorted(git_bits, key=lambda g: -g["commits"]),
            "files": files,
            "docs": docs,
            "commits": sum(g["commits"] for g in git_bits),
            "dirs": [root.name],
        }
        if key in evidence:
            # 同一个项目的多个目录（如「区域产业链通用」与「区域产业链sql更新」）合并
            acc = evidence[key]
            acc["git"] = sorted(acc["git"] + bit["git"], key=lambda g: -g["commits"])
            acc["files"]["files"] += bit["files"]["files"]
            acc["files"]["samples"] = (acc["files"].get("samples") or []) + (bit["files"].get("samples") or [])
            acc["docs"] = acc["docs"] + bit["docs"]
            acc["commits"] += bit["commits"]
            acc["dirs"] = acc["dirs"] + bit["dirs"]
            if acc["mapping"] is None and mapping is not None:
                acc["mapping"] = mapping
        else:
            evidence[key] = bit
    return evidence


def match_mapping_for_dir(dirname: str, cfg: dict):
    """把一个新目录按名字/关键词挂到已有映射上；命中取最长匹配。"""
    target = _squash(dirname)
    if len(target) < 2:
        return None
    best = None
    for m in cfg.get("mappings") or []:
        for token in [m.get("name") or ""] + list(m.get("keywords") or []):
            tok = _squash(token)
            if len(tok) < 2:
                continue
            if tok in target or target in tok:
                if best is None or len(tok) > best[0]:
                    best = (len(tok), m)
    return best[1] if best else None


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


def parse_worklog_doc(path: Path, max_len: int = 1200, max_points: int = 3,
                      max_point_len: int = 90) -> dict:
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
            if re.match(r"^([一二三四五六七八九十]+、\s*)?(工时内容|智管内容|填写内容)$", title):
                continue
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
        if len(body) > max_point_len:
            body = body[:max_point_len].rstrip("，,、 ") + "…"
        points.append("%s：%s" % (label, body))
        if len(points) >= max_points:
            break

    # 手写的「工时内容」段落原样使用，不再走自动摘要
    hand_written = []
    collecting = False
    for ln in lines:
        s = ln.strip()
        if re.match(r"^#{2,4}\s*([一二三四五六七八九十]+、\s*)?(工时内容|智管内容|填写内容)\s*$", s):
            collecting = True
            continue
        if collecting:
            if re.match(r"^#{1,4}\s", s):
                break
            if s:
                hand_written.append(s)
    content = "\n".join(hand_written).strip()

    return {"path": str(path), "topic": topic or project, "mainline": mainline,
            "headlines": headlines[:8], "points": points,
            "content": content, "chars": len(text)}


def trim_content(text: str, cfg: dict) -> str:
    """按时长上限截断，并且一定在句末断开，不留半截句子。"""
    text = re.sub(r"\s+", " ", text).strip()
    limit = int(cfg.get("max_content_len", 1800))
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(mark) for mark in "。；;")
    if cut >= limit // 3:
        return head[: cut + 1]
    return head.rstrip("，,、；;:： ") + "。"


def summarize(mapping_name: str, ev: dict, cfg: dict) -> str:
    limit = int(cfg.get("max_commit_subjects", 6))
    max_heads = int(cfg.get("max_worklog_headlines", 5))
    max_points = int(cfg.get("max_worklog_points", 3))
    parts = []
    for doc in ev.get("docs") or []:
        hand = (doc.get("content") or "").strip()
        if hand:
            text = "【%s】%s" % (mapping_name, hand.replace("**", ""))
            max_len = int(cfg.get("max_content_len", 1800))
            return trim_content(re.sub(r"\s*\n\s*", "", text), cfg)
        topic = (doc.get("topic") or "").strip()
        mainline = (doc.get("mainline") or "").strip()
        heads = [h for h in (doc.get("headlines") or []) if h and not h.startswith("遗留")]
        points = [p.rstrip("，,、；; ") for p in (doc.get("points") or []) if p]
        if topic:
            parts.append("【%s】%s" % (mapping_name, topic.replace("**", "")))
        if mainline:
            parts.append("当日主线：%s。" % re.sub(r"\s+", " ", mainline)[:220])
        if heads:
            parts.append("推进内容：%s。" % "；".join(heads[:max_heads]))
        if points:
            parts.append("要点：%s。" % "；".join(points[:max_points]))
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


def merge_evidence(evidence: dict, names: list, mapping: dict) -> dict:
    """把若干「没匹配上项目」的目录合并成一份痕迹，挂到兜底项目上。"""
    git, docs = [], []
    files = {"files": 0, "samples": []}
    commits = 0
    dirs = []
    for n in names:
        ev = evidence[n]
        git += ev["git"]
        docs += ev.get("docs") or []
        files["files"] += ev["files"]["files"]
        files["samples"] += ev["files"].get("samples") or []
        commits += ev["commits"]
        dirs += ev.get("dirs") or [n]
    return {
        "mapping": mapping,
        "git": sorted(git, key=lambda g: -g["commits"]),
        "files": files,
        "docs": docs,
        "commits": commits,
        "dirs": dirs,
        "fallback": True,
    }


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

    # 当天可以填的项目/产品（DAILY 日常工作排除在外，本项目约定所有工时都挂真实项目）
    eligible_list = [
        t for t in ((ctx.get("eligibleTargets") or {}).get("items") or [])
        if t.get("targetType") != "DAILY"
    ]

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

    evidence = collect_day(cfg, day_dt, eligible_list)
    if not evidence:
        return {"date": day.isoformat(), "status": "skipped_no_activity"}

    # 配置为 SKIP 的目录（内部工具、调研归档）不参与工时分配；
    # 新目录里既没配映射、也没自动匹配上的，同样不占条目，但要报给用户。
    active, skipped, unmapped = {}, [], []
    for name, ev in evidence.items():
        m = ev.get("mapping")
        if not m:
            unmapped.append(name)
            continue
        if m.get("targetType") == "SKIP":
            skipped.append(name)
            continue
        active[name] = ev
    if skipped:
        log("[skip] %s 配置为不单独记工时，工时并入当天主项目" % "、".join(skipped))

    fallback = cfg.get("fallback_target") or {}
    fallback_name = None
    if unmapped and not active and fallback.get("targetId"):
        # 当天只有「没匹配上项目」的目录在动：不空着，整体挂到兜底项目，邮件里点名
        fallback_name = fallback.get("name") or "兜底项目"
        active = {fallback_name: merge_evidence(
            evidence, unmapped,
            {"name": fallback_name, "targetType": fallback.get("targetType") or "PRODUCT",
             "targetId": fallback.get("targetId"), "fallback": True},
        )}
        log("[unmapped] %s 没有对应项目，整体挂到兜底项目 %s" % ("、".join(unmapped), fallback_name))
    elif unmapped:
        log("[unmapped] %s 有工作痕迹但没有对应项目，工时并入当天主项目" % "、".join(unmapped))

    if not active:
        return {
            "date": day.isoformat(),
            "status": "skipped_no_content",
            "skipped": skipped,
            "unmapped": unmapped,
        }

    hours = allocate_hours(active, cfg)
    eligible = {(t.get("targetType"), t.get("id")) for t in eligible_list}

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
        return {
            "date": day.isoformat(),
            "status": "skipped_no_content",
            "skipped": skipped,
            "unmapped": unmapped,
        }

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
        "unmapped": unmapped,
        "fallback": fallback_name,
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


STATUS_TEXT = {
    "draft_saved": "草稿已生成，待你确认",
    "draft_failed": "草稿保存失败",
    "filled": "已填好",
    "dry_run": "试算，未写草稿",
    "context_error": "读不到已有记录，本次跳过",
    "skipped_on_leave": "请假，跳过",
    "skipped_no_content": "没有可填内容，跳过",
    "skipped_no_activity": "当天没有工作痕迹，跳过",
    "skipped_already_filled": "已有记录，跳过",
}


def _as_list(value) -> list:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    return [str(v).strip() for v in items if str(v).strip()]


def _hours_text(value) -> str:
    return "%g" % round(float(value or 0), 2)


def _normalize_body(text: str) -> str:
    """邮件正文按纯文本投递，长横线会被服务端拆成连续的减号，先统一成短横。"""
    for src in ("\u2014", "\u2013", "\u2212"):
        text = text.replace(src, "-")
    return text


def build_notification(results: list, cfg: dict):
    """把一次运行的结果拼成（标题, 正文），没有需要通知的内容时返回 None。"""
    note = cfg.get("notify") or {}
    if not note.get("enabled") or note.get("channel", "mail") != "mail":
        return None

    rows = [r for r in results if r.get("date")]
    if note.get("mode", "always") == "draft_only":
        rows = [r for r in rows if r.get("status") in ("draft_saved", "filled", "draft_failed")]
    if not rows:
        return None

    names = {}
    for item in cfg.get("mappings") or []:
        if item.get("targetId"):
            names[item["targetId"]] = item.get("name") or item["targetId"]
    fallback = cfg.get("fallback_target") or {}
    if fallback.get("targetId"):
        names.setdefault(fallback["targetId"], fallback.get("name") or "兜底项目")

    blocks = []
    drafts = 0
    total = 0.0
    for row in rows:
        status = row.get("status") or ""
        if status in ("draft_saved", "filled"):
            drafts += 1
        total += float(row.get("totalHours") or 0)

        blocks.append("%s  %s" % (row.get("date"), STATUS_TEXT.get(status, status or "未知")))
        for entry in row.get("entries") or []:
            name = names.get(entry.get("targetId")) or entry.get("targetId") or ""
            line = "%s  %s 小时" % (name, _hours_text(entry.get("hours")))
            brief = " ".join(str(entry.get("content") or "").split())
            if brief:
                line += "  %s" % (brief[:80] + "…" if len(brief) > 80 else brief)
            blocks.append(line)
        if row.get("totalHours"):
            blocks.append("合计 %s 小时" % _hours_text(row.get("totalHours")))
        if row.get("unmapped"):
            if row.get("fallback"):
                blocks.append(
                    "以下目录有工作痕迹、但没匹配到项目，工时已整体挂到「%s」，请确认归属：%s"
                    % (row["fallback"], "、".join(row["unmapped"]))
                )
            else:
                blocks.append(
                    "以下目录有工作痕迹、但没匹配到项目，工时已并入上面的主项目，请确认归属：%s"
                    % "、".join(row["unmapped"])
                )
        if row.get("reviewUrl"):
            blocks.append("确认后提交：%s" % row["reviewUrl"])
        if row.get("detail"):
            blocks.append("失败详情：%s" % json.dumps(row["detail"], ensure_ascii=False)[:300])

    dates = [str(row.get("date")) for row in rows]
    label = dates[0] if len(dates) == 1 else "%s 等 %d 天" % (dates[0], len(dates))
    if drafts:
        subject = "【智管工时】%s 草稿 %s 小时 待确认" % (label, _hours_text(total))
    else:
        subject = "【智管工时】%s 未生成草稿" % label

    # 邮件正文按 markdown 渲染，单个换行会被并成空格，所以用空行分段。
    body = "\n\n".join(blocks) + "\n\n" + "（智管工时助手，每天 17:40 自动运行；草稿只有你确认后才会正式提交。）"
    return subject, _normalize_body(body)


def unique_addresses(*groups):
    """收件人去重，保持顺序；同一个地址不会既在 to 又在 cc。"""
    seen, out = set(), []
    for group in groups:
        for addr in group:
            key = str(addr or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def send_mail(cfg: dict, subject: str, body: str):
    """用钉钉邮箱把摘要发给用户自己。"""
    note = cfg.get("notify") or {}
    sender = (note.get("from") or "").strip()
    to = unique_addresses(_as_list(note.get("to")))
    cc = [a for a in unique_addresses(_as_list(note.get("cc"))) if a not in set(to)]
    if not sender or not to:
        log("[notify] 未配置 notify.from / notify.to，跳过发信")
        return None

    args = ["mail", "message", "send", "--from", sender, "--to", ",".join(to)]
    if cc:
        args += ["--cc", ",".join(cc)]
    args += ["--subject", subject, "--content", body, "--format", "json"]
    sent = _run_dws(args)
    if not sent or not sent.get("success"):
        log("[notify] 发信失败：%s" % (json.dumps(sent, ensure_ascii=False)[:300] if sent else "无返回"))
        return None
    return sent


def was_notified(state_dir: Path, fingerprint: str) -> bool:
    """这个指纹的邮件是不是已经发过了。扫全量日志，避免中间夹了别的日期就漏判。"""
    path = Path(state_dir) / LOG_NAME
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") == "notified" and row.get("fingerprint") == fingerprint:
            return True
    return False


def last_notified_fingerprint(state_dir: Path):
    """最近一次真正发出去的邮件指纹。"""
    path = Path(state_dir) / LOG_NAME
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") == "notified":
            return row.get("fingerprint")
    return None


def notify_mail(state_dir: Path, cfg: dict, results: list):
    built = build_notification(results, cfg)
    if built is None:
        log("[notify] 本次没有需要通知的内容")
        return None
    subject, body = built
    # 同一份内容只发一次：手动验证跑一次、定时任务再跑一次，不该收到两封一模一样的邮件
    fingerprint = hashlib.sha1((subject + "\n" + body).encode("utf-8")).hexdigest()
    if was_notified(state_dir, fingerprint):
        log("[notify] 这份内容已经发过，不重复发信")
        return None
    sent = send_mail(cfg, subject, body)
    if not sent:
        return None
    log("[notify] 已发送：%s" % subject)
    append_log(state_dir, {
        "at": datetime.now().isoformat(timespec="seconds"),
        "date": results[-1].get("date") if results else None,
        "status": "notified",
        "subject": subject,
        "to": _as_list((cfg.get("notify") or {}).get("to")),
        "internetMessageId": sent.get("internetMessageId"),
        "fingerprint": fingerprint,
    })
    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(description="把本地工作痕迹填成智管工时草稿")
    ap.add_argument("action", choices=["fill", "context", "check"], nargs="?", default="fill")
    ap.add_argument("--date", action="append", default=None, help="YYYY-MM-DD，可重复；默认今天")
    ap.add_argument("--days-back", type=int, default=0, help="从今天往前补几天（跳过已填/周末）")
    ap.add_argument("--dry-run", action="store_true", help="只计算不写草稿")
    ap.add_argument("--force", action="store_true", help="已有草稿时覆盖重算（有已提交记录时不覆盖）")
    ap.add_argument("--no-notify", action="store_true", help="本次不发邮件通知")
    ap.add_argument("--notify-test", action="store_true", help="只发一封测试邮件后退出")
    ap.add_argument("--state-dir", default=None)
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else DEFAULT_STATE_DIR
    cfg = load_json(state_dir / CONFIG_NAME, None)
    if cfg is None:
        raise SystemExit("找不到配置：%s" % (state_dir / CONFIG_NAME))

    if args.notify_test:
        note = cfg.get("notify") or {}
        built = build_notification(
            [{
                "date": "2026-11-04",
                "status": "draft_saved",
                "totalHours": 7.5,
                "entries": [
                    {"targetId": "249cc0b7-ae48-3a79-832b-d30f7d014e76", "hours": 0.5,
                     "content": "参会：项目转型宣贯（10:00–11:00，1 小时，湘湖）。"},
                    {"targetId": "a114d835-e374-3bec-9ade-d5536cd5638c", "hours": 7.0,
                     "content": "信创改造：接口联调与回归。"},
                ],
                "reviewUrl": "https://zhiguan.example.com/work-hours/drafts/00000000-0000-0000-0000-000000000000",
            }],
            cfg,
        )
        if built is None:
            raise SystemExit("通知未启用或 mode 过滤掉了这条样例，检查 notify 配置")
        subject, body = built
        subject = "[测试] " + subject
        sent = send_mail(cfg, subject, body)
        if not sent:
            raise SystemExit("测试邮件发送失败")
        print(json.dumps({"subject": subject, "to": note.get("to"), "cc": note.get("cc"),
                          "internetMessageId": sent.get("internetMessageId")},
                         ensure_ascii=False, indent=2))
        return 0

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
    if not args.dry_run and not args.no_notify:
        notify_mail(state_dir, cfg, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
