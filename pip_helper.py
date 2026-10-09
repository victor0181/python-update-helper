#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Python 更新助手
------------------------------------------------------------
双击同目录下的「一键配置.bat」即可，也可以命令行调用：

    python pip_helper.py              配国内镜像源 + 升级 pip + 列出已装库
    python pip_helper.py --list-only  只列出已装库，不改任何东西
    python pip_helper.py --dry-run    只预览将要做什么，不实际修改
    python pip_helper.py --restore    把 pip 源改回官方 PyPI
    python pip_helper.py --help       查看全部参数

设计原则：
  * 只用标准库，任何 Python 3.7+ 都能跑，不依赖第三方包
  * 全自动、幂等，重复运行结果一致；改动前先备份原配置
  * 每一步都单独 try/except，某台解释器出问题不影响其它的
"""

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

APP_NAME = "Python 更新助手"
APP_VERSION = "1.1.0"

HOME = Path(__file__).resolve().parent
EXPORT_DIR = HOME / "清单"

# 国内镜像（按经验排序，实际会测速后自动选最快的当主源）
MIRRORS = [
    ("清华 TUNA", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云",    "https://mirrors.aliyun.com/pypi/simple/"),
    ("腾讯云",    "https://mirrors.cloud.tencent.com/pypi/simple"),
    ("中科大",    "https://pypi.mirrors.ustc.edu.cn/simple/"),
]
MIRROR_URLS = {url.rstrip("/") for _, url in MIRRORS}
OFFICIAL = "https://pypi.org/simple"

PROBE_TIMEOUT = 5.0        # 单个镜像测速超时（秒）
CMD_TIMEOUT = 90           # 一般命令超时
PIP_INSTALL_TIMEOUT = 900  # 升级 pip 超时


# --------------------------------------------------------------------------
# 终端输出
# --------------------------------------------------------------------------

_USE_COLOR = False


def fix_stdout_encoding():
    """输出重定向到文件时也用 UTF-8，并强制按行刷新。

    按行刷新很关键：升级 pip 那一步是把子进程的输出直接透传到屏幕的，
    我们自己的 print 若被缓冲，两边的先后顺序就会错乱。
    """
    try:
        if not hasattr(sys.stdout, "reconfigure"):
            return
        kwargs = {"line_buffering": True}
        if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
            kwargs["encoding"] = "utf-8"
            kwargs["errors"] = "replace"
        sys.stdout.reconfigure(**kwargs)
    except Exception:
        pass


def enable_ansi():
    """在 Windows 控制台打开 ANSI 转义支持；输出被重定向时自动关颜色。"""
    global _USE_COLOR
    try:
        fix_stdout_encoding()
        if not sys.stdout.isatty():
            _USE_COLOR = False
            return
        if os.name == "nt":
            os.system("")  # 这行能让 Win10+ 的 cmd 认 ANSI 颜色
        _USE_COLOR = True
    except Exception:
        _USE_COLOR = False


def paint(text, code):
    if not _USE_COLOR:
        return text
    return "\033[" + code + "m" + text + "\033[0m"


def title(text):
    print("")
    print(paint("=" * 66, "36"))
    print(paint("  " + text, "1;36"))
    print(paint("=" * 66, "36"))


def section(text):
    print("")
    print(paint("-- " + text + " " + "-" * max(0, 60 - len(text)), "36"))


def ok(text):
    print("  " + paint("[OK]  ", "32") + text)


def warn(text):
    print("  " + paint("[!]   ", "33") + text)


def bad(text):
    print("  " + paint("[x]   ", "31") + text)


def info(text):
    print("  " + paint("       ", "90") + text)


def detail(text):
    print("        " + paint(text, "90"))


# --------------------------------------------------------------------------
# 子进程封装
# --------------------------------------------------------------------------

def child_env():
    """让所有子进程用 UTF-8 输出，避免中文在管道里变成乱码。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def run(cmd, timeout=CMD_TIMEOUT):
    """跑一条命令，返回 (返回码, 文本)；子进程的 stderr 合并进 stdout。"""
    args = [str(x) for x in cmd]
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=child_env(),
        )
        text = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        return proc.returncode, text
    except FileNotFoundError:
        return 127, "command not found: " + args[0]
    except subprocess.TimeoutExpired:
        return 124, "timeout after " + str(timeout) + "s: " + " ".join(args)
    except Exception as exc:  # noqa: BLE001
        return 1, "error: " + repr(exc)


def first_line(text, default=""):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return default


# --------------------------------------------------------------------------
# 找解释器
# --------------------------------------------------------------------------

# Windows 应用商店的 python.exe 是 0 字节的转发器，执行它只会弹出应用商店，
# 必须在发现阶段就排除。
WINDOWSAPPS_MARKER = "windowsapps"


def _add_candidate(bucket, path, label):
    if not path:
        return
    try:
        p = Path(str(path).strip().strip('"'))
        if not p.exists() or p.is_dir():
            return
        # 同一个目录里的 python3.exe 就是 python.exe，合并掉，别当成两个解释器
        if p.name.lower() in ("python3.exe", "python3"):
            sibling = p.with_name("python.exe")
            if sibling.exists():
                p = sibling
        key = str(p.resolve())
    except Exception:
        return
    low = key.lower()
    if WINDOWSAPPS_MARKER in low:
        return
    bucket.setdefault(key, [])
    if label not in bucket[key]:
        bucket[key].append(label)


