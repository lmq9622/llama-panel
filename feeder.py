#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# llama-panel-feeder.py -- 部署在 x99，向本机面板输出 JSON 行流(每 0.6s 一行)
# 解析口径与 llm-monitor-loop.py 保持一致(复用其正则)。
import os, re, sys, json, time, glob, subprocess, urllib.request
from collections import deque

# 安装向导会在远端生成 ~/llama-panel.json，这里优先读它；
# 没有配置文件时全部走原来的默认值，行为不变。
CFG_PATH = os.environ.get("LLAMA_PANEL_CONFIG") or os.path.join(
    os.path.expanduser("~"), "llama-panel.json")


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cfg_int(key, default):
    try:
        return int(CFG.get(key) or default)
    except Exception:
        return default


CFG = load_cfg()

LOG = CFG.get("backend_log") or "/home/lmq/llama-27b.log"
TRACE = CFG.get("trace_path") or "/home/lmq/panel-trace.jsonl"   # proxy 写入的对话流（喂给模型什么 / 模型输出什么）
PORT_INT = cfg_int("port_internal", 18081)
PORT_EXT = cfg_int("port_external", 8081)
MODEL = CFG.get("alias") or "qwen3.8-27b"
HIST_N = 12
TRACE_N = 500          # 对话流保留的原始事件条数
TRACE_PRIME = 200000   # 启动时回看多少字节，面板刚连上就先有一屏历史

RE_LAUNCH  = re.compile(r"I slot launch_slot_: id\s+(\d+) \| task (\d+) \| processing task")
RE_RELEASE = re.compile(r"I slot\s+release: id\s+(\d+) \| task (\d+) \| (stop|cancel|error) processing(?:[:] n_tokens = (\d+), truncated = (\d+))?")
RE_COMMON  = re.compile(r"I slot print_timing: id\s+(\d+) \| task (-?\d+) \| (.*)")
RE_PP      = re.compile(r"prompt processing, n_tokens =\s*(\d+), progress = ([\d.]+), t =\s*([\d.]+) s / ([\d.]+) tokens per second")
RE_TG      = re.compile(r"n_gen =\s*(\d+), tg =\s*([\d.]+) t/s, tg_3s =\s*([\d.]+) t/s")
RE_PEVAL   = re.compile(r"prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens \(.*?([\d.]+) tokens per second\)")
RE_EVAL    = re.compile(r"\s+eval time =\s*([\d.]+) ms /\s*(\d+) tokens \(.*?([\d.]+) tokens per second\)")
RE_GRAPHS  = re.compile(r"graphs reused =\s*(\d+)")
RE_DRAFT   = re.compile(r"draft acceptance = ([\d.]+) \(\s*(\d+) accepted /\s*(\d+) generated\), mean len =\s*([\d.]+)")

