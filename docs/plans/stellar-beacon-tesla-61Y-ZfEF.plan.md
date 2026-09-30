# 悬浮球扁平化重设计 + 状态卡死 bug 修复

## 用户要求

"太丑了…简单点 不要这样旋转 默认的形态也是 扁平 最多轻度动效 不要这样 现在太丑了"

→ 扁平极简悬浮球：无 3D 金属感、无旋转/轨道点、最多轻度呼吸动效。

## 现状诊断（Explore 已确认，ball.py 587 行）

1. **金属感的来源**（要删）：

- L155 左上偏焦 `QRadialGradient`（经典金属球打光）
- L175-177 内侧高光圈
- idle `base_alpha=0.52` 半透明叠深色壁纸 → 发灰
- L170-174 暗棕描边强化"金属圆球"轮廓

2. **顶部橙点卡死 = 真 bug**：`_submit_text`（L573-584）`set_status("processing")` 后无 `finally` 回滚，`handle_command` 抛异常 → 球永久卡 processing
3. **死代码**：L39 `AURORA`、L60-67 `_aurora_gradient`（零调用）、L135 `intensity`（死变量）
4. 交互（点击/拖拽/悬停）、Alt+Space 热键、托盘——**不动**

## 方案：纯扁平单圆盘

保留：柔和投影（4 层暖色暗部，L147-152，这是"浮起来"不是 3D）、点击/拖拽/悬停逻辑、窗口标志。

删除：偏焦径向渐变 → **纯色扁平圆盘**；内侧高光圈；呼吸环/轨道点/涟漪三种状态装饰；`_angle` 与 STATE_TUNING 的 speed 列；死代码 `_aurora_gradient`/`AURORA`/`intensity`。

**新视觉规格**（唯一圆盘 + 中心状态点，动效只走"透明度呼吸"）：

| 元素 | 规格 |
| --- | --- |
| 圆盘 | 纯色填充 `rgba(250,248,242, 235)`，悬停 alpha +15、半径 +0.5；无渐变无高光 |
| 描边 | 1px `rgba(150,138,112, 200)` |
| 投影 | 保留现有 4 层暖色软影 |
| 中心点 | 半径 3.2px：idle `#9a917f`、listening/session `#6aa87f`、processing `#b45309`、speaking `#d97706` |
| 唯一动效 | 中心点 + 圆盘 alpha 以 2.4s 周期呼吸（`_breathe_phase` 复用，幅度 ≤15%）；无半径动画、无旋转 |


**状态卡死修复**：`QtFloatingBall._submit_text` 包 `try/finally`，finally 里 `set_status("idle")`（成功路径本就有后续状态流转，仅在异常时兜底复位）。

## 修改文件

- `src/screen_agent/voice/ui_qt/ball.py`（唯一）：
- paintEvent 重写（~90 行 → ~55 行）：删渐变/高光圈/三态装饰，加扁平圆盘+状态点呼吸
- `_tick`：删 `_angle`；STATE_TUNING 删 speed 列（保留 intensity/breathe 供呼吸幅度）
- 删 `_aurora_gradient`/`AURORA`/`intensity` 死代码
- `QtFloatingBall._submit_text` 加 finally 状态回滚
- 不动：ball.py 其余逻辑、tests（现有 82 个无 ball 视觉测试，行为兼容）

## 验收

1. 真机看：idle=扁平奶白圆盘+小灰点；processing=中心点变琥珀并轻呼吸（不再有顶部橙点/旋转）
2. 对 `handle_command` 抛异常场景：球状态自动回 idle（不会卡死）
3. 点击/拖拽/Alt+Space/托盘行为不变
4. 全量测试绿