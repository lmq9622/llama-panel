#!/usr/bin/env python3
"""llama.cpp 懒加载代理（单模型 RVN-27B）

- 外部监听 0.0.0.0:8080 和 0.0.0.0:8081（两个端口行为一致）
- 后端 127.0.0.1:18081，首次推理请求时才启动（懒加载）
- /health、/v1/models、/proxy/status 不触发模型加载
- 模型启动后常驻，不自动卸载
- 字节级中继，SSE 流在长 prefill 静默时注入 keep-alive chunk
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

PORTS_EXT = [8080, 8081]
PORT_INT = 18081

# 默认路径都在下面，安装向导会在远端生成 ~/llama-panel.json 覆盖它们，
# 没有配置文件时全部走默认值，行为与之前完全一致。
CFG_PATH = os.environ.get("LLAMA_PANEL_CONFIG") or os.path.join(
    os.path.expanduser("~"), "llama-panel.json")


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


CFG = load_cfg()

LLAMA_BIN = CFG.get("llama_bin") or "/home/lmq/llama.cpp/build/bin/llama-server"
MODEL = CFG.get("model_file") or "/home/lmq/models/RVN-Q4_K_M-mtp.gguf"
MMPROJ = CFG.get("mmproj_file") or "/home/lmq/models/mmproj-Qwen3.8-27B-Q8_0.gguf"
BACKEND_LOG = CFG.get("backend_log") or "/home/lmq/llama-27b.log"
CHAT_TEMPLATE = CFG.get("chat_template") or "/home/lmq/qwen3.jinja"
ALIAS = CFG.get("alias") or "qwen3.8-27b"

CMD = [
    LLAMA_BIN,
    "-m", MODEL,
    "-mm", MMPROJ,
    "-ngl", "99",
    "-sm", "tensor",
    "-ts", "1/1",              # 实测 1:1 最快（不均匀 -10~13%）
    "-fa", "on",
    "-b", "2048", "-ub", "2048",
    "-ctk", "q4_0", "-ctv", "q4_0",
    "-c", "204800",            # 2 × 102400
    "-np", "2",
    "--spec-type", "draft-mtp",
    "--spec-draft-n-max", "1", # 实测 n-max=1 最快
    "--reasoning-format", "deepseek",  # 把  thinking 分离到 reasoning_content
    "--chat-template-file", CHAT_TEMPLATE,
    "--host", "127.0.0.1",
    "--port", str(PORT_INT),
    "--alias", ALIAS,
]

# 额外启动参数（可选）：配置文件里写 extra_args: ["--foo", "bar"]
if isinstance(CFG.get("extra_args"), list):
    CMD.extend([str(x) for x in CFG["extra_args"]])

_lock = threading.Lock()
_state = {
    "started": False, "ready": False, "proc": None, "error": "",
    "start_time": time.time(), "requests": 0, "load_count": 0,
    "ready_since": 0.0, "inflight": 0,
}


def _log(msg):
    print(f"[proxy] {time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# 对话流记录（喂给模型什么 / 模型输出什么）
#
# llama.cpp 自己的日志只有 timing 数字，不含任何请求或生成内容，所以「对话流」
# 只能在这里采集：请求进来时把归一化后的 messages 落一行 in，SSE 输出边中继边
# 抽取 delta 文本落 out/think，结束落一行 end。写成 JSONL，feeder 增量读取后
# 传给本地面板，面板就能显示一个真正的流式终端。
# ---------------------------------------------------------------------------
TRACE_PATH = CFG.get("trace_path") or "/home/lmq/panel-trace.jsonl"
TRACE_MAX_BYTES = 4 * 1024 * 1024      # 超过就轮转，只留后一半
TRACE_KEEP_BYTES = 2 * 1024 * 1024
TRACE_MSG_LIMIT = 16                   # 最多记录多少条消息
TRACE_TEXT_LIMIT = 2000                # 单条消息最多留多少字符
TRACE_TOTAL_LIMIT = 12000              # 整段 prompt 最多留多少字符
TRACE_FLUSH_CHARS = 256                # 攒够这么多字符就落盘
TRACE_FLUSH_SEC = 0.4                  # 或者攒够这么久就落盘

_trace_lock = threading.RLock()
_trace_state = {"seq": 0, "req": 0}


def _clip(text, limit):
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（已截断，原始 {len(text)} 字符）"


def _trace_rotate():
    try:
        if os.path.getsize(TRACE_PATH) <= TRACE_MAX_BYTES:
            return
        with open(TRACE_PATH, "rb") as f:
            f.seek(-TRACE_KEEP_BYTES, os.SEEK_END)
            tail = f.read()
        nl = tail.find(b"\n")           # 从完整的一行开始，避免留下半行 JSON
        if nl >= 0:
            tail = tail[nl + 1:]
        with open(TRACE_PATH, "wb") as f:
            f.write(tail)
        _log("trace rotated")
    except Exception:
        pass


def trace_emit(kind, **kw):
    with _trace_lock:
        _trace_state["seq"] += 1
        kw["n"] = _trace_state["seq"]
    kw["t"] = time.strftime("%H:%M:%S")
    kw["k"] = kind
    line = json.dumps(kw, ensure_ascii=False) + "\n"
    try:
        with _trace_lock:
            with open(TRACE_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        _trace_rotate()
    except Exception:
        pass


def _content_text(content):
    """把 OpenAI 的 content（可能是字符串或分段数组）压成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("text") is not None:
                    parts.append(str(p["text"]))
                elif p.get("type") == "image_url":
                    parts.append("[图片]")
                else:
                    parts.append("<%s>" % p.get("type"))
            else:
                parts.append(str(p))
        return "".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _reasoning_text(item):
    """把 /v1/responses 的 reasoning item 压成可读文本。"""
    parts = []
    for key in ("summary", "content"):
        v = item.get(key)
        if isinstance(v, list):
            for p in v:
                if isinstance(p, dict) and p.get("text") is not None:
                    parts.append(str(p["text"]))
                elif isinstance(p, str):
                    parts.append(p)
        elif isinstance(v, str) and v:
            parts.append(v)
    return "".join(parts)


