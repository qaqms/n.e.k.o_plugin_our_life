# 我们的生活 Design Brief

> 定位：第三方作者插件（独立 Git 仓库，不寄放宿主 `plugin/plugins/`）。
> 风格：**养成系 + 强注入**——数值随真实时间演化，并以"她自己的身体与心情感受"注入对话上下文。

## Identity Lock

- plugin_id: `our_life`
- folder: `F:\ai\neko kaifa2\n.e.k.o_plugin_our_life`（开发工作区，仓库名 `n.e.k.o_plugin_our_life`）
- 挂载态目录名: `our_life` —— 宿主按 entry 的包名匹配目录名，**必须是合法标识符**，
  所以开发工作区目录名不能直接挂载（见「已知陷阱」§11）
- name: `我们的生活`
- entry: `plugin.plugins.our_life:OurLifePlugin`
- main class: `OurLifePlugin`
- repository: `n.e.k.o_plugin_our_life`

## Purpose

为猫娘维护一组随时间**真实演化**的状态数值（饱食 / 精力 / 心情 / 健康 / 好感），
把它们以她自己的身心感受强注入对话上下文，使她的语气、主动性、话题选择随数值变化；
她还有作息与口袋：夜里会睡、每天自己吃掉背包里的口粮、主人用金币囤货。
主人通过日常互动（来找她、陪她、连续相处的天数）喂养数值，通过囤口粮维持日常生活，
长期冷落则按拟真惩罚衰减，掉到"生病 / 闹脾气 / 饿坏了 / 累垮了"档时她会主动开口。
**她自己也会判断"刚才这轮聊得怎么样"**（v0.3.0），把它回流成一个有上限的修正项——
补上"成长只按发言条数算"的失真。
面板可视化当前状态、分档、金币与背包、口粮顾问、连续天数、她自己的感受与近期注入记录。

## Package Type and Capabilities

- package type: `plugin`（独立功能，**完全独立**：不调用任何其它插件、不联网、不改宿主平台层）
- capabilities:
  - callable entries：状态查询 / 手动纠偏 / 重置 / 总开关 / 照顾她（用物品）/ 商店购买 /
    金币补贴 / **口粮顾问**（她每天吃多少、还够几天、缺口多少）
  - LLM tools：她能自主查询身心状态、主动索取陪伴（带冷却）
  - lifecycle：startup 装配、shutdown 落盘
  - timer：周期采样行为（只读总线）+ 节律折算 + 自动进食 + 经济结算 + 注入判定
  - message injection：`push_message(visibility=[], ai_behavior="read"|"respond", coalesce_key=...)`
  - UI：Hosted TSX panel（v0.4.1 起为「顶部常驻状态带 + Tabs 四页」：
    总览(走势/节律) / 过日子(顾问/商店/背包) / 她的世界(感受/事件) / 管理(纠偏/注入史/配置)）
  - store：`PluginStore` 持久化（必须 `[plugin.store].enabled = true`，否则静默不落盘）
  - i18n：zh-CN / en（其余语言后续补齐，见"路线图"）
- inferred architecture:

