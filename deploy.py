#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# llama-panel 远端自动安装器
#
# 由 Windows 安装向导（setup.exe）调用，也可以单独在命令行运行：
#   deploy.exe --host 192.168.2.6 --user lmq --password 123456 --app-dir "C:\App"
#
# 流程：
#   1. 用密码 / 私钥 SSH 登录目标服务器（paramiko，全程无黑框）
#   2. 探测 python3、llama-server、模型、日志等真实路径
#   3. 上传 feeder.py 与 llama-proxy-v2.py（先备份远端旧文件）
#   4. 写远端 ~/llama-panel.json（已存在则合并，不覆盖用户手工改过的项）
#   5. 确保 systemd 服务存在并重启（不存在就自动创建 unit）
#   6. 检查端口监听，并在本机写入 panel-data/settings.json
import argparse
import json
import os
import sys
import tempfile
import time

APP_NAME = "Llama 监控面板"
DEFAULT_SERVICE = "llama-servers"
# 远端文件名（面板 server.py 默认也会去找 llama-panel-feeder.py）
FEEDER_NAME = "llama-panel-feeder.py"
PROXY_NAME = "llama-proxy-v2.py"
# 本地候选名：源码目录里叫 feeder.py，打包进 exe 后也放在根目录
FEEDER_SRC = ("feeder.py", "llama-panel-feeder.py")
PROXY_SRC = ("llama-proxy-v2.py", os.path.join("remote", "llama-proxy-v2.py"))
CFG_NAME = "llama-panel.json"

# 日志文件句柄：安装向导是隐藏启动本程序的（没有控制台），
# 一旦出错只能靠日志文件排错，所以所有输出都同时写一份到文件里。
LOG_FP = None


class DeployError(Exception):
    pass


def log(msg):
    text = str(msg)
    try:
        print(text, flush=True)
    except Exception:
        pass
    if LOG_FP is not None:
        try:
            LOG_FP.write(text + "\n")
            LOG_FP.flush()
        except Exception:
            pass


