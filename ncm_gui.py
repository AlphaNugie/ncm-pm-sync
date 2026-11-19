#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ncm_gui.py —— 网易云私信→歌单 网页管理界面（纯标准库，零第三方依赖）

不是命令行套壳：后端直接 import ncm_sync 调用业务函数
（collect_plan / commit_plan / collect_dump ...），前端完成
「扫码登录 / 配置 / 计划预览勾选 / 入库 / 审计 / 报文探测」全流程。

用法：
    python ncm_gui.py            # 启动后自动打开 http://localhost:8765
    python ncm_gui.py --no-open  # 不自动开浏览器
    python ncm_gui.py --port 8877

安全：仅绑定 127.0.0.1，只供本机使用。
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import ncm_sync as core

HERE = Path(__file__).resolve().parent
LOG_BUF: deque[str] = deque(maxlen=3000)
LOGIN_SESSIONS: dict[str, float] = {}   # qr key -> 创建时间
LAST_PLAN: dict[str, Any] | None = None
LAST_PLAN_AT: float = 0.0
PLAN_CACHE_SEC = 600                     # 计划缓存 10 分钟，避免重复全量扫描
BUSY = threading.Lock()
CFG_LOCK = threading.Lock()

core.LOG_SINK = LOG_BUF.append

DOWNLOADABLE = {"state.json", "sync_history.jsonl", "discover_dump.json", "config.json"}


# ----------------------------------------------------------------------------
# 配置读写
# ----------------------------------------------------------------------------


def load_cfg() -> dict:
    cfg = dict(core.DEFAULT_CONFIG)
    cfg.update(core.load_json(HERE / "config.json", {}))
    return cfg


def save_cfg(update: dict) -> dict:
    with CFG_LOCK:
        cur = core.load_json(HERE / "config.json", {})
        cur.update(update)
        core.save_json(HERE / "config.json", cur)
    return load_cfg()


# ----------------------------------------------------------------------------
# 状态聚合
# ----------------------------------------------------------------------------


def service_ping(cfg: dict) -> tuple[bool, str]:
    """探测 API 服务连通性（未登录返回 301 也算服务正常）。"""
    try:
        c = core.NcmClient(cfg)
        c.load_cookie(HERE / cfg["cookie_file"])
        r = c.get("/user/account")
        return True, f"服务正常（code={r.get('code')}）"
    except Exception as e:  # noqa: BLE001
        return False, f"无法连接 {cfg['api_base']}：{e}"


def account_info(cfg: dict) -> tuple[bool, str | None]:
    """返回 (登录是否有效, 昵称)。"""
    try:
        c = core.NcmClient(cfg)
        if not c.load_cookie(HERE / cfg["cookie_file"]):
            return False, None
        acc = c.get("/user/account")
        profile = acc.get("profile") or (acc.get("data") or {}).get("profile") or {}
        if profile.get("userId"):
            return True, profile.get("nickname")
        return False, None
    except Exception:  # noqa: BLE001
        return False, None


def build_status() -> dict:
    cfg = load_cfg()
    ok, detail = service_ping(cfg)
    logged_in, nick = account_info(cfg) if ok else (False, None)
    state = core.load_json(HERE / cfg["state_file"], {})
    hist = HERE / cfg.get("history_file", "sync_history.jsonl")
    audit = len(hist.read_text(encoding="utf-8").splitlines()) if hist.exists() else 0
    return {
        "service_ok": ok, "service_detail": detail,
        "logged_in": logged_in, "nickname": nick,
        "api_base": cfg["api_base"],
        "playlist_name": cfg.get("playlist_name"), "playlist_id": cfg.get("playlist_id"),
        "processed": len(state.get("processed_msg_ids", [])),
        "added": len(state.get("added_track_ids", [])),
        "audit": audit,
    }