```
timer tick（默认 30s，无 watchdog：每拍自成一拍，不持有跨拍对象）
  ├─ 行为采样 services/sampler.py
  │    └─ bus.conversations（只读快照；**不支持 watch()**）
  │         逐条读 turn 记录：turn_type=user/assistant + lanlan_name
  │         → 去重（conversation_id）→ 聚合出 user_turns / last_touch / streak / 时段分布
  ├─ 作息 core/rhythm.py（睡眠窗、时段、相处天数、纪念日）
  ├─ 惰性衰减 core/model.apply_decay（按清醒/睡眠分段积分 → exp(-Δt/τ) 折算到此刻）
  │    └─ core/model.apply_coupling（饿 → 心情掉更快；累 → 健康恢复更慢）
  ├─ 成长/冷落结算 core/model.apply_impulse† / apply_neglect（纯函数，无副作用）
  ├─ 经济 services/state（金币/背包/吃饭账）+ core/economy
  │    ├─ 自动进食 meal_plan → apply_meal → bump_meal_day（她每天吃几餐的账）
  │    └─ 日薪 + 零花钱 + 里程碑金币 → shard.sodas
  └─ 注入判定 services/injector.py（危机/饿/累 > 纪念日 > 跨档 > 首触 > 判断 > 漂移；三层频控 + 睡觉静默）
       └─ core/injection.build_text（第一人称感受 + 行为倾向 + 时段，{MASTER_NAME}/{LANLAN_NAME}）

她自己的判断（v0.3.0 反馈闭环，与上面那条链并行）
  ├─ LLM 工具 our_life_judge（她自主调用）→ core/judgment.judge 四道闸门
  │    ├─ ① 单次幅度 = growth.turn_*_gain × _TURN_GAIN_SCALE（常量引用，永不写死数字）
  │    ├─ ② 会话递减（复用 ShardState.session_turns，跨会话归零）
  │    ├─ ③ 每日正向/负向双预算（ShardState.judgment_used_add / _subtract）
  │    └─ ④ 真实互动作前提（last_touch_at > last_judgment_at）
  ├─ 记账：ShardState.note_judgment（只存标签/时刻/幅度）+ 进程内增量队列
  └─ 施加：下一拍 tick 在 apply_decay/apply_coupling **之后** apply_judgment
       └─ 然后才判 transitions → 让判断带来的跨档也可能触发一次 tier_change 注入

† v0.1.0 的 `apply_impulse` 在 v0.2.0 里就是 `apply_turn_gain` / `apply_day_greet` / `apply_meal`。
```

## 数值模型（拟真衰减 / 惩罚式）

五项数值均 `0..100`，**按角色卡分片**（store key `ourlife@<lanlan>`）：

| 数值 | 周期 | 衰减 | 基线/地板 | 分档（下界 0/20/40/60/80） |
|---|---|---|---|---|
| `satiety` 饱食 | 小时级 | 清醒 4.0/时、睡眠 1.5/时 ↘ | 地板 0（靠吃饭回来） | 饿坏了 / 有点饿 / 吃饱了 / 吃得很饱 / 撑得慌 |
| `energy` 精力 | 小时级 | 清醒 −2.2/时、睡眠 +6/时；23 点后 ×1.35 ↘ | 地板 0 | 累垮了 / 有点累 / 还行 / 精神不错 / 精力充沛 |
| `mood` 心情 | 小时级 | τ = 3h，向静息基线回落（homeostasis） | 基线 55 | 闹脾气 / 低落 / 平静 / 愉快 / 雀跃 |
| `health` 健康 | 天级 | τ = 36h，向静息基线回落 | 基线 70 | 生病 / 虚弱 / 一般 / 良好 / 精神饱满 |
| `affection` 好感 | 周级 | τ = 45d，只缓慢回落不自动回升 | 地板 0 | 陌生 / 熟悉 / 亲近 / 亲密 / 羁绊 |

- 衰减是**读数时惰性折算**（`exp(-Δt/τ)`），不依赖常驻循环，也不怕进程重启。
  饱和/精力按**清醒与睡眠分段积分**（睡眠窗来自 `[our_life.rhythm]`，支持跨零点）。
- **分档阈值固定在 `core/model.py` 的常量**（单一来源），不暴露成配置项——避免"配置漂移"类回归。
  节律常量（`SATIETY_PER_HOUR_*` / `ENERGY_*` / `LATE_NIGHT_ENERGY_PENALTY`）、
  耦合阈值（`COUPLING_THRESHOLDS` / `COUPLING_FACTORS`）与纪念日锚点（`rhythm.ANNIVERSARY_DAYS`）
  同样是模型常量：它们是"这个角色怎么过日子"的刻画，不是用户调参。
