# DeepSeek API Token 消耗审计

审计日期：2026-10-15
范围：`visual_ppt_studio` 当前代码与本地已保存的运行记录
执行方式：只读检查；未修改程序代码、未调用 DeepSeek、未运行测试。

## 结论摘要

1. **确有可避免的重复上下文。** 最明确的路径是实验性 Editing Runtime：每一轮把从开头到当前的完整 `messages` 再次提交，原始 Design/基线图片、历史工具调用与工具结果会跟着每一轮重新传输。工具结果单条最多截到 15,000 字符，但整个历史没有总长度上限。
2. **主 Visual Agent 也反复发送整页多模态上下文。** 每轮重新编码并发送 Design 和最新整页渲染；反思阶段再发送 Design、修改前、修改后三张整页图，并可附三张放大图。它确实只读取当前页的对象，不是每轮重发整个多页项目；对象、OCR 来源、验证证据、历史和反馈仍会随每轮重发。
3. **部分简单任务默认使用 DeepSeek 高强度思考。** `call_deepseek()` 只有收到 `thinking` 参数时才设置模式。规划、风格、配对 Prompt、粗布局分析、通用视觉 QA、连接测试均未显式设置；DeepSeek 当前文档说明缺省为开启思考且默认 high。Visual Agent、公式识别、结构分析、漏字建议、文字校准则明确关闭思考模式。
4. **现有本地运行日志没有官方 Token 用量或费用明细。** 代码收到 DeepSeek 响应后只取 `choices[0].message.content`，并丢弃 `usage`、响应 ID、实际模型路由等字段。存档中的 `status.json` 有模块/阶段、响应内容和部分耗时，不能恢复官方的 prompt/cache/reasoning Token。
5. 控制台给出的 96 次请求 / 8,051,325 Tokens 对应平均约 **83,868 Tokens/次**。这与本地记录无法逐请求对账：当前 workspace 的多个项目快照存有不同时间的运行记录，且没有请求 ID、API Key 标识或精确计费时间关联。不要把快照累计轮数当作这 96 次请求的账单拆分。

## 1. 调用架构和调用链

### 统一 Chat Completions 封装

`studio/ai.py:292` 的 `call_deepseek()` 是主要 API 传输层：解析 Key 与模型，组装 Chat Completions 请求，应用 JSON 模式和可选思考开关，解析响应 JSON 后仅返回模型的 `content`。它目前不返回 usage，也不记录请求 ID、模块名或重试次数。

该封装被以下功能调用（路由位于 `studio/server.py`）：

| 功能 | 路由 / 入口 | AI 函数 | 图像/文本输入概况 |
|---|---|---|---|
| 规划建议 | `/api/projects/{pid}/plan/ai` | `generate_plan` | 项目主题、受众、标题、页数和页码 |
| 视觉规范建议 | `/api/projects/{pid}/style/ai` | `generate_style` | 项目主题、风格、受众 |
| 双阶段图片 Prompt | `/slides/{sid}/prompts/ai` | `generate_image_prompts` | 项目/页面规划文字 |
| 标题/正文粗定位 | `/slides/{sid}/analyze` | `analyze_visual` | Design 整页图和规划文字 |
| 场景结构识别 | `/slides/{sid}/analyze-structure` | `analyze_structure` | Design 整页图 |
| 公式识别 | `/slides/{sid}/recognize-formulas` | `recognize_formula_crops` | 最多 8 个公式区域图 |
| Design/Clean 配对审核 | `/slides/{sid}/review-image-pair` | `review_image_pair` | Design 与 Clean 两张整图 |
| Clean Prompt 修订 | `/slides/{sid}/prompts/refine-clean` | `refine_clean_prompt` | Design/Clean 整图、精简审核证据、旧 Prompt（最多 7,000 字符） |
| 文字样式校准 | `/slides/{sid}/calibrate-text` | `calibrate_text_layout` | Design/实际渲染、当前文字对象、布局证据 |
| 智能查漏/修复候选 | `/repair/smart-candidates` | `propose_missing_text` | Design/渲染、最多 220 条 OCR 与 220 个对象、最多 4 对局部放大图；相同输入有本地缓存 |
| 页面视觉 QA | `/slides/{sid}/visual-qa` | `review_visual` | Design 与实际渲染两张整图 |
| 第三步 Visual Agent | `/visual-agent/run` | `_bounded_visual_agent_turn` → `visual_agent_turn` | 多轮整页图、当前页对象、来源、历史、工具反馈、反思图和区域裁图 |
| API 连通性测试 | `/api/ai/test` | `call_deepseek` | 一条很短的 JSON 测试消息 |