# ----------------------------------------------------------------------------
# HTTP 处理
# ----------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "NcmGui/2.0"

    # ---- 基础输出（客户端断开不视为业务失败）----
    def _send(self, fn) -> None:
        try:
            fn()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass  # 客户端已断开（如请求超时），结果已生成/缓存，无需报错

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")

        def _do() -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        self._send(_do)

    def _ok(self, **kw) -> None:
        self._json({"ok": True, **kw})

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _html(self, text: str) -> None:
        body = text.encode("utf-8")

        def _do() -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        self._send(_do)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        pass

    # ---- 路由 ----
    def do_GET(self) -> None:
        try:
            self._route("GET")
        except Exception:  # noqa: BLE001
            self._err("服务器内部错误：" + traceback.format_exc(limit=2), 500)

    def do_POST(self) -> None:
        try:
            self._route("POST")
        except Exception:  # noqa: BLE001
            self._err("服务器内部错误：" + traceback.format_exc(limit=2), 500)

    def _route(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = dict(urllib.parse.parse_qsl(parsed.query))

        if method == "GET" and path in ("/", "/index.html"):
            return self._html(PAGE)

        if path == "/api/status" and method == "GET":
            return self._ok(status=build_status())

        if path == "/api/config":
            if method == "GET":
                return self._ok(config=load_cfg())
            cfg = save_cfg(self._body())
            core.log("配置已保存到 config.json")
            return self._ok(config=cfg)

        if path == "/api/test-connection" and method == "GET":
            ok, detail = service_ping(load_cfg())
            return self._ok(service_ok=ok, detail=detail)

        if path == "/api/login/start" and method == "POST":
            return self._login_start()

        if path == "/api/login/status" and method == "GET":
            return self._login_status(qs.get("key", ""))

        if path == "/api/playlists" and method == "GET":
            return self._playlists()

        if path == "/api/playlist-state" and method == "GET":
            return self._playlist_state()

        if path == "/api/plan" and method == "POST":
            return self._plan()

        if path == "/api/commit" and method == "POST":
            return self._commit(self._body())

        if path == "/api/discover" and method == "POST":
            return self._discover()

        if path == "/api/history" and method == "GET":
            return self._history(int(qs.get("limit", 50)))

        if path == "/api/reset" and method == "POST":
            if not self._body().get("yes"):
                return self._err("需要确认（yes=true）")
            cfg = load_cfg()
            core.cmd_reset(cfg, True)
            return self._ok()

        if path == "/api/logs" and method == "GET":
            since = int(qs.get("since", 0))
            lines = list(LOG_BUF)[since:]
            return self._ok(lines=lines, next=since + len(lines))

        if path.startswith("/api/file/") and method == "GET":
            return self._file(path.rsplit("/", 1)[-1])

        if path == "/api/open-folder" and method == "POST":
            try:
                os.startfile(HERE)  # noqa: S606
                return self._ok()
            except Exception as e:  # noqa: BLE001
                return self._err(str(e))

        self._err("未知接口：" + path, 404)

    # ---- 各业务端点 ----
    def _login_start(self) -> None:
        cfg = load_cfg()
        c = core.NcmClient(cfg)
        try:
            key = c.get("/login/qr/key").get("data", {}).get("unikey")
            if not key:
                return self._err("API 服务未返回 unikey，请确认服务已启动")
            created = c.get("/login/qr/create", key=key, qrimg=1).get("data", {})
            qrimg = created.get("qrimg", "")
            LOGIN_SESSIONS[key] = time.time()
            core.log("已生成登录二维码，等待扫码…")
            return self._ok(key=key, qrimg=qrimg, qrurl=created.get("qrurl"))
        except Exception as e:  # noqa: BLE001
            return self._err(f"生成二维码失败：{e}")

    def _login_status(self, key: str) -> None:
        if not key or key not in LOGIN_SESSIONS:
            return self._err("二维码会话不存在或已过期，请重新点击登录")
        cfg = load_cfg()
        c = core.NcmClient(cfg)
        try:
            try:
                res = c.get("/login/qr/check", key=key)
            except Exception:  # noqa: BLE001 文档：偶发 502
                res = c.get("/login/qr/check", key=key, noCookie="true")
        except Exception as e:  # noqa: BLE001
            return self._err(f"查询扫码状态失败：{e}")
        code = res.get("code")
        if code == 803:
            c.cookie = res.get("cookie", "")
            c.save_cookie(HERE / cfg["cookie_file"])
            LOGIN_SESSIONS.pop(key, None)
            core.log("扫码登录成功，cookie 已保存。")
            logged_in, nick = account_info(cfg)
            return self._ok(code=803, nickname=nick)
        if code == 800:
            LOGIN_SESSIONS.pop(key, None)
            core.log("二维码已过期。")
        return self._ok(code=code)

    def _playlists(self) -> None:
        cfg = load_cfg()
        c = core.NcmClient(cfg)
        try:
            if not c.load_cookie(HERE / cfg["cookie_file"]):
                return self._err("未登录，无法获取歌单列表")
            acc = c.get("/user/account")
            profile = acc.get("profile") or (acc.get("data") or {}).get("profile") or {}
            uid = profile.get("userId") or (acc.get("data") or {}).get("userId")
            if not uid:
                return self._err("登录态无效，请重新扫码登录")
            data = c.get("/user/playlist", uid=int(uid), limit=100)
            pls = [{"id": p.get("id"), "name": p.get("name"), "trackCount": p.get("trackCount")}
                   for p in data.get("playlist", [])]
            return self._ok(playlists=pls)
        except Exception as e:  # noqa: BLE001
            return self._err(f"获取歌单失败：{e}")

    def _ensure_names(self) -> None:
        """缓存计划缺曲目名/艺术家时批量补查（/song/detail），补完写回磁盘缓存。"""
        global LAST_PLAN, LAST_PLAN_AT
        if not LAST_PLAN:
            return

        def _needs(it: dict) -> bool:
            if it.get("track_names") is None:
                return True
            return any(len(s) < 4 for s in (it.get("shares") or []) + (it.get("ignored") or []))

        if not any(_needs(it) for it in LAST_PLAN.get("plan", [])):
            return
        try:
            cfg = load_cfg()
            c = core.NcmClient(cfg)
            if not c.load_cookie(HERE / cfg["cookie_file"]):
                return
            core.log("缓存计划缺曲目名，正在批量补查…")
            core.enrich_plan_names(c, LAST_PLAN["plan"], only_missing=True)
            core.save_json(HERE / "last_plan.json", {"at": LAST_PLAN_AT, "plan": LAST_PLAN})
            core.log("名称补查完成，已更新缓存")
        except Exception as e:  # noqa: BLE001
            core.log(f"名称补查失败（不影响使用，缺名处显示 ID）：{e}")

    def _playlist_state(self) -> None:
        """返回目标歌单当前曲目 id 列表（执行后按歌单真实状态刷新界面）。"""
        try:
            cfg = load_cfg()
            c = core.NcmClient(cfg)
            if not c.load_cookie(HERE / cfg["cookie_file"]):
                return self._err("未登录")
            pid = None
            if LAST_PLAN and LAST_PLAN.get("playlist_id"):
                pid = int(LAST_PLAN["playlist_id"])
            elif cfg.get("playlist_id"):
                pid = int(cfg["playlist_id"])
            else:
                acc = c.get("/user/account")
                profile = acc.get("profile") or (acc.get("data") or {}).get("profile") or {}
                uid = profile.get("userId") or (acc.get("data") or {}).get("userId")
                if uid:
                    data = c.get("/user/playlist", uid=int(uid), limit=100)
                    for pl in data.get("playlist", []):
                        if pl.get("name") == cfg.get("playlist_name"):
                            pid = int(pl["id"])
                            break
            if not pid:
                return self._err("未找到目标歌单（先采集一次或在配置里填歌单 ID）")
            ids = [i for i, _ in core.playlist_track_ids(c, pid, cfg)]
            return self._ok(playlist_id=pid, track_ids=ids)
        except Exception as e:  # noqa: BLE001
            return self._err(str(e))

    def _plan(self) -> None:
        global LAST_PLAN, LAST_PLAN_AT
        body = self._body()

        def _cached_payload() -> dict:
            n_new = sum(1 for it in LAST_PLAN["plan"] if it["new_track_ids"])
            n_tracks = sum(len(it["new_track_ids"]) for it in LAST_PLAN["plan"])
            return {"plan": LAST_PLAN, "summary": {"groups": n_new, "tracks": n_tracks},
                    "cached": True}

        # 内存缓存：10 分钟内直接返回上次结果，强制刷新需显式 refresh=true
        if (LAST_PLAN is not None and not body.get("refresh")
                and time.time() - LAST_PLAN_AT < PLAN_CACHE_SEC):
            self._ensure_names()
            return self._ok(**_cached_payload())
        # 磁盘缓存：进程重启后仍可秒开
        if LAST_PLAN is None and not body.get("refresh"):
            disk = core.load_json(HERE / "last_plan.json", {})
            if disk.get("plan") and time.time() - float(disk.get("at", 0)) < PLAN_CACHE_SEC:
                LAST_PLAN = disk["plan"]
                LAST_PLAN_AT = float(disk.get("at", 0))
                core.log("已从 last_plan.json 恢复计划缓存")
                self._ensure_names()
                return self._ok(**_cached_payload())
        if not BUSY.acquire(blocking=False):
            return self._err("已有任务在执行，请稍候")
        try:
            cfg = load_cfg()
            res = core.collect_plan(cfg)
            LAST_PLAN = res
            LAST_PLAN_AT = time.time()
            core.save_json(HERE / "last_plan.json", {"at": LAST_PLAN_AT, "plan": res})
            n_new = sum(1 for it in res["plan"] if it["new_track_ids"])
            n_tracks = sum(len(it["new_track_ids"]) for it in res["plan"])
            core.log(f"计划采集完成：{n_new} 组分享 / {n_tracks} 首待入库")
            return self._ok(plan=res, summary={"groups": n_new, "tracks": n_tracks}, cached=False)
        except Exception as e:  # noqa: BLE001
            core.log(f"采集失败：{e}")
            return self._err(str(e))
        finally:
            BUSY.release()

    def _commit(self, body: dict) -> None:
        global LAST_PLAN
        if LAST_PLAN is None:
            return self._err("请先点击「采集计划」，再执行入库")
        if not BUSY.acquire(blocking=False):
            return self._err("已有任务在执行，请稍候")
        try:
            cfg = load_cfg()
            msg_ids = body.get("msg_ids")
            summary = core.commit_plan(cfg, LAST_PLAN, msg_ids)
            core.log(f"入库完成：{summary}")
            return self._ok(summary=summary)
        except Exception as e:  # noqa: BLE001
            core.log(f"入库失败：{e}")
            return self._err(str(e))
        finally:
            BUSY.release()

    def _discover(self) -> None:
        if not BUSY.acquire(blocking=False):
            return self._err("已有任务在执行，请稍候")
        try:
            cfg = load_cfg()
            dump = core.collect_dump(cfg)
            core.save_json(HERE / "discover_dump.json", dump)
            core.log("原始报文已写入 discover_dump.json")
            return self._ok(dump=dump)
        except Exception as e:  # noqa: BLE001
            return self._err(str(e))
        finally:
            BUSY.release()

    def _history(self, limit: int) -> None:
        cfg = load_cfg()
        path = HERE / cfg.get("history_file", "sync_history.jsonl")
        recs: list[dict] = []
        if path.exists():
            for ln in path.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    recs.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        recs.reverse()  # 新的在前
        return self._ok(records=recs)

    def _file(self, name: str) -> None:
        if name not in DOWNLOADABLE:
            return self._err("不允许下载该文件", 403)
        p = HERE / name
        if not p.exists():
            return self._err("文件不存在", 404)
        data = p.read_bytes()

        def _do() -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        self._send(_do)


# ----------------------------------------------------------------------------
# 前端页面（深色单页）
# ----------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>网易云私信 → 歌单</title>
<style>
:root{
  --bg:#121212; --s1:#181818; --s2:#1f1f1f; --s3:#252525;
  --tx:#ffffff; --tx2:#b3b3b3; --tx3:#cbcbcb;
  --green:#1ed760; --green-b:#1db954;
  --red:#f3727f; --orange:#ffa42b; --blue:#539df5;
  --bd:#4d4d4d; --bd2:#7c7c7c;
  --sh-h:rgba(0,0,0,.5) 0 8px 24px;
  --sh-m:rgba(0,0,0,.3) 0 8px 8px;
  --sh-in:rgb(18,18,18) 0 1px 0, rgb(124,124,124) 0 0 0 1px inset;
  --font:"SpotifyMixUI","CircularSp","Helvetica Neue",Helvetica,Arial,"Hiragino Sans","Microsoft YaHei",sans-serif;
}
*{box-sizing:border-box; margin:0; padding:0}
body{background:var(--bg); color:var(--tx); font:400 16px/1.4 var(--font); padding:24px 28px 48px}

header{display:flex; flex-wrap:wrap; align-items:center; gap:16px; margin-bottom:22px}
.brand{display:flex; align-items:center; gap:12px}
.logo{width:38px; height:38px; border-radius:50%; background:var(--green); position:relative}
.logo::after{content:""; position:absolute; inset:10px; border-radius:50%;
  border:3px solid #000; border-top-color:transparent; transform:rotate(-30deg)}
h1{font-size:24px; font-weight:700; letter-spacing:.2px}
.chips{display:flex; flex-wrap:wrap; gap:8px; margin-left:auto}
.chip{display:inline-flex; align-items:center; gap:7px; background:var(--s1); border-radius:9999px;
  padding:6px 14px; font-size:12px; font-weight:400; color:var(--tx2); box-shadow:var(--sh-m)}
.chip b{color:var(--tx); font-weight:700}
.dot{width:8px; height:8px; border-radius:50%; background:var(--green); flex:none}
.dot.bad{background:var(--red)}
.dot.mute{background:var(--bd2)}

.grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(400px,1fr)); gap:18px}
.card{background:var(--s1); border-radius:8px; padding:22px; box-shadow:var(--sh-m)}
.card.wide{grid-column:1/-1}
h2{font-size:18px; font-weight:600; line-height:1.3; margin-bottom:14px; display:flex; align-items:center; gap:10px}
h2 .idx{display:inline-flex; align-items:center; justify-content:center; width:22px; height:22px;
  border-radius:50%; background:var(--s2); color:var(--green); font-size:11px; font-weight:700}
