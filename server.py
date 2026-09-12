#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# llama-monitor-panel -- Llama 监控面板本地后端（单 exe 交付）
# 流程：部署 feeder.py 到 x99 -> ssh 运行 feeder 输出 JSON 行流(0.6s/行)
#       -> 本机解析并做 token 记账/事件流 -> 本地 HTTP -> 面板页面(webview/浏览器)
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_NAME = "Llama 监控面板"
# 对话流序号回退多少才认定是代理重启（而不是重复投递的老事件）
TRACE_RESET_GAP = 50
DEFAULT_HOST = "lmq@192.168.2.6"
DEFAULT_REMOTE = "/home/lmq/llama-panel-feeder.py"
LLAMA_LABELS = {"BUSY": "推理进行中", "READY": "就绪", "LOADING": "加载模型中", "IDLE": "空闲", "DOWN": "服务离线"}

def no_window():
    """拉起子进程（ssh / where 等控制台程序）时隐藏控制台黑框的参数。

    本程序是无控制台的 GUI 程序，直接运行 ssh.exe 时 Windows 会为每个 ssh
    新建一个控制台窗口（系统默认终端是 Windows Terminal 时就是一个黑框），
    一个面板启动会出现好几个，所以必须显式带 CREATE_NO_WINDOW。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    si = None
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        si.wShowWindow = 0  # SW_HIDE
    return flags, si


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource(name):
    if getattr(sys, "frozen", False):
        p = os.path.join(getattr(sys, "_MEIPASS", base_dir()), name)
        if os.path.exists(p):
            return p
    return os.path.join(base_dir(), name)


def data_dir():
    d = os.path.join(base_dir(), "panel-data")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def fmt_int(x):
    try:
        return "{:,}".format(int(x))
    except Exception:
        return "--"


class Settings(object):
    def __init__(self):
        self.path = os.path.join(data_dir(), "settings.json")
        d = _load(self.path, {})
        self.v = {
            "cache_hit_price": 0.05,
            "input_miss_price": 1.0,
            "output_price": 5.0,
            "host": DEFAULT_HOST,
            "remote_path": DEFAULT_REMOTE,
            "ssh_port": 22,
        }
        for k in self.v:
            if k in d:
                self.v[k] = d[k]
        for k in ("cache_hit_price", "input_miss_price", "output_price"):
            try:
                self.v[k] = max(0.0, float(self.v[k]))
            except Exception:
                self.v[k] = 0.0
        try:
            port = int(self.v["ssh_port"])
        except Exception:
            port = 22
        self.v["ssh_port"] = port if 0 < port < 65536 else 22
        self.v["host"] = str(self.v["host"]).strip()
        self.v["remote_path"] = str(self.v["remote_path"]).strip()

    def update(self, patch):
        changed = []
        if not isinstance(patch, dict):
            return changed
        for k in ("cache_hit_price", "input_miss_price", "output_price"):
            if k in patch and patch[k] not in (None, ""):
                try:
                    self.v[k] = max(0.0, float(patch[k]))
                    changed.append(k)
                except Exception:
                    pass
        for k in ("host", "remote_path"):
            if k in patch and isinstance(patch[k], str) and patch[k].strip():
                self.v[k] = patch[k].strip()
                changed.append(k)
        if "ssh_port" in patch and patch["ssh_port"] not in (None, ""):
            try:
                port = int(patch["ssh_port"])
                if 0 < port < 65536:
                    self.v["ssh_port"] = port
                    changed.append("ssh_port")
            except Exception:
                pass
        if changed:
            _save(self.path, self.v)
        return changed

    @property
    def dict(self):
        return dict(self.v)


class Totals(object):
    FIELDS = ("cache_hit", "input_miss", "output", "tasks", "saved", "cost_total")

    def __init__(self):
        self.path = os.path.join(data_dir(), "state.json")
        d = _load(self.path, {})
        t = d.get("totals", {}) if isinstance(d, dict) else {}
        self.t = {}
        for k in self.FIELDS:
            try:
                self.t[k] = float(t.get(k, 0.0))
            except Exception:
                self.t[k] = 0.0
        seen = d.get("seen", []) if isinstance(d, dict) else []
        self.seen = [str(x) for x in seen[-5000:]]
        self.seen_set = set(self.seen)

    def persist(self):
        _save(self.path, {"totals": self.t, "seen": self.seen[-5000:]})

    def add(self, C, pp, gen, prices):
        C = int(C or 0)
        pp = int(pp or 0)
        gen = int(gen or 0)
        cost = (C * prices.get("cache_hit_price", 0.0)
                + pp * prices.get("input_miss_price", 0.0)
                + gen * prices.get("output_price", 0.0)) / 1e6
        self.t["cache_hit"] += C
        self.t["input_miss"] += pp
        self.t["output"] += gen
        self.t["tasks"] += 1
        self.t["saved"] += cost
        self.t["cost_total"] += cost
        self.persist()

    def reset(self):
        for k in self.FIELDS:
            self.t[k] = 0.0
        self.seen = []
        self.seen_set = set()
        self.persist()

    @property
    def dict(self):
        d = {}
        for k in self.FIELDS:
            d[k] = int(self.t[k]) if k in ("cache_hit", "input_miss", "output", "tasks") else round(self.t[k], 4)
        d["all_in"] = int(self.t["cache_hit"] + self.t["input_miss"])
        d["all_out"] = int(self.t["output"])
        d["all_tokens"] = d["all_in"] + d["all_out"]
        return d
class Feed(object):
    """通过 ssh 在 x99 上运行 feeder，并读取 JSON 行流。"""

    def __init__(self, settings, panel):
        self.settings = settings
        self.panel = panel
        self.feeder_src = self._read_feeder()
        self.proc = None
        self.frame = None
        self.last_frame_at = 0.0
        self.mu = threading.RLock()
        self.log = deque(maxlen=200)
        # 对话流：proxy 写一条 feeder 读一条，序号单调递增，
        # 用序号去重，feeder 重连后重放的历史不会被面板当成新对话。
        self.trace = deque(maxlen=800)
        self.trace_seq = -1

    def _read_feeder(self):
        try:
            with open(resource("feeder.py"), "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def _ssh(self):
        """ssh 基础参数：非 22 端口时补 -p，其余选项保持一致。"""
        host = self.settings.v["host"]
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
        port = int(self.settings.v.get("ssh_port") or 22)
        if port != 22:
            args += ["-p", str(port)]
        return args, host

    def deploy(self):
        if not self.feeder_src:
            return
        base, host = self._ssh()
        remote = self.settings.v["remote_path"]
        local_md5 = hashlib.md5(self.feeder_src.encode("utf-8")).hexdigest()
        remote_md5 = ""
        cf, si = no_window()
        try:
            r = subprocess.run(
                base + [host, "md5sum '%s' 2>/dev/null | awk '{print $1}'" % remote],
                capture_output=True, text=True, timeout=25,
                creationflags=cf, startupinfo=si)
            out = (r.stdout or "").strip()
            if r.returncode == 0 and out:
                remote_md5 = out.split()[0]
        except Exception:
            pass
        if remote_md5 != local_md5:
            try:
                subprocess.run(
                    base + [host, "cat > '%s'" % remote],
                    input=self.feeder_src.encode("utf-8"), timeout=90, check=True,
                    creationflags=cf, startupinfo=si)
            except Exception:
                pass

    def spawn(self):
        with self.mu:
            p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        base, host = self._ssh()
        remote = self.settings.v["remote_path"]
        cf, si = no_window()
        try:
            self.proc = subprocess.Popen(
                base + ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                        host, "python3 -u '%s'" % remote],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="replace",
                creationflags=cf, startupinfo=si)
        except Exception:
            self.proc = None
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        if proc is None:
            return
        try:
            for line in proc.stdout:
                if self.proc is not proc:
                    break
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict) or "ts" not in obj:
                    continue
                for ln in (obj.get("raw_new") or [])[-80:]:
                    self.log.append(ln)
                for ev in (obj.get("trace_new") or []):
                    if not isinstance(ev, dict):
                        continue
                    try:
                        seq = int(ev.get("n"))
                    except Exception:
                        continue
                    if seq <= self.trace_seq:
                        # 代理重启（机器重启、代理重拉）后 n 会从 1 重新开始，而这里的
                        # 水位只增不减，不识别这次回退就会把重启后的新事件全部丢掉，
                        # 表现就是对话流冻死在重启前那一屏、再也不更新。
                        # 退回幅度很大才当成重启，避免把重复投递的老事件误判成新一轮。
                        if self.trace_seq - seq < TRACE_RESET_GAP:
                            continue
                        self.trace.clear()
                        self.trace_seq = -1
                    self.trace_seq = seq
                    self.trace.append(ev)
                self.frame = obj
                self.last_frame_at = time.time()
                try:
                    self.panel.on_frame(obj)
                except Exception:
                    pass
        except Exception:
            pass

    @property
    def status(self):
        if self.proc is None or self.proc.poll() is not None:
            return "offline"
        return "online" if (time.time() - self.last_frame_at) < 10 else "stale"


class Panel(object):
    def __init__(self, settings):
        self.settings = settings
        self.totals = Totals()
        self.events = deque(maxlen=400)
        self.active_tasks = set()
        self.last_llama = None
        self.started_at = time.time()
        self.mu = threading.RLock()
        self.feed = Feed(settings, self)

    def push_event(self, kind, text):
        with self.mu:
            self.events.append({"ts": time.strftime("%H:%M:%S"), "kind": kind, "text": text})

    def on_frame(self, f):
        with self.mu:
            cur = f.get("llama")
            if cur != self.last_llama:
                label = LLAMA_LABELS.get(cur, cur or "未知")
                self.push_event("status", "服务状态变更：%s" % label)
                self.last_llama = cur
            new_active = set()
            for s in (f.get("slots") or []):
                if s.get("busy") and s.get("task") is not None:
                    new_active.add(str(s.get("task")))
            for t in sorted(new_active - self.active_tasks):
                self.push_event("start", "新任务 #%s 启动，推理进行中" % t)
            self.active_tasks = new_active
            tot = self.totals
            for h in (f.get("hist") or []):
                tid = h.get("task")
                if tid is None:
                    continue
                key = str(tid)
                if key in tot.seen_set:
                    continue
                C = h.get("C") or 0
                pp = h.get("pp") or 0
                gen = h.get("gen") or 0
                if not (C or pp or gen):
                    continue
                tot.seen_set.add(key)
                tot.seen.append(key)
                tot.add(C, pp, gen, self.settings.v)
                hit = h.get("hit")
                hit_s = ("命中率 %.0f%%" % hit) if isinstance(hit, (int, float)) else "命中率 --"
                self.push_event(
                    "done",
                    "任务 #%s 完成：输入 %s（%s，新计算 %s）· 输出 %s · 解码 %.1f t/s" % (
                        key, fmt_int(h.get("in")), hit_s, fmt_int(pp),
                        fmt_int(gen), float(h.get("gen_tps") or 0.0)))

    def _live(self, fr):
        busy = 0
        gen = 0
        if fr:
            for s in (fr.get("slots") or []):
                if s.get("busy"):
                    busy += 1
                    gen += int(s.get("gen") or 0)
        return {"busy": busy, "gen": gen}

    def state(self, trace_since=None, trace_limit=240):
        with self.mu:
            ev = list(self.events)[-200:]
        trace_all = list(self.feed.trace)
        if trace_since is None:
            trace = trace_all[-80:]
        else:
            trace = [e for e in trace_all if e.get("n", 0) > trace_since][-trace_limit:]
        trace_seq = trace_all[-1].get("n", 0) if trace_all else (self.feed.trace_seq if self.feed.trace_seq > 0 else 0)
        return {
            "ok": True,
            "ts": time.time(),
            "feed": {
                "status": self.feed.status,
                "host": self.settings.v["host"],
                "uptime_s": int(time.time() - self.started_at),
            },
            "frame": self.feed.frame,
            "feed_log": list(self.feed.log)[-120:],
            "trace": trace,
            "trace_seq": trace_seq,
            "totals": self.totals.dict,
            "prices": {k: self.settings.v[k] for k in ("cache_hit_price", "input_miss_price", "output_price")},
            "events": ev,
            "live": self._live(self.feed.frame),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "LlamaPanel/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        panel = getattr(self.server, "panel", None)
        if panel is None:
            return self._json({"ok": False, "error": "backend not ready"}, 500)
        p = self.path.split("?", 1)[0]
        q = parse_qs(urlparse(self.path).query)
        if p in ("/", "/index.html"):
            try:
                with open(resource("panel.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/state":
            try:
                since = None
                if "trace_since" in q:
                    try:
                        since = int(q["trace_since"][0])
                    except Exception:
                        since = None
                return self._json(panel.state(trace_since=since))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/settings":
            return self._json({"ok": True, "settings": panel.settings.dict})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        panel = getattr(self.server, "panel", None)
        if panel is None:
            return self._json({"ok": False, "error": "backend not ready"}, 500)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        p = self.path.split("?", 1)[0]
        if p == "/api/settings":
            changed = panel.settings.update(body)
            if "host" in changed or "remote_path" in changed or "ssh_port" in changed:
                panel.push_event("system", "连接设置已变更，正在重连数据源…")
                try:
                    panel.feed.deploy()
                    panel.feed.spawn()
                except Exception:
                    pass
            return self._json({"ok": True, "changed": changed})
        if p == "/api/reset":
            with panel.mu:
                panel.totals.reset()
                panel.events.clear()
                panel.active_tasks = set()
                panel.last_llama = None
            panel.push_event("system", "统计与事件已重置")
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "not found"}, 404)


def open_gui(port):
    url = "http://127.0.0.1:%d/" % port
    try:
        import webview
        webview.create_window(APP_NAME, url, width=1320, height=900, min_size=(1000, 640))
        webview.start()
        return
    except Exception:
        pass
    try:
        profile = os.path.join(data_dir(), "browser-profile")
        os.makedirs(profile, exist_ok=True)
        cf, si = no_window()
        for binname in ("msedge", "chrome"):
            try:
                r = subprocess.run(["where", binname], capture_output=True, text=True,
                                   creationflags=cf, startupinfo=si)
            except Exception:
                continue
            if r.returncode == 0 and (r.stdout or "").strip():
                subprocess.Popen(
                    [binname, "--app=" + url, "--window-size=1320,900",
                     "--user-data-dir=" + profile],
                    creationflags=cf, startupinfo=si)
                return
    except Exception:
        pass
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--no-gui", action="store_true", help="不打开窗口，仅启动 HTTP 服务（调试用）")
    ap.add_argument("--port", type=int, default=0, help="指定本地端口（默认随机）")
    args = ap.parse_args()

    settings = Settings()
    panel = Panel(settings)
    panel.push_event("system", "面板启动，正在连接 %s …" % settings.v["host"])
    try:
        panel.feed.deploy()
        panel.feed.spawn()
    except Exception:
        pass

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.panel = panel
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % port
    print("%s ready: %s" % (APP_NAME, url), flush=True)

    if args.no_gui:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        open_gui(port)
    proc = panel.feed.proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    httpd.shutdown()


if __name__ == "__main__":
    main()
