# 悬浮球重做：Apple Intelligence 边缘流光风格 + 移除 glm-4.7-flash

## 问题诊断（用户截图）

现在的球像「摄像头镜头」：深色瞳孔 + 灰紫虹膜，浑浊、扁平、发虚。三个根因：
1. **QGraphicsDropShadowEffect 把整个控件栅格化再模糊**——自绘内容跟着一起糊（发虚的元凶）
2. 中心「瞳孔/虹膜」隐喻在浅色桌面上读成 web cam，廉价感来源
3. 灰蓝主体色在浅色桌面没有存在感

## 设计方案：Siri / Apple Intelligence 边缘流光（有公开取样参数）

社区取样实测值（artofstyleframe.com 对 Apple 官方动画逐帧取样）：
**蓝 #0894FF → 紫 #C959DD → 珊瑚红 #FF2E54 → 琥珀 #FF9004 → 循环回蓝，固定顺序，锥形渐变（QConicalGradient）旋转**

层次结构（照抄 Siri overlay 的三层光）：
1. **外层 wash**：宽环（~10px）低透明度，模拟 blur 光晕（叠 2-3 圈递减 alpha 代替真模糊，避免 effect 栅格化）
2. **中层 bloom**：宽 ~6px 中透明度
3. **内层 core**：宽 ~2.5px 锐利亮环
4. **球体**：深色玻璃圆（#0e1116 径向渐变 + 微蓝），顶部小高光弧——不再是瞳孔镜头
5. **删掉 QGraphicsDropShadowEffect**，光晕全部手绘在 paintEvent 里

状态语义（照 Siri 的「诚实状态灯」）：
- idle：流光极慢速旋转（~8s/圈）、透明度压低
- listening：全亮流光 + 亮度/环宽随**真实音量电平**起伏
- processing：旋转加速 + bloom 收紧
- speaking：流光 + 外圈涟漪脉冲
- 尺寸 76 → 84

## 改动清单

1. `src/screen_agent/voice/ui_qt/ball.py` — BallWidget.paintEvent 全部重写（QConicalGradient 三层环 + 深色玻璃体），删 shadow effect，状态参数表（转速/亮度/环宽）
2. **交互质感同步修**（用户强调交互丑）：
   - 球 hover 反馈：鼠标悬停时光环变亮 + 球体微放大（enterEvent/leaveEvent 驱动，动画过渡）
   - 面板弹出/收起加 150ms 淡入淡出（QPropertyAnimation windowOpacity），不再生硬闪现
   - 面板标题区加一条极细的流光分隔线（与球同一配色，视觉统一）
3. `config.yaml` — 删 glm-4.7-flash 后端（用户拍板不用了），剩 glm-4-flash → deepseek-flash → 本地 vLLM
4. `config.example.yaml` — 同步删 glm-4.7-flash 示例
5. `scripts/render_ball_preview.py`（新）— 离屏渲染 4 个状态的 PNG 预览，**先自检再给用户看**（用 Read 看渲染图确认不糊不浑浊）

## 验证

1. 跑 preview 脚本渲染 4 状态 PNG，Read 检查视觉效果
2. 单测 22 个回归
3. 杀掉旧进程，wt 新终端重启 `main.py voice`，用户看真实效果
4. commit

## 不做

- 面板/托盘样式不动（这次只治球）
- 不推 GitHub