.hint{font-size:12px; color:var(--tx2); margin-top:10px; line-height:1.5}
code{font:400 12px/1.4 Consolas,monospace; background:var(--s2); color:var(--tx3); padding:2px 7px; border-radius:4px}

label{display:block; font-size:12px; font-weight:700; letter-spacing:.5px; text-transform:uppercase;
  color:var(--tx2); margin:14px 0 6px}
input,select{width:100%; background:var(--s2); color:var(--tx); border:none; border-radius:4px;
  padding:11px 13px; font:400 14px/1.2 var(--font); box-shadow:var(--sh-in); outline:none}
input:focus,select:focus{box-shadow:0 0 0 2px var(--green-b)}
.row{display:flex; gap:10px} .row>*{flex:1}
.check{display:flex; align-items:center; gap:9px; margin-top:16px; font-size:14px; color:var(--tx3)}
.check input{width:auto; box-shadow:none; accent-color:var(--green)}

.btn{display:inline-flex; align-items:center; justify-content:center; gap:8px; cursor:pointer;
  border:none; border-radius:9999px; padding:10px 22px; font:700 14px/1 var(--font);
  text-transform:uppercase; letter-spacing:1.6px; transition:.15s; user-select:none; color:var(--tx)}
.btn:hover{transform:scale(1.03)}
.btn:active{transform:scale(.98)}
.btn.green{background:var(--green); color:#000}
.btn.green:hover{background:#1fdf64}
.btn.dark{background:var(--s2); color:var(--tx)}
.btn.dark:hover{background:#2a2a2a}
.btn.outline{background:transparent; color:var(--tx); box-shadow:inset 0 0 0 1px var(--bd2)}
.btn.outline:hover{box-shadow:inset 0 0 0 1px var(--tx)}
.btn.danger{background:transparent; color:var(--red); box-shadow:inset 0 0 0 1px var(--red)}
.btn.danger:hover{background:rgba(243,114,127,.12)}
.btn.sm{padding:7px 15px; font-size:12px; letter-spacing:1.4px}
.btn:disabled{opacity:.4; cursor:not-allowed; transform:none}
.btnrow{display:flex; flex-wrap:wrap; gap:10px; margin-top:16px}

.tag{display:inline-block; padding:3px 9px; border-radius:9999px; font-size:10.5px; font-weight:600;
  line-height:1.33; margin:2px 4px 2px 0; letter-spacing:.3px}
.t-song{background:rgba(83,157,245,.16); color:var(--blue)}
.t-album{background:rgba(255,164,43,.14); color:var(--orange)}
.t-mv{background:var(--s3); color:var(--tx2)}
.t-unread{background:rgba(255,164,43,.14); color:var(--orange)}
.t-added{background:rgba(30,215,96,.15); color:var(--green)}
.t-skip{background:var(--s3); color:var(--tx2)}
.t-err{background:rgba(243,114,127,.15); color:var(--red)}

.tablewrap{overflow-x:auto; margin-top:14px}
table{width:100%; border-collapse:collapse; font-size:12px}
th{text-align:left; padding:9px 10px; color:var(--tx2); font-size:10.5px; font-weight:700;
  text-transform:uppercase; letter-spacing:1.2px; border-bottom:1px solid var(--s3)}
td{padding:11px 10px; border-bottom:1px solid var(--s2); color:var(--tx3); vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.03)}
td b{color:var(--tx); font-weight:700}
.mono{font:400 11px/1.5 Consolas,monospace; color:var(--tx2)}
.names{font:400 11.5px/1.5 var(--font); color:var(--tx3)}
.chk{width:auto; accent-color:var(--green); vertical-align:middle}

.summary{background:var(--s2); border-radius:8px; padding:13px 17px; margin-top:14px;
  font-size:14px; color:var(--tx3); box-shadow:var(--sh-in)}
.summary b{color:var(--green); font-weight:700}

#qrbox{text-align:center; padding:18px 0 6px}
.qrframe{width:190px; height:190px; margin:0 auto; border-radius:50%; background:#fff;
  box-shadow:var(--sh-h); display:flex; align-items:center; justify-content:center; overflow:hidden}
.qrframe img{width:100%; height:100%; object-fit:contain; padding:12px}
.qrframe .ph{color:#888; font-size:12px; letter-spacing:1.4px; text-transform:uppercase}
#qrstate{margin-top:12px; font-size:12px; color:var(--tx2)}

#logpanel{background:var(--s1); border-radius:8px; margin-top:18px; overflow:hidden; box-shadow:var(--sh-m)}
#loghead{display:flex; align-items:center; gap:12px; padding:13px 20px}
#loghead b{font-size:14px; font-weight:700; letter-spacing:1.4px; text-transform:uppercase}
#logbody{height:210px; overflow:auto; padding:4px 20px 18px; font:400 12px/1.75 Consolas,monospace;
  color:var(--tx2); white-space:pre-wrap}
#logbody .hi{color:var(--green)}
#logbody .er{color:var(--red)}
#logbody .wr{color:var(--orange)}

.files a{display:inline-block; margin:6px 12px 0 0; color:var(--tx3); font-size:12px;
  text-decoration:none; box-shadow:inset 0 -1px 0 var(--bd); padding-bottom:2px}
.files a:hover{color:var(--green); box-shadow:inset 0 -1px 0 var(--green)}

.toast{position:fixed; top:18px; right:18px; max-width:420px; padding:13px 20px; border-radius:9999px;
  font-size:13px; z-index:99; box-shadow:var(--sh-h); animation:fade .25s}
.toast.err{background:var(--s2); box-shadow:var(--sh-h), inset 0 0 0 1px var(--red); color:var(--red)}
.toast.ok{background:var(--s2); box-shadow:var(--sh-h), inset 0 0 0 1px var(--green); color:var(--green)}
@keyframes fade{from{opacity:0; transform:translateY(-6px)}to{opacity:1}}
.spin{display:inline-block; width:13px; height:13px; border:2px solid rgba(255,255,255,.25);
  border-top-color:currentColor; border-radius:50%; animation:rot .8s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}
pre.jsonview{background:var(--s2); border-radius:8px; padding:14px; max-height:320px; overflow:auto;
  font:400 11px/1.6 Consolas,monospace; color:var(--tx3); box-shadow:var(--sh-in)}
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="logo"></div>
    <h1>网易云私信 → 歌单</h1>
  </div>
  <div class="chips">
    <span class="chip" id="chipSvc"><span class="dot mute"></span>API 服务：<b>…</b></span>
    <span class="chip" id="chipLogin"><span class="dot mute"></span>登录：<b>…</b></span>
    <span class="chip" id="chipPl"><span class="dot mute"></span>歌单：<b>…</b></span>
    <span class="chip" id="chipMem"><span class="dot mute"></span>记忆：<b>…</b></span>
  </div>
</header>

<div class="grid">

  <!-- ① 连接与登录 -->
  <section class="card">
    <h2><span class="idx">1</span>连接与登录</h2>
    <label>API 服务地址（api-enhanced，npx 一键启动）</label>
    <div class="row">
      <input id="apiBase" placeholder="http://localhost:3000">
      <button class="btn outline sm" onclick="testConn()">测试连接</button>
    </div>
    <div class="hint">启动服务：<code>npx @neteasecloudmusicapienhanced/api@latest</code></div>
    <div id="qrbox">
      <div class="qrframe" id="qrframe"><span class="ph" id="qrph">QR Code</span><img id="qrimg" style="display:none" alt="登录二维码"></div>
      <div id="qrstate">未登录 · 点击下方按钮生成二维码，用网易云手机端扫码</div>
    </div>
    <div class="btnrow" style="justify-content:center">
      <button class="btn green" id="btnLogin" onclick="loginStart()">扫码登录</button>
    </div>
  </section>

  <!-- ② 同步配置 -->
  <section class="card">
    <h2><span class="idx">2</span>同步配置</h2>
    <label>目标歌单（从账号歌单选择，或直接填 ID）</label>
    <div class="row">
      <select id="plSel" onchange="onPlSel()"><option value="">（点击「刷新」加载歌单）</option></select>
      <button class="btn dark sm" onclick="loadPlaylists()">刷新</button>
    </div>
    <div class="row">
      <input id="plName" placeholder="歌单名（找不到则自动创建）">
      <input id="plId" placeholder="或 歌单 ID（可空）">
    </div>
    <div class="row">
      <button class="btn dark sm" onclick="createPlaylist()">新建歌单</button>
    </div>
    <div class="row">
      <div><label>每轮会话条数</label><input id="pollLimit" type="number" min="1"></div>
      <div><label>每会话历史条数</label><input id="histLimit" type="number" min="1"></div>
    </div>
    <div class="row">
      <div><label>最大展开会话数</label><input id="maxConv" type="number" min="1"></div>
      <div><label>请求间隔（秒）</label><input id="interval" type="number" min="0" step="0.5"></div>
    </div>
    <div class="row">
      <div><label>专辑入库模式</label>
        <select id="albumMode">
          <option value="all">整张专辑全部曲目</option>
          <option value="first_n">仅前 N 首</option>
        </select></div>
      <div><label>专辑最多取 N 首</label><input id="albumMax" type="number" min="1"></div>
    </div>
    <div class="check">
      <input type="checkbox" id="alsoPl" style="width:auto"> 私信里分享的【歌单】也批量入库
    </div>
    <div class="btnrow"><button class="btn dark" onclick="saveConfig()">保存配置</button></div>
    <div class="hint">保存到 <code>config.json</code> · 计划采集与入库时即时生效</div>
  </section>

  <!-- ③ 同步执行 -->
  <section class="card wide">
    <h2><span class="idx">3</span>同步执行 · 先预览勾选，再入库</h2>
    <div class="btnrow" style="margin-top:0">
      <button class="btn green" id="btnPlan" onclick="doPlan(false)">采集计划</button>
      <button class="btn dark" id="btnPlanForce" onclick="doPlan(true)">强制重新采集</button>
      <button class="btn outline" id="btnCheckUnread" onclick="checkUnread()" disabled>勾选未读</button>
      <button class="btn outline" id="btnCheckUndet" onclick="checkUndet()" disabled>勾选状态未定</button>
      <button class="btn outline" id="btnCommitSel" onclick="doCommit(false)" disabled>入库已勾选</button>
      <button class="btn outline" id="btnCommitAll" onclick="doCommit(true)" disabled>全部入库</button>
    </div>
    <div class="summary" id="planSummary">尚未采集 · 点「采集计划」将拉取私信并展开专辑曲目，展示将入库内容（只读，不动歌单）。首次全量扫描会话较多时约需 5~10 分钟（日志实时滚动），10 分钟内再次点击走缓存。</div>
    <div class="tablewrap">
      <table id="planTable" style="display:none">
        <thead><tr>
          <th style="width:34px"><input type="checkbox" class="chk" id="chkAll" onchange="toggleAll(this)"></th>
          <th>来源</th><th>分享内容</th><th>可入库</th><th>曲目 ID 预览</th><th>状态</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="hint">未勾选的分享条目保持「待处理」，下次采集还会出现 · 已存在的曲目自动跳过不重复入库</div>
  </section>

  <!-- ④ 记忆与审计 -->
  <section class="card">
    <h2><span class="idx">4</span>记忆与审计</h2>
    <div class="row">
      <select id="histFilter" onchange="loadHistory()">
        <option value="">全部事件</option>
        <option value="added">仅入库</option>
        <option value="skip">仅跳过</option>
        <option value="error">仅失败</option>
        <option value="dry_run">仅预演</option>
      </select>
      <button class="btn dark sm" onclick="loadHistory()">刷新</button>
    </div>
    <div class="tablewrap" style="max-height:300px; overflow:auto">
      <table>
        <thead><tr><th>时间</th><th>事件</th><th>来源</th><th>详情</th></tr></thead>
        <tbody id="histBody"><tr><td colspan="4" class="hint">暂无记录</td></tr></tbody>
      </table>
    </div>
    <div class="btnrow"><button class="btn danger" onclick="doReset()">清空本地记忆</button></div>
    <div class="hint">记忆含 state.json（去重）与审计流水 · 清空后仍有歌单内容查重兜底，不会重复入库</div>
  </section>

  <!-- ⑤ 报文探测与文件 -->
  <section class="card">
    <h2><span class="idx">5</span>报文探测与输出文件</h2>
    <div class="btnrow" style="margin-top:0">
      <button class="btn dark" id="btnDump" onclick="doDiscover()">抓取私信报文</button>
      <button class="btn outline" onclick="api('/api/open-folder',{method:'POST'}).then(()=>toast('已打开数据目录','ok')).catch(e=>toast(e,'err'))">打开数据目录</button>
    </div>
    <pre class="jsonview" id="dumpView" style="margin-top:12px; display:none"></pre>
    <div class="files">
      <a href="/api/file/config.json">config.json</a>
      <a href="/api/file/state.json">state.json</a>
      <a href="/api/file/sync_history.jsonl">sync_history.jsonl</a>
      <a href="/api/file/discover_dump.json">discover_dump.json</a>
    </div>
    <div class="hint">探测结果用于校准私信报文解析 · 同步的入库明细在审计流水中</div>
  </section>
</div>

<div id="logpanel">
  <div id="loghead">
    <b>运行日志</b>
    <span class="hint" id="logCount" style="margin:0"></span>
    <span style="margin-left:auto; display:flex; gap:8px">
      <button class="btn dark sm" onclick="clearLogView()">清空显示</button>
      <button class="btn dark sm" onclick="toggleLog()">折叠</button>
    </span>
  </div>
  <div id="logbody"></div>
</div>

<script>
const $ = id => document.getElementById(id);
let LOG_POS = 0, LOGIN_KEY = null, LOGIN_TIMER = null, PLAN_DATA = null;

function toast(msg, kind) {
  document.querySelectorAll('.toast').forEach(t => t.remove());
  const d = document.createElement('div');
  d.className = 'toast ' + (kind || 'err');
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 4200);
}

async function api(path, opts) {
  opts = opts || {};
  if (opts.body && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
    opts.headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
  }
  const r = await fetch(path, opts);
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function busy(el, on, text) {
  if (!el) return;
  if (on) { el.dataset.old = el.innerHTML; el.innerHTML = '<span class="spin"></span>' + (text || '处理中'); el.disabled = true; }
  else { el.innerHTML = el.dataset.old || el.innerHTML; el.disabled = false; }
}

function setChip(id, ok, label, val) {
  const el = $(id);
  el.innerHTML = '<span class="dot ' + (ok === null ? 'mute' : ok ? '' : 'bad') + '"></span>' +
    label + '：<b>' + val + '</b>';
}

/* ---------- 状态栏 ---------- */
async function refreshStatus() {
  try {
    const s = (await api('/api/status')).status;
    setChip('chipSvc', s.service_ok, 'API 服务', s.service_ok ? '正常' : '不可用');
    setChip('chipLogin', s.logged_in, '登录', s.logged_in ? (s.nickname || '已登录') : '未登录');
    setChip('chipPl', null, '歌单', s.playlist_name || s.playlist_id || '未设置');
    setChip('chipMem', null, '记忆', s.processed + ' / ' + s.added + ' / ' + s.audit);
    if (!$('apiBase').value) $('apiBase').value = s.api_base;
  } catch (e) { /* 状态栏静默 */ }
}

/* ---------- 连接与登录 ---------- */
async function testConn() {
  try {
    await api('/api/config', {method: 'POST', body: {api_base: $('apiBase').value.trim()}});
    const r = await api('/api/test-connection');
    toast(r.detail, r.service_ok ? 'ok' : 'err');
    refreshStatus();
  } catch (e) { toast(e.message, 'err'); }
}

async function loginStart() {
  const btn = $('btnLogin');
  busy(btn, true, '生成二维码');
  try {
    const r = await api('/api/login/start', {method: 'POST'});
    LOGIN_KEY = r.key;
    const img = $('qrimg');
    img.src = r.qrimg; img.style.display = 'block';
    $('qrph').style.display = 'none';
    $('qrstate').textContent = '请用网易云手机端扫码并在手机上确认…';
    if (LOGIN_TIMER) clearInterval(LOGIN_TIMER);
    LOGIN_TIMER = setInterval(pollLogin, 2500);
  } catch (e) { toast(e.message, 'err'); }
  finally { busy(btn, false); }
}

async function pollLogin() {
  if (!LOGIN_KEY) return;
  try {
    const r = await api('/api/login/status?key=' + encodeURIComponent(LOGIN_KEY));
    if (r.code === 801) $('qrstate').textContent = '等待扫码…';
    if (r.code === 802) $('qrstate').textContent = '已扫码，请在手机上确认登录…';
    if (r.code === 800) { $('qrstate').textContent = '二维码已过期，请重新点击「扫码登录」。'; clearInterval(LOGIN_TIMER); }
    if (r.code === 803) {
      clearInterval(LOGIN_TIMER); LOGIN_KEY = null;
      $('qrstate').textContent = '登录成功：' + (r.nickname || '') + '。可进行采集与入库。';
      toast('登录成功', 'ok'); refreshStatus();
    }
  } catch (e) { clearInterval(LOGIN_TIMER); toast(e.message, 'err'); }
}

/* ---------- 配置 ---------- */
async function loadConfig() {
  try {
    const c = (await api('/api/config')).config;
    $('apiBase').value = c.api_base || '';
    $('plName').value = c.playlist_name || '';
    $('plId').value = c.playlist_id || '';
    $('pollLimit').value = c.poll_limit;
    $('histLimit').value = c.history_limit_per_chat;
    $('maxConv').value = c.max_conversations_per_sync;
    $('interval').value = c.request_interval_sec;
    $('albumMode').value = c.album_mode;
    $('albumMax').value = c.max_album_tracks;
    $('alsoPl').checked = !!c.also_add_shared_playlist;
  } catch (e) { toast(e.message, 'err'); }
}

function configFromForm() {
  return {
    api_base: $('apiBase').value.trim(),
    playlist_name: $('plName').value.trim(),
    playlist_id: $('plId').value.trim() ? Number($('plId').value.trim()) : null,
    poll_limit: Number($('pollLimit').value) || 30,
    history_limit_per_chat: Number($('histLimit').value) || 30,
    max_conversations_per_sync: Number($('maxConv').value) || 20,
    request_interval_sec: Number($('interval').value) || 2,
    album_mode: $('albumMode').value,
    max_album_tracks: Number($('albumMax').value) || 20,
    also_add_shared_playlist: $('alsoPl').checked
  };
}

async function saveConfig() {
  try {
    await api('/api/config', {method: 'POST', body: configFromForm()});
    toast('配置已保存', 'ok'); refreshStatus();
  } catch (e) { toast(e.message, 'err'); }
}

async function loadPlaylists() {
  try {
    const ps = (await api('/api/playlists')).playlists;
    const sel = $('plSel');
    sel.innerHTML = '<option value="">（选择目标歌单）</option>' +
      ps.map(p => `<option value="${p.id}">${p.name}（${p.trackCount || 0}首）</option>`).join('');
    toast('已加载 ' + ps.length + ' 个歌单', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

function onPlSel() {
  const v = $('plSel').value;
  if (v) $('plId').value = v;
}

async function createPlaylist() {
  const name = $('plName').value.trim();
  if (!name) return toast('请先填写歌单名', 'err');
  try {
    await api('/api/config', {method: 'POST', body: configFromForm()});
    toast('已保存歌单名「' + name + '」，首次采集时若不存在会自动创建', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------- 同步执行 ---------- */
function shareTags(shares) {
  const LB = {song:'歌曲', album:'专辑', playlist:'歌单', mv:'MV'};
  const CL = {song:'t-song', album:'t-album', playlist:'t-song', mv:'t-mv'};
  return (shares || []).map(s => {
    const [k, i, name, artist] = s;
    const label = name ? `${LB[k] || k}《${name}》` : `${LB[k] || k} ${i}`;
    const tip = 'id ' + i + (artist ? ' · ' + artist : '');
    return `<span class="tag ${CL[k] || 't-mv'}" title="${tip}">${label}</span>`;
  }).join('');
}

function trackPreview(it) {
  const names = it.track_names || {};
  const ids = it.new_track_ids.slice(0, 8);
  const cells = ids.map(t => names[t] ? names[t] : ('#' + t));
  const more = it.new_track_ids.length > 8 ? '…' : '';
  const tip = 'ID: ' + it.new_track_ids.slice(0, 30).join(', ');
  return `<span class="names" title="${tip}">${cells.join('、')}${more}</span>`;
}

function planRow(it) {
  const isNew = it.new_track_ids.length > 0;
  const already = !isNew && it.track_ids.length > 0;
  const isRead = it.msg_unread === false;   // 推导已读
  const un = it.msg_unread === true
    ? `<span class="tag t-unread" title="推导未读${it.unread ? ' · 会话未读 ' + it.unread : ''}">未读</span>`
    : isRead ? '<span class="tag t-skip" title="推导已读（会话未读数之外的消息）">已读</span>'
    : '<span class="tag t-skip" title="无法判定已读/未读（该会话未带未读数字段）">未定</span>';
  const ignored = (it.ignored && it.ignored.length)
    ? shareTags(it.ignored) + '<span class="tag t-mv">不入库</span>' : '';
  const chk = it.in_playlist ? 'disabled' : (isNew ? (isRead ? '' : 'checked') : 'disabled');  // 已入库/已读不预勾
  const status = it.in_playlist ? '<span class="tag t-added">已入库</span>'
    : isNew ? '<span class="tag t-added">待入库</span>'
    : already ? '<span class="tag t-skip">已存在</span>'
    : (it.shares.length ? '<span class="tag t-err">无曲目</span>' : '<span class="tag t-skip">已忽略</span>');
  return `<tr>
    <td><input type="checkbox" class="chk rowchk" data-mid="${it.msg_id}" data-unread="${it.msg_unread === true ? 1 : 0}" data-undet="${it.msg_unread == null ? 1 : 0}" ${chk}></td>
    <td><b>${it.sender_label || it.sender}</b> ${un}</td>
    <td>${shareTags(it.shares)}${ignored}</td>
    <td><b>${it.new_track_ids.length}</b> / ${it.track_ids.length}</td>
    <td>${isNew ? trackPreview(it) : '<span class="mono">-</span>'}</td>
    <td>${status}
        ${it.errors && it.errors.length ? '<span class="tag t-err">' + it.errors.join('; ') + '</span>' : ''}</td>
  </tr>`;
}

function renderPlan(plan, summary, cached) {
  // 兼容两种形状：直接数组，或 collect_plan 整包对象（内含 .plan 数组）
  const items = Array.isArray(plan) ? plan
    : (plan && Array.isArray(plan.plan) ? plan.plan : []);
  const tb = $('planTable');
  tb.querySelector('tbody').innerHTML = items.map(planRow).join('') ||
    '<tr><td colspan="6" class="hint">没有发现新的分享条目</td></tr>';
  tb.style.display = 'table';
  $('planSummary').innerHTML =
    `采集完成${cached ? '（10 分钟内缓存，点「强制重新采集」刷新）' : ''}：可入库 <b>${summary.groups}</b> 组分享 / <b>${summary.tracks}</b> 首；` +
    `目标歌单现有 ${PLAN_DATA.existing_count} 首。勾选后点「入库已勾选」，或直接「全部入库」。`;
  $('btnCommitSel').disabled = $('btnCommitAll').disabled = summary.groups === 0;
  $('btnCheckUnread').disabled = $('btnCheckUndet').disabled = items.length === 0;
  $('chkAll').checked = summary.groups > 0;
}

function toggleAll(el) {
  document.querySelectorAll('.rowchk:not(:disabled)').forEach(c => c.checked = el.checked);
}

function checkUnread() {
  // 把所有推导未读的行强制打钩（不管之前状态）；MV/已存在等禁用行天然忽略
  let n = 0;
  document.querySelectorAll('.rowchk[data-unread="1"]').forEach(c => {
    if (c.disabled) return;
    c.checked = true;
    n++;
  });
  toast(n ? `已勾选 ${n} 条推导未读` : '没有可勾选的推导未读条目', n ? 'ok' : 'err');
}

function checkUndet() {
  // 把所有状态未定（无法判定已读/未读）的行强制打钩；MV/已存在等禁用行天然忽略
  let n = 0;
  document.querySelectorAll('.rowchk[data-undet="1"]').forEach(c => {
    if (c.disabled) return;
    c.checked = true;
    n++;
  });
  toast(n ? `已勾选 ${n} 条状态未定` : '没有可勾选的状态未定条目', n ? 'ok' : 'err');
}

async function doPlan(force) {
  const btn = force ? $('btnPlanForce') : $('btnPlan');
  busy(btn, true, '采集中');
  $('planSummary').innerHTML = '<span class="spin"></span> 正在拉取私信并展开专辑（每 2 秒一次 API，首次全量扫描需 5~10 分钟，日志实时滚动）…';
  try {
    await saveConfigSilent();
    const r = await api('/api/plan', {method: 'POST', body: {refresh: !!force}});
    PLAN_DATA = r.plan;
    renderPlan(r.plan, r.summary, r.cached);
    refreshFromPlaylist();   // 按歌单真实状态校准（已入库/已存在 -> 已入库）
  } catch (e) { toast(e.message, 'err'); $('planSummary').textContent = '采集失败：' + e.message; }
  finally { busy(btn, false); refreshStatus(); }
}

async function saveConfigSilent() {
  await api('/api/config', {method: 'POST', body: configFromForm()});
}

async function refreshFromPlaylist() {
  // 执行后按歌单真实状态刷新：曲目已全部在歌单的行 -> 已入库 + 取消勾选（未读标签保留）
  try {
    const r = await api('/api/playlist-state');
    const set = new Set(r.track_ids);
    let n = 0;
    ((PLAN_DATA && PLAN_DATA.plan) || []).forEach(it => {
      const tid = it.track_ids || [];
      if (!tid.length) return;                       // MV/忽略行不动
      if (!tid.every(t => set.has(t))) return;
      it.in_playlist = true;
      const chk = document.querySelector(`.rowchk[data-mid="${it.msg_id}"]`);
      if (!chk) return;
      chk.checked = false;
      chk.disabled = true;
      const tr = chk.closest('tr');
      if (tr && tr.lastElementChild) {
        tr.lastElementChild.innerHTML = '<span class="tag t-added">已入库</span>';
      }
      n++;
    });
    if (n >= 0) refreshStatus();
    return n;
  } catch (e) { toast(e.message, 'err'); return -1; }
}

async function doCommit(all) {
  const ids = all ? null : Array.from(document.querySelectorAll('.rowchk:checked')).map(c => c.dataset.mid);
  if (!all && (!ids || !ids.length)) return toast('请至少勾选一条要入库的分享', 'err');
  const btn = all ? $('btnCommitAll') : $('btnCommitSel');
  busy(btn, true, '入库中');
  try {
    const r = await api('/api/commit', {method: 'POST', body: {msg_ids: ids}});
    const s = r.summary;
    toast(`入库完成：${s.added_msgs} 组 / ${s.added_tracks} 首（跳过 ${s.skipped}，失败 ${s.errors}）`, 'ok');
    $('planSummary').innerHTML += `<br>上次执行结果：入库 ${s.added_msgs} 组 / ${s.added_tracks} 首，跳过 ${s.skipped}，失败 ${s.errors}，仍待处理 ${s.pending}`;
    await refreshFromPlaylist();   // 执行后查歌单真实状态，把已入库的行刷新并取消勾选
    loadHistory(); refreshStatus();
  } catch (e) { toast(e.message, 'err'); }
  finally { busy(btn, false); }
}

/* ---------- 审计 ---------- */
async function loadHistory() {
  try {
    const filter = $('histFilter').value;
    let recs = (await api('/api/history?limit=80')).records;
    if (filter) recs = recs.filter(r => r.event === filter);
    const EV = {added:'t-added', skip:'t-skip', error:'t-err', dry_run:'t-unread'};
    $('histBody').innerHTML = recs.length ? recs.map(r => {
      const detail = r.event === 'added' || r.event === 'dry_run'
        ? `${r.count} 首 ${(r.track_ids || []).slice(0, 6).join(', ')}` + (r.count > 6 ? '…' : '')
        : (r.reason || r.error || '');
      return `<tr><td>${r.ts || ''}</td><td><span class="tag ${EV[r.event] || 't-skip'}">${r.event}</span></td>
        <td><b>${r.sender || ''}</b></td><td>${detail}</td></tr>`;
    }).join('') : '<tr><td colspan="4" class="hint">暂无记录</td></tr>';
  } catch (e) { toast(e.message, 'err'); }
}

async function doReset() {
  if (!confirm('确定清空本地记忆（state.json + 审计流水）？此操作不可恢复。')) return;
  try {
    await api('/api/reset', {method: 'POST', body: {yes: true}});
    toast('记忆已清空', 'ok'); loadHistory(); refreshStatus();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------- 探测 ---------- */
async function doDiscover() {
  const btn = $('btnDump');
  busy(btn, true, '抓取中');
  try {
    const r = await api('/api/discover', {method: 'POST'});
    const view = $('dumpView');
    view.style.display = 'block';
    view.textContent = JSON.stringify(r.dump, null, 2);
    toast('报文已抓取（同时写入 discover_dump.json）', 'ok');
  } catch (e) { toast(e.message, 'err'); }
  finally { busy(btn, false); }
}

/* ---------- 日志 ---------- */
function logLineHtml(l) {
  const esc = l.replace(/&/g, '&amp;').replace(/</g, '&lt;');
  if (/失败|Error|错误|404/.test(l)) return '<span class="er">' + esc + '</span>';
  if (/可入库|计划采集完成|已入库|登录成功|正常/.test(l)) return '<span class="hi">' + esc + '</span>';
  if (/仅 MV|跳过|已在|忽略/.test(l)) return '<span class="wr">' + esc + '</span>';
  return esc;
}

async function pollLogs() {
  try {
    const r = await api('/api/logs?since=' + LOG_POS);
    if (r.lines.length) {
      const body = $('logbody');
      const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
      body.innerHTML += r.lines.map(logLineHtml).join('\n') + '\n';
      if (atBottom) body.scrollTop = body.scrollHeight;
      LOG_POS = r.next;
      $('logCount').textContent = '（' + LOG_POS + ' 行）';
    }
  } catch (e) { /* 静默 */ }
}

function clearLogView() { $('logbody').innerHTML = ''; }
function toggleLog() { $('logbody').style.display = $('logbody').style.display === 'none' ? '' : 'none'; }

/* ---------- 启动 ---------- */
(async function init() {
  await loadConfig();
  await refreshStatus();
  await loadHistory();
  pollLogs();
  setInterval(pollLogs, 1500);
  setInterval(refreshStatus, 8000);
})();
</script>
</body>
</html>

"""


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="网易云私信→歌单 网页管理界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[NcmGui] 界面已启动：{url}   （Ctrl+C 退出）")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[NcmGui] 已退出。")


if __name__ == "__main__":
    main()
