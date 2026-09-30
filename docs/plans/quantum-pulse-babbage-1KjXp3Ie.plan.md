# 评测 / 飞轮 / 自进化：五项改进实施计划

项目：`D:\素材存储\Agent-Retina-World`

## 一、现状

已建成三层：**评测**（`eval/`）、**数据飞轮**（`eval/flywheel.py`）、**自进化**（`evolve/loop.py`）。
当前指标（规则模式）：活动 macro-F1 **0.687**，黄金集 **71 条**（51 活动 + 20 隐私）。

调研中发现 **5 个缺陷**，其中两个让「隐私红线」形同虚设。**必须先修，再谈新功能。**

## 二、P0：五个必须先修的缺陷

### D1 / D2 —— 隐私评测恒真（已核对代码确认）

**`eval/runner.py:126-128`** 与 **`evolve/loop.py:239-241`** 各有一处完全相同的错误：

```python
allowed, _ = self.privacy.verdict(case.title, case.process, case.texts)
expected.append(not allowed)
predicted.append(not allowed)   # ← 和 expected 是同一个值
```

`expected == predicted` ⇒ `gate_report` 的漏放数恒为 0 ⇒ **`recall` 永远 1.0**。

**连带后果**：
- `PRIVACY_TOLERANCE = 0.0` 这条门禁从来没拦过任何东西
- `Evolver.verify` 的第三条采纳红线（隐私不许退）恒过
- `archive.json` 里所有变体的 `holdout_privacy` 全是 1.0
- 黄金集里 `PrivacyCase.expect_blocked` **全程未被使用**（20 条隐私样本白标了）

**修法**：`expected.append(case.expect_blocked)`，`predicted.append(not decision.allowed)`。
修完必须**重新评测并重建 baseline**（`privacy_recall` 会第一次显出真实值，必然不是 1.0）。

### D3 —— 档案树名存实亡（四小项）

1. `parent_id`（`loop.py:187`）取生成那刻的 `archive.best()`，**同批候选共享同一父**；实测 `archive.json` 里 v001–v004 的 `parent_id` **全为空串**
2. **`parent_id` 不参与任何计算** —— `verify`（L258）用 `copy.deepcopy(self.rules)`（当前累积规则）当基线，不是父节点规则
3. `propose_from_failures` 的 `current = tuple(self.rules.get(expect, ()))`（L181）用批次起始快照，本批候选看不到前序累积
4. `_score`（L213-232）临时改写**模块全局** `classify._RULES`，线程不安全；多轮/并行会互相污染

### D4 —— 画像层「检索」是假的

- `run()`（`memory.py:141`）retrieve 分支实际调的是 `_trial_extract(case)`
- `_trial_retrieve`（L177-178）是桩：`return self._trial_extract(case)`
- **检索能力从未被测过**，且 `kind=="retrieve"` 只有 1 条样本（m08），其答案就在回复里、抽取链路会写库 → 恒命中

### D5 —— 飞轮节流是丢弃式的

`watcher._feed_flywheel`（L267-269）30 秒节流：喂一条后 **30 秒内其他难例直接丢**，无队列、**漏采不可观测**。`data/eval/` 下至今没有 `flywheel.json`（飞轮从未真实跑过）。

---

## 三、五项任务

### 任务 1：档案树 + 从任意节点变异（对标 DGM 开放探索）· **大**

**为什么**：DGM 消融实验——去掉开放探索从 50% 掉到 **23%**。我们现在是单线变异，就属于「没有开放探索」那一档。

**验收**：① 重跑后 `parent_id` 有真实指向（非全空）；② `verify` 基线 = `rules_of(parent_id)`；③ `sample_parent` 能产出跨分支的父节点；④ D1 修好后 `holdout_privacy < 1.0`。

**改动**：

- **`understand/classify.py`** 新增纯函数 `classify_with_rules(blob, rules)`，规则显式传入、不读全局；`ActivityClassifier._classify_rules` 改为它的薄包装（向后兼容）。**这是 D3④ 的修法**。
- **`evolve/loop.py` `Archive`** 增 `roots()` / `children(id)` / `depth(id)` / `lineage_rules_order(id)`；`next_id` 改为按现有最大编号递增（避免多轮撞号）。
- **`Evolver`** 增 `_base_rules`（常量快照）与 `rules_of(variant_id)`——沿 lineage 根→叶重放 `payload.keywords` 增量。**含节点自身增量**：它表示「这条配置」，`accepted` 只表示相对父基线是否过门禁。
- **`sample_parent(strategy, epsilon, rng)`**：`best` / `random` / `epsilon`（ε-greedy，默认）/ `weighted`（按 `holdout_f1² / (1+children)` 抑制过度开发同一分支）。档案空时返回 `""`（从 base 变异）。
- **`propose_from_failures(..., parent_id="")`**：`current` 改从 `rules_of(parent_id)` 取；`Variant.parent_id` 用传入值。
- **`verify`**：基线改 `rules_of(variant.parent_id)`。
- **`_score`**：改用 `classify_with_rules`；同时修 D1。

