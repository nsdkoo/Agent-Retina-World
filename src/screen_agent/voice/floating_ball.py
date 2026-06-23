from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from screen_agent.voice.assistant import VoiceAssistant

BALL_SIZE = 52
COLORS = {
    "idle": "#64748b",
    "listening": "#22c55e",
    "processing": "#3b82f6",
    "session": "#a855f7",
}


class FloatingBallUI:
    """桌面悬浮球 · 可拖动 · 双击展开日志。"""

    def __init__(self, assistant: VoiceAssistant) -> None:
        self.assistant = assistant
        self._pulse_on = False
        self._popover: tk.Toplevel | None = None

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#0f1419")

        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"{BALL_SIZE}x{BALL_SIZE}+{sw - BALL_SIZE - 24}+80")

        self.canvas = tk.Canvas(
            self.root,
            width=BALL_SIZE,
            height=BALL_SIZE,
            bg="#0f1419",
            highlightthickness=0,
        )
        self.canvas.pack()
        self._ball = self.canvas.create_oval(4, 4, BALL_SIZE - 4, BALL_SIZE - 4, fill=COLORS["idle"], outline="#1e293b", width=2)
        self._label = self.canvas.create_text(BALL_SIZE // 2, BALL_SIZE // 2, text="AR", fill="white", font=("Segoe UI", 10, "bold"))

        self.canvas.bind("<Button-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Double-Button-1>", lambda _e: self._toggle_popover())
        self.canvas.bind("<Button-3>", self._show_menu)

        assistant.on_status(self._on_status)
        assistant.on_transcript(lambda t: self._log(f"🎤 {t}"))
        assistant.on_result(lambda t: self._log(f"→ {t}"))
        assistant.on_session(lambda active: self._log("💬 连续对话开始" if active else "💤 连续对话结束"))

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event: tk.Event) -> None:
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _on_status(self, status: str) -> None:
        color = COLORS.get(status, COLORS["idle"])

        def apply() -> None:
            self.canvas.itemconfigure(self._ball, fill=color)
            if status == "listening":
                self._start_pulse()
            else:
                self._stop_pulse()

        self.root.after(0, apply)

    def _start_pulse(self) -> None:
        if self._pulse_on:
            return
        self._pulse_on = True
        self._pulse()

    def _stop_pulse(self) -> None:
        self._pulse_on = False

    def _pulse(self) -> None:
        if not self._pulse_on:
            self.canvas.itemconfigure(self._ball, outline="#1e293b", width=2)
            return
        w = self.canvas.itemcget(self._ball, "outlinewidth")
        nw = 4 if float(w) <= 2 else 2
        self.canvas.itemconfigure(self._ball, outline=COLORS["listening"], width=nw)
        self.root.after(400, self._pulse)

    def _log(self, text: str) -> None:
        if self._popover and self._popover.winfo_exists():
            self.root.after(0, lambda: self._append_popover(text))

    def _toggle_popover(self) -> None:
        if self._popover and self._popover.winfo_exists():
            self._popover.destroy()
            self._popover = None
            return
        self._popover = tk.Toplevel(self.root)
        self._popover.overrideredirect(True)
        self._popover.attributes("-topmost", True)
        self._popover.configure(bg="#1a2332")
        x = self.root.winfo_x() - 240
        y = self.root.winfo_y()
        self._popover.geometry(f"260x200+{max(x, 8)}+{y}")

        tk.Label(
            self._popover,
            text="Agent-Retina-World",
            fg="#e7ecf3",
            bg="#1a2332",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor=tk.W, padx=8, pady=(6, 2))

        hint = "、".join(self.assistant.wake_names[:2])
        tk.Label(
            self._popover,
            text=f"唤醒后免唤醒连续对话\n说「{hint}，截图」",
            fg="#8b9cb3",
            bg="#1a2332",
            font=("Segoe UI", 8),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=8)

        self._log_text = scrolledtext.ScrolledText(
            self._popover,
            height=8,
            bg="#0f1419",
            fg="#e7ecf3",
            font=("Consolas", 8),
            relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self._log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        self._log_text.configure(state=tk.DISABLED)

    def _append_popover(self, text: str) -> None:
        if not hasattr(self, "_log_text"):
            return
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.insert(tk.END, text + "\n")
        self._log_text.see(tk.END)
        self._log_text.configure(state=tk.DISABLED)

    def _show_menu(self, event: tk.Event) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="展开日志", command=self._toggle_popover)
        menu.add_command(label="退出", command=self._quit)
        menu.tk_popup(event.x_root, event.y_root)

    def _quit(self) -> None:
        self.assistant.stop()
        if self._popover:
            self._popover.destroy()
        self.root.destroy()

    def run(self) -> None:
        self.assistant.run_in_background()
        self.root.mainloop()
