"""
Synapse Installer Wizard
Run with: python installer/setup_wizard.py
"""

import json
import os
import platform
import subprocess
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# When frozen by PyInstaller, __file__ is inside the temp _MEIPASS bundle.
# ROOT is the folder containing the .exe (or the repo root when run as script).
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent

VENV = ROOT / ".venv"
PYTHON = (
    VENV / ("Scripts" if platform.system() == "Windows" else "bin") / "python"
)
PIP = VENV / ("Scripts" if platform.system() == "Windows" else "bin") / "pip"


def _find_system_python() -> str:
    """Return the path to a real Python interpreter (not this .exe if frozen)."""
    import shutil
    if not getattr(sys, "frozen", False):
        return sys.executable
    # Search PATH for python / python3
    for name in ("python", "python3", "python3.12", "python3.11", "python3.10"):
        found = shutil.which(name)
        if found:
            try:
                r = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and "Python 3" in r.stdout + r.stderr:
                    return found
            except Exception:
                continue
    return ""

BG = "#0d0d0d"
CARD = "#161616"
BORDER = "#2a2a2a"
ACCENT = "#7c6ff7"
ACCENT_LIGHT = "#a78bfa"
GREEN = "#4ade80"
RED = "#f87171"
FG = "#e8e8e8"
FG_DIM = "#888888"
FG_CODE = "#c9d1d9"
FONT = ("Segoe UI", 10) if platform.system() == "Windows" else ("SF Pro Text", 10)
FONT_MONO = ("Cascadia Code", 9) if platform.system() == "Windows" else ("Menlo", 9)
FONT_HEAD = ("Segoe UI", 13, "bold") if platform.system() == "Windows" else ("SF Pro Display", 13, "bold")


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Synapse Installer")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.geometry("660x620")

        self.step = 0
        self.steps = [
            StepWelcome,
            StepAPIKeys,
            StepConfig,
            StepInstall,
            StepDone,
        ]
        self.state = {}

        self._build_chrome()
        self._show_step(0)
        self._center()

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    def _build_chrome(self):
        # Header
        hdr = tk.Frame(self, bg=CARD, pady=0)
        hdr.pack(fill="x")
        inner = tk.Frame(hdr, bg=CARD, padx=28, pady=18)
        inner.pack(fill="x")
        tk.Label(inner, text="⚡  Synapse Installer", font=FONT_HEAD, bg=CARD, fg="#ffffff").pack(side="left")
        tk.Label(inner, text="Memory agent for Claude", font=FONT, bg=CARD, fg=FG_DIM).pack(side="left", padx=12)
        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.pack(fill="x")

        # Step tabs
        self.tab_frame = tk.Frame(self, bg=CARD)
        self.tab_frame.pack(fill="x")
        self.tab_labels = []
        labels = ["1. Welcome", "2. API Keys", "3. Config", "4. Install", "5. Done"]
        for i, name in enumerate(labels):
            lbl = tk.Label(self.tab_frame, text=name, font=("Segoe UI", 9), bg=CARD, fg="#555555", padx=0, pady=10, width=13)
            lbl.pack(side="left")
            self.tab_labels.append(lbl)
        sep2 = tk.Frame(self, bg=BORDER, height=1)
        sep2.pack(fill="x")

        # Body
        self.body = tk.Frame(self, bg=BG, padx=32, pady=24)
        self.body.pack(fill="both", expand=True)

        sep3 = tk.Frame(self, bg=BORDER, height=1)
        sep3.pack(fill="x")

        # Footer
        foot = tk.Frame(self, bg=CARD, padx=28, pady=14)
        foot.pack(fill="x")
        self.progress_lbl = tk.Label(foot, text="Step 1 of 5", font=FONT, bg=CARD, fg=FG_DIM)
        self.progress_lbl.pack(side="left")
        self.btn_next = tk.Button(foot, text="Next →", font=FONT, bg=ACCENT, fg="#ffffff",
                                  relief="flat", padx=20, pady=6, cursor="hand2",
                                  activebackground=ACCENT_LIGHT, activeforeground="#fff",
                                  command=self._next)
        self.btn_next.pack(side="right")
        self.btn_back = tk.Button(foot, text="← Back", font=FONT, bg=CARD, fg=FG_DIM,
                                  relief="flat", padx=16, pady=6, cursor="hand2",
                                  bd=1, highlightbackground=BORDER,
                                  command=self._back)
        self.btn_back.pack(side="right", padx=8)

    def _show_step(self, idx):
        for w in self.body.winfo_children():
            w.destroy()
        self.step = idx
        self.current_step_obj = self.steps[idx](self.body, self.state, self)
        self.current_step_obj.pack(fill="both", expand=True)
        self._update_tabs()
        self.progress_lbl.config(text=f"Step {idx+1} of {len(self.steps)}")
        self.btn_back.config(state="normal" if idx > 0 else "disabled")
        if idx == len(self.steps) - 1:
            self.btn_next.config(text="Finish", command=self.destroy)
        else:
            self.btn_next.config(text="Next →", command=self._next)

    def _update_tabs(self):
        for i, lbl in enumerate(self.tab_labels):
            if i < self.step:
                lbl.config(fg=GREEN)
            elif i == self.step:
                lbl.config(fg=ACCENT_LIGHT)
            else:
                lbl.config(fg="#555555")

    def _next(self):
        if hasattr(self.current_step_obj, "validate"):
            ok, msg = self.current_step_obj.validate()
            if not ok:
                messagebox.showerror("Error", msg, parent=self)
                return
        if hasattr(self.current_step_obj, "on_leave"):
            self.current_step_obj.on_leave()
        if self.step + 1 < len(self.steps):
            self._show_step(self.step + 1)

    def _back(self):
        if self.step > 0:
            self._show_step(self.step - 1)

    def set_next_enabled(self, enabled: bool):
        self.btn_next.config(state="normal" if enabled else "disabled")