**新增测试** `tests/test_evolve_loop.py`：单链/分叉重放正确；两候选共享父时基线一致（而非累积）；`parent_id` 非空且指向存在节点；`sample_parent` 分布（固定 seed）；**D1 回归**（构造应拦未拦的用例，断言 `holdout_privacy < 1.0`）。

**风险**：`rules_of` 重放只支持 prepend 增量，payload 语义不能扩展成删除操作，否则还原不准。回滚只需还原 `loop.py`（JSON 结构未变）。

---

### 任务 2：多轮迭代 · **中**

**依赖**：任务 1（否则每轮都从同一 `best` + 累积规则出发，多轮没意义）。

**验收**：`--rounds 5` 跑完，`archive.json` 出现**深度 > 1** 的节点（`parent_id` 指向上轮节点）。

**改动**：

- `Evolver` 增 `dev_failures(rules)`（用给定规则在 dev 集重算错判，供后续轮用）与 `run(max_rounds=5, patience=2, strategy, epsilon)`：每轮「选父 → 从父变异 → 验证 → 采纳则把 `self.rules` 迁到新叶」，连续 `patience` 轮无采纳即停。
- `step(failures)` 保留为单轮薄包装，向后兼容。
- CLI `benchmarks/perception_eval.py` 加 `--rounds` / `--patience` / `--strategy` / `--explore-eps`，按轮分组打印。

**风险**：多轮会放大对 dev 集的过拟合 —— `challenge` 池是唯一护栏，必须保证它**不被飞轮污染**（见任务 3 纪律）。

---

### 任务 3：飞轮把黄金集养到 150+ · **中**

**依赖**：D5。

**验收**：黄金集 71 → **≥150**（建议 activity ≥110 / privacy ≥40）；`flywheel.json` 真实存在；`--flywheel` 能看到来源分布与 `dropped` 计数。

**改动**：

- **`capture/watcher.py`**：`_feed_flywheel` 改「入队 + 到期排空」。`collections.deque(maxlen=100)`，满队丢最旧并累加 `flywheel_dropped`（**可观测**）；`drain_flywheel()` 到期一次性写入。
- **候选质量**：启用已定义但未用的 `"uncovered"` 来源（规则把高信息量内容兜底判成 other/idle 且置信低）；`mark_disagreement` 保持最高优先级。
- **人工确认入口**：`Flywheel.correct()` 目前**无人调用**。新增 CLI `--flywheel-review`，可读 `--corrections-file`（JSON）批量标注。
- **防「自己判自己」**（关键纪律）：
  - 晋升**只进 `rolling`**，`challenge`（holdout）**永不自动填充**
  - `promote` 前按归一化文本**去重**
  - `promote` **加断言拒绝无 `corrected` 的候选**（现在用 `candidate.truth` 会在无人工确认时回落到 `guess`）
- **扩种**：`dataset.py` 的 `SEED_ACTIVITY` / `SEED_PRIVACY` 补齐到约 activity 90 / privacy 35，再靠飞轮长到 150+。
- **顺带**：`cmd_eval --save` 现在同时写 baseline **和 golden**（评测带副作用），拆成 `--save-baseline` / `--save-golden`。

**新增测试** `tests/test_flywheel_queue.py`：入队不丢（除满队丢最旧）+ `dropped` 计数；到期排空条数正确；**`promote` 拒绝无 `corrected` 的候选**；去重生效。

---

### 任务 4：细粒度隐私策略（对标 Screenpipe per-pipe）· **大**

**依赖**：D2。

**验收**：`PrivacyCase` 能声明通道级期望且 runner 不再返回恒 1.0；默认配置**行为等价于旧的整条拦截**。

**改动**：