- 配置只露**行为旋钮**（衰减时间常数、睡眠窗与纪念日奖励、经济旋钮、成长步长、冷落惩罚、注入频控）。
- **跨轴耦合不是"额外扣分"，而是改变时间常数**（`apply_coupling`）：饱食 < 40 → 心情朝向基线
  掉落 ×1.6（< 20 再 ×1.4）；精力 < 40 → 健康朝向基线**恢复** ×0.55（< 20 再 ×0.7）。
  所以"饿一天"的效果是心情一路往下掉，"熬夜攒久了"会病——因果读得出来。
  **耦合只在 `apply_coupling` 里施加**（`apply_decay` 只管时间）：第一版两处都放了，
  低精力那侧健康被恢复两次，被自己的门抓住（见 CHANGELOG v0.2.0）。
- **分档有两套读法，不许混用**（`core/model.py` 的模块 docstring 是权威说明）：
  - `tier_of` / `tier_transitions` = 硬比较，**面板与"这数值算哪一档"用它**，忠实反映每个数值；
  - `crosses_tier_boundary` / `eventful_tier_transitions` = **带 0.05 分迟滞**，
    只给**注入判定**用。否则分界线上的亚分噪声会被当成跨档事件：默认好评 20.0 正好压着
    stranger/acquainted 的线，第一拍 30 秒心跳就把它折成 19.9998，于是凭空发一条
    `tier_change` 强注入（真机 store 里抓到过这条，见 CHANGELOG 对应轮次）。

## 经济层与日常循环（v0.2.0）

口径：**她有作息、有肚子、有口袋**，而"每天要回来一次"这个动力来自一条会持续产生压力的循环——
**她每天自己吃掉背包里的口粮**。

```
rhythm（睡眠窗/时段/纪念日）
  ├─► model.apply_decay（清醒/睡眠分段折算 + 深夜惩罚）
  │        └─► model.apply_coupling（饿 → 心情掉更快；累 → 健康恢复更慢）
  ├─► economy.meal_plan ──► model.apply_meal（她吃饭：消耗背包里的一份口粮）
  │        └─► economy.bump_meal_day（吃饭账：面板"实测每天几餐"）
  ├─► economy.daily_income + daily_allowance（日薪与零花钱 → shard.sodas）
  └─► economy.advise（口粮顾问：每天几餐 / 还够几天 / 缺口 / 建议囤多少）
```

- **物品表固定在 `core/economy.py`**（`meat/fish/cake/medicine/toy/gift`），
  `plugin.toml` 只露旋钮（`staple_item_id` / `daily_allowance` / `carry_max` /
  `shop_daily_limit` / `meal_threshold` / 顾问视野与告警 / 日薪四项 / `start_sodas`）。
  与"分档阈值不进配置"同一条纪律。
- **不含随机性**：不做抽卡/暴击。数值养成的手感来自可预期的规划，随机奖励会让"她饿了"变成赌博，
  且无法写确定性测试门。
- **睡眠期照吃**（只有"饿到阈值"这一道闸）：否则默认睡眠窗（8 小时掉 12 分）会把饱食压到阈值以下，
  她一醒来就同时"饿着"且"今天还没吃过"，账面对不上。
- **冷落只扣心情/健康/好感**：饱食与精力由作息与进食驱动，再按天扣一次就是双重惩罚。
- **口粮顾问是纯计算**（`economy.advise`，不读时钟不看 store）：输入是"她的消耗画像 + 背包 + 金币"，
  所以能直接单测"三天没回来会饿几天"这类问题。它与真实衰减速**同源**
  （`model.satiety_per_day` 与 `_satiety_points` 共用同一组常量）。

## First Version Scope

v0.1.0（首版）：

