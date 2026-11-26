#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网易云音乐：私信里分享的歌曲/专辑 -> 自动加入指定歌单（v2）

API 服务端请使用仍在维护的 NeteaseCloudMusicApiEnhanced/api-enhanced
（原 Binaryify/NeteaseCloudMusicApi 已于 2024-02 因版权原因归档停更）：
    npx @neteasecloudmusicapienhanced/api@latest        # 最简单
    # 或 git clone https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced
    #    pnpm i && node app.js                          # 默认 http://localhost:3000

以下路由/参数均按 api-enhanced 源码与官方文档核实（module/*.js）：
  /msg/private            limit, offset                      私信列表
  /msg/private/history    uid*, limit, before(上一页末项 time) 私信内容
  /msg/recentcontact      -                                  最近联系人
  /login/qr/key           -                                  扫码 key
  /login/qr/create        key, qrimg                         二维码
  /login/qr/check         key, noCookie(502 时加 true)        扫码状态 800/801/802/803
  /playlist/tracks        op=add|del, pid, tracks=逗号        收藏/删除单曲【必须带 timestamp】
  /album                  id                                 专辑内容(取 songs)
  /playlist/track/all     id, limit, offset                  歌单全部曲目
  /playlist/create        name                               新建歌单
  /user/playlist          uid, limit                         用户歌单
  /user/account           -                                  账号信息
  登录后所有请求带 ?cookie=encodeURIComponent(cookie)（v3.30.0+ 手动传 cookie 方式）

用法：
  python ncm_sync.py login                # 扫码登录，保存 cookie
  python ncm_sync.py discover             # 抓私信原始报文，确认字段结构（首次必做）
  python ncm_sync.py sync [--dry-run]     # 正式同步（可配 Windows 任务计划定时跑）
  python ncm_sync.py status               # 查看登录状态与已处理计数
  python ncm_sync.py history [-n 20]      # 查看同步历史（谁、何时、加了哪些歌）
  python ncm_sync.py reset --yes          # 清空本地记忆

同步策略（两阶段，保证不漏"不是最新一条"的分享）：
  1. /msg/private 拿会话列表 -> 每个会话的对方 uid
  2. /msg/private/history?uid= 拉每个会话的消息流 -> 解析分享卡片

去重记忆（不依赖已读状态）三层：
  1. 目标歌单现有曲目 —— 入库前实时拉取，最权威
  2. state.json 的 added_track_ids / processed_msg_ids —— 跨轮次记忆
  3. sync_history.jsonl —— 追加式审计流水，可回查每次入库来源

注：/msg/private 的返回报文结构官方无公开 schema（老文档仅截图），
    解析器按多种已知形态容错；discover 可 dump 原始报文以便校准。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "api_base": "http://localhost:3000",
    "cookie_file": "cookies.json",
    "state_file": "state.json",
    "history_file": "sync_history.jsonl",
    "playlist_name": "私信收藏",
    "playlist_id": None,          # 填了歌单 id 就不再按名字查找
    "poll_limit": 30,             # /msg/private 每次拉取的会话条数
    "max_conversations_per_sync": 20,   # 每轮最多展开多少个会话的历史
    "history_limit_per_chat": 30,       # 每个会话拉取的历史消息条数
    "request_interval_sec": 2.0,  # 每次 API 调用间隔，防风控
    "album_mode": "all",          # all = 整张专辑入库; first_n = 只收前 N 首
    "max_album_tracks": 20,
    "also_add_shared_playlist": False,  # 私信里分享的歌单是否也批量入库
    "album_tracks_reverse": False,  # 专辑曲目倒序入库（保证整张专辑在歌单内正序排列）
    "private_msg_path": "/msg/private",              # 已核实
    "private_history_path": "/msg/private/history",  # 已核实
    "max_state_ids": 2000,
    "dry_run": False,
}

# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


LOG_SINK = None  # GUI 可注入回调，实时捕获日志（值为 callable(str)）


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG_SINK is not None:
        try:
            LOG_SINK(line)
        except Exception:  # noqa: BLE001
            pass


def record_event(cfg: dict, event: str, **fields: Any) -> None:
    """追加一条审计记录到 jsonl 流水（append-only，可回查每次入库来源）。"""
    path = HERE / cfg.get("history_file", "sync_history.jsonl")
    rec = {"ts": time.strftime("%F %T"), "event": event, **fields}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _as_dict(v: Any) -> dict:
    """msg 字段可能是 JSON 字符串，也可能是对象。"""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{"):
            try:
                d = json.loads(s)
                return d if isinstance(d, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def read_flag(item: dict) -> Any:
    """
    尽力提取"已读/未读"标记。各版本字段名不统一，取不到返回 None。
    仅作参考信息记录，判重不依赖它（打开 App 一眼就变已读，不可靠）。
    """
    for src in (item, _as_dict(item.get("msg")), _as_dict(item.get("lastMsg"))):
        for k in ("readed", "isRead", "read", "hasRead"):
            if k in src:
                v = src[k]
                return bool(v) if isinstance(v, (int, bool)) else v
    return None


class NcmClient:
    """NeteaseCloudMusicApi 服务的最小封装。cookie 以 query 参数传入（文档指定方式）。"""

    def __init__(self, cfg: dict):
        self.base = cfg["api_base"].rstrip("/")
        self.cookie = ""
        self.interval = float(cfg.get("request_interval_sec", 2.0))
        self._last = 0.0

    def load_cookie(self, cookie_file: Path) -> bool:
        data = load_json(cookie_file, {})
        self.cookie = data.get("cookie", "")
        return bool(self.cookie)

    def save_cookie(self, cookie_file: Path) -> None:
        save_json(cookie_file, {"cookie": self.cookie, "saved_at": time.strftime("%F %T")})

    def _throttle(self) -> None:
        wait = self.interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _request(self, path: str, method: str, params: dict, _retry: int = 2) -> dict:
        self._throttle()
        # v4.29.7+ 登录后请求必须带 timestamp，否则请求不合法（文档警告）
        params.setdefault("timestamp", int(time.time() * 1000))
        if self.cookie:
            params["cookie"] = self.cookie
        url = self.base + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 本地 API 服务
                body = resp.read().decode("utf-8", "replace")
            return json.loads(body)
        except (OSError, urllib.error.URLError, ConnectionError) as e:
            # 瞬时断连（如 WinError 10053）自动重试，避免长任务被单点故障打断
            if _retry > 0:
                log(f"  请求失败({e})，{1.5}s 后重试…")
                time.sleep(1.5)
                return self._request(path, method, params, _retry=_retry - 1)
            raise

    def get(self, path: str, **params) -> dict:
        return self._request(path, "GET", params)

    def post(self, path: str, **params) -> dict:
        return self._request(path, "POST", params)


# ----------------------------------------------------------------------------
# 子命令：login（扫码登录；800 过期 / 801 等待 / 802 已扫待确认 / 803 成功）
# ----------------------------------------------------------------------------


def cmd_login(cfg: dict) -> None:
    c = NcmClient(cfg)
    key = c.get("/login/qr/key").get("data", {}).get("unikey")
    if not key:
        print("拿不到二维码 key，确认 API 服务已启动：", c.base)
        sys.exit(1)
    created = c.get("/login/qr/create", key=key, qrimg=1).get("data", {})
    qrimg = created.get("qrimg", "")
    if qrimg.startswith("data:"):
        qrimg = qrimg.split(",", 1)[1]
    qr_png = HERE / "qr.png"
    qr_png.write_bytes(base64.b64decode(qrimg))
    log(f"二维码已保存：{qr_png}")
    if os.name == "nt":
        try:
            os.startfile(qr_png)  # noqa: S606  Windows 下直接弹图
        except Exception:
            pass
    log("请用网易云音乐手机端扫码登录（120 秒内有效）...")
    for _ in range(40):
        time.sleep(3)
        try:
            res = c.get("/login/qr/check", key=key)
        except Exception:  # noqa: BLE001 文档：扫码后偶发 502
            res = c.get("/login/qr/check", key=key, noCookie="true")
        code = res.get("code")
        if code == 800:
            log("二维码过期，请重新运行 login")
            sys.exit(1)
        if code == 802:
            log("已扫码，待确认...")
        if code == 803:
            c.cookie = res.get("cookie", "")
            c.save_cookie(Path(HERE / cfg["cookie_file"]))
            log("登录成功，cookie 已保存。")
            return
    log("等待超时，未完成登录。")
    sys.exit(1)


# ----------------------------------------------------------------------------
# 子命令：discover —— 抓原始报文，确认私信结构（官方无公开 schema，以此校准）
# ----------------------------------------------------------------------------


def collect_dump(cfg: dict, c: NcmClient | None = None) -> dict:
    """抓取私信原始报文（含 2 个会话的历史）供校准解析。只读，不改状态。"""
    client = c or NcmClient(cfg)
    if not client.load_cookie(Path(HERE / cfg["cookie_file"])):
        raise RuntimeError("未登录：请先扫码登录（login）")
    dump: dict[str, Any] = {}
    acc = client.get("/user/account")
    dump["user_account"] = acc
    profile = acc.get("profile") or (acc.get("data") or {}).get("profile") or {}
    log(f"当前账号：{profile.get('nickname')}")

    try:
        data = client.get(cfg["private_msg_path"], limit=5)
        dump[cfg["private_msg_path"]] = data
        log(f"{cfg['private_msg_path']} -> code={data.get('code')} 顶层keys={list(data.keys())}")
        convs = iter_messages(data)
        log(f"  识别到会话/消息条目 {len(convs)} 条")
        for conv in convs[:2]:
            uid = extract_uid(conv)
            if not uid:
                continue
            hist = client.get(cfg["private_history_path"], uid=uid, limit=5)
            dump[f"{cfg['private_history_path']}?uid={uid}"] = hist
            log(f"  history uid={uid} -> code={hist.get('code')} keys={list(hist.keys())}")
    except Exception as e:  # noqa: BLE001
        dump["private_error"] = str(e)
        log(f"私信拉取失败：{e}")

    try:
        dump["/msg/recentcontact"] = client.get("/msg/recentcontact")
    except Exception as e:  # noqa: BLE001
        dump["/msg/recentcontact"] = {"error": str(e)}
    return dump


def cmd_discover(cfg: dict) -> None:
    dump = collect_dump(cfg)
    out = HERE / "discover_dump.json"
    save_json(out, dump)
    log(f"原始报文已写入 {out}")
    log("可把 discover_dump.json 贴给我校准解析；结构相符时无需改动即可 sync。")


# ----------------------------------------------------------------------------
# 私信报文解析 —— 官方无 schema，按多种已知形态容错
# ----------------------------------------------------------------------------


def _artist_of(obj: dict) -> str | None:
    """从资源对象取主艺术家名：artist.name / artists[0].name / ar[0].name。"""
    a = obj.get("artist")
    if isinstance(a, dict) and a.get("name"):
        return str(a["name"])
    for key in ("artists", "ar"):
        arr = obj.get(key)
        if isinstance(arr, list) and arr and isinstance(arr[0], dict) and arr[0].get("name"):
            return str(arr[0]["name"])
    return None


def sender_label(sender: str, shares: list) -> str:
    """
    来源显示名：云音乐小秘书的条目拼接分享卡片里的真实艺术家，
    形如「云音乐小秘书-CHVRCHES」；其余来源原样返回。
    """
    if sender != "云音乐小秘书":
        return sender
    for s in shares:
        if len(s) > 3 and s[3]:
            return f"{sender}-{s[3]}"
    return sender


def extract_shares(item: dict) -> list[tuple[str, int, str | None, str | None]]:
    """
    从一条私信里提取 (kind, id, name, artist) 列表。kind: song / album / playlist / mv。
    name/artist 来自分享卡片自带字段（album.name、album.artist.name ...），零额外请求。
    兼容形态示例：
      {"msg": "{\\"type\\":\\"song\\",\\"id\\":123,\\"name\\":\\"xx\\"}"}
      {"msg": {"song": {"id": 123, "name": "xx"}}}
      {"lastMsg": {"type": "album", "id": 123, "name": "xx"}}   # 会话列表条目
      {"type": "song", "id": 123}                               # 顶层（无名）
    """
    found: list[tuple[str, int, str | None, str | None]] = []

    def add(kind: str, raw: Any, name: Any = None, artist: Any = None) -> None:
        try:
            i = int(raw)
            if i > 0:
                nm = str(name).strip() if name else None
                art = str(artist).strip() if artist else None
                found.append((kind, i, nm or None, art or None))
        except (TypeError, ValueError):
            pass

    def scan(msg: dict) -> None:
        t = str(msg.get("type", "")).lower()
        if t in ("song", "album", "playlist", "mv") and msg.get("id"):
            add(t, msg["id"], msg.get("name"), _artist_of(msg))
        for kind in ("song", "album", "playlist", "mv"):
            sub = msg.get(kind)
            if isinstance(sub, dict) and sub.get("id"):
                add(kind, sub["id"], sub.get("name"), _artist_of(sub))
            elif sub is not None and not isinstance(sub, dict):
                add(kind, sub, None, None)

    # 本条消息体（可能嵌套一层 {"msg": ...}）
    msg = _as_dict(item.get("msg")) or _as_dict(item.get("message"))
    inner = msg.get("msg") if isinstance(msg.get("msg"), (dict, str)) else None
    if inner:
        msg2 = _as_dict(inner)
        if msg2:
            msg = {**msg, **msg2}
    scan(msg)

    # 会话列表条目：最后一条消息挂在 lastMsg
    last = _as_dict(item.get("lastMsg"))
    if last:
        inner2 = last.get("msg") if isinstance(last.get("msg"), (dict, str)) else None
        if inner2:
            last2 = _as_dict(inner2)
            if last2:
                last = {**last, **last2}
        scan(last)

    # 顶层兜底
    t2 = str(item.get("type", "")).lower()
    if t2 in ("song", "album", "playlist", "mv") and item.get("id") and not found:
        add(t2, item["id"])

    merged: dict[tuple[str, int], tuple] = {}
    for kind, i, nm, art in found:
        key = (kind, i)
        if key not in merged:
            merged[key] = (kind, i, nm, art)
        else:
            _, _, n2, a2 = merged[key]
            merged[key] = (kind, i, n2 or nm, a2 or art)  # 后到的补空字段
    return list(merged.values())


def iter_messages(data: dict) -> list[dict]:
    """从接口返回里尽量找出私信/会话条目列表（msgList、msgs、data ...）。"""
    for key in ("msgList", "msgs", "messages", "data", "notices", "contacts"):
        v = data.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
        if isinstance(v, dict):
            for k2 in ("msgList", "msgs", "messages", "list", "contacts"):
                v2 = v.get(k2)
                if isinstance(v2, list) and v2 and isinstance(v2[0], dict):
                    return v2
    return []


def extract_uid(item: dict, own_uid: int | None = None) -> int | None:
    """从会话/消息条目取出对方 uid（优先最近发消息的人，若是自己则取对方）。"""
    for key in ("fromUser", "toUser", "user"):
        u = item.get(key)
        if isinstance(u, dict):
            for uk in ("userId", "fromUserId", "toUserId"):
                if u.get(uk):
                    uid = int(u[uk])
                    if own_uid is not None and uid == own_uid:
                        continue
                    return uid
    for key in ("fromUserId", "userId", "uid"):
        if item.get(key):
            try:
                uid = int(item[key])
                if own_uid is not None and uid == own_uid:
                    continue
                return uid
            except (TypeError, ValueError):
                pass
    return None


def msg_key(item: dict, fallback_uid: int | None = None) -> str:
    """
    消息去重键：优先 id。会话列表条目用 lastMsgId
    （实测与 /msg/private/history 的消息 id 一致，可跨源去重）。
    """
    for k in ("id", "lastMsgId", "msgId", "messageId"):
        if item.get(k):
            return str(item[k])
    t = item.get("msgTime") or item.get("lastMsgTime") or item.get("time")
    return f"{fallback_uid or ''}:{t}" if t else ""


# ----------------------------------------------------------------------------
# 业务：歌单解析 / 专辑展开 / 入库
# ----------------------------------------------------------------------------


def ensure_playlist(c: NcmClient, cfg: dict, uid: int) -> int:
    if cfg.get("playlist_id"):
        return int(cfg["playlist_id"])
    name = cfg["playlist_name"]
    data = c.get("/user/playlist", uid=uid, limit=100)
    for pl in data.get("playlist", []):
        if pl.get("name") == name:
            log(f"找到歌单「{name}」id={pl['id']}")
            return int(pl["id"])
    created = c.get("/playlist/create", name=name)
    pid = created.get("id") or (created.get("playlist") or {}).get("id")
    if not pid:
        raise RuntimeError(f"创建歌单失败：{created}")
    log(f"已创建歌单「{name}」id={pid}")
    return int(pid)


def album_track_ids(c: NcmClient, album_id: int, cfg: dict) -> list[tuple[int, str | None]]:
    """返回 [(曲目id, 曲名)]，曲名来自 /album 响应（零额外请求）。"""
    data = c.get("/album", id=album_id)
    songs = data.get("songs") or (data.get("album") or {}).get("songs") or []
    pairs = [(int(s["id"]), s.get("name")) for s in songs
             if isinstance(s, dict) and s.get("id")]
    if cfg.get("album_mode") == "first_n":
        pairs = pairs[: int(cfg.get("max_album_tracks", 20))]
    return pairs


def playlist_track_ids(c: NcmClient, playlist_id: int, cfg: dict) -> list[tuple[int, str | None]]:
    """返回 [(曲目id, 曲名)]，曲名来自 /playlist/track/all 响应。"""
    data = c.get("/playlist/track/all", id=playlist_id, limit=1000)
    songs = data.get("songs") or data.get("privileges") or []
    return [(int(s["id"]), s.get("name")) for s in songs
            if isinstance(s, dict) and s.get("id")]


def fetch_names(c: NcmClient, track_ids: list[int]) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    """
    批量补查曲名/专辑名/艺术家（/song/detail，100 首/次，上限 15 次请求）。
    返回 (曲名表, 专辑名表, 曲目艺术家表)；用于报文缺名时兜底。
    """
    tnames: dict[int, str] = {}
    anames: dict[int, str] = {}
    tartists: dict[int, str] = {}
    ids = list(dict.fromkeys(int(i) for i in track_ids if i))[:1500]
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        try:
            data = c.get("/song/detail", ids=",".join(map(str, batch)))
        except Exception as e:  # noqa: BLE001
            log(f"  名称补查失败：{e}")
            break
        for s in data.get("songs") or []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            if s.get("name"):
                tnames[int(s["id"])] = str(s["name"])
            al = s.get("al") or {}
            if al.get("id") and al.get("name"):
                anames[int(al["id"])] = str(al["name"])
            art = _artist_of(s)  # s.ar[0].name
            if art:
                tartists[int(s["id"])] = art
    return tnames, anames, tartists


def enrich_plan_names(c: NcmClient, plan: list[dict], only_missing: bool = True) -> int:
    """
    为计划条目补齐曲目名/分享资源名/艺术家（原地修改 plan），并重算来源标签。
    返回补查请求数。only_missing=True 时仅对缺名/缺艺术家项发起 /song/detail。
    兼容旧结构：2/3 元组自动归一化为 [kind, id, name, artist]。
    """
    for it in plan:  # 归一化旧结构 + JSON 反序列化后的字符串键
        it["track_names"] = {int(k): v for k, v in (it.get("track_names") or {}).items()}
        for key in ("shares", "ignored"):
            lst = it.get(key) or []
            for idx, t in enumerate(lst):
                if len(t) < 3:
                    lst[idx] = [t[0], t[1], None, None]
                elif len(t) < 4:
                    lst[idx] = [t[0], t[1], t[2], None]
    missing: list[int] = []
    for it in plan:
        names = it.setdefault("track_names", {})
        for t in it.get("new_track_ids", []):
            if not only_missing or not names.get(t):
                names.setdefault(t, None)
                if not names.get(t):
                    missing.append(int(t))
        # 分享缺艺术家时：歌曲用自己的 id，专辑/歌单用其首个曲目做代表查询
        reps = it.get("track_ids") or it.get("new_track_ids") or []
        for triple in it.get("shares", []) + it.get("ignored", []):
            if triple[3]:
                continue
            if triple[0] == "song":
                missing.append(int(triple[1]))
            elif reps:
                missing.append(int(reps[0]))
    # 说明：专辑名/歌曲名/艺术家优先取报文与 /album 响应；缺时仅按曲目 id 补查
    # （专辑名/艺术家会随曲目的 al.name、ar[0].name 一并返回），资源 id 不进补查列表。
    missing = list(dict.fromkeys(missing))
    if not missing:
        for it in plan:
            it["sender_label"] = sender_label(str(it.get("sender", "")),
                                              it.get("shares", []) + it.get("ignored", []))
        return 0
    calls = (len(missing) + 99) // 100
    log(f"名称补查：{len(missing)} 个 id（约 {calls} 次请求）…")
    tnames, anames, tartists = fetch_names(c, missing)
    for it in plan:
        names = it.setdefault("track_names", {})
        for t in it.get("new_track_ids", []):
            if not names.get(t) and int(t) in tnames:
                names[t] = tnames[int(t)]
        reps = it.get("track_ids") or it.get("new_track_ids") or []
        rep_art = tartists.get(int(reps[0])) if reps else None
        for triple in it.get("shares", []) + it.get("ignored", []):
            if len(triple) >= 3 and not triple[2]:
                nm = tnames.get(int(triple[1])) or anames.get(int(triple[1]))
                if nm:
                    triple[2] = nm
            if not triple[3]:
                triple[3] = tartists.get(int(triple[1])) or rep_art
        it["sender_label"] = sender_label(str(it.get("sender", "")),
                                          it.get("shares", []) + it.get("ignored", []))
    return calls


def resp_code(res: Any) -> Any:
    """
    兼容不同模块的响应包装取 code。
    多数接口：顶层 {code: 200, ...}；
    /playlist/tracks 在 api-enhanced 里被包了一层：{status, body:{code,...}, cookie:[...]}（双层嵌套）。
    """
    if isinstance(res, dict):
        if res.get("code") is not None:
            return res.get("code")
        body = res.get("body")
        if isinstance(body, dict):
            return body.get("code")
    return None


def add_tracks(c: NcmClient, pid: int, track_ids: list[int]) -> dict:
    result: dict[str, Any] = {}
    for i in range(0, len(track_ids), 100):
        batch = track_ids[i : i + 100]
        result = c.post("/playlist/tracks", op="add", pid=pid, tracks=",".join(map(str, batch)))
        code = resp_code(result)
        log(f"  入库 {len(batch)} 首 -> code={code}")
        if code != 200:
            log(f"  入库原始返回：{json.dumps(result, ensure_ascii=False)[:300]}")
            break
    return result


# ----------------------------------------------------------------------------
# 同步核心：collect_plan（只读采集） + commit_plan（入库记账）
# CLI 与 GUI 共用，GUI 可在两段之间插入人工勾选
# ----------------------------------------------------------------------------


def derive_unread_ids(items: list[tuple[dict, int | None]]) -> tuple[set[str], set[str]]:
    """
    推导单条消息的已读/未读（接口不下发显式已读标记）。
    规则：会话未读数 N（newMsgCount）= 该会话按时间倒序最新的 N 条未读。
    返回 (未读消息键集合, 参与推导的消息键集合)；会话无未读数字段时不做推导（不在后者中）。
    """
    budget: dict[int, int] = {}
    timed: dict[int, list[tuple[int, str]]] = {}
    for item, cuid in items:
        if not cuid:
            continue
        n = item.get("newMsgCount")
        if n is not None:
            budget[cuid] = max(budget.get(cuid, 0), int(n))
        t = item.get("time") or item.get("msgTime") or item.get("lastMsgTime")
        mid = msg_key(item, cuid)
        if mid and t:
            timed.setdefault(cuid, []).append((int(t), mid))
    unread: set[str] = set()
    derived: set[str] = set()
    for uid, lst in timed.items():
        if uid not in budget:
            continue
        n = budget[uid]
        seen: set[str] = set()
        count = 0
        for _, mid in sorted(lst, key=lambda x: -x[0]):
            if mid in seen:
                continue
            seen.add(mid)
            derived.add(mid)
            if count < n:
                unread.add(mid)
                count += 1
            else:
                break
    return unread, derived


def collect_plan(cfg: dict, c: NcmClient | None = None) -> dict:
    """
    只读采集同步计划，不做任何入库/状态写入。
    返回：{own_uid, nickname, playlist_id, existing_count, plan:[...], scanned_msg_ids:[...]}
    plan 元素：{msg_id, sender, read_flag, shares, track_ids, new_track_ids, errors}
    """
    client = c or NcmClient(cfg)
    if not client.load_cookie(Path(HERE / cfg["cookie_file"])):
        raise RuntimeError("未登录：请先扫码登录（login）")
    acc = client.get("/user/account")
    profile = acc.get("profile") or (acc.get("data") or {}).get("profile") or {}
    uid = profile.get("userId") or (acc.get("data") or {}).get("userId")
    if not uid:
        raise RuntimeError("cookie 已失效：请重新扫码登录")
    own_uid = int(uid)
    log(f"账号：{profile.get('nickname')} uid={own_uid}")

    state = load_json(HERE / cfg["state_file"], {"processed_msg_ids": [], "added_track_ids": []})
    processed = set(str(x) for x in state.get("processed_msg_ids", []))
    added = set(int(x) for x in state.get("added_track_ids", []))

    pid = ensure_playlist(client, cfg, own_uid)
    existing = {i for i, _ in playlist_track_ids(client, pid, cfg)}
    log(f"目标歌单 id={pid} 现有 {len(existing)} 首")

    # ---- 阶段 1：会话列表 ----
    data = client.get(cfg["private_msg_path"], limit=cfg["poll_limit"])
    convs = iter_messages(data)
    log(f"阶段1 会话/消息条目 {len(convs)} 条")

    # ---- 阶段 2：逐会话拉历史（保证"不是最新一条"的分享也不漏）----
    items: list[tuple[dict, int | None]] = [(cv, extract_uid(cv, own_uid)) for cv in convs]
    scanned = 0
    for conv, cuid in list(items):
        if scanned >= int(cfg.get("max_conversations_per_sync", 20)):
            break
        if not cuid:
            continue
        scanned += 1
        try:
            hist = client.get(cfg["private_history_path"], uid=cuid,
                              limit=cfg.get("history_limit_per_chat", 30))
            for m in iter_messages(hist):
                items.append((m, cuid))
        except Exception as e:  # noqa: BLE001
            log(f"  会话 uid={cuid} 历史拉取失败：{e}")
    log(f"阶段2 累计待解析条目 {len(items)} 条")
    unread_ids, derived_ids = derive_unread_ids(items)  # 推导单条未读：会话未读数 N = 最新 N 条
    if derived_ids:
        log(f"推导未读消息 {len(unread_ids)}/{len(derived_ids)} 条（会话未读数 N=最新 N 条）")

    plan: list[dict] = []
    scanned_ids: list[str] = []
    reserved: set[int] = set()  # 本轮计划内已排入的曲目，防止同轮重复
    # 业务口径：song/album 一定收；playlist 看开关；mv 等其它类型识别但不入库
    allowed = {"song", "album"}
    if cfg.get("also_add_shared_playlist"):
        allowed.add("playlist")
    for item, cuid in items:
        mid = msg_key(item, cuid)
        if mid and mid in processed:
            continue
        if mid:
            scanned_ids.append(mid)
        all_shares = extract_shares(item)
        if not all_shares:
            continue
        addable = [s for s in all_shares if s[0] in allowed]
        ignored = [[k, i, n, a] for k, i, n, a in all_shares if k not in allowed]
        if not addable and not ignored:
            continue
        sender = ((item.get("fromUser") or {}).get("nickname")
                  or (item.get("fromUser") or {}).get("userId") or f"uid:{cuid}")
        rf = read_flag(item)
        unread = item.get("newMsgCount")  # 会话条目才有，即该会话未读数
        to_add: list[int] = []
        track_names: dict[int, str | None] = {}
        errors: list[str] = []
        for kind, rid, sname, sartist in addable:
            if kind == "song":
                to_add.append(rid)
                track_names.setdefault(rid, sname)
            elif kind == "album":
                try:
                    pairs = album_track_ids(client, rid, cfg)
                    if cfg.get("album_tracks_reverse"):
                        pairs = pairs[::-1]  # 倒序入库：歌单新曲靠前插入，倒序收才能正序收听
                    for tid, tname in pairs:
                        to_add.append(tid)
                        track_names.setdefault(tid, tname)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"专辑 {rid} 解析失败：{e}")
                    log(f"  专辑 {rid} 解析失败：{e}")
            elif kind == "playlist":
                try:
                    for tid, tname in playlist_track_ids(client, rid, cfg):
                        to_add.append(tid)
                        track_names.setdefault(tid, tname)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"歌单 {rid} 解析失败：{e}")
                    log(f"  歌单 {rid} 解析失败：{e}")
        uniq: list[int] = []
        for t in to_add:
            if t in existing or t in added or t in reserved or t in uniq:
                continue
            uniq.append(t)
            reserved.add(t)
        plan.append({
            "msg_id": mid or f"anon-{len(plan)}",
            "msg_unread": (mid in unread_ids) if (mid and mid in derived_ids) else None,
            "sender": str(sender),
            "sender_label": sender_label(str(sender), all_shares),
            "read_flag": rf,
            "unread": unread,
            "shares": [[k, i, n, a] for k, i, n, a in addable],
            "ignored": ignored,
            "track_ids": to_add,
            "track_names": track_names,
            "new_track_ids": uniq,
            "errors": errors,
        })
        if addable and to_add and not uniq:
            log(f"  {sender}：全部 {len(to_add)} 首已在歌单/入库记忆中")
        elif uniq:
            log(f"  {sender}：可入库 {len(uniq)} 首 {uniq[:6]}{'...' if len(uniq) > 6 else ''}")
        elif ignored:
            log(f"  {sender}：仅 MV/不支持类型 {ignored}（识别但不入库）")

    # 报文/响应里缺名的曲目批量补查（多数情况下为 0 次请求）
    enrich_plan_names(client, plan)
    # 推导未读/无法判定的排前，推导已读的靠后（稳定排序，同组内保持原顺序）
    plan.sort(key=lambda it: 0 if it.get("msg_unread") is not False else 1)

    return {
        "own_uid": own_uid,
        "nickname": profile.get("nickname"),
        "playlist_id": pid,
        "existing_count": len(existing),
        "plan": plan,
        "scanned_msg_ids": scanned_ids,
    }


def commit_plan(cfg: dict, plan_result: dict, chosen_msg_ids: list[str] | None = None,
                c: NcmClient | None = None) -> dict:
    """
    按计划入库并记账。chosen_msg_ids=None 表示全部执行。
    只有所选条目入库；未选中的分享条目保持"待处理"（下次计划还会出现）。
    """
    client = c or NcmClient(cfg)
    if not client.load_cookie(Path(HERE / cfg["cookie_file"])):
        raise RuntimeError("未登录：请先扫码登录（login）")
    plan = plan_result.get("plan", [])
    chosen = set(chosen_msg_ids) if chosen_msg_ids is not None else {it["msg_id"] for it in plan}
    pid = int(plan_result.get("playlist_id") or cfg.get("playlist_id") or 0)

    state_path = HERE / cfg["state_file"]
    state = load_json(state_path, {"processed_msg_ids": [], "added_track_ids": []})
    processed = set(str(x) for x in state.get("processed_msg_ids", []))
    added = set(int(x) for x in state.get("added_track_ids", []))

    # 无分享的普通消息一律记为已处理；分享条目只记"已执行/已跳过"的
    plan_ids = {it["msg_id"] for it in plan}
    nonshare = [m for m in plan_result.get("scanned_msg_ids", []) if m not in plan_ids]
    processed.update(nonshare)

    added_msgs, added_tracks, skipped, errors = 0, 0, 0, 0
    for it in plan:
        mid = it["msg_id"]
        # 1) 无可入库类型（仅 MV/分享歌单未开启等）：直接记已处理，不打扰用户
        if not it.get("shares"):
            record_event(cfg, "skip", msg_id=mid, sender=it.get("sender_label") or it["sender"], reason="unsupported_type",
                         ignored=it.get("ignored"), read_flag=it.get("read_flag"), msg_unread=it.get("msg_unread"))
            skipped += 1
            processed.add(mid)
            continue
        # 2) 解析失败且无曲目
        if not it["track_ids"] and it["errors"]:
            record_event(cfg, "error", msg_id=mid, sender=it.get("sender_label") or it["sender"], error="; ".join(it["errors"]))
            errors += 1
            processed.add(mid)
            continue
        # 3) 全部曲目已在歌单/记忆中
        if not it["new_track_ids"]:
            if it["track_ids"]:
                record_event(cfg, "skip", msg_id=mid, sender=it.get("sender_label") or it["sender"], reason="already_added",
                             shares=it["shares"], read_flag=it.get("read_flag"), msg_unread=it.get("msg_unread"))
                skipped += 1
                processed.add(mid)
            else:
                record_event(cfg, "skip", msg_id=mid, sender=it.get("sender_label") or it["sender"], reason="no_tracks",
                             shares=it["shares"], read_flag=it.get("read_flag"), msg_unread=it.get("msg_unread"))
                skipped += 1
                processed.add(mid)
            continue
        # 4) 用户未勾选：保持待处理
        if mid not in chosen:
            continue
        try:
            res = add_tracks(client, pid, it["new_track_ids"])
        except Exception as e:  # noqa: BLE001 网络抖动等，不中断整批
            log(f"  入库异常：{e}")
            record_event(cfg, "error", msg_id=mid, sender=it.get("sender_label") or it["sender"], error=str(e))
            errors += 1
            continue
        if resp_code(res) == 200:
            added.update(it["new_track_ids"])
            added_msgs += 1
            added_tracks += len(it["new_track_ids"])
            processed.add(mid)
            log(f"  已入库 {len(it['new_track_ids'])} 首（来自 {it['sender']}）")
            record_event(cfg, "added", msg_id=mid, sender=it.get("sender_label") or it["sender"], count=len(it["new_track_ids"]),
                         track_ids=it["new_track_ids"], shares=it["shares"],
                         read_flag=it.get("read_flag"), msg_unread=it.get("msg_unread"), playlist_id=pid)
        else:
            errors += 1
            log(f"  入库失败：{res}")
            record_event(cfg, "error", msg_id=mid, sender=it.get("sender_label") or it["sender"], error=str(res))

    state["processed_msg_ids"] = list(processed)[-int(cfg.get("max_state_ids", 2000)):]
    state["added_track_ids"] = list(added)[-int(cfg.get("max_state_ids", 2000)):]
    save_json(state_path, state)
    return {"added_msgs": added_msgs, "added_tracks": added_tracks,
            "skipped": skipped, "errors": errors, "pending": len(plan) - added_msgs - skipped - errors}


def cmd_sync(cfg: dict, dry_run: bool) -> None:
    res = collect_plan(cfg)
    if dry_run:
        for it in res["plan"]:
            log(f"  [dry-run] {it['sender']}：将入库 {len(it['new_track_ids'])} 首 {it['new_track_ids'][:10]}")
            if it["new_track_ids"]:
                record_event(cfg, "dry_run", msg_id=it["msg_id"], sender=it.get("sender_label") or it["sender"],
                             count=len(it["new_track_ids"]), track_ids=it["new_track_ids"],
                             shares=it["shares"], read_flag=it.get("read_flag"), msg_unread=it.get("msg_unread"))
        log(f"[dry-run] 完成：{sum(1 for it in res['plan'] if it['new_track_ids'])} 组分享待入库")
        return
    summary = commit_plan(cfg, res)
    log(f"同步完成：入库 {summary['added_msgs']} 组 / {summary['added_tracks']} 首；"
        f"跳过 {summary['skipped']}，失败 {summary['errors']}，待处理 {summary['pending']}")


# ----------------------------------------------------------------------------
# 子命令：status / history / reset
# ----------------------------------------------------------------------------


def cmd_status(cfg: dict) -> None:
    c = NcmClient(cfg)
    ok = c.load_cookie(Path(HERE / cfg["cookie_file"]))
    log(f"cookie 文件：{'存在' if ok else '缺失'}")
    if ok:
        acc = c.get("/user/account")
        profile = acc.get("profile") or (acc.get("data") or {}).get("profile") or {}
        log(f"账号：{profile.get('nickname') or '未知/失效'}")
    state = load_json(HERE / cfg["state_file"], {})
    log(f"已处理消息 {len(state.get('processed_msg_ids', []))} 条，已入库歌曲 {len(state.get('added_track_ids', []))} 首")
    hist = HERE / cfg.get("history_file", "sync_history.jsonl")
    n = len(hist.read_text(encoding="utf-8").splitlines()) if hist.exists() else 0
    log(f"审计流水 {hist.name}：{n} 条记录（history 命令可查看）")


def cmd_history(cfg: dict, limit: int) -> None:
    path = HERE / cfg.get("history_file", "sync_history.jsonl")
    if not path.exists():
        log("暂无历史记录（跑过 sync 后会产生）")
        return
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    shown = lines[-limit:]
    log(f"最近 {len(shown)}/{len(lines)} 条记录：")
    for line in shown:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = rec.get("event", "?")
        base = f"{rec.get('ts', '')}  [{ev:8s}]  {rec.get('sender', '')}"
        if ev in ("added", "dry_run"):
            ids = rec.get("track_ids", [])
            print(f"{base}  {rec.get('count', 0)} 首 {ids[:8]}{'...' if len(ids) > 8 else ''}")
        elif ev == "skip":
            print(f"{base}  {rec.get('reason', '')} shares={rec.get('shares')}")
        elif ev == "error":
            print(f"{base}  {str(rec.get('error', ''))[:80]}")
        else:
            print(f"{base}  {rec}")


def cmd_reset(cfg: dict, yes: bool) -> None:
    if not yes:
        log("这会删除 state.json 与审计流水，且不可恢复。确认请加 --yes")
        return
    for name in (cfg.get("state_file", "state.json"), cfg.get("history_file", "sync_history.jsonl")):
        p = HERE / name
        if p.exists():
            p.unlink()
            log(f"已删除 {name}")
    log("记忆已清空。下次 sync 会重新扫描私信，但已入库曲目仍会被歌单内容查重挡住，不会重复添加。")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="网易云私信分享歌曲/专辑 -> 自动入歌单")
    ap.add_argument("cmd", choices=["login", "discover", "sync", "status", "history", "reset"])
    ap.add_argument("--dry-run", action="store_true", help="只打印将入库的内容，不实际写歌单")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("-n", "--limit", type=int, default=20, help="history 显示条数（默认 20）")
    ap.add_argument("--yes", action="store_true", help="reset 时确认清空记忆")
    args = ap.parse_args()

    cfg = dict(DEFAULT_CONFIG)
    cfg_path = HERE / args.config
    if cfg_path.exists():
        cfg.update(load_json(cfg_path, {}))
    else:
        log(f"未找到 {cfg_path.name}，使用默认配置（建议复制 config.example.json）")

    if args.cmd == "login":
        cmd_login(cfg)
    elif args.cmd == "discover":
        cmd_discover(cfg)
    elif args.cmd == "sync":
        cmd_sync(cfg, dry_run=args.dry_run or cfg.get("dry_run", False))
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "history":
        cmd_history(cfg, args.limit)
    elif args.cmd == "reset":
        cmd_reset(cfg, args.yes)


if __name__ == "__main__":
    main()
