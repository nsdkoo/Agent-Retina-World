from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from screen_agent.voice.assistant import VoiceAssistant

STATUS_LABELS = {
    "idle": ("● 待机", "#64748b"),
    "listening": ("● 聆听中", "#22c55e"),
    "processing": ("● 执行中", "#3b82f6"),
    "session": ("● 连续对话", "#a855f7"),
}


class VoiceSidebar:
    """桌面右侧常驻语音助手侧边栏（无需对话窗口打字）。"""

    WIDTH = 300

    def __init__(self, assistant: VoiceAssistant) -> None:
        self.assistant = assistant
        self.root = tk.Tk()
        self.root.title("Agent-Retina-World")
        self.root.configure(bg="#0f1419")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, True)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{self.WIDTH}x{sh - 40}+{sw - self.WIDTH - 8}+20")

        self._build_ui()
        assistant.on_status(self._update_status)
        assistant.on_transcript(self._append_log)
        assistant.on_result(self._append_log)

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg="#0f1419")
        header.pack(fill=tk.X, padx=16, pady=(16, 8))
        tk.Label(
            header,
            text="Agent-Retina-World",
            fg="#e7ecf3",
            bg="#0f1419",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            header,
            text="语音常驻 · 呼唤名字即可",
            fg="#8b9cb3",
            bg="#0f1419",
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(2, 0))

        self.status_label = tk.Label(
            self.root,
            text="● 待机",
            fg="#64748b",
            bg="#0f1419",
            font=("Segoe UI", 11, "bold"),
        )
        self.status_label.pack(anchor=tk.W, padx=16, pady=8)

        hint = "、".join(self.assistant.wake_names[:3])
        tk.Label(
            self.root,
            text=f"唤醒：「{hint}，截图」\n连续对话：唤醒后直接说指令\n结束：说「退出」",
            fg="#8b9cb3",
            bg="#1a2332",
            font=("Segoe UI", 9),
            justify=tk.LEFT,
            padx=12,
            pady=10,
        ).pack(fill=tk.X, padx=16, pady=(0, 8))

        tk.Label(
            self.root,
            text="交互记录",
            fg="#8b9cb3",
            bg="#0f1419",
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, padx=16)

        self.log = scrolledtext.ScrolledText(
            self.root,
            height=20,
            bg="#1a2332",
            fg="#e7ecf3",
            font=("Consolas", 9),
            relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self.log.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)
        self.log.configure(state=tk.DISABLED)

        btn_frame = tk.Frame(self.root, bg="#0f1419")
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 16))
        tk.Button(
            btn_frame,
            text="退出",
            command=self._quit,
            bg="#2a3548",
            fg="#e7ecf3",
            relief=tk.FLAT,
            padx=12,
            pady=4,
        ).pack(side=tk.RIGHT)

    def _update_status(self, status: str) -> None:
        label, color = STATUS_LABELS.get(status, ("●", "#64748b"))

        def apply() -> None:
            self.status_label.configure(text=label, fg=color)

        self.root.after(0, apply)

    def _append_log(self, text: str) -> None:
        def apply() -> None:
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, text + "\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)

        self.root.after(0, apply)

    def _quit(self) -> None:
        self.assistant.stop()
        self.root.destroy()

    def run(self) -> None:
        self.assistant.run_in_background()
        self.root.mainloop()