1. 三项数值 + 拟真衰减 + 成长/冷落结算（纯函数，可单测）
2. 行为统计：用户发言次数、最近互动时间、连续互动天数、时段分布（全部本地，零外部依赖）
3. 事件驱动 + 频控的强注入（分档跨越即时注入；低档跨越升级为让她主动开口）
4. Hosted TSX 面板：三项数值 + 分档 + 连续天数 + 近期注入记录 + 开关与手动纠偏
5. LLM 工具两个：她查询自身状态、她主动索取陪伴（带冷却）
6. fail-closed 总开关 `[our_life].enabled = false`（默认关：未打开前不注入、不结算、不推送）
7. 中英 i18n、`tests/` 数值门 + i18n 契约门、`tools/release_gate.py` 五门

## v0.4.1 Scope（面板分区重构：状态带 + 标签页，第九轮）

1. **顶部常驻状态带**：五轴 Progress + 今日事实徽章（第几天 / 连续天数 / 时段 / 睡眠 / 距边界 /
   危机轴）+ 总开关与刷新。告警（冻结 / 错误码 / 口粮告急）仍在带上方，任何位置都看得到她的状态。
2. **Tabs 四页**（kit 的 `Tabs`，激活态由宿主 `useLocalState("tabs:<id>")` 持久化，刷新不丢位置）：
   总览（走势 + 相处节律）/ 过日子（口粮顾问 + 商店×背包双栏）/ 她的世界（感受 + 经历）/
   管理（纠偏 + 近期注入 + 配置）。独立「总开关」卡并入状态带，不再单列。
3. **纯布局轮**：Python 侧零改动（context / action / 码 / 台账都不动），只新增面板文案键
   （`panel.band.*` / `panel.tab.*`，双语纯插入保 CRLF）与一条文本级布局门。

## v0.4.0 Scope（「阶段性事件与面板叙事」主线，第八轮）

1. `core/events.py`（新）：两个事件（`sick_recovery` / `cheered_up`）、判定顺序、
   冷却窗口、台账读取助手。**全是纯函数**——不读时钟、不接 `ShardState`、不产生文案。
2. **判据线引用档位表**：病愈 = 健康跨过 `TIER_BOUNDS[2]`（40，脱离 sick+frail 两档，
   因为那两档面板都读作"病着"）；哄好 = 心情跨过 `TIER_BOUNDS[1]`（20，脱离 sulking）。
   import 时自检，改档位表就会炸而不是悄悄错位。
3. `services/injector.plan_for_event`（新通道）：与 `plan_for_tick` **并列**而不是插进它的链，
   避免事件与危机互相挤掉；同轴抑制；睡眠抑制按 `wake_ok` 分。
4. `core/injection`：新增 `TRIGGER_STAGED_EVENT` 与 `EVENT_NARRATION_ZH`（第一人称叙事模板，
   按既有注入契约：不给数字、不给档名、带 `{MASTER_NAME}` / `{LANLAN_NAME}`）。
5. tick 接线（`__init__.py` 步骤 6.6）：**先无条件记账，再决定发不发**——
   睡过去的事件也要留在经历里，而"记账必须无条件"正是冷却判定的前提。
6. 面板「她经历过什么」块（时刻 / 事件 / 轴）+ 说明文案。
7. schema 3 → 4：新增有界 `event_history`（12 条，只存事件名/轴/时刻/数值快照）。

刻意不做（见 CHANGELOG 与下节）：**生日**（需要新配置面与用户真实输入，单独立轮）、
**事件给数值奖励**（会让"康复"变成刷数值路径）、**为事件开第三条频控**（复用总闸门）。

## v0.3.0 Scope（「反馈闭环」主线）

1. `core/judgment.py`（新）：五个标签的白名单枚举、输入收敛（非法 → `neutral`）、
   **四道闸门**（单次幅度引用 `turn_*_gain`、会话递减、每日正向/负向双预算、真实互动作前提）、
   修正量计算。全是纯函数，不接 `ShardState`、不读时钟。
