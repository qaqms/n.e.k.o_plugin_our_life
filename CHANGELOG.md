# Changelog

本插件采用"轮次叙事"记录：每一轮写清**病因 / 做法 / 验证 / 测试数**，而不是只列增删。
历史条目只追加、不改写。

## [0.1.0] - 2026-09-14（第二轮 · 五门打通 + 双态修正）

### 打通了五门

`pytest` / `ruff` / `check` / `release` / `hosted-tsx` 全绿。其中 **release 门是在真挂载态通过的**：
插件被真的复制进宿主 `<host>/plugin/plugins/our_life/`，再按**裸 id** 跑 `check -r`
（校验 → 插件本地测试 → 打包 → 包内 payload hash 复核），随后无条件清理宿主仓副本。

### 两个真实缺陷（都是"独立仓全绿、挂载态/CI 全红"这一类）

1. **挂载态下测不了**：能导入真 SDK 时，真 `NekoPluginBase` 会自己建一套真通道
   （真 `PluginStore` / `PluginConfig`），`bus` 更是**只读 property**，测试既接管不了、
   一碰就是 `TransportError: ctx.get_own_config is not available`。
   根因有两层：①`make_plugin` 夹具绕过了通道接管逻辑；②即使接管，`bus` 也无法赋值。
   修法：测试进程里**无条件**把 `plugin.sdk.plugin` 换成桩门面，让两种形态行为一致
   （挂载态真正要验的裸 id 目录 / entry 解析 / 清单校验 / 打包，由 `check -r` 自己覆盖）。
2. **hosted-tsx 检查器是文本级规则，连注释都不跳过**：面板里一句解释性注释写了
   "`api.call()` 返回的是信封…"，就被判成"用了全局对象"直接拒收。
   同时确认：从 props 解构出与全局对象同名的短名字也会被拒（catgirl_seiyuu 台账同款坑）。
   修法：注释与代码都避开该标识符的裸形态，一律写完整的成员访问形式。

### 反向对照（确认门不是摆设）

- 删掉一个 `panel.errors.*` 键 → i18n 契约门 **2 failed**；
- 只改 `plugin.toml` 的 `mood_tau_hours`（3.0 → 4.0）→ 文档↔配置同源门 **2 failed**；
- 把 `[plugin.store].enabled` 改成 `false` → 静默失效门 **1 failed**。
还原后 `130 passed`。

### 已修掉的（本轮）

- pytest 的 `<Package <目录名>>` 节点会以模块名 `"__init__"` 导入仓根 `__init__.py`，
  触发无父包相对导入崩溃 → conftest 预注册 `sys.modules["__init__"]` + 目录强制按 `Dir` 收集。
- CI 的 ruff 门带 `--ignore-noqa`：裸 `import plugin.sdk.plugin  # noqa: F401` 会被判成未使用导入，
  且 ruff 的修复动作是**删掉整行**（桩探测就此失效）→ 改用 `importlib.import_module` 探测。
- 两处 `import pytest` 与自家包之间的空行不符合 isort 对第三方包的归组 → 已对齐。

## [0.1.0] - 2026-09-14（第一轮 · 首版）

### 落地的东西

- **数值模型**（`core/model.py`，纯函数零 SDK 依赖）：好感度 / 心情 / 健康三项，惰性衰减
  （`exp(-Δt/τ)` 读数时折算）、心情与健康向静息基线回落、好感只缓慢回落不自动回升、
  分档阈值固定为单一来源（不进配置）。
- **行为统计**（`core/behavior.py`）：只读宿主 `bus.conversations` 快照，按 `lanlan_name`
  归属 + `conversation_id` 去重，聚合出用户发言数 / 最近互动 / 连续相处天数 / 时段分布。
- **强注入**（`core/injection.py` + `services/injector.py`）：事件驱动（危机 / 跨档 / 跨天首触 /
  显著漂移）+ 三重频控（间隔、每小时上限、字符预算）；注入正文不给模型看原始数字与档名。
- **入口与面板**：`status` / `tune` / `reset` / `switch` 四个入口 + Hosted TSX 仪表盘；
  fail-closed 总开关默认关闭。
- **她可自主调用的 LLM 工具**：`our_life_feel`（查状态）、`our_life_company`（索取陪伴，带冷却）。
- **中英双语 i18n** + 常驻契约门（码形 / 码↔键双向同步 / 键集一致 / 引用面 / tier 与 trigger 结构门）。
- **文档 ↔ 配置同源门**：`plugin.toml`、`config.example.toml` 与 `core/configuration.py` 的默认值
  必须逐键相等（防"默认值漂移"这类历史债）。
- **五门发版校验链** `tools/release_gate.py`：pytest / ruff / check / release / hosted-tsx，
  其中 release 门真的挂载到宿主 `plugin/plugins/our_life/` 后按裸 id 跑 `check -r`。

### 开发期抓到的真实缺陷（都留下了回归门）

1. **`"{MASTER_NAME} ...".format()` 会 KeyError**：占位符被当成 format 字段，冷落分支一走到就炸。
   由 ruff 的 `F524` 抓到，`test_injection.py` 留了 `test_gap_branches_are_all_reachable`。
2. **唯一分片兜底只在内存缓存里找**：面板早于第一次 tick 打开时会误报 `invalid_lanlan`。
   改为缓存空时回查一次持久化分片（`_single_known_lanlan`）。
3. **总开关关着时「索取陪伴」回 `company_cooldown`**：语义撒谎（不是冷却，是没开）。
   改为回 `not_enabled`。
4. **禁用期的冻结语义自相矛盾**：只把折算基准点推在内存里，进程重启后会一次性补算整段衰减，
   等于惩罚"关掉插件"的用户。改为启动时归一化一次并落盘；禁用期的每拍 tick 不写盘（避免写放大）。

### 测试基建（踩坑记录）

- pytest 9 的 importlib 模式会以模块名 `"__init__"` 导入仓根 `__init__.py`，
  触发"无父包的相对导入"崩溃；`tests/conftest.py` 预注册 `sys.modules["__init__"]`
  并强制目录按 `Dir` 收集。
- 挂载态（市场 CI 把仓库挂进宿主包树）下外层父包会被一起导入，轻量 venv 里没有那些依赖；
  预注册父包链的轻量桩挡住。
- 新增**隐私门**：会话记录里的 `content`（对话正文）在整个持久化状态里必须找不到。

**验证**：`129 → 130 passed`；`ruff`（CI 原样参数）全绿；五门全链通过。

### 已知但有意缓做

- 其余 6 语言 i18n 与注入模板多语言。
- 情绪感知（读对话判心情）——按需求留待后续以独立插件联动方式接入。
- LLM 工具在宿主重启后的自动重注册（宿主不自动重注册；本版靠重载插件恢复）。