def responses_input(data):
    """把 /v1/responses 的 instructions + input 归一成 messages 列表，
    这样 agent 客户端的每一轮也能在面板里看清到底喂了什么。"""
    out = []
    ins = data.get("instructions")
    if isinstance(ins, str) and ins.strip():
        out.append({"role": "system", "content": ins})
    elif isinstance(ins, list) and ins:
        out.append({"role": "system", "content": ins})
    inp = data.get("input")
    if isinstance(inp, str):
        if inp:
            out.append({"role": "user", "content": inp})
        return out
    if not isinstance(inp, list):
        return out
    for it in inp:
        if not isinstance(it, dict):
            out.append({"role": "?", "content": str(it)})
            continue
        t = it.get("type")
        if t in (None, "message"):
            out.append({"role": str(it.get("role") or "user"), "content": it.get("content")})
        elif t == "function_call":
            out.append({"role": "assistant",
                        "content": "调用 %s(%s)" % (it.get("name"), it.get("arguments"))})
        elif t == "function_call_output":
            out.append({"role": "tool", "content": it.get("output")})
        elif t in ("reasoning", "reasoning_text"):
            out.append({"role": "reasoning", "content": _reasoning_text(it)})
        elif t in ("input_text", "output_text", "text"):
            out.append({"role": "user", "content": it.get("text")})
        else:
            try:
                out.append({"role": str(t or "?"), "content": json.dumps(it, ensure_ascii=False)})
            except Exception:
                out.append({"role": str(t or "?"), "content": str(it)})
    return out


