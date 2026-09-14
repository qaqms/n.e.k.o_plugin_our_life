# 我们的生活 Design Brief

> 定位：第三方作者插件（独立 Git 仓库，不寄放宿主 `plugin/plugins/`）。
> 风格：**养成系 + 强注入**——数值随真实时间演化，并以"她自己的身体与心情感受"注入对话上下文。

## Identity Lock

- plugin_id: `our_life`
- folder: `D:\neko kaifa\n.e.k.o_plugin_our_life`
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
面板可视化当前状态、分档、金币与背包、口粮顾问、连续天数与近期注入记录。

## Package Type and Capabilities

- package type: `plugin`（独立功能，**完全独立**：不调用任何其它插件、不联网、不改宿主平台层）
- capabilities:
  - callable entries：状态查询 / 手动纠偏 / 重置 / 总开关 / 照顾她（用物品）/ 商店购买 /
    金币补贴 / **口粮顾问**（她每天吃多少、还够几天、缺口多少）
  - LLM tools：她能自主查询身心状态、主动索取陪伴（带冷却）
  - lifecycle：startup 装配、shutdown 落盘
  - timer：周期采样行为（只读总线）+ 节律折算 + 自动进食 + 经济结算 + 注入判定
  - message injection：`push_message(visibility=[], ai_behavior="read"|"respond", coalesce_key=...)`
  - UI：Hosted TSX panel（五项数值 + 走势字符图 + 口粮顾问 + 商店 + 背包 + 节律/纪念日 + 开关/纠偏）
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
  └─ 注入判定 services/injector.py（危机/饿/累 > 纪念日 > 跨档 > 首触；三层频控 + 睡觉静默）
       └─ core/injection.build_text（第一人称感受 + 行为倾向 + 时段，{MASTER_NAME}/{LANLAN_NAME}）

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
- ❌ 让模型自己回传"聊天让她开心了"（反馈闭环）——留到下一轮单独做，需要更谨慎的防刷设计

## Inferred Technical Needs

- plugin.toml sections: `[plugin]`、`[plugin.author]`、`[plugin.sdk]`、`[plugin.i18n]`、`[plugin.store]`、`[plugin.ui]` + `[[plugin.ui.panel]]`、`[plugin_runtime]`、自定义 `[our_life]` 族
- SDK surfaces: `NekoPluginBase`、`@neko_plugin`、`@plugin_entry`、`@lifecycle`、`@timer_interval`、`@ui.context`、`@ui.action`、`@llm_tool`、`push_message`、`Ok/Err/SdkError`、`self.store`、`self.config`、`self.bus.conversations`
- UI surfaces: hosted-tsx panel `ui/panel.tsx`，权限 `state:read` + `config:read` + `action:call`
- state/config: `self.store` 存数值与时间戳；`self.config` 读 `[our_life]`；开关写入走 entry → `self.config.set`
- lifecycle/background work: startup 装配采样状态；tick 内完成采样+结算+注入，**不持有跨拍对象**（timer 每拍新 event loop）
- external integrations: 无（仅宿主本地 bus 只读）
- v0.2.0 新增 SDK 面：`push_message` 的 `coalesce_key` / `priority`（折叠与加急）、
  `self.store.set/get/keys/delete`（分片与背包）、`@plugin_entry` 的 `input_schema.enum`（商店与物品选择）

## Read Context Plan

- 已读：`.agent/rules/neko-guide.md`、`.agent/skills/neko-plugin/**`（含 execution-boundary / core-plugin-contract /
  plugin-creation-workflow / checks-and-tests / cli-and-debugging / surface-map）、`plugin/PLUGIN_DEVELOPMENT_GUIDE.md`、
  `docs/zh-CN/plugins/**`、`plugin/config/schema.py`、`plugin/neko_plugin_cli/**`（init/build/validate 源码）、
  `plugin/sdk/plugin/**`、`plugin/sdk/shared/core/bus_context.py`、`plugin/core/host.py`、`plugin/sdk/hosted-ui/index.d.ts`、
  宿主 `main_routers/system_router/emotion.py` + `main_routers/proactive_router.py`（确认本地端点边界）、
  三份台账（`未完成的问题/*.md`）与三个既有插件仓库（catgirl_seiyuu / forever_companion / your_memory）
- 参考落盘：`../neko-plugin-sdk-api-reference.md`、`../hosted-ui-reference.zh-CN.md`

## Write Workspace

`D:\neko kaifa\n.e.k.o_plugin_our_life\`（独立 Git 仓库；不触碰 N.E.K.O 源码树）

## 已知陷阱（实现时必须绕开，均源码核实）

1. **`ctx._current_lanlan` 是脏值**：`plugin/plugins/jukebox_controller` 明确写了 "Deliberately no
   `ctx._current_lanlan` fallback"——该属性由"某次调用"的 `_ctx["lanlan_name"]` 写入，本次调用不带时
   里面留着**上一次**的角色名。归属只能取本次注入的 `_ctx`，或总线记录自带的 `lanlan_name`。
2. **`bus.conversations` 不支持 `watch()`**：只有 `messages` / `events` / `lifecycle` 可 watch
   （`migration-v0.9`）。行为采样只能定时轮询只读快照。
3. **timer 每拍 `asyncio.run(fn())`（新 event loop）且无 watchdog**：tick 内不得使用在其它拍/`startup`
   里创建的 loop 绑定对象；异常只记日志不停表，需自己兜。
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

- **反馈闭环（下一轮，用户已确认要做但要单独一轮）**：给她一个"这轮聊得怎么样"的评估工具，
  让她的判断回流成数值修正项（只在小范围内加减、带会话级与每日上限），
  补上"只按发言条数算成长"的失真。风险集中在防刷（她把每轮都报成"超开心"）与防崩
  （模型返回非法值）——两条都要有独立门。
- 病愈 / 哄好 / 生日等**阶段性事件与面板叙事**（与反馈闭环一起做才有味道）
- 其余 6 语言（zh-TW / ja / ko / ru / es / pt）补齐，并加"键集一致门"
- 情绪感知：以**独立插件联动**方式接入（用户明确要求后做），届时按跨插件契约单列设计
- 作息推断精细化（当前是固定睡眠窗 + 互动时段分布）
