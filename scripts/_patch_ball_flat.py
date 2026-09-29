"""悬浮球扁平化重设计：纯色圆盘 + 中心状态点呼吸（无 3D/无旋转/无轨道点）。"""
import pathlib

p = pathlib.Path('src/screen_agent/voice/ui_qt/ball.py')
s = p.read_text(encoding='utf-8')

# ---- 1) 清死代码与 import ----
s = s.replace('''from PyQt6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)''', '''from PyQt6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPen,
)''')

s = s.replace('''# Apple Intelligence 流光配色（固定顺序，勿打乱）
AURORA = ["#0894FF", "#C959DD", "#FF2E54", "#FF9004"]

# 状态 → (转速 deg/s, 强度 0-1, 呼吸幅度)
STATE_TUNING = {
    "idle": (40, 0.40, 0.15),
    "listening": (130, 1.00, 0.10),
    "processing": (220, 0.85, 0.05),
    "speaking": (130, 0.95, 0.20),
    "session": (130, 1.00, 0.10),
}''', '''# 状态 → 呼吸幅度（唯一动效：中心点/圆盘透明度轻呼吸）
STATE_TUNING = {
    "idle": (0.15,),
    "listening": (0.10,),
    "processing": (0.05,),
    "speaking": (0.20,),
    "session": (0.10,),
}''')

old_aurora = '''def _aurora_gradient(cx: float, cy: float, angle: float):  # noqa: ANN201 - 保留给后续视觉扩展
    from PyQt6.QtGui import QConicalGradient

    grad = QConicalGradient(cx, cy, angle)
    for i, hex_color in enumerate(AURORA):
        grad.setColorAt(i / len(AURORA), QColor(hex_color))
    grad.setColorAt(1.0, QColor(AURORA[0]))
    return grad


'''
assert old_aurora in s
s = s.replace(old_aurora, '')

# ---- 2) _tick：删 _angle，tuning 单元组 ----
s = s.replace('''        self._status = "idle"
        self._angle = 0.0''', '''        self._status = "idle"''')
old_tick = '''        speed, _intensity, breathe = STATE_TUNING.get(self._status, STATE_TUNING["idle"])
        self._angle = (self._angle + speed * dt) % 360.0'''
new_tick = '''        (breathe,) = STATE_TUNING.get(self._status, STATE_TUNING["idle"])'''
assert old_tick in s
s = s.replace(old_tick, new_tick)

# ---- 3) paintEvent 重写为扁平形态 ----
old = s[s.index('    def paintEvent(self, event) -> None:  # noqa: N802'):s.index('    # ---- 交互', s.index('def paintEvent'))]
new = '''    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        (breathe,) = STATE_TUNING.get(self._status, STATE_TUNING["idle"])
        active = self._status in ("listening", "processing", "speaking", "session")
        accent = QColor(STATE_ACCENTS.get(self._status, STATE_ACCENTS["idle"]))
        hover = self._hover
        # 轻度呼吸：仅作用于中心点透明度（幅度 ≤15%）
        pulse = breathe * (0.5 + 0.5 * math.sin(time.monotonic() * 2 * math.pi / 2.4))

        cx = cy = BALL_SIZE / 2
        radius = DOT_RADIUS + hover * 0.8

        # 柔和投影（浮起来，不重）
        for i in range(4):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(58, 48, 32, int((6 + i * 4) * (0.6 + hover * 0.3))))
            spread = 4 - i
            painter.drawEllipse(QPointF(cx, cy + 2.0), radius + spread, radius + spread)

        # 扁平圆盘：纯色填充 + 1px 描边（无渐变无高光）
        disc_alpha = 235 + int(hover * 15)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(250, 248, 242, min(255, disc_alpha)))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.setPen(QPen(QColor(150, 138, 112, 200), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # 中心状态点：唯一的状态表达（颜色 + 轻呼吸透明度）
        base_alpha = 150 + int(70 * (1.0 - pulse))
        if not active:
            core = QColor(STATE_ACCENTS["idle"])
        else:
            core = QColor(accent)
        core.setAlpha(min(255, base_alpha))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(cx, cy), 3.2, 3.2)

        painter.end()


'''
s = s.replace(old, new)
p.write_text(s, encoding='utf-8'); print('ball flat ok')

# ---- 4) _submit_text 状态卡死修复 ----
p = pathlib.Path('src/screen_agent/voice/ui_qt/ball.py'); s = p.read_text(encoding='utf-8')
old = '''    def _submit_text(self, text: str) -> None:'''
idx = s.index(old)
seg = s[idx:idx + 900]
print('--- _submit_text 原文 ---')
print(seg)