- **`capture/privacy.py`** 新增通道级接口：
  ```python
  CHANNELS = ("window_meta", "a11y_text", "ocr_text", "screenshot", "event")

  @dataclass(frozen=True)
  class ChannelPolicy:      # allow_apps / deny_apps / active_hours / redact_secrets
  @dataclass
  class ChannelVerdict:     # channel / allowed / reason
  @dataclass
  class PrivacyDecision:    # channels: dict[str, ChannelVerdict]
                            # allowed = 全通道放行；reason = 首个被拦原因

  def evaluate(self, title, process_name, texts, *, ocr_text="", moment=None) -> PrivacyDecision
  def verdict(...) -> tuple[bool, str]   # 兼容包装，三个调用点零改动
  ```
- **通道语义**：`window_meta` 管私密窗口/时段；`a11y_text`、`ocr_text` 管敏感内容（`redact_secrets=True` 时打码保骨架，否则整通道丢）；`screenshot` **默认 `allowed=False`（策略钩子，不建真实截图链路）**；`event` 是骨架通道。
- **`SightEvent` 无图像字段**，`_fill_ocr` 只取文本不存图 ⇒ 「连截图都不给」需**先新增通道字段**再谈采集。
- **分通道落点收敛**：现在清字段逻辑**硬编码在 `watcher.poll`(L211-239) 与 `journal.record`(L122-159) 两处**，加通道要同时改两处 —— 建议统一走 `evaluate` 的返回值。
- **`eval/dataset.py` `PrivacyCase`** 加 `channel_expectations: dict[str, bool]` 与 `ocr_text`，保留 `expect_blocked` 兼容。
- **`eval/runner.py`** 修 D2 并支持通道级聚合。

**风险**：默认配置必须等价旧行为（所有通道默认 = 旧 `verdict` 结果），只在显式配置时细化。**修 D2 后 baseline 必须重建。**

**新增测试**：`tests/test_privacy_channels.py`（各通道裁决、三层组合、`redact_secrets`、包装与 `evaluate` 一致）、`tests/test_privacy_eval.py`（runner 尊重 `expect_blocked`，断言 recall **非恒 1.0**）。

---

### 任务 5：画像层补检索场景 · **中**

**依赖**：D4。

**验收**：`run` 真调 `_trial_retrieve`；retrieve 用例 **1 → ≥8**；召回不再是平凡值。

**改动**：

- **新增 `memory/fact_retriever.py`**：`HybridRetriever` **只吃 `ActivityEvent`，不能直接复用**。`FactRetriever.retrieve(query, top_k)` 按 `relevance(查询重叠) × fact_score(confidence × 类型衰减)` 排序。复用 `memory/retriever.py` 的 `_tokens`/`_overlap`（建议抽到 `memory/_text.py` 公开化，别跨模块引私有名）。
- **`eval/memory.py`**：`MemoryCase` 加 `seed_facts`（预置事实）/ `query` / `expect_recall`；`_trial_retrieve` 改真实检索（先 `add_fact` 灌种子 → `FactRetriever.retrieve` → 看命中）；`run` 改调它；`MemoryReport` 加 `recall_at_k`。
- **补 seed**：`SEED_MEMORY` 9 → 约 18（extract 5 / reconcile 3 / **retrieve 8** / forbid 2）。覆盖姓名、偏好、项目、跨类别、**无匹配不误召回**、**已被 supersede 的旧事实不该排前**。
- `benchmarks/memory_eval.py` S3 补一段事实级检索检查。

**风险**：`_extracted_text` 用 `confidence >= 0.3` 过滤，检索判分口径要与之一致，否则 supersede 场景两边打架。

**新增测试** `tests/test_memory_retrieval.py`：排序正确、无 query 时按 `fact_score`、supersede 后旧事实降权、**断言 `_trial_retrieve` 不再走 `_trial_extract`**（monkeypatch）。

---

## 四、依赖关系

```
D1 (loop 隐私恒真)  ─┬─→ 任务1 ──→ 任务2
D3①②③ (parent/基线) ─┘
D3④ (全局可变)      ───→ 任务1 前置
D2 (runner 隐私恒真) ───→ 任务4
D4 (检索桩)         ───→ 任务5
D5 (节流丢弃)       ───→ 任务3
任务3 ⇢ 任务1/2（弱依赖：数据多了进化更有料）
```

## 五、执行顺序

| 阶段 | 内容 | 工作量 |
|---|---|---|
| **P0** | 修 D1–D5 → **重建 baseline** | 小~中 |
| **P1** | 任务 1 档案树（含 `classify_with_rules` 去全局） | 大 |
| **P2** | 任务 2 多轮迭代（默认 `--rounds 1` 保兼容） | 中 |
| **P3** | 任务 4 隐私细粒度（默认等价旧行为） | 大 |
| **P4** | 任务 5 画像检索 | 中 |
| **P5** | 任务 3 飞轮 150+（可与 P1–P4 并行） | 中 |