def registry_interpreters():
    """Return Python installs registered by the Windows Python installer."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    found = []
    base = r"Software\Python\PythonCore"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, base) as root:
                versions = [winreg.EnumKey(root, i) for i in range(winreg.QueryInfoKey(root)[0])]
            for version in versions:
                try:
                    with winreg.OpenKey(hive, base + "\\" + version + r"\InstallPath") as key:
                        install = winreg.QueryValueEx(key, "")[0]
                    found.append((Path(install) / "python.exe", "Windows 注册表 v" + version))
                except (OSError, TypeError):
                    continue
        except OSError:
            continue
    return found


def virtualenv_interpreters():
    """Return likely virtualenv/Conda interpreters without requiring activation.

    A virtual environment is not normally registered with the Windows Python
    launcher, so inspect the active environment, common environment folders,
    and shallow project directories as a practical discovery pass.
    """
    found = []
    prefixes = []

    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        value = os.environ.get(var)
        if value:
            prefixes.append((Path(value), var + "（当前激活）"))

    user_profile = os.environ.get("USERPROFILE")
    local_appdata = os.environ.get("LOCALAPPDATA")
    known_parents = [
        os.environ.get("WORKON_HOME"),
        str(Path(user_profile) / ".virtualenvs") if user_profile else None,
        str(Path(user_profile) / "Envs") if user_profile else None,
        str(Path(user_profile) / ".conda" / "envs") if user_profile else None,
        str(Path(user_profile) / "miniconda3" / "envs") if user_profile else None,
        str(Path(user_profile) / "anaconda3" / "envs") if user_profile else None,
        str(Path(local_appdata) / "conda" / "conda" / "envs") if local_appdata else None,
    ]
    for raw_parent in known_parents:
        if not raw_parent:
            continue
        parent = Path(raw_parent).expanduser()
        if not parent.is_dir():
            continue
        try:
            for child in parent.iterdir():
                if child.is_dir():
                    prefixes.append((child, "常见虚拟环境目录"))
        except OSError:
            continue

    # Project environments are usually named .venv, venv, env, or .env.
    project_roots = []
    for base in (Path.cwd(), HOME):
        current = base
        for _ in range(3):
            # 盘符根（C:\、D:\）永远不遍历：工具一旦被放在盘符下一级目录，
            # 上溯就会走到根目录，按深度 3 全盘枚举——实测 C 盘要 8 秒、
            # 两万七千个条目，而收益几乎为零，等于让每次扫描白等一堆时间。
            if current.parent == current:
                break
            if current not in project_roots:
                project_roots.append(current)
            current = current.parent
    ignored_dirs = {".git", "__pycache__", "node_modules", ".tox", ".mypy_cache"}
    for base in project_roots:
        # Do a bounded walk instead of rglob: one of the roots can be a large
        # workspace or drive subtree, and discovery must stay responsive.
        pending = [(base, 0)]
        while pending:
            current, depth = pending.pop()
            try:
                cfg_path = current / "pyvenv.cfg"
                if cfg_path.is_file():
                    prefixes.append((current, "项目虚拟环境"))
                    continue
                if depth >= 3:
                    continue
                for child in current.iterdir():
                    if child.is_dir() and not child.is_symlink() and child.name not in ignored_dirs:
                        pending.append((child, depth + 1))
            except OSError:
                continue

    # Conda can report environments stored outside its conventional folders.
    code, out = run(["conda", "env", "list", "--json"], timeout=20)
    if code == 0:
        start = out.find("{")
        if start >= 0:
            try:
                payload = json.loads(out[start:])
                for env_path in payload.get("envs", []):
                    prefixes.append((Path(env_path), "conda 环境"))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass

    seen = set()
    for prefix, label in prefixes:
        try:
            prefix = prefix.resolve()
        except OSError:
            continue
        key = str(prefix).lower()
        if key in seen:
            continue
        seen.add(key)
        for executable in (prefix / "Scripts" / "python.exe", prefix / "python.exe"):
            if executable.exists():
                found.append((executable, label))
                break
    return found


def _pretty_py_tag(tag):
    """把 py -0p 的标签收拾成人看得懂的样子。

    py 启动器给 uv 装的解释器会打上 "Astral/CPython3.12.15" 这种带厂商前缀的
    标签，直接拼进来源说明就变成「py 启动器 vAstral/CPython3.12.15」。
    """
    tag = (tag or "").strip()
    if "/" in tag:
        tag = tag.rsplit("/", 1)[-1]
    if tag.startswith("CPython"):
        tag = tag[len("CPython"):]
    return tag or "?"


def discover_interpreters():
    """自动发现本机所有 Python 解释器，返回 [(Path, [来源说明])]。"""
    bucket = {}

    _add_candidate(bucket, sys.executable, "当前助手使用的 Python")
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        prefix = os.environ.get(var)
        if prefix:
            executable = Path(prefix) / "python.exe"
            if var == "VIRTUAL_ENV":
                executable = Path(prefix) / "Scripts" / "python.exe"
            _add_candidate(bucket, executable, var)

    for path, label in virtualenv_interpreters():
        _add_candidate(bucket, path, label)

    for path, label in registry_interpreters():
        _add_candidate(bucket, path, label)

    # 1) py 启动器注册表里的全部版本
    code, out = run(["py", "-0p"])
    if code == 0:
        for line in out.splitlines():
            m = re.match(r"\s*-V:(\S+)\s+\*?\s*(.+?)\s*$", line)
            if m:
                _add_candidate(bucket, m.group(2), "py 启动器 v" + _pretty_py_tag(m.group(1)))

    # 2) PATH 里的 python / python3
    for name in ("python", "python3"):
        code, out = run(["where", name])
        if code == 0:
            for line in out.splitlines():
                if line.strip():
                    _add_candidate(bucket, line, "PATH 里的 " + name)

    # 3) 常见安装目录
    roots = []
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base) / "Programs" / "Python")
            roots.append(Path(base) / "Python")
    roots.append(Path("C:/"))
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for child in sorted(root.glob("Python3*")):
                if child.is_dir():
                    _add_candidate(bucket, child / "python.exe", "安装目录 " + str(root))
        except Exception:
            continue

    result = []
    for key in sorted(bucket, key=lambda s: s.lower()):
        result.append((Path(key), sorted(bucket[key])))
    return result


def inspect_interpreter(python):
    """确认解释器能跑，返回 (版本, 可执行路径) 或 None。"""
    code, out = run([python, "-c", "import sys;print(sys.version.split()[0]);print(sys.executable)"], timeout=40)
    if code != 0:
        return None
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    if not lines:
        return None
    version = lines[0]
    real = lines[1] if len(lines) > 1 else str(python)
    return version, real


def pip_state(python):
    """返回 (pip 版本 or None, 说明)。"""
    code, out = run([python, "-m", "pip", "--version"], timeout=60)
    if code != 0:
        return None, first_line(out, "pip 不可用")
    m = re.search(r"\bpip\s+([0-9][^\s]*)", out)
    return (m.group(1) if m else "?"), out.strip()


def probe_many(targets, workers=4):
    """并行确认解释器；返回 [(path, labels, probe 或 None)]，顺序与入参一致。

    逐个启动 Python 进程是本工具最慢的一环（实测每次 0.5~1.6 秒，杀毒软件
    实时扫描是大头），四个解释器串行就要七八秒，并行后只等最慢的那一个。
    """
    items = list(targets)
    if not items:
        return []
    paths = [path for path, _ in items]
    limit = max(1, min(workers, len(paths)))
    with ThreadPoolExecutor(max_workers=limit) as pool:
        probes = list(pool.map(inspect_interpreter, paths))
    return [(path, labels, probe) for (path, labels), probe in zip(items, probes)]


def describe_interpreter(target):
    """给 --discover-json 用：一次拿齐版本 / 真实路径 / pip 状态。"""
    path, labels = target
    probe = inspect_interpreter(path)
    if probe is None:
        return None
    version, real = probe
    pip_version, pip_detail = pip_state(path)
    return {
        "path": str(path),
        "real": str(real),
        "version": version,
        "pip": pip_version or "未安装",
        "labels": list(labels),
        "pip_detail": pip_detail,
    }


def newest_pip_version():
    """查官方最新的 pip 版本号，用来判断要不要升级。失败返回 None。"""
    code, out = run([sys.executable, "-m", "pip", "index", "versions", "pip"], timeout=45)
    if code == 0:
        m = re.search(r"Available versions:\s*([0-9][^\s,]*)", out)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------
# 镜像测速与配置写入
# --------------------------------------------------------------------------

def probe_mirror(item):
    name, url = item
    # Probe the same simple-index endpoint that will be configured for pip.
    target = url.rstrip("/") + "/"
    req = urllib.request.Request(target, headers={"User-Agent": "pip-helper/" + APP_VERSION})
    started = time.time()
    try:
        # 遵循系统代理设置；企业网络常常必须通过代理访问外网。
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            resp.read(2048)
        return {"name": name, "url": url, "cost": time.time() - started, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "url": url, "cost": None, "error": first_line(str(exc), "连接失败")}


def probe_all():
    with ThreadPoolExecutor(max_workers=len(MIRRORS)) as pool:
        return list(pool.map(probe_mirror, MIRRORS))


def pip_config_paths(python):
    """问 pip 自己：它的 user 级配置文件到底在哪。

    Windows 上 pip 用系统 API 定位配置目录，不一定等于 %APPDATA%，
    所以以 pip 的回答为准，避免「写在一个地方、读的是另一个地方」。
    """
    code, out = run([python, "-m", "pip", "config", "debug"], timeout=45)
    if code != 0:
        return []
    paths = []
    in_user = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            in_user = stripped[:-1] == "user"
            continue
        if in_user:
            m = re.match(r"^(.*?),\s*exists:\s*(True|False)$", stripped)
            if m:
                paths.append(Path(m.group(1).strip()))
    return paths


def resolve_user_ini(python, override=None):
    """决定 pip.ini 写到哪：命令行指定 > pip 自己说的 > %APPDATA% 兜底。"""
    if override:
        return Path(override).expanduser().resolve()

    # PIP_CONFIG_FILE 的优先级高于普通用户配置；尊重用户的显式设置。
    env_config = os.environ.get("PIP_CONFIG_FILE")
    if env_config and env_config.lower() not in ("nul", "/dev/null"):
        return Path(env_config).expanduser().resolve()

    if python is not None:
        candidates = pip_config_paths(python)
        if candidates:
            existing = [p for p in candidates if p.exists()]
            return existing[-1] if existing else candidates[-1]

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "pip" / "pip.ini"
    return Path.home() / ".config" / "pip" / "pip.conf"


def read_pip_ini(path):
    """用 configparser 解析 pip.ini（只用来读取现有值做合并判断）。"""
    def parser():
        cfg = configparser.RawConfigParser(strict=False)
        # pip 的配置键不区分大小写；统一为小写，避免出现 Index-Url/index-url 重复键。
        cfg.optionxform = str.lower
        return cfg

    if not path.exists():
        return parser()
    try:
        text = _read_config_text(path)[0]
    except OSError as exc:
        raise ValueError("无法读取 pip 配置文件 %s：%s" % (path, exc)) from exc
    cfg = parser()
    try:
        cfg.read_string(text, source=str(path))
    except (configparser.Error, UnicodeError) as exc:
        raise ValueError("无法解析 pip 配置文件 %s：%s" % (path, exc)) from exc
    return cfg


def _text_score(text):
    """给一个解码结果打分：汉字和可打印 ASCII 加分，常见乱码区间减分。

    GBK 编码的中文常常恰好也是合法的 UTF-8 字节序列（例如"中文"），
    只按"能不能解码成功"来判断会解出满屏乱码，所以这里比一比谁更像中文。
    """
    cjk = 0
    ascii_ok = 0
    weird = 0
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            cjk += 1
        elif char.isascii() and (char.isprintable() or char in "\r\n\t"):
            ascii_ok += 1
        elif "\u0100" <= char <= "\u2bff":
            weird += 1
    return cjk * 3 + ascii_ok - weird * 2


def _decode_config(raw):
    """猜 pip.ini 的编码，返回 (文本, 写回时该用的编码)。"""
    if all(byte < 0x80 for byte in raw):
        return raw.decode("ascii"), "utf-8"
    candidates = []
    for encoding in ("utf-8", "gbk"):
        try:
            candidates.append((raw.decode(encoding), encoding))
        except UnicodeDecodeError:
            continue
    if not candidates:
        return raw.decode("utf-8", "replace"), "utf-8"
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda item: _text_score(item[0]))


def _read_config_text(path):
    """读回 pip.ini 原文，返回 (文本, 写回用的编码, 换行符)。

    编码、BOM、换行符都按原样保留：只动要改的那几行，其余字节尽量不变，
    免得给用户留下一个"被整体重排过"的配置文件。
    """
    raw = path.read_bytes() if path.exists() else b""
    newline = "\r\n" if b"\r\n" in raw else "\n"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig", newline
    text, encoding = _decode_config(raw)
    if text.startswith("\ufeff"):
        text = text[1:]
    return text, encoding, newline


def _format_ini_entry(key, value):
    """一个键写一行；多值（extra-index-url / trusted-host）按 pip 自己的写法缩进。"""
    parts = [p.strip() for p in str(value).split("\n") if p.strip()]
    if not parts:
        return [key]
    if len(parts) == 1:
        return [key + " = " + parts[0]]
    return [key + " ="] + ["\t" + p for p in parts]


def _atomic_write_text(path, body, encoding, dry_run=False):
    """先备份、再同目录临时文件 + 原子替换，避免中断时留下半个 pip.ini。"""
    if dry_run:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path.with_name(path.name + ".bak-" + stamp)
        try:
            backup.write_bytes(path.read_bytes())
        except Exception as exc:
            raise OSError("无法备份原 pip 配置，已停止写入：%s" % exc) from exc
    temp_path = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    try:
        with open(str(temp_path), "w", encoding=encoding, newline="") as fh:
            fh.write(body)
        os.replace(str(temp_path), str(path))
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
    return backup


def rewrite_global_keys(path, updates, dry_run=False):
    """按行改写 pip.ini 的 [global] 段，保留注释、键顺序和其它段落。

    updates[key] 给值表示写入，给 None 表示删除该键。
    用 configparser 重写整份文件会把注释、缩进和键顺序统统丢掉，
    所以这里只替换真正要改的那几行，其余原样搬运。
    """
    text, encoding, newline = _read_config_text(path)
    lines = text.split(newline)
    out = []
    tail = []
    section = None
    open_key = None
    seen_global = False
    pending = dict(updates)

    def flush_pending():
        """把还没写上的键补在当前 [global] 段的末尾（保持 updates 里的顺序）。"""
        for key in list(pending):
            value = pending.pop(key)
            if value is not None:
                out.extend(_format_ini_entry(key, value))

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "global":
                flush_pending()
                out.extend(tail)
            tail = []
            section = stripped[1:-1].strip().lower()
            if section == "global":
                seen_global = True
            open_key = None
            out.append(line)
            continue
        if section != "global":
            out.append(line)
            continue
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            # 空行 / 注释：如果后面还要补键，就先攒着，补在它们前面
            if pending:
                tail.append(line)
            else:
                out.append(line)
            continue
        if line[:1] in (" ", "\t"):
            # 多值键的续行：要么所属的键会被整体重写，要么它压根没被动过
            if open_key is None:
                out.append(line)
            continue
        if tail:
            out.extend(tail)
            tail = []
        raw_key = stripped.split("=", 1)[0].strip()
        key = raw_key.lower()
        open_key = key
        if key in updates:
            if key not in pending:
                continue  # 同一个键在本段里重复出现：多余的丢掉
            value = pending.pop(key)
            if value is None:
                continue
            out.extend(_format_ini_entry(raw_key, value))
            continue
        out.append(line)

    if section == "global":
        flush_pending()
        out.extend(tail)
    if not seen_global:
        head = ["[global]"]
        for key in list(pending):
            value = pending.pop(key)
            if value is not None:
                head.extend(_format_ini_entry(key, value))
        rest = out if any(line.strip() for line in out) else []
        out = head + ([""] + rest if rest else [])
    while out and not out[-1].strip():
        out.pop()
    body = newline.join(out) + newline if out else ""
    return _atomic_write_text(path, body, encoding, dry_run=dry_run)


def _kept_user_values(cfg, option, is_mirror_value):
    """读出用户自己写的值，去掉本工具以前写入的镜像条目。"""
    if not cfg.has_option("global", option):
        return []
    return [value for value in cfg.get("global", option).split()
            if not is_mirror_value(value)]


def set_mirror_in_ini(path, main_url, extra_urls, trusted_hosts=None, dry_run=False):
    """写入国内主源，并清掉本工具以前留下的镜像条目。

    只设置一个主源：pip 从多个公共索引择优下载会带来依赖混淆风险。
    用户自己的 extra-index-url（例如公司私有源）原样保留。
    """
    cfg = read_pip_ini(path)  # 只用来读取现有值；写回走按行改写，保留注释
    mirror_hosts = {host_of(url).lower() for url in MIRROR_URLS}
    merged = list(dict.fromkeys(
        _kept_user_values(cfg, "extra-index-url", lambda u: u.rstrip("/") in MIRROR_URLS)
        + list(extra_urls or [])))
    merged_hosts = list(dict.fromkeys(
        _kept_user_values(cfg, "trusted-host", lambda h: h.lower() in mirror_hosts)
        + list(trusted_hosts or [])))
    updates = {
        "index-url": main_url,
        "extra-index-url": "\n".join(merged) if merged else None,
        "trusted-host": "\n".join(merged_hosts) if merged_hosts else None,
    }
    return rewrite_global_keys(path, updates, dry_run=dry_run)


def restore_official_in_ini(path, dry_run=False):
    """改回官方 PyPI；只移除本工具写入的国内镜像，保留用户自己的私有源。"""
    cfg = read_pip_ini(path)
    mirror_hosts = {host_of(url).lower() for url in MIRROR_URLS}
    kept = _kept_user_values(cfg, "extra-index-url", lambda u: u.rstrip("/") in MIRROR_URLS)
    kept_hosts = _kept_user_values(cfg, "trusted-host", lambda h: h.lower() in mirror_hosts)
    updates = {
        "index-url": OFFICIAL,
        "extra-index-url": "\n".join(kept) if kept else None,
        "trusted-host": "\n".join(kept_hosts) if kept_hosts else None,
    }
    return rewrite_global_keys(path, updates, dry_run=dry_run)


def effective_index_url(python):
    """问 pip 它现在实际用的是哪个源（含更高优先级的 site / env 配置）。

    注意 `pip config get global.index-url` 对 index-url 会报
    "No such key"，只能解析 `pip config list`；而且 list 里
    extra-index-url 按字母序排在 index-url 前面，所以必须精确匹配键名。
    """
    code, out = run([python, "-m", "pip", "config", "list"], timeout=45)
    if code != 0:
        return ""
    # 环境变量优先于配置文件；先解析 :env:，再解析 global。
    for line in out.splitlines():
        m = re.match(r"^\s*:env:\.index-url\s*=\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip().strip("'\"")

    # PIP_CONFIG_FILE is loaded after the normal config files.
    env_config = os.environ.get("PIP_CONFIG_FILE")
    if env_config and env_config.lower() not in ("nul", "/dev/null"):
        try:
            cfg = read_pip_ini(Path(env_config).expanduser())
            if cfg.has_option("global", "index-url"):
                return cfg.get("global", "index-url").strip().strip("'\"")
        except (OSError, ValueError):
            # 配置步骤会报告具体错误；这里核实时只跳过这份损坏配置，
            # 不能让一个辅助检查异常掩盖主流程结果。
            pass

    # pip config debug includes values per file; site overrides user, user
    # overrides global.  This catches a site-level pip.ini that pip config list
    # does not identify by its source file.
    debug_code, debug = run([python, "-m", "pip", "config", "debug"], timeout=45)
    if debug_code == 0:
        values = {"global": [], "user": [], "site": []}
        section = None
        for line in debug.splitlines():
            stripped = line.strip()
            if stripped in values and line.rstrip().endswith(":"):
                section = stripped
                continue
            if stripped.endswith(":") and not stripped.startswith("global.index-url"):
                continue
            if section in values:
                m = re.match(r"^global\.index-url:\s*(.+?)\s*$", stripped)
                if m:
                    values[section].append(m.group(1).strip().strip("'\""))
        for section in ("site", "user", "global"):
            if values[section]:
                return values[section][-1]

    for line in out.splitlines():
        m = re.match(r"^\s*global\.index-url\s*=\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip().strip("'\"")
    return ""


def host_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="pip_helper.py",
        description=APP_NAME + " v" + APP_VERSION + " —— 一键配国内 pip 镜像 / 升级 pip / 盘点已装库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python pip_helper.py                 全自动：配源 + 升级 pip + 列出已装库\n"
            "  python pip_helper.py --list-only     只看已装库\n"
            "  python pip_helper.py --dry-run       预览，不修改任何东西\n"
            "  python pip_helper.py --restore       改回官方 PyPI（只改源，不升级 pip）\n"
            "  python pip_helper.py -p D:\\x\\python.exe   只处理指定的解释器\n"
        ),
    )
    parser.add_argument("-p", "--python", action="append", default=[], metavar="路径",
                        help="手动指定解释器（可重复）；给了这个就跳过自动发现")
    parser.add_argument("--exclude", action="append", default=[], metavar="关键词",
                        help="跳过路径里含该关键词的解释器（可重复），"
                             "例如 --exclude WorkBuddy 不碰工具自带的 Python")
    parser.add_argument("--mirror", metavar="名称",
                        help="指定主镜像，可选：" + " / ".join(m[0] for m in MIRRORS))
    parser.add_argument("--no-probe", action="store_true", help="不测速，直接用预设顺序里的第一个")
    parser.add_argument("--list-only", action="store_true", help="只列出已装库，不改配置、不升级 pip")
    parser.add_argument("--no-upgrade", action="store_true", help="只配源和列库，不升级 pip")
    parser.add_argument("--no-config", action="store_true", help="不动 pip 源配置")
    parser.add_argument("--no-export", action="store_true", help="不导出清单文件")
    parser.add_argument("--restore", action="store_true",
                        help="把 pip 源改回官方 PyPI（只改源，不升级 pip、不列库）")
    parser.add_argument("--configure-only", action="store_true",
                        help="只配置 pip 源，不升级 pip、不列出库")
    parser.add_argument("--upgrade-only", action="store_true",
                        help="只升级 pip，不修改源、不列出库")
    parser.add_argument("--probe-only", action="store_true",
                        help="只测速镜像，不修改配置")
    parser.add_argument("--discover-json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe-json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--trusted", action="store_true",
                        help="同时写入 trusted-host（网络有 SSL 拦截时用）")
    parser.add_argument("--config-file", metavar="路径",
                        help="指定要写入的 pip.ini（默认自动定位到 pip 真正读取的那份）")
    parser.add_argument("--dry-run", action="store_true", help="只预览将要做什么，不实际修改")
    args = parser.parse_args(argv)
    exclusive_modes = [
        ("--list-only", args.list_only),
        ("--configure-only", args.configure_only),
        ("--upgrade-only", args.upgrade_only),
        ("--probe-only", args.probe_only),
    ]
    active = [name for name, enabled in exclusive_modes if enabled]
    if len(active) > 1:
        parser.error("这些模式不能同时使用：" + ", ".join(active))
    if args.probe_only and (args.python or args.exclude):
        parser.error("--probe-only 不需要指定 Python 解释器")
    if args.restore and args.list_only:
        parser.error("--restore 不能与 --list-only 同时使用")
    if args.restore and args.upgrade_only:
        parser.error("--restore 不能与 --upgrade-only 同时使用")
    if args.restore and args.no_config:
        parser.error("--restore 不能与 --no-config 同时使用")
    if args.restore and (args.mirror or args.no_probe or args.trusted):
        parser.error("--restore 不需要 --mirror、--no-probe 或 --trusted")
    for name, enabled, antonym, antonym_on in (
        ("--configure-only", args.configure_only, "--no-config", args.no_config),
        ("--upgrade-only", args.upgrade_only, "--no-upgrade", args.no_upgrade),
    ):
        if enabled and antonym_on:
            parser.error(name + " 与 " + antonym + " 互相矛盾，那样一步都不会执行")
    if args.probe_only and args.dry_run:
        parser.error("--probe-only 本来就不写任何东西，不需要 --dry-run")
    if args.restore:
        # 还原源就只还原源：从官方 PyPI 升级 pip 在国内又慢又容易失败，
        # 而用户点「恢复官方源」想做的只是把源改回去。
        args.configure_only = True
    return args


def step_discover(args):
    section("第 1 步 / 找出本机所有 Python")

    if args.python:
        targets = [(Path(p).expanduser().resolve(), ["命令行指定"]) for p in args.python]
        info("按命令行指定的解释器处理，未做自动发现。")
    else:
        targets = discover_interpreters()
        info("自动发现（来自 py 启动器 / PATH / 常见安装目录）。")

    excludes = [x.lower() for x in args.exclude]
    todo = []
    for path, labels in targets:
        if any(word in str(path).lower() for word in excludes):
            warn("按 --exclude 跳过：" + str(path))
            continue
        todo.append((path, labels))

    if todo:
        info("正在确认 " + str(len(todo)) + " 个解释器的版本与 pip 状态 ...")
    usable = []
    seen = {}
    for path, labels, probe in probe_many(todo):
        if probe is None:
            warn("跳过（跑不起来）：" + str(path))
            detail("可能是坏掉的虚拟环境，或者解释器已被卸载。")
            continue
        version, real = probe
        real_path = Path(real)
        key = (str(real_path.parent).lower(), version)
        if key in seen:
            item = seen[key]
            for label in labels:
                if label not in item["labels"]:
                    item["labels"].append(label)
            detail("（同一个解释器，合并忽略：" + str(path) + "）")
            continue
        item = {"path": path, "real": real_path, "version": version, "labels": list(labels)}
        seen[key] = item
        usable.append(item)
        ok("Python " + version + "  ->  " + str(real_path))
        detail("来源：" + "、".join(labels))

    if usable:
        info("共 " + str(len(usable)) + " 个解释器将被处理。")
    if not usable:
        bad("没找到可用的 Python 解释器。")
        info("请确认已安装 Python，或者用 -p 参数手动指定 python.exe 路径。")
    return usable


def step_mirror(args):
    section("第 2 步 / 选一个最快的国内镜像")

    if args.mirror:
        chosen = None
        for name, url in MIRRORS:
            if args.mirror.lower() in name.lower() or args.mirror == url:
                chosen = {"name": name, "url": url, "cost": None, "error": None}
                break
        if chosen is None:
            bad("没听过这个镜像：" + args.mirror)
            info("可选：" + " / ".join(m[0] for m in MIRRORS))
            return None
        info("按参数指定主源：" + chosen["name"])
        return [chosen] + [{"name": n, "url": u, "cost": None, "error": None}
                           for n, u in MIRRORS if u != chosen["url"]]

    if args.no_probe:
        info("跳过了测速，按预设顺序使用。")
        return [{"name": n, "url": u, "cost": None, "error": None} for n, u in MIRRORS]

    info("正在测速（每站最多 " + str(int(PROBE_TIMEOUT)) + " 秒）...")
    results = probe_all()
    alive = []
    for item in results:
        if item["error"]:
            warn(item["name"] + "  " + item["error"])
        else:
            alive.append(item)
            ok(item["name"] + "  " + str(round(item["cost"] * 1000)) + " ms")
    if not alive:
        warn("所有镜像都连不上，退回预设顺序（清华优先）。")
        return [{"name": n, "url": u, "cost": None, "error": None} for n, u in MIRRORS]
    alive.sort(key=lambda x: x["cost"])
    info("主源选定：" + alive[0]["name"] + "（只写入一个源，避免多索引择优带来的安全风险）")
    return alive


def step_configure(args, mirror_list, ini):
    section("第 3 步 / 把 pip 源指向国内镜像")

    info("配置文件：" + str(ini))

    if args.restore:
        info("模式：改回官方 PyPI")
        try:
            backup = restore_official_in_ini(ini, dry_run=args.dry_run)
        except Exception as exc:
            bad("配置失败：" + str(exc))
            return False
        ok("已把 index-url 设为 " + OFFICIAL + ("（预览，未写入）" if args.dry_run else ""))
        if backup:
            detail("原配置已备份到 " + backup.name)
        return True

    main_item = mirror_list[0]
    extras = []
    trusted = None
    if args.trusted:
        trusted = [host_of(main_item["url"])]
        trusted = [h for h in trusted if h]

    print("")
    info("主源   index-url        = " + main_item["name"] + "  " + main_item["url"])
    info("其他镜像仅用于测速选择，不会写入 extra-index-url。")

    if args.dry_run:
        info("(--dry-run 预览，不写文件)")
        return True

    try:
        backup = set_mirror_in_ini(ini, main_item["url"], extras, trusted, dry_run=False)
    except Exception as exc:
        bad("配置失败：" + str(exc))
        return False
    ok("已写入 " + str(ini))
    if backup:
        detail("原配置已备份到 " + backup.name)
    else:
        detail("（原来没有这个文件，属于全新创建）")
    info("这份配置对当前用户的**所有** pip 生效，不用逐个解释器改。")
    return True


def verify_effective(interpreters, expect_url, dry_run=False):
    section("第 4 步 / 核实每个解释器实际用哪个源")
    if dry_run:
        info("预览模式没有真正写配置，跳过核实。")
        return
    if not expect_url:
        info("跳过核实。")
        return
    for item in interpreters:
        python = item["path"]
        try:
            current = effective_index_url(python)
        except (OSError, ValueError) as exc:
            warn("Python " + item["version"] + "：读取配置失败：" + str(exc))
            continue
        if not current:
            warn("Python " + item["version"] + "：读不到配置")
            continue
        if current.rstrip("/").lower() == expect_url.rstrip("/").lower():
            ok("Python " + item["version"] + "  ->  " + current)
        else:
            warn("Python " + item["version"] + "  实际用的是 " + current)
            detail("说明这个解释器自己目录下（site 级）另有配置，优先级高于用户级。")
            detail("需要的话把那个 pip.ini 里的 index-url 一并改掉。")


def step_upgrade(args, interpreters):
    section("第 5 步 / 把 pip 升级到最新")
    if args.list_only:
        info("「只看已装库」模式下不动 pip，跳过。")
        return True
    if args.no_upgrade:
        info("按参数跳过（--no-upgrade）。")
        return True
    success = True
    latest = newest_pip_version()
    if latest:
        info("官方最新版本：" + latest)

    for item in interpreters:
        python = item["path"]
        before, _ = pip_state(python)
        label = "Python " + item["version"] + "（pip " + str(before) + "）"
        if before is None:
            if args.dry_run:
                info(label + " 没有 pip；预览模式不会执行 ensurepip。")
                continue
            warn(label + " 没有可用的 pip，先尝试 ensurepip 补上 ...")
            run([python, "-m", "ensurepip", "--upgrade"], timeout=300)
            before, _ = pip_state(python)
            if before is None:
                bad(label + " 仍然没有 pip，跳过升级。")
                success = False
                continue

        if args.dry_run:
            info(label + " -> 预览：将执行 pip install --upgrade pip")
            continue

        print("")
        info("正在升级 " + label + " ...")
        code, out = run([python, "-m", "pip", "install", "--upgrade", "pip"],
                        timeout=PIP_INSTALL_TIMEOUT)
        for line in out.strip().splitlines():
            if line.strip():
                detail(line.strip())

        if code == 0:
            after, _ = pip_state(python)
            if after and before and after != before:
                ok("pip " + str(before) + "  ->  " + str(after))
            else:
                ok("pip " + str(after) + " 已经是最新，无需变更")
        elif "externally-managed-environment" in out:
            warn(label + " 由工具托管（uv / 系统包管理器），PEP 668 不允许改它的 pip。")
            detail("它自带的 pip " + str(before) + " 可以直接用，本工具不去强改。")
            detail("（真要动它得加 --break-system-packages，风险自负，这里不做。）")
            success = False
        else:
            after, _ = pip_state(python)
            bad("升级失败（返回码 " + str(code) + "），当前仍是 pip " + str(after))
            detail("多半是网络或镜像的问题，可以换个 --mirror 再试一次。")
            success = False
    return success


def collect_packages(python):
    code, out = run([python, "-m", "pip", "list", "--format=json"], timeout=180)
    if code != 0:
        return None, out
    try:
        decoder = json.JSONDecoder()
        packages = None
        for start, char in enumerate(out):
            if char != "[":
                continue
            try:
                candidate, _ = decoder.raw_decode(out[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, list):
                packages = candidate
                break
        if packages is None:
            packages = json.loads(out)
        if not isinstance(packages, list):
            raise ValueError("pip list JSON 不是数组")
        rows = []
        for item in packages:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            version = str(item.get("version", "")).strip()
            if name:
                rows.append(name + (" " + version if version else ""))
        return rows, None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, "无法解析 pip list 输出：" + str(exc)


def step_list(args, interpreters):
    section("第 6 步 / 列出已安装的库")
    report_blocks = []
    success = True
    for item in interpreters:
        python = item["path"]
        print("")
        print(paint("  Python " + item["version"] + "   " + str(item["real"]), "1"))
        text, err = collect_packages(python)
        if text is None:
            bad("pip list 执行失败：" + first_line(err, "未知错误"))
            report_blocks.append("Python " + item["version"] + "  " + str(item["real"]) + "\n(读取失败)\n")
            success = False
            continue
        body = list(text)
        print("  共 " + str(len(body)) + " 个包：")
        for line in body:
            print("      " + line)
        header = "Python " + item["version"] + "   " + str(item["real"])
        report_blocks.append(header + "\n" + "\n".join(body) + "\n")
    return report_blocks, success


EXPORT_KEEP = 30  # 「清单」目录里最多保留多少份历史导出


def prune_exports(keep=EXPORT_KEEP):
    """只保留最近 keep 份清单，返回清理掉的数量；任何异常都不影响主流程。"""
    if keep <= 0 or not EXPORT_DIR.is_dir():
        return 0
    try:
        files = sorted(EXPORT_DIR.glob("已安装库清单_*.txt"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def export_report(blocks, args):
    if args.no_export or not blocks or args.dry_run:
        return None
    try:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = EXPORT_DIR / ("已安装库清单_" + stamp + ".txt")
        index = 2
        while path.exists():  # 同一秒里跑两次也不会互相覆盖
            path = EXPORT_DIR / ("已安装库清单_" + stamp + "_" + str(index) + ".txt")
            index += 1
        content = [APP_NAME + " v" + APP_VERSION + "  导出时间 " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""]
        for block in blocks:
            content.append("=" * 60)
            content.append(block.rstrip())
            content.append("")
        path.write_text("\n".join(content), encoding="utf-8-sig")
        removed = prune_exports()
        if removed:
            detail("已清理 " + str(removed) + " 份更旧的清单（只保留最近 " + str(EXPORT_KEEP) + " 份）。")
        return path
    except Exception as exc:  # noqa: BLE001
        warn("导出清单失败：" + repr(exc))
        return False


def main(argv=None):
    enable_ansi()
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.discover_json:
        targets = discover_interpreters()
        if targets:
            limit = max(1, min(4, len(targets)))
            with ThreadPoolExecutor(max_workers=limit) as pool:
                described = list(pool.map(describe_interpreter, targets))
        else:
            described = []
        rows = []
        seen = set()
        for item in described:
            if not item:
                continue
            key = (str(Path(item["real"]).parent).lower(), item["version"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
        print(json.dumps(rows, ensure_ascii=False))
        return 0 if rows else 1

    if args.probe_json:
        results = probe_all()
        print(json.dumps(results, ensure_ascii=False))
        return 0 if any(not item["error"] for item in results) else 1

    if args.probe_only:
        title(APP_NAME + "  v" + APP_VERSION)
        info("正在测速（每站最多 " + str(int(PROBE_TIMEOUT)) + " 秒）...")
        results = probe_all()
        alive = [item for item in results if not item["error"]]
        for item in results:
            if item["error"]:
                warn(item["name"] + "  " + item["error"])
            else:
                ok(item["name"] + "  " + str(round(item["cost"] * 1000)) + " ms")
        if not alive:
            bad("所有镜像都无法连接。")
            return 1
        alive.sort(key=lambda item: item["cost"])
        mirrors = alive
        print("")
        ok("最快镜像：" + mirrors[0]["name"] + "  " + mirrors[0]["url"])
        return 0

    title(APP_NAME + "  v" + APP_VERSION)
    if args.list_only:
        info("模式：只看已装库（不修改配置、不升级 pip）")
    if args.dry_run:
        info("模式：预览（不会写配置或执行 ensurepip/pip install）")
    if args.restore:
        info("模式：还原为官方 PyPI 源（只改源，不升级 pip、不列库）")
    elif args.configure_only:
        info("模式：只配置 pip 源（不升级 pip、不列库）")

    interpreters = step_discover(args)

    expect_url = ""
    failed = False
    if not interpreters:
        failed = True

    if not args.list_only and not args.no_config and not args.upgrade_only:
        anchor = interpreters[0]["path"] if interpreters else None
        ini_path = resolve_user_ini(anchor, args.config_file)
        if args.restore:
            configured = step_configure(args, [], ini_path)
            expect_url = OFFICIAL
        else:
            mirror_list = step_mirror(args)
            if mirror_list:
                configured = step_configure(args, mirror_list, ini_path)
                expect_url = mirror_list[0]["url"]
        if not configured:
            failed = True
            expect_url = ""  # 没写成功就别去核实，否则前后两句提示自相矛盾
        elif interpreters:
            verify_effective(interpreters, expect_url, dry_run=args.dry_run)
    else:
        section("第 2~4 步 / 配置 pip 源")
        info("已跳过。")

    if interpreters and not args.list_only and not args.configure_only:
        if not step_upgrade(args, interpreters):
            failed = True
    else:
        if args.configure_only:
            section("第 5 步 / 把 pip 升级到最新")
            info("仅配置源模式下已跳过。")

    if not args.upgrade_only and not args.configure_only:
        blocks, list_ok = step_list(args, interpreters)
        if not list_ok:
            failed = True
    else:
        blocks = []
    path = export_report(blocks, args)
    if path is False:
        failed = True
        path = None

    title("执行完成" if not failed else "执行结束（存在失败步骤）")
    if failed:
        bad("有步骤失败，请根据上面的提示处理后再试。")
    if args.dry_run:
        info("以上是预览，磁盘上什么都没改。")
    if path:
        ok("已装库清单已导出：" + str(path))
    print("")
    info("以后想再看一次：双击「一键配置.bat」，或运行")
    detail("python pip_helper.py --list-only")
    info("想改回官方源：")
    detail("python pip_helper.py --restore")
    print("")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("")
        print("已取消。")
        sys.exit(130)