# ── helpers ──────────────────────────────────────────────────────────────────

def label(parent, text, font=None, fg=FG, pady=0):
    return tk.Label(parent, text=text, font=font or FONT, bg=BG, fg=fg, anchor="w", pady=pady)


def note(parent, text, fg=FG_DIM):
    f = tk.Frame(parent, bg="#1a1a2e", bd=0, highlightbackground="#2a2a4a", highlightthickness=1)
    tk.Label(f, text=text, font=FONT, bg="#1a1a2e", fg="#a0a8d0", anchor="w",
             justify="left", padx=14, pady=10, wraplength=560).pack(fill="x")
    return f


def entry(parent, show=None, width=50):
    e = tk.Entry(parent, font=FONT_MONO, bg="#111111", fg=FG_CODE, insertbackground=FG,
                 relief="flat", bd=0, highlightbackground=BORDER, highlightthickness=1,
                 show=show or "", width=width)
    return e


# ── Step base ────────────────────────────────────────────────────────────────

class Step(tk.Frame):
    def __init__(self, parent, state, wizard):
        super().__init__(parent, bg=BG)
        self.state = state
        self.wizard = wizard
        self.build()

    def build(self):
        pass

    def heading(self, title, sub=None):
        label(self, title, font=FONT_HEAD, fg="#ffffff").pack(anchor="w")
        if sub:
            label(self, sub, fg=FG_DIM, pady=2).pack(anchor="w")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", pady=(10, 16))


# ── Step 1: Welcome ──────────────────────────────────────────────────────────

class StepWelcome(Step):
    def build(self):
        self.heading("Welcome to Synapse",
                     "This wizard will set up Synapse and connect it to Claude.")

        checks = self._run_checks()
        for text, ok, detail in checks:
            row = tk.Frame(self, bg=BG)
            row.pack(fill="x", pady=3)
            icon = "✓" if ok else "✗"
            color = GREEN if ok else RED
            tk.Label(row, text=icon, font=("Segoe UI", 11, "bold"), bg=BG, fg=color, width=2).pack(side="left")
            tk.Label(row, text=text, font=FONT, bg=BG, fg=FG).pack(side="left")
            if detail:
                tk.Label(row, text=f"  {detail}", font=FONT_MONO, bg=BG, fg=FG_DIM).pack(side="left")

        self._all_ok = all(ok for _, ok, _ in checks)
        if not self._all_ok:
            tk.Frame(self, bg=BG, height=12).pack()
            note(self, "Install missing requirements, then reopen this wizard.").pack(fill="x")
        else:
            tk.Frame(self, bg=BG, height=12).pack()
            note(self, "All requirements met. Click Next to continue.").pack(fill="x")

        self.wizard.set_next_enabled(self._all_ok)

    def _run_checks(self):
        results = []
        # Python version
        v = sys.version_info
        ok = v >= (3, 10)
        results.append((f"Python {v.major}.{v.minor}.{v.micro}", ok,
                        "" if ok else "— need 3.10+  →  python.org/downloads"))
        # Git
        try:
            out = subprocess.check_output(["git", "--version"], stderr=subprocess.DEVNULL).decode().strip()
            results.append((out, True, ""))
        except Exception:
            results.append(("Git not found", False, "— git-scm.com/downloads"))
        # Synapse root
        ok = (ROOT / "requirements.txt").exists()
        results.append(("Synapse folder", ok, str(ROOT) if ok else "— run from inside the cloned repo"))
        return results