比你给的优先序多了一步：**5 个缺陷作为 P0 先行**。理由很直接——D1/D2 不修，任务 1 和任务 4 的验收标准（"隐私召回不许退"）根本无法成立，新功能全建在流沙上。

## 六、跨任务统一纪律

1. **holdout 隔离**：飞轮晋升只进 `rolling`，`challenge` 仅人工策展
2. **评测只读**：打分路径不得改写 `classify._RULES` 全局，一律用 `classify_with_rules` 显式传参
3. **基线重定**：D1/D2 修复后 `privacy_recall/precision` 会变，任何对比前先重建 baseline
4. **CLI 副作用拆分**：`--save-baseline` / `--save-golden` 分开
5. **测试补齐**：目前 `evolve/loop.py`、`eval/flywheel.py`、`eval/memory.py`、`eval/runner.py`、`capture/privacy.py` **零测试覆盖**，五个新测试文件必须落地

## 七、涉及文件

**改动**
- `src/screen_agent/understand/classify.py`（L77-88 / L181-193）
- `src/screen_agent/evolve/loop.py`（L157-195 / L205-245 / L250-305）
- `src/screen_agent/eval/runner.py`（L122-129）
- `src/screen_agent/eval/memory.py`（L24-40 / L123-185）
- `src/screen_agent/eval/dataset.py`（L47-64 及 seed）
- `src/screen_agent/eval/flywheel.py`
- `src/screen_agent/capture/privacy.py`（L39-86）
- `src/screen_agent/capture/watcher.py`（L257-287）
- `src/screen_agent/memory/journal.py`（L122-159）
- `benchmarks/perception_eval.py`（L48-120）

**新增**
- `src/screen_agent/memory/fact_retriever.py`
- `tests/test_evolve_loop.py`、`tests/test_privacy_channels.py`、`tests/test_privacy_eval.py`、`tests/test_flywheel_queue.py`、`tests/test_memory_retrieval.py`

## 八、执行方式（用户已确认）

**1. 串行推进，一个做完验证 OK 才进行下一个。这是个长任务。**

不追求一次性交付。每一项的完成标准（缺一不可）：

- 代码改完，且改动点与计划一致
- **新测试通过**（该任务新增的测试文件）
- **全量回归通过**（基线 156 例，只增不减）
- **实测数据对比**：改进前 vs 改进后，跑真实评测给出数字
- 提交一条 commit

任一项不达标就停下修，不带病进入下一项。

**2. 黄金集来源：手工扩种打底 + 飞轮持续补。**

先手工把 `SEED_ACTIVITY` / `SEED_PRIVACY` 扩到约 activity 90 / privacy 35 打底，
之后飞轮从真实使用里持续挖难例补进来。**手工样本是骨架，飞轮的才是真价值**——
但飞轮要跑一段时间才够，所以不指望短期全靠它。

**3. 隐私门禁：修好后先只报不拦。**

D1/D2 修完，真实召回率会**第一次显形**（大概率不是 100%）。
所以第一步**只报不拦**：跑几轮看真实水平，确认误报漏报的分布，再决定是否收紧到硬门禁。

具体做法：`PRIVACY_TOLERANCE` 先设成一个宽松值（或让 `compare_to_baseline` 对隐私项
只输出警告不返回失败），等真实数字稳定后再恢复成 `0.0`。

**这一条的意义**：D1/D2 是「评测写错了，导致门禁从来没生效过」。
修好之后门禁第一次真正开始工作——**先让它叫，别让它咬人**。

## 九、每项任务的验收命令

```bash
# 全量回归（每项改完必跑）
.venv/Scripts/python.exe -m unittest discover -s tests

# 感知层评测（看活动 macro-F1 与隐私召回的真实值）
.venv/Scripts/python.exe benchmarks/perception_eval.py --no-service

# 自进化（任务 1/2 完成后）
.venv/Scripts/python.exe benchmarks/perception_eval.py --no-service --evolve --rounds 5

# 飞轮（任务 3 完成后）
.venv/Scripts/python.exe benchmarks/perception_eval.py --flywheel

# 画像层（任务 5 完成后）
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'src'); from screen_agent.eval.memory import MemoryEvaluator; print(MemoryEvaluator().run().summary())"
```