本地 OCR、确定性布局检查、渲染、导出、导入和多数工具执行本身没有调用 DeepSeek。智能查漏会先运行本地规则，并对绑定 revision、图片、渲染、对象和反馈的结果做缓存。

### 第二个独立 API 入口

`studio/visual_ppt_runtime/agent.py:34-57` 的 `DeepSeekChatProvider.complete()` 自己发送 `/chat/completions`，没有经过 `call_deepseek()`，也没有 usage 统计。它由 `studio/editing_runtime_api.py` 的隔离编辑会话入口调用。这个独立入口必须纳入任何后续统一监控设计。

## 2. 本地运行记录能证实什么

### 可直接数出的模型轮次

本地保存的当前工作区状态文件中：

- `workspace/visual-agent-s001-isolated/dbbf0726115e/build/visual_agent/`：30 份运行状态、137 条模型轮次记录；其中 `diagnose_after_skip` 13 条，重复问题继续诊断 1 条。
- 邻近快照 `.../6d4d98dcf358/build/visual_agent/`：17 份状态、121 条模型轮次；包括 38 条 `diagnose_after_skip`、6 条 `diagnose_duplicate_skip_and_continue`、40 条反思轮次。
- 当前浏览器 URL 对应的 run `dbbf0726115e/.../736d70f1cb814abdb960b090d73f03df/status.json`：S002 共 10 条模型轮次，5 次 diagnose、1 次 targeted diagnose、3 次 reflect、1 次 skip 后诊断。该记录没有任何 usage 字段。
- 更复杂的单页 run `6d4d98dcf358/.../0edab2bd108e4dc5938ee5391272e745/status.json`：S002 共 29 轮，含 11 次 reflect、5 次 skip 后诊断、6 次重复问题继续诊断。响应元数据记录 69 次整页图片输入（按每次请求的图片标签计）及 11 张问题区域裁图。
- 独立 Editing Runtime 目前存有 4 个 agent trace，模型调用数分别为 4、7、5、4；trace 只记轮数和工具事件，无 usage 字段。

这些是本地运行档案中的请求轮次/图片标签证据，不是 DeepSeek 账单明细，也不能断言每一条都落在用户截图所示的相同 Key、账单周期或 96 次请求之内。

### 现有记录缺失

代码与状态记录均没有以下官方字段：`prompt_tokens`、`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`、`completion_tokens`、`completion_tokens_details.reasoning_tokens`、`total_tokens`。也没有 provider request ID、重试序号和单次调用耗时的完整可靠记录。主 Agent 有 `elapsed_seconds`，但它只记录应用层单轮计时；部分初轮记录为 `0.0`，不可当作精确网络耗时。

因此，目前**无法按功能、页面、运行任务计算真实 Token/费用，也无法把 8,051,325 Tokens 分解为文字、图片、缓存输入和推理输出**。对这组历史数据，只能计算总量和均值：8,051,325 ÷ 96 ≈ 83,868 Tokens/请求；费用需按命中输入、未命中输入、输出 Token 和计费时段分别核算。

## 3. 五个最值得进一步验证的浪费点

按代码证据和潜在影响排序。没有官方 usage 拆分的项不伪装成已量化费用。

### 1) Editing Runtime 每轮重放完整累积对话和图片

- 证据：`studio/visual_ppt_runtime/agent.py:96-100` 把 Design 和初始渲染图放进 `messages`；`121-164` 持续追加 Assistant/tool 消息和局部图；`174-182` 追加每轮真实渲染图；`104` 每次都把整个 `messages` 传给 Provider。
- 工具结果单条最多 15,000 字符，但历史总量没有截断上限。
- **这是最确定的重复传输机制。** 对一个 7 次调用 trace，初始 Design/基线两图在 7 个请求中分别重复出现；初始图像输入项目有 14 次传输，其中 12 次是后续重放。它表示图像输入重复次数，不等于总 Token 可节省比例；图片缓存命中和图像 Token 计价明细没有本地数据。