2. `core/model.apply_judgment`：把已算好的增量加到数值上（加法 + 夹取，未知轴忽略）。
   **与 `apply_turn_gain` 并列但不替代它**——判断是修正项，成长基线原样保留。
3. `our_life_judge` LLM 工具：她自主回传"刚才那轮聊得怎么样"。工具**只判定 + 记账 + 排队**，
   数值由 tick 在折算之后施加；**永不抛异常、返回值不含数值**。
4. 注入第 8 档触发 `TRIGGER_JUDGMENT`：她的判断作为"她自己的感受"进上下文（静默、不打断），
   排在 `daily_greet` 之后、`interval` 之前。
5. 面板新增「她自己的感受」块：今日次数、剩余额度、最近几条判断。
6. schema 2 → 3：判断台账（**只存标签 / 时刻 / 幅度，不存任何对话正文**）。

刻意不做（见 CHANGELOG 与本档「Out of Scope」）：代餐成长、情绪感知、阶段性叙事、随机性。

## v0.2.0 Scope（「过日子」主线）

1. 五轴：新增 `satiety`（饱食）与 `energy`（精力），并把衰减改成**节律感知**
   （清醒/睡眠分段积分、深夜熬夜惩罚）
2. 作息：`core/rhythm.py` —— 睡眠窗（支持跨零点）、时段、相处天数与**纪念日**（含一次性礼物）
3. 经济：金币（日薪 + 每日零花钱 + 里程碑）、物品表、背包、携带上限与当日消费上限、
   **商店入口**、**照顾她入口**、**口粮顾问入口**
4. 日常循环：**她每天自己吃掉背包里的口粮**；囤的吃完而主人没回来就会饿（饿坏了会主动开口）
5. 跨轴耦合：饿 → 心情掉更快；累 → 健康恢复更慢（改时间常数，不是额外扣分）
6. 注入层加固：`respond` 独立小时上限、`coalesce_key` 折叠、**睡觉静默**（非危机不打扰）
7. 面板重写：五条 + 走势字符图 + 口粮顾问 + 商店卡片 + 背包 + 节律/纪念日
8. schema 1 → 2（向后兼容：旧分片逐字段回退默认值，不需要迁移脚本）

## Out of Scope

- ❌ 调用任何其它插件（用户明确要求完全独立；情绪感知后续以独立插件联动方式另开）
- ❌ 联网、读宿主 `core_config.json`、直连模型端点（不做任何数据出域）
- ❌ 复用宿主 `/api/emotion/analysis` 等内部端点（无版本承诺面，留待后续轮次评估）
- ❌ 修改宿主平台层任何代码
- ❌ 真图表组件（Hosted UI Kit 无图表组件）：v0.2.0 的走势用**字符画折线**（`▁▂▃▄▅▆▇█`）由
  `props.state.trend` 里的注入快照渲染，不做 canvas/SVG 图表
- ❌ **作息推断**：睡眠窗是配置项，不根据互动时段反推（推断错了会明显暴露成"她半夜不困"）
- ❌ 抽卡 / 随机奖励 / 暴击：数值养成的手感来自可预期的规划，随机化也写不出确定性测试门
- ❌ **让判断替代成长基线**（v0.3.0 明确了这条）：`apply_turn_gain` 按发言条数给的成长**原样保留**，
  她的判断只作为**叠加的、有上限的修正项**。两者是"基线 + 微调"，不是"新算法替换旧算法"——
  这样 v0.2.0 的全部测试门一条都不用改，回归面为零
- ❌ **给反馈闭环开负向大口子**：负向日预算刻意只有正向一半，且同样需要真实互动作前提
  （让工具能扣分等于给模型一条惩罚通道）
- ❌ **判断工具接受自由文本 / 数值绝对值**：签名里只有一个枚举 + 一个 0..1 的强度，
  从根上杜绝"模型把对话原文写进持久化状态"与"直接设数值"

## Inferred Technical Needs