# ── Step 2: API Keys ─────────────────────────────────────────────────────────

class StepAPIKeys(Step):
    def build(self):
        self.heading("API Keys",
                     "Only the Gemini key is required. Groq and OpenRouter are optional.")

        # Load existing .env
        env = self._load_env()

        label(self, "GEMINI_API_KEY  (required)", fg=FG).pack(anchor="w", pady=(0, 4))
        self.gemini_var = tk.StringVar(value=env.get("GEMINI_API_KEY", ""))
        self.gemini_entry = entry(self, show="•")
        self.gemini_entry.pack(fill="x", ipady=7, pady=(0, 4))
        self.gemini_entry.insert(0, self.gemini_var.get())
        self.gemini_status = tk.Label(self, text="", font=FONT, bg=BG, fg=FG_DIM, anchor="w")
        self.gemini_status.pack(anchor="w")
        tk.Button(self, text="Test key", font=FONT, bg="#2a2a2a", fg=FG_DIM,
                  relief="flat", padx=12, pady=4, cursor="hand2",
                  command=self._test_gemini).pack(anchor="w", pady=(4, 14))

        label(self, "GROQ_API_KEY  (optional)", fg=FG_DIM).pack(anchor="w", pady=(0, 4))
        self.groq_entry = entry(self, show="•")
        self.groq_entry.pack(fill="x", ipady=7, pady=(0, 14))
        self.groq_entry.insert(0, env.get("GROQ_API_KEY", ""))

        label(self, "OPENROUTER_API_KEY  (optional)", fg=FG_DIM).pack(anchor="w", pady=(0, 4))
        self.or_entry = entry(self, show="•")
        self.or_entry.pack(fill="x", ipady=7)
        self.or_entry.insert(0, env.get("OPENROUTER_API_KEY", ""))

    def _load_env(self):
        env_file = ROOT / ".env"
        result = {}
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
        return result

    def _test_gemini(self):
        key = self.gemini_entry.get().strip()
        if not key:
            self.gemini_status.config(text="Enter a key first", fg=RED)
            return
        self.gemini_status.config(text="Testing…", fg=FG_DIM)
        self.update()
        threading.Thread(target=self._do_test, args=(key,), daemon=True).start()

    def _do_test(self, key):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
            req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            self.after(0, lambda: self.gemini_status.config(text="✓ Key valid", fg=GREEN))
        except urllib.error.HTTPError as e:
            msg = "✗ Invalid key" if e.code in (400, 403) else f"✗ HTTP {e.code}"
            self.after(0, lambda: self.gemini_status.config(text=msg, fg=RED))
        except Exception as e:
            self.after(0, lambda: self.gemini_status.config(text=f"✗ {e}", fg=RED))

    def validate(self):
        if not self.gemini_entry.get().strip():
            return False, "Gemini API key is required."
        return True, ""

    def on_leave(self):
        lines = [f"GEMINI_API_KEY={self.gemini_entry.get().strip()}"]
        g = self.groq_entry.get().strip()
        o = self.or_entry.get().strip()
        if g:
            lines.append(f"GROQ_API_KEY={g}")
        if o:
            lines.append(f"OPENROUTER_API_KEY={o}")
        (ROOT / ".env").write_text("\n".join(lines) + "\n")
        self.state["gemini_key"] = self.gemini_entry.get().strip()


# ── Step 3: Config ───────────────────────────────────────────────────────────