def trace_begin(path, body):
    """请求开头记一行 in，返回该请求的流式上下文。"""
    with _trace_lock:
        _trace_state["req"] += 1
        rid = _trace_state["req"]
    ctx = {"id": rid, "pk": None, "pv": "", "t": time.time(),
           "body": b"", "why": "", "usage": None}
    try:
        data = json.loads(body)
    except Exception:
        return ctx
    if not isinstance(data, dict):
        return ctx
    msgs, total, dropped = [], 0, 0
    raw = data.get("messages")
    # /v1/responses（Codex / 各类 agent 客户端走的就是它）的输入在 instructions + input 里，
    # 以前这里只认 messages，导致这些轮次显示「0 条消息 · 0 字符」，输入整段看不见。
    if not (isinstance(raw, list) and raw):
        raw = responses_input(data)
    if isinstance(raw, list) and raw:
        if len(raw) > TRACE_MSG_LIMIT:
            dropped = len(raw) - TRACE_MSG_LIMIT
        for m in raw[-TRACE_MSG_LIMIT:]:
            if not isinstance(m, dict):
                continue
            txt = _clip(_content_text(m.get("content")), TRACE_TEXT_LIMIT)
            total += len(txt)
            msgs.append({"r": str(m.get("role") or "?"), "x": txt})
    if not msgs:
        p = data.get("prompt")
        if p is not None:
            txt = _clip(p if isinstance(p, str) else json.dumps(p, ensure_ascii=False),
                        TRACE_TOTAL_LIMIT)
            total = len(txt)
            msgs.append({"r": "prompt", "x": txt})
    params = {}
    for k in ("temperature", "top_p", "top_k", "min_p", "repeat_penalty",
              "presence_penalty", "frequency_penalty", "max_tokens", "n_predict",
              "seed", "stream", "stop", "chat_template_kwargs",
              "reasoning_budget_tokens", "reasoning_format", "tools", "tool_choice"):
        if k in data:
            v = data[k]
            params[k] = ("%d 个工具" % len(v)) if k == "tools" and isinstance(v, list) else v
    tools = data.get("tools")
    trace_emit("in", id=rid, path=path, model=data.get("model") or ALIAS,
               stream=bool(data.get("stream")), msgs=msgs, params=params,
               chars=total, dropped=dropped,
               tools=len(tools) if isinstance(tools, list) else 0)
    return ctx


def trace_drain(ctx):
    """把攒着的增量文本落成一条 out/think 记录。"""
    if ctx.get("pv"):
        trace_emit(ctx["pk"], id=ctx["id"], x=ctx["pv"])
    ctx["pk"] = None
    ctx["pv"] = ""
    ctx["t"] = time.time()


def trace_delta(ctx, kind, text):
    if not text:
        return
    if ctx.get("pk") != kind:
        trace_drain(ctx)
        ctx["pk"] = kind
        ctx["pv"] = text
        ctx["t"] = time.time()
    else:
        ctx["pv"] += text
    if len(ctx["pv"]) >= TRACE_FLUSH_CHARS or (time.time() - ctx["t"]) >= TRACE_FLUSH_SEC:
        trace_drain(ctx)


def trace_tool_calls(ctx, calls):
    """工具调用（function calling）的参数是模型解码出来的，算输出的一部分。"""
    if not isinstance(calls, list):
        return
    for tc in calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        if fn.get("name"):
            trace_delta(ctx, "tool", "\n[调用 %s] " % fn["name"])
        if fn.get("arguments"):
            trace_delta(ctx, "tool", fn["arguments"])


def trace_obj(ctx, obj):
    """从一条响应对象里抽取正文/思考增量，兼容流式与非流式。"""
    if not isinstance(obj, dict):
        return
    ty = obj.get("type")
    if isinstance(ty, str):            # /v1/responses 的事件格式
        if ty.endswith("output_text.delta"):
            trace_delta(ctx, "out", obj.get("delta"))
            return
        if ty.endswith("reasoning_summary_text.delta") or ty.endswith("reasoning_text.delta"):
            trace_delta(ctx, "think", obj.get("delta"))
            return
        # 工具调用参数也是解码出来的内容，以前这一整块根本没记进对话流
        if ty.endswith("function_call_arguments.delta"):
            trace_delta(ctx, "tool", obj.get("delta"))
            return
        if ty.endswith("output_item.added"):
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                trace_delta(ctx, "tool", "\n[调用 %s] " % (item.get("name") or "?"))
            return
        if ty.endswith("completed") and isinstance(obj.get("response"), dict):
            u = obj["response"].get("usage")
            if isinstance(u, dict):
                ctx["usage"] = u
            return
    u = obj.get("usage")
    if isinstance(u, dict):
        ctx["usage"] = u
    ch = obj.get("choices")
    if not isinstance(ch, list) or not ch:
        return
    c0 = ch[0]
    if not isinstance(c0, dict):
        return
    if c0.get("finish_reason"):
        ctx["why"] = c0["finish_reason"]
    delta = c0.get("delta")
    if isinstance(delta, dict):
        trace_delta(ctx, "think", delta.get("reasoning_content"))
        trace_delta(ctx, "out", delta.get("content"))
        trace_tool_calls(ctx, delta.get("tool_calls"))
        return
    msg = c0.get("message")
    if isinstance(msg, dict):
        trace_delta(ctx, "think", msg.get("reasoning_content"))
        trace_delta(ctx, "out", msg.get("content"))
        trace_tool_calls(ctx, msg.get("tool_calls"))
        return
    if c0.get("text"):
        trace_delta(ctx, "out", c0["text"])