def http_json(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

class LogTail(object):
    def __init__(self, path):
        self.path = path
        self.pos = 0
    def read_new(self):
        try:
            with open(self.path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size < self.pos:
                    f.seek(max(0, size - 400000)); self.pos = f.tell()
                else:
                    f.seek(self.pos)
                data = f.read().decode("utf-8", "replace")
                self.pos = f.tell()
                return data
        except Exception:
            return ""

class TraceTail(object):
    """增量读取 proxy 写的对话流 JSONL。

    与日志不同，这里必须处理「半行」和「轮转」两种情况：
      - 写入方是逐行 append 的，读到半行时留在缓冲区，等下次补齐；
      - 文件超过上限会轮转（变小），此时直接跳到新文件末尾，
        否则会把旧对话当成新内容重放一遍。
    """
    def __init__(self, path, prime=0):
        self.path = path
        self.buf = ""
        self.pos = 0
        try:
            size = os.path.getsize(path)
            self.pos = max(0, size - max(0, prime))
        except Exception:
            self.pos = 0
    def read_new(self):
        try:
            with open(self.path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size < self.pos:
                    self.pos = 0        # 轮转：从头读新文件
                f.seek(self.pos)
                data = f.read().decode("utf-8", "replace")
                self.pos = f.tell()
        except Exception:
            return []
        if not data:
            return []
        text = self.buf + data
        lines = text.split("\n")
        self.buf = lines.pop()          # 最后一段可能是半行
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

class Feeder(object):
    def __init__(self):
        self.tail = LogTail(LOG)
        self.slots = {}
        self.tasks = {}
        self.hist = deque(maxlen=HIST_N)
        self.prev_ctx = {}
        self.gpu_cache = ("", 0.0)
        self.last_cpu = None
        self.cpu_model = self._cpu_model()
        self.topo = self._cpu_topology()
        self.trace_tail = TraceTail(TRACE, TRACE_PRIME)
        self.trace = deque(maxlen=TRACE_N)
        self._digest(self.tail.read_new())

    def _cpu_model(self):
        try:
            with open("/proc/cpuinfo") as f:
                for ln in f:
                    if ln.lower().startswith("model name"):
                        return ln.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _cpu_topology(self):
        """返回物理核数 / 逻辑线程数 / 物理插槽数。

        os.cpu_count() 是逻辑线程数（开了超线程就是物理核的两倍），
        面板上直接当「核」显示会把 16C/32T 的机器写成 32 核，
        所以这里按 /proc/cpuinfo 的 (physical id, core id) 去重数真实物理核。
        """
        logical = os.cpu_count() or 0
        cores, sockets = set(), set()
        try:
            phys_id = core_id = None
            with open("/proc/cpuinfo") as f:
                for ln in f:
                    if not ln.strip():
                        if phys_id is not None and core_id is not None:
                            cores.add((phys_id, core_id))
                        phys_id = core_id = None
                        continue
                    k, _, v = ln.partition(":")
                    k = k.strip().lower()
                    v = v.strip()
                    if k == "physical id":
                        phys_id = v
                    elif k == "core id":
                        core_id = v
            if phys_id is not None and core_id is not None:
                cores.add((phys_id, core_id))
            sockets = set(p for p, _ in cores)
        except Exception:
            cores, sockets = set(), set()
        physical = len(cores)
        if not physical or physical > logical:
            physical = logical
        return {"cores": physical, "threads": logical,
                "sockets": len(sockets) or (1 if logical else 0)}

    def _new_rec(self, sid, task):
        return {"task": task, "phase": "prompt", "pp": None, "tg": None,
                "fin": {}, "release": None, "C": None, "t0": time.time()}

    def _digest(self, data):
        if not data:
            return
        for ln in data.splitlines():
            m = RE_LAUNCH.search(ln)
            if m:
                self.slots[int(m.group(1))] = self._new_rec(int(m.group(1)), int(m.group(2)))
                continue
            m = RE_RELEASE.search(ln)
            if m:
                sid, task = int(m.group(1)), int(m.group(2))
                n_tok, trunc = int(m.group(4)), int(m.group(5))
                rec = self.slots.get(sid)
                if rec is not None and rec["task"] == task:
                    rec["phase"] = "idle"; rec["kind"] = m.group(3)
                    rec["release"] = (n_tok, trunc)
                    self._finalize(sid, rec)
                self.prev_ctx[sid] = n_tok
                continue
            m = RE_COMMON.search(ln)
            if not m:
                continue
            sid, task = int(m.group(1)), int(m.group(2))
            body = m.group(3)
            rec = self.slots.get(sid)
            if rec is None or (task >= 0 and rec["task"] != task):
                rec = self._new_rec(sid, task); self.slots[sid] = rec
            pp = RE_PP.search(body)
            if pp:
                rec["phase"] = "prompt"; rec["pp"] = (int(pp.group(1)), float(pp.group(2)), float(pp.group(3)), float(pp.group(4))); continue
            tg = RE_TG.search(body)
            if tg:
                rec["phase"] = "decode"; rec["tg"] = (int(tg.group(1)), float(tg.group(2)), float(tg.group(3))); continue
            pe = RE_PEVAL.search(body)
            if pe:
                rec["fin"]["pp_n"] = int(pe.group(2)); rec["fin"]["pp_tps"] = float(pe.group(3)); continue
            ev = RE_EVAL.search(body)
            if ev:
                rec["fin"]["gen"] = int(ev.group(2)); rec["fin"]["gen_tps"] = float(ev.group(3)); continue
            g = RE_GRAPHS.search(body)
            if g:
                rec["fin"]["graphs"] = int(g.group(1)); continue
            d = RE_DRAFT.search(body)
            if d:
                rec["fin"]["draft"] = float(d.group(1)); rec["fin"]["draft_acc"] = int(d.group(2))
                rec["fin"]["draft_gen"] = int(d.group(3)); rec["fin"]["mean_len"] = float(d.group(4))

    def _finalize(self, sid, rec):
        task = rec["task"]
        pp_n = rec["fin"].get("pp_n"); gen = rec["fin"].get("gen")
        rel = rec["release"]; rel_n = rel[0] if rel else None; rel_tr = rel[1] if rel else None
        C = rec["C"]
        if C is None and rel_n is not None and pp_n is not None and gen is not None and rel_tr != 1:
            C = max(rel_n - pp_n - gen, 0)
        if C is None:
            C = self.prev_ctx.get(sid)
        if pp_n is not None:
            inp = (C or 0) + pp_n
            hit = ((C or 0) / inp * 100.0) if inp else 0.0
        else:
            inp, hit = None, None
        self.tasks[task] = {"task": task, "t": time.strftime("%H:%M:%S"), "kind": rec.get("kind", "stop"),
                            "C": C, "pp": pp_n, "in": inp, "hit": hit, "gen": gen,
                            "gen_tps": rec["fin"].get("gen_tps"), "pp_tps": rec["fin"].get("pp_tps"),
                            "draft": rec["fin"].get("draft"), "rel": rel_n, "trunc": rel_tr,
                            "dur": round(time.time() - rec.get("t0", time.time()), 1)}
        self.hist.append(self.tasks[task])

    def read_gpus(self):
        now = time.time()
        if now - self.gpu_cache[1] < 2.0 and self.gpu_cache[0]:
            return self._parse_gpu(self.gpu_cache[0])
        args = ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
                "--format=csv,noheader"]
        envs = [None]
        # NVML 库与内核驱动版本不匹配时（driver/library version mismatch），
        # 优先使用与内核模块版本匹配的 libnvidia-ml（x99: ~/nvml-580.173.02）
        match_dir = os.path.expanduser("~/nvml-580.173.02")
        if os.path.isdir(match_dir):
            lp = match_dir + ((":" + os.environ["LD_LIBRARY_PATH"]) if os.environ.get("LD_LIBRARY_PATH") else "")
            envs.insert(0, dict(os.environ, LD_LIBRARY_PATH=lp))
        out = ""
        for env in envs:
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=5, env=env)
                out = (r.stdout or "").strip()
                if out:
                    break
            except Exception:
                out = ""
        if not out:
            out = self.gpu_cache[0] or ""
        self.gpu_cache = (out, now)
        return self._parse_gpu(out)

    def _parse_gpu(self, s):
        res = []
        def _i(x):
            try: return int(float(x.split()[0]))
            except Exception: return 0
        def _f(x):
            try: return float(x.split()[0])
            except Exception: return 0.0
        for ln in (s or "").splitlines():
            p = [x.strip() for x in ln.split(",")]
            if len(p) >= 8:
                res.append({"idx": _i(p[0]), "name": p[1], "util": _i(p[2]),
                            "mem_used": _i(p[3]), "mem_total": _i(p[4]), "temp": _i(p[5]),
                            "power": _f(p[6]), "pwr_limit": _f(p[7])})
        return res

    def _cpu_sample(self):
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()[1:]
            idle = int(parts[3]) + (int(parts[4]) if len(parts) > 4 else 0)
            total = sum(int(x) for x in parts)
            return (total, idle)
        except Exception:
            return None

    def cpu_usage(self):
        cur = self._cpu_sample()
        if cur is None:
            return None
        usage = None
        if self.last_cpu is not None:
            pt, pi = self.last_cpu
            dt = cur[0] - pt; di = cur[1] - pi
            if dt > 0:
                usage = max(0.0, min(100.0, (1.0 - di / dt) * 100.0))
        self.last_cpu = cur
        return round(usage, 1) if usage is not None else None

    def cpu_temp(self):
        best = None
        for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
            try:
                v = int(open(z).read().strip())
                if 1000 <= v <= 150000:
                    c = v / 1000.0
                    if best is None or c > best:
                        best = c
            except Exception:
                pass
        return round(best, 1) if best is not None else None

    def meminfo_mb(self):
        d = {}
        try:
            with open("/proc/meminfo") as f:
                for ln in f:
                    k, _, rest = ln.partition(":")
                    try:
                        kb = int(rest.strip().split()[0])
                    except Exception:
                        continue
                    if k in ("MemTotal", "MemAvailable"):
                        d[k] = kb // 1024
        except Exception:
            pass
        return d

    def tick(self):
        data = self.tail.read_new()
        self._digest(data)
        raw_new = [l.strip() for l in (data or "").splitlines() if l.strip()][-60:]
        trace_new = self.trace_tail.read_new()
        if trace_new:
            for ev in trace_new:
                self.trace.append(ev)
            trace_new = trace_new[-TRACE_N:]
        sj = http_json("http://127.0.0.1:%d/slots" % PORT_INT)
        live = {}
        if isinstance(sj, list):
            for s in sj:
                if not isinstance(s, dict):
                    continue
                sid = s.get("id", 0)
                nxt = (s.get("next_token") or [{}])[0] or {}
                rec = self.slots.get(sid)
                if rec is not None and rec["task"] == s.get("id_task") and s.get("n_prompt_tokens_cache") is not None:
                    rec["C"] = s.get("n_prompt_tokens_cache")
                live[sid] = {"busy": bool(s.get("is_processing")), "task": s.get("id_task"),
                             "C": s.get("n_prompt_tokens_cache"), "X": s.get("n_prompt_tokens_processed"),
                             "ctx": s.get("n_prompt_tokens"), "gen": nxt.get("n_decoded"),
                             "remain": nxt.get("n_remain"), "has_next": nxt.get("has_next_token"),
                             "params": s.get("params") or {}}
        order = [sid for sid in live.keys()] + [sid for sid in self.slots.keys() if sid not in live]
        out_slots = []
        for sid in order:
            rec = self.slots.get(sid)
            lv = live.get(sid)
            phase = rec["phase"] if rec else "unknown"
            if lv and lv.get("busy") and rec and rec.get("phase") == "prompt" and (lv.get("gen") or 0) > 0:
                phase = "decode"
            out_slots.append({"id": sid, "busy": bool(lv.get("busy")) if lv else False,
                              "task": (lv.get("task") if lv and lv.get("task") is not None else (rec.get("task") if rec else None)),
                              "phase": phase, "C": (lv or {}).get("C") if lv else (rec.get("C") if rec else None),
                              "X": (lv or {}).get("X"), "ctx": (lv or {}).get("ctx"),
                              "gen": (lv or {}).get("gen"), "remain": (lv or {}).get("remain"),
                              "has_next": (lv or {}).get("has_next"),
                              "pp": rec.get("pp") if rec else None, "tg": rec.get("tg") if rec else None,
                              "fin": (rec.get("fin") or {}) if rec else {},
                              "params": (lv or {}).get("params") or {}})
        st = http_json("http://127.0.0.1:%d/proxy/status" % PORT_EXT) or {}
        if out_slots and any(o["busy"] for o in out_slots):
            llama = "BUSY"
        elif st.get("ready"):
            llama = "READY"
        elif st.get("started"):
            llama = "LOADING"
        elif st:
            llama = "IDLE"
        else:
            llama = "DOWN"
        mi = self.meminfo_mb()
        cpu = {"usage": self.cpu_usage(), "temp": self.cpu_temp(),
               "cores": self.topo["cores"], "threads": self.topo["threads"],
               "sockets": self.topo["sockets"],
               "model": self.cpu_model, "mem_total": mi.get("MemTotal"), "mem_avail": mi.get("MemAvailable")}
        if mi.get("MemTotal") and mi.get("MemAvailable") is not None:
            cpu["mem_used"] = mi["MemTotal"] - mi["MemAvailable"]
        frame = {"ts": time.strftime("%H:%M:%S"), "model": MODEL, "llama": llama,
                 "raw_new": raw_new, "trace_new": trace_new,
                 "proxy": {"ready": bool(st.get("ready")), "started": bool(st.get("started")),
                           "loading": bool(st.get("loading")), "error": st.get("error", ""),
                           "uptime_s": int(st.get("uptime_s", 0) or 0), "requests": int(st.get("requests", 0) or 0),
                           "inflight": int(st.get("inflight", 0) or 0), "load_count": int(st.get("load_count", 0) or 0),
                           "np": st.get("np"), "ctx": int(st.get("ctx", 0) or 0), "mtp": st.get("mtp"),
                           "kv": st.get("kv"), "ts": st.get("ts"), "model": st.get("model", "")},
                 "slots": out_slots, "hist": list(self.hist)[-HIST_N:],
                 "gpus": self.read_gpus(), "cpu": cpu}
        sys.stdout.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

def main():
    f = Feeder()
    while True:
        try:
            f.tick()
        except Exception as e:
            try:
                sys.stdout.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "error": str(e)}, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except Exception:
                pass
        time.sleep(0.6)

if __name__ == "__main__":
    main()