def open_log(path):
    global LOG_FP
    if not path:
        return
    try:
        LOG_FP = open(path, "w", encoding="utf-8-sig")
        log("=== llama-panel 远端自动安装器 %s ===" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        LOG_FP = None


def default_log_path():
    return os.path.join(tempfile.gettempdir(), "llama-panel-deploy.log")


def scan_arg(argv, name):
    """在正式解析参数之前，先把 --xxx 的值捞出来（用于提前开日志文件）。"""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return ""


def mask_argv(argv):
    """写日志前把密码打码。"""
    out = []
    hide = False
    for a in argv:
        if hide:
            out.append("***")
            hide = False
            continue
        out.append(a)
        if a in ("--password", "--sudo-password"):
            hide = True
    return out


def note_early(path, text):
    """参数没解析成功时，也要往进度文件写一行，免得向导一直转圈没提示。"""
    if not path:
        return
    try:
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig" if new else "utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def args_file_argv(path):
    """把安装向导写的 JSON 参数文件展开成命令行参数。

    走文件是为了绕开 Windows 命令行的引号 / 空格 / 中文转义问题：
    向导里密码留空时，曾经拼出「--password --remote-dir /home/lmq」这种错位参数，
    argparse 直接把 --remote-dir 当成密码值然后报错退出，向导就一直卡在进度页。
    这里空值项会被直接丢掉，不会再出现开关后面跟着另一个开关的情况。
    """
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        log("参数文件读取失败：%s（%s）" % (path, e))
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for key, val in data.items():
        opt = "--" + str(key).strip().replace("_", "-")
        if isinstance(val, bool):
            if val:
                out.append(opt)
            continue
        if val is None or val == "":
            continue
        out.extend([opt, str(val)])
    return out


def quote(s):
    """把参数安全地塞进远端 shell 命令（自己实现，避免依赖 shlex 的引号风格）。"""
    return "\"" + str(s).replace("\\", "\\\\").replace("\"", "\\\"").replace("$", "\\$").replace("`", "\\`") + "\""


def run(ssh, cmd, timeout=120):
    """执行远端命令，返回 (返回码, 标准输出, 标准错误)。"""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def run_ok(ssh, cmd, timeout=120, what=""):
    rc, out, err = run(ssh, cmd, timeout)
    if rc != 0:
        raise DeployError("%s 执行失败：%s" % (what or cmd, (err or out).strip()[:400]))
    return out.strip()


def sudo_variants(cmd, password):
    """sudo 的几种可能形态，按「成功率从高到低」排列。

    关键点：命令必须整体交给 bash -c，否则 "a; b" 里的 b 不会提权。
    """
    q = quote(cmd)
    tries = [("sudo -n bash -c " + q, None)]
    tries.append(("sudo -n " + cmd, None))
    if password:
        tries.append(("sudo -S -p \"\" bash -c " + q, password))
        tries.append(("sudo -S -p \"\" " + cmd, password))
    return tries


def sudo_attempt(ssh, cmd, password, timeout=300):
    """依次尝试各种 sudo 形态，返回 (成功?, 输出, 错误)。"""
    last = ("", "")
    for full, pw in sudo_variants(cmd, password):
        stdin, stdout, stderr = ssh.exec_command(full, timeout=timeout)
        if pw:
            try:
                stdin.write(pw + "\n")
                stdin.flush()
            except Exception:
                pass
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if rc == 0:
            return True, out, err
        last = (out, err)
    return False, last[0], last[1]


def sudo_run(ssh, password, cmd, timeout=300):
    """提权执行，失败抛异常。"""
    ok, out, err = sudo_attempt(ssh, cmd, password, timeout)
    if ok:
        return out
    msg = (err or out).strip().replace("Sorry, try again.", "密码不正确")
    if "interactive authentication is required" in msg or "a password is required" in msg:
        msg = "sudo 需要密码，但向导里没有填写密码（或密码不正确）"
    raise DeployError("sudo 执行失败：%s" % msg[:300])


def sudo_text(ssh, password, cmd, timeout=120):
    """提权执行但不关心失败，只取输出（用于收集诊断日志）。"""
    try:
        ok, out, err = sudo_attempt(ssh, cmd, password, timeout)
    except Exception:
        return ""
    return out if ok else ""


def sftp_text(sftp, text, remote_path, mode=0o644):
    tmp = remote_path + ".new"
    with sftp.open(tmp, "w") as f:
        f.write(text)
    sftp.chmod(tmp, mode)
    sftp.posix_rename(tmp, remote_path)


def upload_local(sftp, local_path, remote_path, backup_suffix):
    if not os.path.exists(local_path):
        raise DeployError("本地缺少文件：%s" % local_path)
    try:
        old = sftp.open(remote_path, "rb").read()
        with open(local_path, "rb") as f:
            new = f.read()
        if old == new:
            log("  = %s 内容一致，跳过" % remote_path)
            return False
        sftp.posix_rename(remote_path, remote_path + backup_suffix)
        log("  已备份旧文件 -> %s%s" % (remote_path, backup_suffix))
    except IOError:
        pass
    tmp = remote_path + ".new"
    sftp.put(local_path, tmp)
    sftp.chmod(tmp, 0o755)
    sftp.posix_rename(tmp, remote_path)
    log("  已上传 -> %s" % remote_path)
    return True


def first_existing(ssh, candidates):
    for c in candidates:
        rc, out, err = run(ssh, "test -e " + quote(c) + " && echo yes || echo no", timeout=30)
        if out.strip() == "yes":
            return c
    return ""


def detect(ssh, home):
    """探测远端真实路径，探测不到的留空（由远端脚本自己的默认值兜底）。"""
    info = {}
    rc, out, err = run(ssh, "command -v llama-server 2>/dev/null || true")
    info["llama_bin"] = first_existing(ssh, [
        home + "/llama.cpp/build/bin/llama-server",
        "/usr/local/bin/llama-server",
        "/opt/llama.cpp/build/bin/llama-server",
    ]) or out.strip()
    log_hits = []
    for cand in ("llama-27b.log", "llama-server.log", "llama.log"):
        if first_existing(ssh, [home + "/" + cand]):
            log_hits.append(home + "/" + cand)
    info["backend_log"] = log_hits[0] if log_hits else ""
    info["chat_template"] = first_existing(ssh, [
        home + "/qwen3.jinja", home + "/chat_template.jinja",
    ])
    rc, out, err = run(ssh, "ls -1 " + quote(home + "/models") + "/*.gguf 2>/dev/null | head -n 40 || true")
    ggufs = [x.strip() for x in out.splitlines() if x.strip()]
    models = [x for x in ggufs if "mmproj" not in os.path.basename(x).lower()]
    mmprojs = [x for x in ggufs if "mmproj" in os.path.basename(x).lower()]
    # 只有唯一候选时才敢自动填。目录里有好几个模型时，猜错的代价是
    # llama-server 直接起不来（曾经把 9B 的 mmproj 配给 27B 的模型），
    # 所以宁可留空，让远端脚本用它自己的默认值。
    if len(models) == 1:
        info["model_file"] = models[0]
    elif len(models) > 1:
        info["model_file_ambiguous"] = "%d 个候选，已跳过" % len(models)
    if len(mmprojs) == 1:
        info["mmproj_file"] = mmprojs[0]
    elif len(mmprojs) > 1:
        info["mmproj_file_ambiguous"] = "%d 个候选，已跳过" % len(mmprojs)
    return info


def ensure_unit(ssh, sudo_pw, service, user, home, proxy_path, log_path):
    """已经有同名 unit 就完全不动它；没有才生成一个新的。"""
    rc, out, err = run(ssh,
        "(systemctl cat %s >/dev/null 2>&1 || test -e /etc/systemd/system/%s.service) && echo yes || echo no"
        % (quote(service), service))
    if out.strip() == "yes":
        log("  已存在 systemd 单元 %s.service" % service)
        return False
    unit = "\n".join([
        "[Unit]",
        "Description=llama.cpp server + panel proxy (%s)" % service,
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "User=%s" % user,
        "WorkingDirectory=%s" % home,
        "ExecStart=/usr/bin/python3 %s" % proxy_path,
        "Restart=always",
        "RestartSec=5",
        "StandardOutput=append:%s" % log_path,
        "StandardError=append:%s" % log_path,
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])
    tmp = "/tmp/%s.service" % service
    sftp = ssh.open_sftp()
    sftp_text(sftp, unit, tmp, 0o644)
    sudo_run(ssh, sudo_pw, "mv %s /etc/systemd/system/%s.service" % (quote(tmp), service))
    sudo_run(ssh, sudo_pw, "systemctl daemon-reload")
    log("  已创建 systemd 单元 %s.service" % service)
    return True


def write_local_settings(app_dir, host_str, remote_dir, ssh_port):
    if not app_dir:
        return None
    data = os.path.join(app_dir, "panel-data")
    os.makedirs(data, exist_ok=True)
    path = os.path.join(data, "settings.json")
    cur = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        cur = {}
    cur["host"] = host_str
    cur["remote_path"] = remote_dir.rstrip("/") + "/" + FEEDER_NAME
    cur["ssh_port"] = int(ssh_port)
    cur.setdefault("cache_hit_price", 0.05)
    cur.setdefault("input_miss_price", 1.0)
    cur.setdefault("output_price", 5.0)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def main():
    argv = list(sys.argv[1:])
    # 参数文件（安装向导写的 JSON）排在真实命令行前面，真实命令行可以覆盖它
    argv = args_file_argv(scan_arg(argv, "--args-file")) + argv
    open_log(scan_arg(argv, "--log") or default_log_path())
    log("命令行参数：%s" % (mask_argv(argv),))

    ap = argparse.ArgumentParser(description=APP_NAME + " 远端自动安装")
    ap.add_argument("--host", required=True, help="服务器地址（IP 或域名）")
    ap.add_argument("--port", type=int, default=22, help="SSH 端口，默认 22")
    ap.add_argument("--user", required=True, help="SSH 登录用户（同时用于 sudo）")
    ap.add_argument("--password", default="", help="SSH 登录密码")
    ap.add_argument("--sudo-password", default="", help="sudo 密码，留空表示与登录密码相同")
    ap.add_argument("--key", default="", help="改用私钥登录时的私钥路径（可选）")
    ap.add_argument("--service", default=DEFAULT_SERVICE, help="远端 systemd 服务名")
    ap.add_argument("--remote-dir", default="", help="远端安装目录，默认取远端 $HOME")
    ap.add_argument("--app-dir", default="", help="本机面板目录，用于写入 panel-data/settings.json")
    ap.add_argument("--feeder", default="", help="本地 feeder.py 路径（默认取打包内文件）")
    ap.add_argument("--proxy", default="", help="本地 llama-proxy-v2.py 路径（默认取打包内文件）")
    ap.add_argument("--result", default="", help="把执行结果写成 JSON 给安装向导读取")
    ap.add_argument("--progress", default="", help="把每一步实时写成纯文本，供安装向导滚动显示")
    ap.add_argument("--no-restart", action="store_true", help="只上传，不重启服务")
    ap.add_argument("--args-file", default="", help="从 JSON 文件读取参数（安装向导用，避免命令行转义问题）")
    ap.add_argument("--log", default="", help="日志文件路径，默认写到系统临时目录")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        log("参数解析失败：%s" % (mask_argv(argv),))
        note_early(scan_arg(argv, "--progress"),
                   "结果: FAIL 启动参数不正确，请检查服务器信息是否填写完整。")
        return 2

    steps = []
    ok = False
    message = ""

    # 安装向导要边跑边显示，所以每走一步就把纯文本进度追加到文件里。
    # 最后一行固定是「结果: OK」或「结果: FAIL 原因」，向导据此判断成败。
    progress_fp = None
    if args.progress:
        try:
            # 带 BOM 写：安装向导用 LoadStringsFromFile 读，没有 BOM 会把中文读成乱码。
            progress_fp = open(args.progress, "w", encoding="utf-8-sig")
            progress_fp.write("开始：连接 %s:%d\n" % (args.host, args.port))
            progress_fp.flush()
        except Exception:
            progress_fp = None

    def note(text):
        if not progress_fp:
            return
        try:
            progress_fp.write(text + "\n")
            progress_fp.flush()
        except Exception:
            pass

    def step(name, text):
        steps.append({"step": name, "text": text})
        log("[%s] %s" % (name, text))
        note("%s：%s" % (name, text))

    try:
        try:
            import paramiko
        except ImportError:
            raise DeployError("缺少 paramiko 依赖，无法自动安装（请重新下载完整安装包）")
        base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
        res_dir = getattr(sys, "_MEIPASS", base)

        def pick(p, names):
            if p:
                return p
            for d in (res_dir, base):
                for name in names:
                    cand = os.path.join(d, name)
                    if os.path.exists(cand):
                        return cand
            return os.path.join(base, names[0])

        feeder = pick(args.feeder, FEEDER_SRC)
        proxy = pick(args.proxy, PROXY_SRC)
        sudo_pw = args.sudo_password or args.password

        step("连接", "正在连接 %s@%s:%d …" % (args.user, args.host, args.port))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {"hostname": args.host, "port": args.port, "username": args.user,
              "timeout": 15, "banner_timeout": 25, "auth_timeout": 25,
              "allow_agent": False, "look_for_keys": False}
        if args.key:
            kw["key_filename"] = args.key
        elif not args.password:
            # 没填密码时改用本机默认私钥（~/.ssh/id_ed25519、id_rsa 等）登录
            kw["look_for_keys"] = True
        kw["password"] = args.password or None
        try:
            client.connect(**kw)
        except paramiko.AuthenticationException:
            raise DeployError("登录失败：用户名或密码不正确")
        except Exception as e:
            raise DeployError("无法连接 %s:%d：%s" % (args.host, args.port, e))

        rc, out, err = run(client, "echo $HOME; id -un", timeout=30)
        parts = [x.strip() for x in out.splitlines() if x.strip()]
        home = parts[0] if parts else "/home/" + args.user
        remote_dir = (args.remote_dir or home).rstrip("/")
        run_ok(client, "mkdir -p " + quote(remote_dir), what="创建远端目录")
        step("环境", "登录成功，远端目录 %s" % remote_dir)

        rc, out, err = run(client, "python3 -V 2>&1 || true")
        if "Python 3" not in out:
            raise DeployError("远端缺少 python3，请先安装：sudo apt install -y python3")

        info = detect(client, home)
        found = ", ".join("%s=%s" % (k, v) for k, v in sorted(info.items()) if v)
        step("探测", found or "未探测到 llama.cpp 相关文件（沿用远端默认配置）")

        sftp = client.open_sftp()
        stamp = time.strftime(".bak-%Y%m%d-%H%M%S")
        feeder_remote = remote_dir + "/" + FEEDER_NAME
        proxy_remote = remote_dir + "/" + PROXY_NAME
        upload_local(sftp, feeder, feeder_remote, stamp)
        upload_local(sftp, proxy, proxy_remote, stamp)
        step("上传", "feeder.py / llama-proxy-v2.py 已就位")

        cfg_path = remote_dir + "/" + CFG_NAME
        cfg = {}
        try:
            with sftp.open(cfg_path, "r") as f:
                cfg = json.loads(f.read().decode("utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
        for k, v in info.items():
            if k.endswith("_ambiguous"):
                continue
            if v and not cfg.get(k):
                cfg[k] = v
        cfg.setdefault("trace_path", remote_dir + "/panel-trace.jsonl")
        cfg.setdefault("backend_log", remote_dir + "/llama-27b.log")
        cfg["service"] = args.service
        sftp_text(sftp, json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", cfg_path, 0o644)
        step("配置", "已写入远端配置 %s" % cfg_path)

        if args.no_restart:
            step("服务", "按参数要求跳过重启")
        else:
            created = ensure_unit(client, sudo_pw, args.service, args.user, remote_dir,
                                  proxy_remote, cfg["backend_log"])
            sudo_run(client, sudo_pw, "systemctl enable %s >/dev/null 2>&1; systemctl restart %s" % (args.service, args.service))
            time.sleep(4)
            rc, out, err = run(client, "systemctl is-active %s || true" % args.service)
            state = out.strip() or "unknown"
            if state != "active":
                logs = sudo_text(client, sudo_pw, "journalctl -u %s -n 30 --no-pager" % args.service)
                raise DeployError("服务 %s 状态为 %s，启动失败。日志：\n%s" % (args.service, state, logs.strip()[-800:]))
            step("服务", "systemd 服务 %s 已%s并处于运行状态" % (args.service, "创建" if created else "重启"))

        rc, out, err = run(client, "ss -ltn 2>/dev/null | grep -c -E \":(8080|8081) \" || true")
        step("端口", "监听检查：8080/8081 命中 %s 条" % (out.strip() or "0"))
        client.close()

        host_str = "%s@%s" % (args.user, args.host)
        sp = write_local_settings(args.app_dir, host_str, remote_dir, args.port)
        if sp:
            step("本机", "已写入 %s" % sp)
        ok = True
        message = "安装完成：%s 已连接 %s:%d" % (APP_NAME, args.host, args.port)
    except DeployError as e:
        message = str(e)
    except Exception as e:
        message = "%s: %s" % (type(e).__name__, e)

    # 顺序很重要：先把「结果:」写进进度文件并关闭，再写结果 JSON。
    # 安装向导是「看见结果 JSON 出现就停轮询、立刻读进度文件最后一行」，
    # 反过来写的话向导会读到上一行，把成功的部署判成失败。
    note("结果: " + ("OK " if ok else "FAIL ") + message)
    if progress_fp:
        try:
            progress_fp.close()
        except Exception:
            pass
    if args.result:
        try:
            with open(args.result, "w", encoding="utf-8") as f:
                json.dump({"ok": ok, "message": message, "steps": steps}, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    log(("完成：" if ok else "失败：") + message)
    if LOG_FP is not None:
        try:
            LOG_FP.close()
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