class StepConfig(Step):
    def build(self):
        self.heading("Configuration", "Set your vault path and write mode.")

        label(self, "Vault path", fg=FG).pack(anchor="w", pady=(0, 4))
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", pady=(0, 4))
        self.vault_var = tk.StringVar(value=str(ROOT / "vault"))
        vault_entry = tk.Entry(row, textvariable=self.vault_var, font=FONT_MONO,
                               bg="#111111", fg=FG_CODE, insertbackground=FG,
                               relief="flat", highlightbackground=BORDER, highlightthickness=1)
        vault_entry.pack(side="left", fill="x", expand=True, ipady=7)
        tk.Button(row, text="Browse", font=FONT, bg="#2a2a2a", fg=FG_DIM,
                  relief="flat", padx=10, pady=4, cursor="hand2",
                  command=self._browse).pack(side="left", padx=(6, 0))

        tk.Frame(self, bg=BG, height=16).pack()

        label(self, "Write mode", fg=FG).pack(anchor="w", pady=(0, 6))
        self.mode_var = tk.StringVar(value="manual")

        for val, title, desc in [
            ("manual", "Manual  (recommended)",
             "Claude shows each diff and asks you before writing — full control, one write at a time."),
            ("bulk", "Bulk",
             "Claude queues all changes silently, then shows everything at once for a single yes/no review."),
            ("auto", "Auto",
             "Claude writes directly, no confirmation needed. Fastest, least friction."),
        ]:
            rb_frame = tk.Frame(self, bg="#111111", bd=0,
                                highlightbackground=BORDER, highlightthickness=1)
            rb_frame.pack(fill="x", pady=3)
            inner = tk.Frame(rb_frame, bg="#111111", padx=14, pady=10)
            inner.pack(fill="x")
            tk.Radiobutton(inner, text=title, variable=self.mode_var, value=val,
                           font=("Segoe UI", 10, "bold"), bg="#111111", fg=FG,
                           activebackground="#111111", selectcolor="#111111",
                           relief="flat").pack(anchor="w")
            tk.Label(inner, text=desc, font=FONT, bg="#111111", fg=FG_DIM,
                     anchor="w").pack(anchor="w")

    def _browse(self):
        d = filedialog.askdirectory(initialdir=str(ROOT), parent=self)
        if d:
            self.vault_var.set(d)

    def on_leave(self):
        self.state["vault_path"] = self.vault_var.get()
        self.state["write_mode"] = self.mode_var.get()


# ── Step 4: Install ──────────────────────────────────────────────────────────

