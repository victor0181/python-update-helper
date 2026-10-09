#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows front end for the Python update helper.

The command-line program remains the source of truth. This window discovers
interpreters first, lets the user choose targets, and then runs one explicit
operation at a time.
"""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

# 版本号以命令行版为准，避免两处各写一份、日久不同步。
try:  # pragma: no cover - 同目录导入，正常都能成功
    from pip_helper import APP_NAME, APP_VERSION
except Exception:  # noqa: BLE001
    APP_NAME, APP_VERSION = "Python 更新助手", ""


BG = "#171a21"
PANEL = "#20242d"
FG = "#e7eaf0"
MUTED = "#9aa4b2"
ACCENT = "#4da3ff"
GOOD = "#64d18b"
ERROR = "#ff7b72"
CHECKED = "☑"
UNCHECKED = "☐"
# 命令行版会给「正在运行本工具的那个解释器」打上带这个字样的来源标签，
# 默认不勾选它，免得一键操作把工具自己依赖的 Python 也改了。
SELF_LABEL_MARK = "当前助手"


def extract_json(text):
    """从可能混进警告/回溯的输出里挑出真正的 JSON 载荷。

    子进程的 stderr 是并到 stdout 一起读回来的，它只要多打一行，
    整段 json.loads 就会失败。这里逐个候选起点试着解析，取跨度最大的那段：
    真正的载荷一定比"警告里凑巧合法的小片段"长得多。
    """
    decoder = json.JSONDecoder()
    best = None
    best_length = -1
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if end > best_length:
            best, best_length = value, end
    return best


def kill_process_tree(process):
    """结束整棵进程树。

    pip_helper 下面还挂着 pip 这个孙进程，只 terminate 直接子进程的话，
    界面显示「已停止」，pip 却还在后台改文件。所以 Windows 上用 taskkill /T
    整棵杀，其它平台按进程组发信号，最后再兜一次 terminate。
    """
    if process is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001 - 失败就退回到 terminate
        pass
    try:
        if process.poll() is None:
            process.terminate()
    except OSError:
        pass


class PipHelperWindow:
    def __init__(self, root, initial_args):
        self.root = root
        self.initial_args = list(initial_args)
        self.process = None
        self.output_queue = queue.Queue()
        self.selected = {}
        self.mirrors = []
        self.fastest_mirror = ""
        self.initial_args_started = False

        root.title(APP_NAME + ("  v" + APP_VERSION if APP_VERSION else ""))
        root.geometry("1180x780")
        root.minsize(900, 620)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG,
                        font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=FG,
                        font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Section.TLabel", background=BG, foreground=FG,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", padding=(10, 6),
                        font=("Microsoft YaHei UI", 10))
        style.configure("Accent.TButton", padding=(12, 6), foreground="white",
                        background="#1769aa",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#2186d1")])
        style.configure("Treeview", background="#11151b", fieldbackground="#11151b",
                        foreground=FG, rowheight=28, borderwidth=0,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Panel.TCheckbutton", background=PANEL, foreground=FG,
                        focuscolor=PANEL, font=("Microsoft YaHei UI", 9))
        style.map("Panel.TCheckbutton",
                  background=[("active", PANEL)],
                  foreground=[("disabled", MUTED)])
        style.configure("Treeview.Heading", background="#2a313c", foreground=FG,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#254e70")])

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        title_text = APP_NAME + ("  v" + APP_VERSION if APP_VERSION else "")
        ttk.Label(header, text=title_text, style="Title.TLabel").pack(side="left")
        self.status = ttk.Label(header, text="正在扫描 Python...", style="Muted.TLabel")
        self.status.pack(side="right", pady=5)

        action_panel = ttk.Frame(outer, style="Panel.TFrame", padding=9)
        action_panel.pack(fill="x", pady=(0, 9))
        self.scan_button = self._button(action_panel, "重新扫描", self.scan)
        self.scan_button.pack(side="left", padx=(0, 5))
        self.probe_button = self._button(action_panel, "测速镜像", self.probe, accent=True)
        self.probe_button.pack(side="left", padx=5)
        self.configure_button = self._button(action_panel, "配置 pip 源", self.configure)
        self.configure_button.pack(side="left", padx=5)
        self.upgrade_button = self._button(action_panel, "更新选中 pip", self.upgrade)
        self.upgrade_button.pack(side="left", padx=5)
        self.list_button = self._button(action_panel, "列出选中库", self.list_packages)
        self.list_button.pack(side="left", padx=5)
        self.all_button = self._button(action_panel, "执行选中项", self.run_all)
        self.all_button.pack(side="left", padx=5)
        self.restore_button = self._button(action_panel, "恢复官方源", self.restore)
        self.restore_button.pack(side="left", padx=5)
        self.stop_button = self._button(action_panel, "停止", self.stop)
        self.stop_button.pack(side="right", padx=(5, 0))
        self.stop_button.configure(state="disabled")

        target_panel = ttk.Frame(outer, style="Panel.TFrame", padding=9)
        target_panel.pack(fill="both", expand=False, pady=(0, 9))
        target_header = ttk.Frame(target_panel, style="Panel.TFrame")
        target_header.pack(fill="x", pady=(0, 6))
        ttk.Label(target_header, text="选择目标 Python", style="Section.TLabel").pack(side="left")
        self.target_hint = ttk.Label(target_header, text="扫描中...", style="Muted.TLabel")
        self.target_hint.pack(side="left", padx=12)
        self._button(target_header, "全不选", self.clear_selection).pack(side="right", padx=(5, 0))
        self._button(target_header, "全选", self.select_all).pack(side="right")

        tree_frame = ttk.Frame(target_panel, style="Panel.TFrame")
        tree_frame.pack(fill="x", expand=False)
        self.interpreter_tree = ttk.Treeview(
            tree_frame,
            columns=("selected", "version", "pip", "path", "source"),
            show="headings", height=5, selectmode="browse",
        )
        headings = {
            "selected": ("选择", 58),
            "version": ("Python", 100),
            "pip": ("pip", 90),
            "path": ("解释器路径", 510),
            "source": ("发现来源", 270),
        }
        for col, (heading, width) in headings.items():
            self.interpreter_tree.heading(col, text=heading)
            self.interpreter_tree.column(col, width=width, minwidth=width,
                                         anchor="center" if col == "selected" else "w",
                                         stretch=col in ("path", "source"))
        self.interpreter_tree.pack(side="left", fill="x", expand=True)
        target_scroll = ttk.Scrollbar(tree_frame, orient="vertical",
                                      command=self.interpreter_tree.yview)
        target_scroll.pack(side="right", fill="y")
        self.interpreter_tree.configure(yscrollcommand=target_scroll.set)
        self.interpreter_tree.bind("<Button-1>", self.toggle_interpreter)

        mirror_panel = ttk.Frame(outer, style="Panel.TFrame", padding=9)
        mirror_panel.pack(fill="x", pady=(0, 9))
        mirror_top = ttk.Frame(mirror_panel, style="Panel.TFrame")
        mirror_top.pack(fill="x", pady=(0, 5))
        ttk.Label(mirror_top, text="pip 源", style="Section.TLabel").pack(side="left")
        ttk.Label(mirror_top, text="测速后可直接采用最快镜像，也可手动指定。配置源通常对当前用户所有 Python 生效。",
                  style="Muted.TLabel").pack(side="left", padx=12)
        ttk.Label(mirror_top, text="主源：", style="Muted.TLabel").pack(side="right")
        self.mirror = tk.StringVar(value="自动选择")
        self.mirror_box = ttk.Combobox(
            mirror_top, textvariable=self.mirror, state="readonly", width=20,
            values=("自动选择", "清华 TUNA", "阿里云", "腾讯云", "中科大"),
        )
        self.mirror_box.pack(side="right", padx=(5, 0))
        status_row = ttk.Frame(mirror_panel, style="Panel.TFrame")
        status_row.pack(fill="x")
        self.mirror_status = ttk.Label(status_row, text="尚未测速", style="Muted.TLabel")
        self.mirror_status.pack(side="left")
        self.trusted = tk.BooleanVar(value=False)
        self.trusted_box = ttk.Checkbutton(
            status_row, text="写入 trusted-host（SSL 被公司/代理拦截时用）",
            variable=self.trusted, style="Panel.TCheckbutton")
        self.trusted_box.pack(side="right")

        option_row = ttk.Frame(mirror_panel, style="Panel.TFrame")
        option_row.pack(fill="x", pady=(5, 0))
        ttk.Label(option_row, text="排除关键词：", style="Muted.TLabel").pack(side="left")
        self.exclude = tk.StringVar(value="")
        self.exclude_entry = ttk.Entry(option_row, textvariable=self.exclude, width=24)
        self.exclude_entry.pack(side="left", padx=(0, 8))
        ttk.Label(option_row,
                  text="路径里含该词的 Python 一律跳过，多个用逗号分隔，例如 WorkBuddy",
                  style="Muted.TLabel").pack(side="left")

        log_panel = ttk.Frame(outer, style="Panel.TFrame", padding=8)
        log_panel.pack(fill="both", expand=True)
        self.log = tk.Text(log_panel, wrap="none", state="disabled", undo=False,
                           bg="#0f1116", fg=FG, insertbackground=FG,
                           selectbackground="#315b80", relief="flat",
                           font=("Consolas", 10), padx=9, pady=7)
        log_y = ttk.Scrollbar(log_panel, orient="vertical", command=self.log.yview)
        log_x = ttk.Scrollbar(log_panel, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_y.grid(row=0, column=1, sticky="ns")
        log_x.grid(row=1, column=0, sticky="ew")
        log_panel.rowconfigure(0, weight=1)
        log_panel.columnconfigure(0, weight=1)

        self.append("启动后会先扫描系统 Python、虚拟环境和 Conda/uv 环境。\n")
        self.append("勾选目标后，再点击具体操作；更新 pip 只作用于勾选项。\n")
        self.append("「排除关键词」里命中的解释器在执行任何操作时都会被跳过。\n")
        self.root.after(80, self.poll_output)
        self.root.after(160, self.scan)

    @staticmethod
    def _button(parent, text, command, accent=False):
        return ttk.Button(parent, text=text, command=command,
                          style="Accent.TButton" if accent else "TButton")

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def scan(self):
        if self.process is not None:
            return
        self.launch(["--discover-json"], "scan")

    def toggle_interpreter(self, event):
        row = self.interpreter_tree.identify_row(event.y)
        column = self.interpreter_tree.identify_column(event.x)
        if not row or column != "#1":
            return
        self.selected[row] = not self.selected.get(row, False)
        self.refresh_interpreter_row(row)

    def refresh_interpreter_row(self, row):
        values = list(self.interpreter_tree.item(row, "values"))
        if values:
            values[0] = CHECKED if self.selected.get(row, False) else UNCHECKED
            self.interpreter_tree.item(row, values=values)

    def select_all(self):
        for row in self.interpreter_tree.get_children():
            self.selected[row] = True
            self.refresh_interpreter_row(row)

    def clear_selection(self):
        for row in self.interpreter_tree.get_children():
            self.selected[row] = False
            self.refresh_interpreter_row(row)

    def selected_paths(self):
        paths = []
        for row in self.interpreter_tree.get_children():
            if self.selected.get(row, False):
                values = self.interpreter_tree.item(row, "values")
                if len(values) > 3:
                    paths.append(values[3])
        if not paths:
            messagebox.showinfo("未选择目标", "请先勾选一个或多个 Python 解释器。")
        return paths

    def target_args(self, paths=None):
        paths = self.selected_paths() if paths is None else paths
        args = []
        for path in paths:
            args.extend(["-p", path])
        return args

    def mirror_args(self):
        choice = self.mirror.get()
        if choice == "自动选择":
            if self.fastest_mirror:
                return ["--mirror", self.fastest_mirror]
            return []
        return ["--mirror", choice]

    def exclude_args(self):
        """排除关键词（逗号分隔）拆成命令行参数。"""
        args = []
        for word in self.exclude.get().replace("，", ",").split(","):
            word = word.strip()
            if word:
                args.extend(["--exclude", word])
        return args

    def trusted_args(self):
        return ["--trusted"] if self.trusted.get() else []

    def probe(self):
        if self.process is None:
            self.mirror_status.configure(text="正在测速...", foreground=ACCENT)
            self.launch(["--probe-json"], "probe")

    def configure(self):
        paths = self.selected_paths()
        if paths:
            self.launch(self.target_args(paths) + self.exclude_args()
                        + self.mirror_args() + self.trusted_args()
                        + ["--configure-only"], "command")

    def upgrade(self):
        paths = self.selected_paths()
        if paths:
            self.launch(self.target_args(paths) + self.exclude_args()
                        + ["--upgrade-only"], "command")

    def list_packages(self):
        paths = self.selected_paths()
        if paths:
            self.launch(self.target_args(paths) + self.exclude_args()
                        + ["--list-only"], "command")

    def run_all(self):
        paths = self.selected_paths()
        if paths:
            self.launch(self.target_args(paths) + self.exclude_args()
                        + self.mirror_args() + self.trusted_args(), "command")

    def restore(self):
        paths = self.selected_paths()
        if paths:
            self.launch(self.target_args(paths) + self.exclude_args() + ["--restore"], "command")

    def launch(self, args, kind):
        if self.process is not None:
            return
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pip_helper.py")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self.append("\n" + "=" * 86 + "\n")
        self.status.configure(text="运行中...", foreground=ACCENT)
        self.set_busy(True)
        try:
            self.process = subprocess.Popen(
                [sys.executable, script] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self.process = None
            self.set_busy(False)
            self.finish(1, "启动失败：" + repr(exc))
            return
        threading.Thread(target=self.read_output, args=(self.process, kind), daemon=True).start()

    def read_output(self, process, kind):
        chunks = []
        try:
            for line in process.stdout:
                chunks.append(line)
                self.output_queue.put(("line", line))
            code = process.wait()
            self.output_queue.put(("done", (kind, code, "".join(chunks))))
        except Exception as exc:
            self.output_queue.put(("error", (kind, repr(exc), "".join(chunks))))

    def poll_output(self):
        try:
            while True:
                kind, value = self.output_queue.get_nowait()
                if kind == "line":
                    self.append(value)
                elif kind == "done":
                    job, code, output = value
                    self.finish_job(job, code, output)
                else:
                    job, error, output = value
                    self.finish_job(job, 1, output + "\n读取输出失败：" + error)
        except queue.Empty:
            pass
        self.root.after(80, self.poll_output)

    def finish_job(self, kind, code, output):
        if kind == "scan":
            if code == 0:
                self.apply_scan(output)
            else:
                self.target_hint.configure(
                    text="扫描失败（退出码 %s），详见下方日志" % code, foreground=ERROR)
        elif kind == "probe":
            self.apply_probe(output, code)
        self.finish(code)
        # The batch file may pass a CLI mode (for example --list-only). Keep
        # that behavior when the GUI is launched with arguments, after the
        # initial discovery has populated the window.
        if kind == "scan" and code == 0 and self.initial_args and not self.initial_args_started:
            self.initial_args_started = True
            args = list(self.initial_args)
            self.initial_args.clear()
            self.launch(args, "command")

    def apply_scan(self, output):
        rows = extract_json(output)
        if not isinstance(rows, list):
            last = ""
            for line in reversed(output.splitlines()):
                if line.strip():
                    last = line.strip()
                    break
            self.target_hint.configure(text="扫描结果无法解析，详见日志", foreground=ERROR)
            self.append("扫描结果解析失败，最后一行输出：" + last[:200] + "\n")
            return
        previous = {self.interpreter_tree.item(row, "values")[3]: self.selected.get(row, False)
                    for row in self.interpreter_tree.get_children()
                    if len(self.interpreter_tree.item(row, "values")) > 3}
        for row in self.interpreter_tree.get_children():
            self.interpreter_tree.delete(row)
        self.selected.clear()
        for index, item in enumerate(rows):
            path = item.get("path", "")
            labels = item.get("labels", [])
            default = not any(SELF_LABEL_MARK in str(x) for x in labels)
            checked = previous.get(path, default)
            row = self.interpreter_tree.insert(
                "", "end", iid="py-%d" % index,
                values=(CHECKED if checked else UNCHECKED,
                        item.get("version", ""), item.get("pip", ""),
                        path, "、".join(labels)),
            )
            self.selected[row] = checked
        if not rows:
            self.target_hint.configure(text="没有发现可用的 Python 解释器", foreground=ERROR)
        else:
            self.target_hint.configure(
                text="已发现 %d 个解释器（工具自己用的那个默认不勾选）" % len(rows),
                foreground=GOOD)

    def apply_probe(self, output, code):
        rows = extract_json(output)
        self.mirrors = rows if isinstance(rows, list) else []
        alive = [item for item in self.mirrors if not item.get("error")]
        if alive:
            alive.sort(key=lambda item: item.get("cost", 999999))
            fastest = alive[0]
            self.fastest_mirror = fastest.get("name", "")
            # 不碰下拉框：用户手动指定的主源不该被一次测速悄悄改掉。
            self.mirror_status.configure(
                text="最快：%s（约 %.0f ms）" % (fastest.get("name", ""), fastest.get("cost", 0) * 1000),
                foreground=GOOD,
            )
            summary = "；".join("%s %.0f ms" % (x.get("name", ""), x.get("cost", 0) * 1000)
                               for x in alive)
            self.append("测速结果：" + summary + "\n")
        else:
            self.fastest_mirror = ""
            self.mirror_status.configure(text="所有镜像均不可用，将使用默认顺序", foreground=ERROR)
        if code != 0 and not alive:
            self.append("没有可用镜像。\n")

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for button in (self.scan_button, self.probe_button, self.configure_button,
                       self.upgrade_button, self.list_button, self.all_button,
                       self.restore_button):
            button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.mirror_box.configure(state="disabled" if busy else "readonly")
        self.exclude_entry.configure(state="disabled" if busy else "normal")
        self.trusted_box.configure(state="disabled" if busy else "normal")

    def finish(self, code, message=None):
        if message:
            self.append(message + "\n")
        self.status.configure(
            text="完成" if code == 0 else "失败（退出码 %s）" % code,
            foreground=GOOD if code == 0 else ERROR,
        )
        self.process = None
        self.set_busy(False)

    def stop(self):
        if self.process is not None:
            kill_process_tree(self.process)
            self.append("\n已请求停止（连同它启动的 pip 一起结束）。\n")

    def close(self):
        if self.process is not None:
            if not messagebox.askyesno("确认退出", "任务仍在运行，确定要停止并退出吗？"):
                return
            kill_process_tree(self.process)
        self.root.destroy()


def main():
    root = tk.Tk()
    PipHelperWindow(root, sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