### 2) Visual Agent 在 skip/duplicate 路径多次重发整页图、对象和证据

- 证据：`studio/server.py:1751-1806` 每跳过一个问题，再请求模型处理下一个；`1995` 等 duplicate skip 也另发一次诊断；`studio/ai.py:772-785` 每次从当前页提取最多 400 个对象，并序列化 history、feedback、OCR 来源和验证证据；`838-897` 每次重新放入整页图和长任务指令；`910` reflect 再放入修改前/后渲染和问题区域裁图。
- 已存档的极端样本单页 29 轮，其中共有 69 个整页图输入和 11 个区域图输入。每个反思轮都重新发 Design；这是视觉核验的一部分，但只为跳过一个候选而重做不变的大量上下文可能不必要。
- 整套多页 project 在 run 开始读取一次；各模型轮次传的是当前页对象，不是每次都发送完整项目。这一点不支持“每轮重发整个 PPT”的说法。

### 3) 简单任务默认开启 high 思考

- 证据：`studio/ai.py:293-300` 仅在 `thinking is not None` 时才把开关写入请求。`generate_plan`、`generate_style`、`generate_image_prompts`、`analyze_visual`、`review_visual` 和 `/api/ai/test` 调用时未传 `thinking`。
- DeepSeek 官方文档当前说明：思考模式缺省开启，默认 effort 为 high。推理输出算在 completion tokens 中，因此上面这些短 JSON/基础建议功能存在真实的额外推理 Token 风险。
- 相对地，复杂主 Agent 和多数 Vision 处理显式传 `thinking=False`；不能把全部 8M Token 都归因于“Agent 深度思考”。

### 4) 当前页对象、OCR、历史和反馈重复进入每轮 Prompt

- 证据：`studio/ai.py:772-785` 序列化当前页最多 400 个对象；来源区域上限 16,000 字符、verified evidence 6,000 字符、历史 12,000 字符、工具反馈 12,000 字符。后续在 `prompt` 拼接时把这些内容每轮再发一次。
- 单轮最多可重复携带约 46,000 字符的这几类结构化上下文（未计固定提示词、工具 Schema、对象本身、图片和用户意图）。这是字符上界，不是 Token 数；目前没有在请求前记录其实际字节/字符/图片尺寸。
- 代码采用 12,000 字符裁剪，但历史条目可能包含重复工具事件和较长渲染 QA，裁剪不能消除跨轮重复。

### 5) JSON/空响应/网络重试会重发整个原始多模态请求

- 证据：`studio/ai.py:303-335` 对非法 JSON 最多追加一次重问、对空响应最多追加一次重问、HTTP 网络异常最多重试一次；所有路径都沿用原来的完整 `request_messages`，包括相同图片和上下文。
- 这是有限重试，不是无限重试；但应用日志不存重试次数，不能量化它在 96 次中的占比。状态码错误被转成 `AIError` 后不会由此处网络重试捕获。应用层 Visual Agent 另有 45 秒 wait_for，而 HTTP 客户端超时是 90 秒；取消发生时服务端是否已处理/计费无法从本地判断。

## 4. 思考模式与工具协议

主 `visual_agent_turn()` 明确 `thinking=False`，所以它不应产生大量 reasoning tokens；规划/风格等未传模式的路径默认启用 high 是应优先用下一阶段监控验证的点。

独立 `DeepSeekChatProvider` 则既未设置 `thinking`，又在后续轮次只把 `content` 和 `tool_calls` 填回 assistant 消息，未保留 `reasoning_content`（`studio/visual_ppt_runtime/agent.py:50-57,121`）。DeepSeek 官方对带 tools 的思考模式要求后续请求回传上一轮 `reasoning_content`。这条链路默认处于思考模式，存在协议不完整/失败重试的风险；它的 Provider 当前没有自动重试，但模型调用失败会结束运行。后续修复此路径时必须同时明确思考模式和协议，不能简单删历史后继续启用 thinking。