- plugin.toml sections: `[plugin]`、`[plugin.author]`、`[plugin.sdk]`、`[plugin.i18n]`、`[plugin.store]`、`[plugin.ui]` + `[[plugin.ui.panel]]`、`[plugin_runtime]`、自定义 `[our_life]` 族
- SDK surfaces: `NekoPluginBase`、`@neko_plugin`、`@plugin_entry`、`@lifecycle`、`@timer_interval`、`@ui.context`、`@ui.action`、`@llm_tool`、`push_message`、`Ok/Err/SdkError`、`self.store`、`self.config`、`self.bus.conversations`
- UI surfaces: hosted-tsx panel `ui/panel.tsx`，权限 `state:read` + `config:read` + `action:call`
- state/config: `self.store` 存数值与时间戳；`self.config` 读 `[our_life]`；开关写入走 entry → `self.config.set`
- lifecycle/background work: startup 装配采样状态；tick 内完成采样+结算+注入，**不持有跨拍对象**（timer 每拍新 event loop）
- external integrations: 无（仅宿主本地 bus 只读）
- v0.2.0 新增 SDK 面：`push_message` 的 `coalesce_key` / `priority`（折叠与加急）、
  `self.store.set/get/keys/delete`（分片与背包）、`@plugin_entry` 的 `input_schema.enum`（商店与物品选择）
- v0.3.0 新增 SDK 面：`@llm_tool` 的 `parameters.enum`（判断标签的**白名单**——把"非法输入"
  挡在模型这一侧的同时，插件侧仍按不可信输入再收敛一次）；`@llm_tool` 返回值刻意**不含数值**

## Read Context Plan

- 已读：`.agent/rules/neko-guide.md`、`.agent/skills/neko-plugin/**`（含 execution-boundary / core-plugin-contract /
  plugin-creation-workflow / checks-and-tests / cli-and-debugging / surface-map）、`plugin/PLUGIN_DEVELOPMENT_GUIDE.md`、
  `docs/zh-CN/plugins/**`、`plugin/config/schema.py`、`plugin/neko_plugin_cli/**`（init/build/validate 源码）、
  `plugin/sdk/plugin/**`、`plugin/sdk/shared/core/bus_context.py`、`plugin/core/host.py`、`plugin/sdk/hosted-ui/index.d.ts`、
  宿主 `main_routers/system_router/emotion.py` + `main_routers/proactive_router.py`（确认本地端点边界）、
  三份台账（`未完成的问题/*.md`）与三个既有插件仓库（catgirl_seiyuu / forever_companion / your_memory）
- 参考落盘：`../neko-plugin-sdk-api-reference.md`、`../hosted-ui-reference.zh-CN.md`

## Write Workspace