def trace_sse_chunk(ctx, payload):
    """chunked 传输里拿到一段完整 SSE 载荷后解析出增量文本。"""
    try:
        text = payload.decode("utf-8", "replace")
    except Exception:
        return
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            trace_obj(ctx, json.loads(data))
        except Exception:
            continue


def trace_finish(ctx):
    trace_drain(ctx)
    kw = {"id": ctx["id"], "why": ctx.get("why") or ""}
    u = ctx.get("usage")
    if isinstance(u, dict):
        kw["in_tok"] = u.get("prompt_tokens")
        kw["out_tok"] = u.get("completion_tokens")
    trace_emit("end", **kw)


def backend_ready():
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT_INT}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_backend(timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if backend_ready():
            return True
        if _state["proc"] is not None and _state["proc"].poll() is not None:
            _state["error"] = f"llama-server exited rc={_state['proc'].returncode}"
            _log(_state["error"])
            return False
        time.sleep(1)
    _state["error"] = "timeout waiting for backend"
    return False


def start_backend():
    """首次推理请求时启动 llama-server（线程安全，等待就绪）。"""
    with _lock:
        if _state["ready"]:
            if backend_ready():
                return True
            # 后端被外部杀掉：重置状态，重新拉起
            _state["ready"] = False
            _state["started"] = False
            _state["proc"] = None
            _state["error"] = "backend died, restarting"
        if _state["started"]:
            started = True
        else:
            _state["started"] = True
            started = False

    if not started:
        _log(f"lazy start: {' '.join(CMD)}")
        logf = open(BACKEND_LOG, "ab", buffering=0)
        _state["proc"] = subprocess.Popen(CMD, stdout=logf, stderr=logf,
                                          start_new_session=True)
        _state["load_count"] += 1

    ok = wait_for_backend()
    with _lock:
        _state["ready"] = ok
        if ok:
            _state["ready_since"] = time.time()
    _log(f"backend ready={ok} err={_state['error']!r}")
    return ok


def read_http_request(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        data += chunk

    head_end = data.index(b"\r\n\r\n") + 4
    head = data[:head_end].decode("utf-8", errors="replace")
    body = data[head_end:]

    lines = head.split("\r\n")
    try:
        method, path, _ = lines[0].split(" ", 2)
    except ValueError:
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    need = int(headers.get("content-length", 0))
    while len(body) < need:
        chunk = sock.recv(need - len(body))
        if not chunk:
            break
        body += chunk
    return method, path, headers, body


def is_stream_request(method, path, body):
    if method != "POST":
        return False
    if path not in ("/v1/chat/completions", "/v1/responses", "/v1/completions"):
        return False
    try:
        return bool(json.loads(body).get("stream"))
    except Exception:
        return False


def static_json(sock, obj, status=200):
    body = json.dumps(obj).encode()
    sock.sendall(
        f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    )


def relay_response(client_sock, remote_sock, is_stream, tctx=None):
    """字节级中继；SSE 在 chunk 边界注入保活 ping。"""
    send_lock = threading.Lock()
    st = {"last_data": time.time(), "done": False, "pinged": 0, "headers_done": False}

    def send(b):
        with send_lock:
            try:
                client_sock.sendall(b)
            except Exception:
                pass

    tracker = {"state": "size", "need": 0, "buf": b""}

    def track(data):
        t = tracker
        if not is_stream:
            # 非流式响应：整包留到最后一次解析，中途的字节不可能是完整 JSON
            if tctx is not None:
                tctx["body"] += data
                if len(tctx["body"]) > 8 * 1024 * 1024:
                    tctx["body"] = b""
            return
        t["buf"] += data
        while True:
            if t["state"] == "size":
                idx = t["buf"].find(b"\r\n")
                if idx < 0:
                    if len(t["buf"]) > 64:
                        t["state"] = "dead"
                    return
                line = t["buf"][:idx]
                t["buf"] = t["buf"][idx + 2:]
                try:
                    n = int(line.split(b";")[0].strip(), 16)
                except ValueError:
                    t["state"] = "dead"
                    return
                if n == 0:
                    t["state"] = "done"
                    return
                t["need"] = n
                t["state"] = "data"
            elif t["state"] == "data":
                if len(t["buf"]) >= t["need"] + 2:
                    payload = t["buf"][:t["need"]]
                    t["buf"] = t["buf"][t["need"] + 2:]
                    t["state"] = "size"
                    if tctx is not None:
                        trace_sse_chunk(tctx, payload)
                else:
                    return
            else:
                return

    def pinger():
        while not st["done"]:
            time.sleep(1.0)
            if st["done"]:
                return
            # 必须等响应头已转发后才能注入，否则会污染 HTTP 状态行
            if st["headers_done"] and tracker["state"] == "size" and time.time() - st["last_data"] > 8.0:
                with send_lock:
                    try:
                        ping = b": ping\n\n"
                        client_sock.sendall(b"%x\r\n" % len(ping) + ping + b"\r\n")
                        st["pinged"] += 1
                    except Exception:
                        pass

    try:
        if is_stream:
            threading.Thread(target=pinger, daemon=True).start()
        headers_done = False
        pre = b""
        while True:
            data = remote_sock.recv(65536)
            if not data:
                break
            send(data)
            if headers_done:
                st["last_data"] = time.time()
                track(data)
            else:
                pre += data
                idx = pre.find(b"\r\n\r\n")
                if idx >= 0:
                    headers_done = True
                    st["headers_done"] = True
                    rest = pre[idx + 4:]
                    if rest:
                        st["last_data"] = time.time()
                        track(rest)
                    pre = b""
    except Exception as e:
        _log(f"relay error: {e}")
    finally:
        st["done"] = True
        if is_stream and st["pinged"]:
            _log(f"stream: injected {st['pinged']} keep-alive pings")
        if tctx is not None:
            if not is_stream and tctx["body"]:
                try:
                    trace_obj(tctx, json.loads(tctx["body"].decode("utf-8", "replace")))
                except Exception:
                    pass
            trace_finish(tctx)
        try:
            client_sock.shutdown(socket.SHUT_WR)
        except Exception:
            pass


# 思考强度三档 -> 思考 token 预算（llama.cpp 的 reasoning_budget_tokens）
# 模型自带模板只有 enable_thinking、没有 effort 概念，所以低/高两档额外附一句思考指令，
# 效果才看得出来（不想改提示词就设 REASONING_PROMPT_HINT=0）。
REASONING_BUDGETS = {
    "minimal": int(os.environ.get("REASONING_BUDGET_MINIMAL", 256)),
    "low": int(os.environ.get("REASONING_BUDGET_LOW", 512)),
    "medium": int(os.environ.get("REASONING_BUDGET_MEDIUM", 2048)),
    "high": int(os.environ.get("REASONING_BUDGET_HIGH", 8192)),
}
REASONING_PROMPT_HINT = os.environ.get("REASONING_PROMPT_HINT", "1") != "0"
REASONING_HINTS = {
    "minimal": "\uff08思考强度：最低\uff09请几乎不推理，直接给出答案。",
    "low": "\uff08思考强度：低\uff09请用尽量简短的推理，快速给出结论，不要展开长篇分析。",
    "high": "\uff08思考强度：高\uff09请先在思考中充分、深入、多角度地推理验证，再给出最终答案。",
}


def _apply_reasoning_hint(data, effort):
    hint = REASONING_HINTS.get(effort)
    if not hint or not REASONING_PROMPT_HINT:
        return False
    msgs = data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    first = msgs[0]
    if isinstance(first, dict) and first.get("role") == "system":
        content = first.get("content")
        if isinstance(content, str):
            if hint in content:
                return False
            msgs[0] = dict(first, content=content + chr(10) + chr(10) + hint)
            return True
        if isinstance(content, list):
            msgs[0] = dict(first, content=list(content) + [{"type": "text", "text": hint}])
            return True
        return False
    msgs.insert(0, {"role": "system", "content": hint})
    return True


def ensure_stream_usage(method, path, body):
    """流式请求强制带上 stream_options.include_usage。

    不加这个，后端不会在收尾补一个 usage 分片，面板就拿不到这一轮的
    输入 / 输出 token 数（对话流结尾那行一直是空的）。加了之后 usage 里的
    completion_tokens 已经是「这一轮解码出来的全部 token」——思考、正文、
    工具调用参数都算在里面，和 llama.cpp 日志里 eval time 的 token 数一致。
    """
    if method != "POST" or path not in ("/v1/chat/completions", "/v1/completions"):
        return body
    try:
        data = json.loads(body)
    except Exception:
        return body
    if not isinstance(data, dict) or not data.get("stream"):
        return body
    so = data.get("stream_options")
    if not isinstance(so, dict):
        so = {}
    if so.get("include_usage") is True:
        return body
    so["include_usage"] = True
    data["stream_options"] = so
    try:
        return json.dumps(data, ensure_ascii=False).encode()
    except Exception:
        return body


def normalize_reasoning(method, path, body, headers):
    """把 LobeChat/qwen 的思考参数翻译成 llama.cpp 认识的形式。

    - thinking.type = enabled/auto -> chat_template_kwargs.enable_thinking = true
    - thinking.type = disabled     -> enable_thinking = false（不再带预算）
    - reasoning_effort low/medium/high -> reasoning_budget_tokens + 思考指令（三档）
    - thinking.budget_tokens（LobeChat“思考预算”滑杆）在没有三档时兜底
    """
    if method != "POST" or not body or "completions" not in path:
        return body, headers
    try:
        data = json.loads(body)
    except Exception:
        return body, headers
    if not isinstance(data, dict):
        return body, headers

    thinking = data.get("thinking") if isinstance(data.get("thinking"), dict) else None
    raw_effort = data.get("reasoning_effort") or data.get("reasoningEffort")
    effort = ""
    if isinstance(raw_effort, str):
        effort = raw_effort.lower().replace(chr(34), "").replace(chr(39), "").strip()

    enable = None
    if thinking is not None:
        t = str(thinking.get("type") or "").lower()
        enable = t in ("enabled", "auto", "on", "true", "1")
    elif effort:
        enable = effort not in ("none", "off", "disabled", "false", "0")

    budget = None
    if enable:
        if effort in REASONING_BUDGETS:
            budget = REASONING_BUDGETS[effort]
        else:
            bt = data.get("reasoning_budget_tokens")
            if bt is None and thinking is not None:
                bt = thinking.get("budget_tokens")
            if isinstance(bt, bool):
                bt = None
            if isinstance(bt, (int, float)) and bt > 0:
                budget = int(bt)
            elif isinstance(bt, str) and bt.strip().isdigit() and int(bt) > 0:
                budget = int(bt)

    changed = False
    for k in ("thinking", "thinkingBudget", "reasoning_effort", "reasoningEffort",
              "enabledContextCaching", "verbosity", "textVerbosity", "urlContext", "apiMode"):
        if k in data:
            data.pop(k, None)
            changed = True

    if enable is not None:
        kw = data.get("chat_template_kwargs")
        if not isinstance(kw, dict):
            kw = {}
        if kw.get("enable_thinking") != enable:
            kw["enable_thinking"] = enable
            data["chat_template_kwargs"] = kw
            changed = True

    if budget is not None:
        if data.get("reasoning_budget_tokens") != budget:
            data["reasoning_budget_tokens"] = budget
            changed = True
    elif enable is False and "reasoning_budget_tokens" in data:
        data.pop("reasoning_budget_tokens", None)
        changed = True

    if enable and effort in REASONING_HINTS:
        if _apply_reasoning_hint(data, effort):
            changed = True

    if changed:
        _log("reasoning -> enable_thinking=%s budget=%s effort=%r thinking=%r" % (enable, budget, effort, thinking))
        nb = json.dumps(data, ensure_ascii=False).encode()
        headers = dict(headers)
        headers["content-length"] = str(len(nb))
        return nb, headers
    return body, headers


def handle_client(client_sock, addr):
    try:
        client_sock.settimeout(30)
        req = read_http_request(client_sock)
        if req is None:
            return
        method, path, headers, body = req
        client_sock.settimeout(None)

        # 静态端点：不触发模型加载
        if path in ("/health", "/v1/health"):
            static_json(client_sock, {"status": "ok",
                                      "model_loaded": _state["ready"],
                                      "loading": _state["started"] and not _state["ready"]})
            return
        if path.endswith("/v1/models") or path.endswith("/models"):
            static_json(client_sock, {"object": "list", "data": [{
                "id": ALIAS, "object": "model", "created": int(time.time()),
                "owned_by": "llama.cpp"}]})
            return
        if path == "/proxy/status":
            now = time.time()
            static_json(client_sock, {
                "ready": _state["ready"], "started": _state["started"],
                "loading": _state["started"] and not _state["ready"],
                "error": _state["error"],
                "backend": f"127.0.0.1:{PORT_INT}",
                "ports": PORTS_EXT,
                "uptime_s": round(now - _state["start_time"], 1),
                "requests": _state["requests"],
                "load_count": _state["load_count"],
                "ready_s": round(now - _state["ready_since"], 1) if _state["ready"] else 0,
                "inflight": _state["inflight"],
                "model": MODEL,
                "ctx": 204800, "np": 2, "mtp": 1, "kv": "q4_0", "ts": "1/1",
            })
            return
        if path == "/proxy/unload":
            with _lock:
                proc = _state.get("proc")
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=15)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                _state["proc"] = None
                _state["ready"] = False
                _state["started"] = False
                _state["error"] = ""
                _state["ready_since"] = 0.0
            _log("unloaded backend on request")
            static_json(client_sock, {"status": "unloaded", "ready": False})
            return

        # 真实推理请求：懒加载
        with _lock:
            _state["requests"] += 1
            _state["inflight"] += 1
        if not start_backend():
            with _lock:
                _state["inflight"] -= 1
            static_json(client_sock, {"error": _state["error"] or "backend failed to start"}, 502)
            return

        try:
            remote_sock = socket.create_connection(("127.0.0.1", PORT_INT), timeout=30)
        except Exception as e:
            static_json(client_sock, {"error": f"backend unavailable: {e}"}, 502)
            return
        # 长 prefill 期间上游可能数十秒无数据，recv 必须阻塞而不是 30s 超时
        remote_sock.settimeout(None)

        body, headers = normalize_reasoning(method, path, body, headers)
        body = ensure_stream_usage(method, path, body)

        # 对话流：记下这一轮到底喂了什么（normalize 之后 = 后端真实收到的内容）
        tctx = None
        if method == "POST" and path in ("/v1/chat/completions", "/v1/completions",
                                         "/v1/responses"):
            try:
                tctx = trace_begin(path, body)
            except Exception as e:
                _log(f"trace begin failed: {e}")

        out_headers = {k: v for k, v in headers.items() if k.lower() != "connection"}
        raw = f"{method} {path} HTTP/1.1\r\n"
        for k, v in out_headers.items():
            raw += f"{k}: {v}\r\n"
        raw += "Connection: close\r\n\r\n"
        remote_sock.sendall(raw.encode() + body)

        def client_to_upstream():
            try:
                while True:
                    data = client_sock.recv(65536)
                    if not data:
                        break
                    remote_sock.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    remote_sock.close()
                except Exception:
                    pass

        is_stream = is_stream_request(method, path, body)
        t1 = threading.Thread(target=relay_response,
                              args=(client_sock, remote_sock, is_stream, tctx), daemon=True)
        t2 = threading.Thread(target=client_to_upstream, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=3600)
        t2.join(timeout=3600)
        with _lock:
            _state["inflight"] = max(0, _state["inflight"] - 1)
    except Exception as e:
        _log(f"handle_client error {addr}: {e}")
    finally:
        try:
            client_sock.close()
        except Exception:
            pass


def serve(port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(128)
    _log(f"listening on 0.0.0.0:{port}")
    while True:
        try:
            client_sock, addr = srv.accept()
        except Exception:
            continue
        threading.Thread(target=handle_client, args=(client_sock, addr), daemon=True).start()


def cleanup(sig, frame):
    _log("shutting down, stopping backend")
    proc = _state.get("proc")
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    sys.exit(0)


signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)

if __name__ == "__main__":
    _log(f"lazy proxy for {ALIAS}; external={PORTS_EXT} backend={PORT_INT}")
    threads = [threading.Thread(target=serve, args=(p,), daemon=True) for p in PORTS_EXT]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