## 5. 当前官方计价规则与截图金额

DeepSeek 官方实时价格页当前将 `deepseek-flash` 对应 V4.1-Flash，输入缓存命中、缓存未命中和输出分别计价，并区分峰时/闲时。官方公式是各类 Token 数乘以各自单价；不能把全部 8,051,325 Tokens 乘一个统一单价。截图金额 ¥8.04 是控制台的实际结果，但因没有 hit/miss/output 拆分、账单时间段和请求映射，无法仅凭现有代码重算。

官方文档：

- [DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)
- [Chat Completions API Usage 字段](https://api-docs.deepseek.com/api/create-chat-completion/)
- [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)
- [DeepSeek Thinking Mode 与工具历史要求](https://api-docs.deepseek.com/guides/thinking_mode/)

## 6. 有证据与待验证假设

### 代码/档案已证实

- 当前有两个 Chat Completions 传输入口，其中 Editing Runtime 绕过统一封装。
- 所有 API 入口都没有保留/落盘官方 usage；主封装丢弃 usage。
- Visual Editing Runtime 会逐轮重传累积消息、图片和先前工具结果。
- Visual Agent 多轮重复包含整页图和当前页数据；已有运行档案能数轮数、阶段和图片标签。
- 某些调用没显式关闭 thinking；DeepSeek 当前默认 thinking/high。
- 智能查漏已有输入绑定的本地缓存；本地 OCR、QA 和渲染不是模型 API。

### 目前无法证实

- 截图中 96 次请求分别来自哪些功能/页面/run，及它们是否都由这个项目或同一 API Key 产生。
- 每次图片、文本、缓存命中、缓存未命中、completion、reasoning 的真实 Token 数和金额。
- JSON/空响应/网络重试到底发生多少次；超时请求是否被 DeepSeek 计费。
- 多轮重复图片中哪些块命中 DeepSeek 前缀缓存，以及命中率对费用而非 Token 总量的实际影响。
- 上述优化实际能降低总 Tokens 或费用多少。现在没有逐请求 usage 基线，给节省比例只能算猜测。

## 7. 后续优化建议（本轮不实施）

顺序按收益/风险比排列，全部保留视觉核验：

1. 先在两个 HTTP 传输层统一采集响应 usage、模型响应 ID、任务/页面/模块、时间、耗时、HTTP/应用成功失败、重试序号和 thinking 配置；只记录组成部分的长度、图片尺寸/哈希与类型，绝不写 API Key、base64、完整 prompt 或完整用户文件。流式调用需取最后一个 chunk 的 usage；当前两个入口均为非流式。
2. 把 `reasoning` 配置显式化：简短规划/连通性/粗定位按实际质量评估 `none` 或 `low`，复杂视觉诊断保留足够推理；用代表页实测质量和 reasoning_tokens，而不是统一关掉。
3. 为 Editing Runtime 的历史做瘦身/摘要，只保留当前页相关状态、最近工具结果、必要的 Design 和最新前后渲染；保留问题区域裁图与整页上下文的必要视觉验证。调用数 N 时完整历史重复回送造成随 N 增长的输入累积，优先级最高。
4. 对 Visual Agent 优先减少“跳过/重复诊断”回合里没有新证据的完整重发；仍在每个实际编辑后保留真实渲染反思，并在最终导出保留完整 PPTX/渲染验收。
5. 保持 JSON/空响应重试有界，但记录每次 retry 的实际响应 ID/usage。检查 finish_reason 和 token 上限，防止因为截断重新传整页多模态输入；只有缺失/截断的输出可局部重试时，评估是否能避免重发图像。
6. 以 usage 日志跑少量代表性任务后再计算优化百分比。基线记录至少覆盖一个普通页面、含公式页面、重复/遮挡问题页面、一次成功修改和一次失败/重试。

### 可作参考的上界计算（不是总成本预测）

对 Editing Runtime 的 7 轮 trace，若将初始两张图只保留在第一轮请求，至少可避免后续 6 轮重送这两张初始图，即初始两张图的重复出现次数由 12 降为 0。**这只量化初始图重复次数，不能推出总输入 Token 或费用节省 85.7%**，因为文字历史、每轮新渲染、模型计价缓存命中以及图像的官方 Token 计数都未记录。

## 8. 第一阶段已实施：监控与 Mock 基准

本节记录 2026-10-15 完成的监控能力。本轮没有改动 Agent 的轮次、提示词内容、问题接受条件、工具权限、修复/渲染/验收流程，也没有调用真实 DeepSeek API。

### 调用链和监控覆盖

- `studio/ai.py::call_deepseek`：异步非流式 Chat Completions。每次实际 HTTP 尝试各写一条记录；网络重试、空回复重试、模型 JSON 内容不合法的重试都能在请求记录中体现。重试计数表示当前 HTTP 尝试之前已经发生的应用重试数。
- `studio/visual_ppt_runtime/agent.py::DeepSeekChatProvider.complete`：同步非流式、含可选 tools 的独立 Provider；成功、HTTP 失败、传输/解析失败均进入同一统计文件。Provider 返回的 tool-call message 结构未被监控代码改写。
- `studio/visual_ppt_runtime/cli.py::agent`：同一同步 Provider 的 CLI 入口也设置独立任务 ID、模块和页码上下文；仍写入相同本地请求/任务日志。
- 新增 `studio/api_usage.py::StreamingRequestTelemetry`，只在最终 SSE chunk 收到官方 usage 后记录用量；若最终 chunk 没带 usage，则记录为未知。审计到的当前两条生产调用链都不是流式调用，因此这个适配器尚无线上流式路径可挂接。
- FastAPI middleware 将发起请求关联为任务，并从 URL 读取项目、页码和功能模块；Visual Agent / Editing Runtime 再用各自的 run ID 覆盖 task ID。Visual Agent 会关联已提供的问题 ID。

### 本地数据和费用

- 文件：`workspace/api_usage/requests.jsonl` 逐 API 请求追加记录；`workspace/api_usage/outcomes.jsonl` 记录任务结束状态、候选验证结果和端到端耗时。`workspace/` 已被 Git 忽略，日志留在本机。
- 每条请求含本地 request ID、DeepSeek 响应 ID（若有）、任务/项目/页码/模块/问题 ID、模型、北京时间时段、请求耗时、重试数、HTTP/应用状态、错误类型、thinking 开关请求值、官方 usage 和组件结构字节数。
- 监控写入采用 best-effort：本地磁盘/日志不可写时只输出不含请求内容的错误类型告警，不把已成功的 DeepSeek 请求或候选结果变成失败。
- Token 字段只取 DeepSeek 响应：prompt/input、cache hit、cache miss、completion/output、reasoning、total。Responses API 字段和 Chat Completions 字段均有归一化；缓存 miss 只在 API 返回 prompt 总数与 hit 数时通过二者差值计算。缺少计费拆分、usage 或模型费率时费用为未知，不以字符数补算。
- `component_bytes` 只统计 system/user/assistant/tool 文本、tool-call 参数以及图像 data URL 的序列化字节规模/数量，并非 Token。提示词、OCR 正文、PPT 对象文本、API Key、Base64 图片正文均不写入日志。一个单向 SHA-256 输入指纹仅用于同任务重复请求提示；不保存原始输入。
- 按 DeepSeek 中文官方价目表，以 CNY/百万 Token 计算缓存命中、未命中、输出费用。Flash 闲时/峰时费率依次为 ¥0.02/0.04、¥1/2、¥4/8；Pro 费率依次为 ¥0.15/0.30、¥4.5/9、¥13.5/27。推理 Token 是输出 Token 的子集，费用不重复计收。官方峰时为北京时间工作日 9:00–12:00、14:00–18:00，法定假日除外。
- 为避免误报“精确费用”，没有完整假期日历时，可能为假日的峰时请求按峰时费率作**保守上界**并标明未核实。管理员可配置 `PPT_STUDIO_CN_HOLIDAYS`（逗号分隔日期）和 `PPT_STUDIO_CN_HOLIDAYS_COMPLETE_YEARS`（已提供完整假期表的年份）。其他已知时段按官方费率计算。
- API：`GET /api/usage/summary?project_id=...` 和 `GET /api/usage/requests?project_id=...`。前端侧栏新增“API 用量与费用”，可看当前项目费用/Token、按页/模块/任务汇总、输入缓存/普通输出/推理拆分、最贵请求、失败重试、异常和候选修复基线。

### 仅告警的异常标记

- 单请求输入或总 Token ≥ 250,000。
- 同一任务完全相同的输入指纹重复出现；第三次及以后标记可能循环。
- 有相同 Visual Agent 问题 ID 的多次模型请求：第三次开始标记重复检查，第四次开始标记可能循环。
- 同任务结构化输入字节数相较上一请求增长超过 1.8 倍或至少 20 KB。
- 上述情况只被记录和展示，**不改变流程、不取消请求、不阻止修复**。阈值只供定位异常，后续可基于运行数据调整。

### Mock 验证与真实基准方案

- 新增 `tests/test_api_usage.py`，11 项 Mock 测试覆盖 usage 字段、Responses/SSE 最终用量、峰谷及假日计价、缺 usage/未知模型、隐私日志、本地分组、修复成功率/端到端耗时、Token 异常/重复/增长、异步调用、同步工具 Provider、JSON 重试和日志目录不可写时不影响主流程。测试命令：`.venv/bin/python -m pytest tests/test_api_usage.py -q`；结果：**11 passed**。
- 另已通过 Python `compileall`、`node --check web/app.js` 和应用导入检查；新增 API 路由可被 FastAPI 加载。未改动任何旧测试。
- 实际基准尚未执行，也没有产生新的 DeepSeek 费用。由用户确认后，建议从同一项目选一张简单页（短标题和卡片，无公式）与一张复杂页（公式、多语言/小字/多个区域）；各按当前工作流完整运行一次。记录请求数、各类真实 usage、费用、API/整页耗时、通过最终 PPTX/Chromium/LibreOffice 验收的候选比例和待复核项。复杂页需保留完整视觉上下文与最终验收；不以降低质量作为省钱手段。真实测试前应确认当时监控页面能看到请求，避免未知费用被误认为 0。

### A/B/C 后续实施提案（本轮未实施）

1. **A · Editing Runtime 历史**：先看真实日志确认哪几轮反复发送相同 Design、基线图、工具结果。后续按“稳定 System/Schema 前置、变化内容后置”组织缓存前缀；会话保留必要全局图、当前问题相关对象差异、最近真实渲染与必要工具反馈，避免复制已完成的旧工具回执。每次确实修改后仍做真实渲染复核；清理 thinking history 前先兼容 DeepSeek tools 协议所需的 `reasoning_content`，绝不在 thinking 模式下丢掉必要协议消息。
2. **B · Thinking 配置**：显式记录请求的 thinking 开关；当前 DeepSeek 不在响应中回报“实际 reasoning effort”档位，因此 UI 会把实际档位标为 API 未提供，同时单列官方 `reasoning_tokens`。取得简单/复杂页基线后，只对简单规划/结构化小任务做低风险对照，复杂视觉、公式和修复继续保留充分推理。必须比较文字/公式准确率、通过率和返工，不做统一关闭或切便宜模型。
3. **C · Visual Agent 多轮**：用 task/page/issue ID、重复指纹、usage 与每轮耗时先定位 29 轮样本究竟消耗在 skip、重复诊断、工具失败还是有效修复。后续可以加入“无新证据且同一问题重复”的审阅提示和安全终止提案，但不能因 Token 高就中断；实际编辑后的渲染、最终 PPTX/Chromium/LibreOffice 验收必须保留。任何终止策略先用历史轨迹回放评估漏修率，再分阶段开启。

**节省比例暂不预测。** 当前只有 Mock 数据，不构成成本/修复质量实验。新监控积累一组用户认可的真实基准后，再按相同页、相同工作流比较每次调用的真实 usage、金额、修复通过率和耗时。只有质量不下降并通过用户确认后才进入 A/B/C。

官方费率与 usage 字段以 DeepSeek 实时文档为准：[中文模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[Chat Completions API](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)。