`F:\ai\neko kaifa2\n.e.k.o_plugin_our_life\`（独立 Git 仓库；不触碰 N.E.K.O 源码树）。
宿主仓在同级的 `F:\ai\neko kaifa2\N.E.K.O\` —— `tools/release_gate.py` 的默认宿主探测
（`PLUGIN_ROOT.parent / "N.E.K.O"`）正好命中这个布局，无需 `--host-root`。

## 已知陷阱（实现时必须绕开，均源码核实）

1. **`ctx._current_lanlan` 是脏值**：`plugin/plugins/jukebox_controller` 明确写了 "Deliberately no
   `ctx._current_lanlan` fallback"——该属性由"某次调用"的 `_ctx["lanlan_name"]` 写入，本次调用不带时
   里面留着**上一次**的角色名。归属只能取本次注入的 `_ctx`，或总线记录自带的 `lanlan_name`。
2. **`bus.conversations` 不支持 `watch()`**：只有 `messages` / `events` / `lifecycle` 可 watch
   （`migration-v0.9`）。行为采样只能定时轮询只读快照。
3. **timer 每拍 `asyncio.run(fn())`（新 event loop）且无 watchdog**：tick 内不得使用在其它拍/`startup`
   里创建的 loop 绑定对象；异常只记日志不停表，需自己兜。
   - **已核实 = 真·独立线程**（v0.4.x 复核，`plugin/core/host.py:1217-1250`）：每个
     `auto_start=True` 的 `@timer_interval` 由 `threading.Thread(daemon=True)` 起**专属线程**，
     循环体是 `asyncio.run(fn())` + `stop_event.wait(interval)`（`:1218-1230`）；
     而入口 / 工具 / 消息走**另一条** `asyncio.run(_async_command_loop())`（`:1788`，主线程）。
   - ⇒ `on_tick`（timer 线程）与 `our_life_judge` 等入口/工具（command loop 线程）是**真并发**，
     不是同一事件循环里的交替。两者共享同一份 `ShardState` 实例
     （`services/state.py:512-520` 的 `load` 返回缓存对象、`:522-524` 的 `save` 原地更新缓存），
     因此在 `await` 点上的读-改-写**理论上有交错窗口**。
   - 当前结论（不夸大）：`judgment_*` 轴的唯一写入方就是 `our_life_judge` 自己
     （`__init__.py:1127-1147`，入口只在 `judge` 里调），所以没有第二个写者去丢它的更新；
     `daily_allowance_granted` / 里程碑同样只有 `_settle` 写。**未观察到真实损失，暂不加锁**，
     但这条窗口是**没有测试门覆盖**的——新增任何"由入口/工具写、由 tick 消费"的双写字段时，
     必须先回头评估这里（或改为单一队列/加 `threading.Lock`）。
4. **`[plugin.store].enabled = false` 时 store 静默失效**：`set` 返回 `Ok` 却不落盘、`get` 返回默认值。
5. **`push_message` 的 `submitted=True` 不等于宿主已消费**：启动钩子期推送会落在订阅窗口之前被静默丢弃。
   注入只在 tick / 入口 / 工具里发，不在 `startup` 里发。
6. **面向用户的文本必须用 `{MASTER_NAME}` / `{LANLAN_NAME}` 占位符**，不得硬编码"主人/用户"。
7. **面板可达入口的 `Err(SdkError(...))` 与 `note` 只用稳定 ASCII 码**（`^[a-z][a-z0-9_]*$`），
   动态细节进日志；前端按 `panel.errors.<camelCase(码)>` 翻译。
8. **`data/` 与 `cache/` 才是可写位置**：`self.plugin_dir`（= `config_dir` 别名）是只读安装目录。
9. **隐私**：注入正文含用户互动信息，只进总线不进日志正文；涉及原文一律不上 `logger`。
10. **`@llm_tool` 名称**必须匹配 `^[A-Za-z0-9_.\-]{1,64}$`，且工具注册表在 `main_server` 内存里，
    宿主重启即丢、无自动重注册——首版不额外做重注册心跳（记为待办），并在 README 说明。
11. **挂载态目录名必须等于 entry 的包名**（宿主源码核实）。两段链：
    - `plugin/server/application/plugins/lifecycle_service.py:1078-1091` 先调
      `normalize_plugin_entry_point(...)`，再调 `describe_plugin_entry_directory_mismatch(...)`，
      不一致即 `code="PLUGIN_ENTRY_DIRECTORY_MISMATCH"` / `status_code=400` / 插件停在 failed。
    - 判定本体在 `plugin/core/entry_points.py:47-74`：取 entry 模块路径的**第 2 段**
      （`plugins.our_life` → `our_life`）与 `config_path.parent.name` 硬比对。
    - **归一化会把 entry 改写**（`entry_points.py:15-44`）：只能 entry 对应**在宿主仓内**的插件，
      canonical 写法 `plugin.plugins.our_life:OurLifePlugin` 原样保留；**用户安装态**会被重写成
      `plugins.our_life:OurLifePlugin`。所以两种形态下目录名都必须真叫 `our_life`。
    - `n.e.k.o_plugin_our_life`（合法 Git 仓名，不是合法 Python 包名）因此**永远挂不上**。
      **软链接 / junction 也救不回来**（`resolve()` 会把链接解开、目录名变回真实名）。
      唯一挂载方式就是**复制**：`tools/release_gate.py` 的 release / hosted-tsx 两门就是
      真复制进 `<宿主>/plugin/plugins/our_life/`（探针副本，用后即删）。
    - 另注（易混）：目录名与 `[plugin].id` 不一致**只是 warning**
      （`plugin/config/plugin_toml_semantics.py:64-76`，`PLUGIN_DIRECTORY_ID_MISMATCH`），
      与上面这条 entry 包名不匹配的**硬 400** 是两回事，别把两者当成同一条规则。
12. **Hosted TSX 检查要求被检路径在宿主仓内**（`frontend/plugin-manager/scripts/check-hosted-tsx.mjs`
    的 `assertPathInsideRepo` 用 `realpathSync` 比对），所以它**只能**对宿主仓内的副本跑，
    对仓外工作区路径会直接报 "Plugin search target outside repo root"。
    这也是 release_gate 必须复制副本的原因之一。

## Risk Follow-ups

- **惩罚式衰减的用户体验**：拟真衰减会让长期不互动明显掉档。默认 `enabled = false` 是唯一知情同意闸门；
  README 必须写清"关掉即冻结、不结算不衰减"，且面板要显示"当前是否在结算"。
- **私有依赖面**：本版**不依赖**任何宿主内部端点，只依赖 `plugin.sdk.plugin` 公共门面与 `bus` 只读快照；
  若后续要用 `/api/emotion/analysis`，需按台账惯例单列"无版本承诺面"债务。
- **prompt 膨胀**：注入受 `max_chars`(320) + `min_interval_sec`(1200) + `max_per_hour`(3) 三重约束；
  注入文案不含数值表以外的长文本，且不做每轮注入。
- **跨环境双态验证**：常驻门必须区分独立仓态与宿主挂载态（CI 是 `cp -R` 进 `plugin/plugins/our_life`
  后按**裸 id** 跑 `check -r`），`tools/release_gate.py` 复刻该形态。
- **Windows 码面**：`release_gate.py` 的子进程管道与 stdout 必须钉 UTF-8（本机已两次踩 GBK 坑）。

## 路线图（后续轮次）

- **阶段性事件（已完成，v0.4.0 第八轮）**：病愈 / 哄好两个事件 + 面板「她经历过什么」。
  下一轮可以在此之上加**叙事深度**：把同一件事在不同处境下的说法区分开
  （比如"病愈时主人就在旁边"与"病愈时他刚回来"该是两种语气），或加"她记得自己病过多久"。
- **生日**：需要"角色的生日"这个新配置面与用户的真实输入，与下面的情绪感知一样属于
  **需要单独设计的一轮**——v0.4.0 刻意没把它塞进事件层（那会让"事件"混进两种时间语义：
  档位跨越 vs 日历日期）。
- 其余 6 语言（zh-TW / ja / ko / ru / es / pt）补齐，并加"键集一致门"
  （注意：**键集一致门已有**，见 `tests/test_i18n_contract.py`；缺的是那 6 份译文本身）。
- 情绪感知：以**独立插件联动**方式接入（用户明确要求后做），届时按跨插件契约单列设计。
  注意它与 v0.3.0 的反馈闭环**不是同一件事**：后者是**她自己**回传感受，
  前者是插件去读对话正文推情绪（会碰隐私边界，故单独立项）。
- 作息推断精细化（当前是固定睡眠窗 + 互动时段分布）
- `@llm_tool` 重注册心跳：宿主 `main_server` 注册失败时只 warning 且不重试
  （`plugin/server/messaging/llm_tool_registry.py`），所以她在"宿主起得比插件晚"的那次启动里
  会暂时没有判断工具。当前靠重载插件恢复，值得做一条自愈通道。