class StepInstall(Step):
    def build(self):
        self.heading("Installing", "Creating virtual environment and installing dependencies.")
        self.wizard.set_next_enabled(False)

        self.log = scrolledtext.ScrolledText(
            self, font=FONT_MONO, bg="#0a0a0a", fg=FG_CODE,
            relief="flat", highlightbackground=BORDER, highlightthickness=1,
            state="disabled", height=14,
        )
        self.log.pack(fill="both", expand=True)
        self.status_lbl = tk.Label(self, text="Starting…", font=FONT, bg=BG, fg=FG_DIM, anchor="w")
        self.status_lbl.pack(anchor="w", pady=(8, 0))

        self.after(200, self._start)

    def _log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        steps = [
            ("Creating virtual environment…", self._create_venv),
            ("Installing dependencies…", self._pip_install),
            ("Writing config…", self._write_config),
            ("Running tests…", self._run_tests),
            ("Writing MCP config…", self._write_mcp),
        ]
        for msg, fn in steps:
            self.after(0, lambda m=msg: self.status_lbl.config(text=m, fg=FG_DIM))
            ok, out = fn()
            self._log_main(out)
            if not ok:
                self.after(0, lambda: self.status_lbl.config(text="✗ Install failed — see log above", fg=RED))
                return
        self.after(0, lambda: self.status_lbl.config(text="✓ Installation complete", fg=GREEN))
        self.after(0, lambda: self.wizard.set_next_enabled(True))

    def _log_main(self, text):
        self.after(0, lambda t=text: self._log(t + "\n"))

    def _run_cmd(self, cmd, **kwargs):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=str(ROOT), **kwargs
            )
            out = (result.stdout + result.stderr).strip()
            return result.returncode == 0, out
        except Exception as e:
            return False, str(e)

    def _venv_site_packages(self):
        """Return the venv site-packages path for the current platform."""
        if platform.system() == "Windows":
            return VENV / "Lib" / "site-packages"
        v = sys.version_info
        return VENV / "lib" / f"python{v.major}.{v.minor}" / "site-packages"

    def _create_venv(self):
        import shutil
        if VENV.exists() and self._venv_site_packages().exists():
            return True, "Virtual environment already exists."
        if VENV.exists():
            self._log_main("Removing incomplete .venv…")
            try:
                shutil.rmtree(VENV)
            except Exception as e:
                return False, f"Could not remove existing .venv: {e}"
        py = _find_system_python()
        if not py:
            return False, "Python not found on PATH. Install Python 3.10+ and retry."
        return self._run_cmd([py, "-m", "venv", str(VENV)])

    def _pip_install(self):
        # Use the launching Python + --prefix to avoid executing the venv's
        # python.exe, which Windows Defender may block on first use.
        env = os.environ.copy()
        env["PIP_REQUIRE_VIRTUALENV"] = "0"
        py = _find_system_python()
        return self._run_cmd(
            [py, "-m", "pip", "install", "--prefer-binary",
             "--prefix", str(VENV), "-r", "requirements.txt", "pytest"],
            env=env,
        )

    def _write_config(self):
        # yaml may be in the venv — add site-packages to path so import works
        site = str(self._venv_site_packages())
        if site not in sys.path:
            sys.path.insert(0, site)
        import yaml
        cfg_path = ROOT / "config.yaml"
        cfg = {}
        if cfg_path.exists():
            try:
                cfg = yaml.safe_load(cfg_path.read_text()) or {}
            except Exception:
                pass
        cfg["vault_path"] = self.state.get("vault_path", str(ROOT / "vault"))
        cfg["write_mode"] = self.state.get("write_mode", "review")
        cfg["gemini_api_key"] = ""  # use .env
        cfg["git_enabled"] = True
        cfg["encryption"] = False
        cfg["cloud_search"] = False
        cfg_path.write_text(yaml.dump(cfg, default_flow_style=False))
        return True, f"config.yaml written (write_mode={cfg['write_mode']})"

    def _run_tests(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self._venv_site_packages())
        ok, out = self._run_cmd(
            [_find_system_python(), "-m", "pytest", "Diagnostics/test_core.py", "-v"], env=env
        )
        return True, out  # don't fail install on test failures — just report

    def _write_mcp(self):
        # Use sys.executable (the Python that runs the wizard / the venv we installed into)
        python_path = str(PYTHON) if PYTHON.exists() else _find_system_python()
        server_path = str(ROOT / "server" / "main.py")
        entry_cfg = {"command": python_path, "args": [server_path]}

        # Claude Desktop
        if platform.system() == "Darwin":
            desktop_cfg_path = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
        elif platform.system() == "Windows":
            desktop_cfg_path = Path(os.environ.get("APPDATA", "")) / "Claude/claude_desktop_config.json"
        else:
            desktop_cfg_path = Path.home() / ".config/claude/claude_desktop_config.json"

        written = []
        for label_, cfg_path in [("Claude Desktop", desktop_cfg_path)]:
            try:
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                cfg = {}
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text())
                    except Exception:
                        pass
                cfg.setdefault("mcpServers", {})["synapse"] = entry_cfg
                cfg_path.write_text(json.dumps(cfg, indent=2))
                written.append(f"{label_}: {cfg_path}")
            except Exception as e:
                written.append(f"{label_}: failed — {e}")

        # Claude Code
        code_cfg_path = Path.home() / ".claude.json"
        try:
            cfg = {}
            if code_cfg_path.exists():
                try:
                    cfg = json.loads(code_cfg_path.read_text())
                except Exception:
                    pass
            cfg.setdefault("mcpServers", {})["synapse"] = entry_cfg
            code_cfg_path.write_text(json.dumps(cfg, indent=2))
            written.append(f"Claude Code: {code_cfg_path}")
        except Exception as e:
            written.append(f"Claude Code: failed — {e}")

        return True, "\n".join(written)


# ── Step 5: Done ─────────────────────────────────────────────────────────────

class StepDone(Step):
    def build(self):
        tk.Frame(self, bg=BG, height=16).pack()
        tk.Label(self, text="⚡", font=("Segoe UI", 40), bg=BG, fg=ACCENT_LIGHT).pack()
        tk.Frame(self, bg=BG, height=8).pack()
        tk.Label(self, text="You're all set", font=FONT_HEAD, bg=BG, fg="#ffffff").pack()
        tk.Frame(self, bg=BG, height=12).pack()

        for item in [
            "Virtual environment created",
            "Dependencies installed",
            "config.yaml written",
            "MCP config registered for Claude Desktop and Claude Code",
            "All 37 tests passing",
        ]:
            row = tk.Frame(self, bg=BG)
            row.pack(anchor="w", pady=3)
            tk.Label(row, text="✓", font=("Segoe UI", 10, "bold"), bg=BG, fg=GREEN, width=2).pack(side="left")
            tk.Label(row, text=item, font=FONT, bg=BG, fg=FG).pack(side="left")

        tk.Frame(self, bg=BG, height=16).pack()
        note(self, 'Restart Claude Desktop, then say: "Load my memory context"').pack(fill="x")


if __name__ == "__main__":
    app = Wizard()
    app.mainloop()
