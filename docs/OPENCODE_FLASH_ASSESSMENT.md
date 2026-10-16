# OpenCode DeepSeek V4.1 Flash Free 接入评估

## 后续实际验证（2026-10-15）

用户在 Visual PPT Studio 中实际测试后收到 HTTP 403 `FreeTierError`：OpenCode 免费层只能从 OpenCode 客户端内部调用。故该免费模型不能作为 Studio 的外部 API provider 使用；没有尝试绕过限制。Studio 的模型设置已改为可在 DeepSeek 与智谱 GLM 之间切换，OpenCode 免费档在界面中标为仅限 OpenCode 内部使用。曾经的本机 OpenCode provider 配置仍保留作审计记录，但不再是当前活动 provider。

当前已把 `GLM.rtf` 配置为 GLM 凭据文件，活动模型为 `glm-4.6v-flash`。只验证了本地凭据文件格式、provider 切换和服务健康；没有向 GLM 或 DeepSeek 发送推理请求。官方 OpenAI 兼容 API Base URL 可确认是 `https://open.bigmodel.cn/api/paas/v4`，但本次未找到智谱官方资料明确确认该型号当前 API 免费额度，因此用量成本会显示未知，不能预设为零。GLM 的 PPT 视觉修复质量及工具调用协议仍待用户测试。

## 本机试用配置（2026-10-15）

已将本地 Visual PPT Studio 的活动 provider 指向 OpenCode：

- Base URL：`https://opencode.ai/inference/v1`
- 免费模型 ID：`deepseek-v4.1-flash-free`
- Key 从项目外的 `/Users/ck/PPT自动生成项目/opencode_api.rtf` 本地读取；文件权限已收紧为仅当前用户可读写。原 DeepSeek 官方 Key 配置仍留在原路径，OpenCode 不会回退使用它。
- OpenCode 请求不发送 DeepSeek 专用 `thinking` 参数；图片维持内嵌传输，不假设 Files API 兼容。模型请求严格锁定免费 ID，避免页面旧模型值意外调用付费型号。
- 用量统计仅将这个 OpenCode provider + 精确免费模型 ID 按公开临时免费价格记为 ¥0；Token 仍照常记录。免费资格是 OpenCode 当前公布政策，不保证永久有效。
- 服务与页面默认模型已切换，但**没有发起连接测试或模型请求**。请由用户手动测试，以免未经授权消耗 API 或上传 PPT 页面。

日期：2026-10-15。仅核对官方文档、公开模型配置和本地代码/日志。未修改运行配置，未发送模型推理请求，未上传 PPT。

## 结论

值得作为独立的免费提供方进行小规模验证，有望承担当前最昂贵的 Visual Agent。现有代码不能只更改模型名称就完整切换；视觉网关兼容性、限流和图片复用尚未通过实际请求验证。不能根据同名模型承诺效果与原厂完全相同。

## 已核实

- OpenCode 官方当前列出 `deepseek-v4.1-flash-free`，输入、输出和缓存读取免费。`deepseek-v4.1-flash` 是另一个收费 ID，不能混用。
- 官方声明免费仅限一段时间；其提供方零留存且不使用请求数据训练。没有公布保证永久免费的日期。
- 官方模型表当前使用 `https://opencode.ai/inference/v1/chat/completions`，推理文档还明确列出协议专用别名 `/inference/openai/v1/chat/completions`。旧 Zen 文档链接已跳转新版。
- 官方推理文档说明：免费聊天模型可不带 Authorization；付费模型需要 Console service account key。因此不应把原 DeepSeek Key 自动发送给 OpenCode，也无需仅为验证免费模型而预先充值。
- 同组织的 models.dev 公开配置：免费模型覆盖为 context=200,000、output=32,000；继承 DeepSeek V4.1 Flash 基础模型的图文输入、工具调用和结构化输出标记。支持 reasoning_effort low/high/max，交错思考字段为 reasoning_content。
- 基础模型元数据的图文标记是接入线索，不是本项目已通过网关视觉测试的证明。

## 限流与连通性

没有核实到此新模型公开保证的 RPM/TPM/RPD 数字及重置周期。官方仓库存在旧 V4 免费模型的 429/FreeUsageLimitError 报告，不能把旧模型用户观察到的“约200次”等数字当作 V4.1 的正式额度。

本机对旧、新公开 models GET 端点的 urllib 请求均返回 HTTP 403；网页检索工具能读取公开清单，清单包含新免费模型。这不代表推理接口一定不可用，但本机连通性尚未验证，可能涉及网关防护或网络路径，不能宣称已接通。

## 与当前工程的关系

历史55条请求中，最高已知单次输入为130,095 Token。旧样本不因输入本身就超过200K；仍须给输出/推理保留空间。当前主 Agent 请求 max_tokens=6500，低于公开32K输出上限。新压缩流程的真实 Token 尚未测量，不能用字节数换算。

Visual Agent 贡献约99.71%的已知估计费用：¥10.045254 / ¥10.074736。只把规划等少量文字工作转免费，对这次账单收益有限；要显著省钱，需要验证免费入口能胜任截图诊断与修复复核。

待适配项：

1. **提供方配置**：主异步入口读取 DEEPSEEK_BASE_URL，但 Editing Runtime 实例当前使用自身原厂默认地址。必须统一路由两个入口，并隔离凭据，才能避免一部分请求继续收费。
2. **费用监控**：现有计价表没有 OpenCode 免费模型；直接更换会记录未知费用，且两次未知后预算保护会暂停。应按提供方+准确模型ID登记经核实的免费价格，保留官方返回的真实 Token。不能把没有 usage 的请求伪造为零 Token，也不能将付费同名路由误标免费。
3. **图片传输**：当前 Files API 去重仅自动用于 api.deepseek.com。尚无已核实的 OpenCode 对应 Files API 支持；原厂 file_id 也不能假定可跨提供方使用。若使用 Base64 图片，免费推理仍会消耗手机流量，重复上传可能比当前原厂引用模式更多。
4. **参数**：需验证 image_url、response_format=json_object、thinking 开关、reasoning_effort、工具消息及usage字段的实际行为。HTTP成功还不够，要证实模型确实读到了图片和细小公式。
5. **限流处理**：429时有界等待或保存候选暂停，不逐问题反复重试；免费不可用时由用户主动选择是否启用付费提供方。

## 推荐的后续验证（尚未执行）

先完成隔离的适配器与Mock测试，然后只用免费模型进行三个有界试验：短文本JSON、带已知小字/分式的公开或合成图片、用户选定的一张PPT的一轮诊断/复核。先确认前两项，再上传用户页面；不自动跑完整修复循环。

每项都记录真实usage、返回模型ID、实际传输量、耗时和错误；验证小字、缺字、空格和公式分子/分母/上下标读取正确。任务按单页串行，有明确请求数上限，关闭自动付费回退。是否长期替换以同一页的修复效果对照决定。

如果以后绑定付款方式，注意官方文档的自动充值设置：余额低于5美元默认可补充20美元，可关闭。免费试验不需要为此充值。

## 来源

- [OpenCode 官方模型与价格](https://opencode.ai/v2/docs/console/models/)
- [OpenCode 官方推理接口与免费模型鉴权说明](https://opencode.ai/v2/docs/console/api/inference/)
- [免费模型公开配置：200K/32K/零价格](https://raw.githubusercontent.com/anomalyco/models.dev/dev/providers/opencode/models/deepseek-v4.1-flash-free.toml)
- [基础模型公开能力：text+image、tools、structured_output](https://raw.githubusercontent.com/anomalyco/models.dev/dev/models/deepseek/deepseek-v4.1-flash.toml)
- [OpenCode提供方端点配置](https://raw.githubusercontent.com/anomalyco/models.dev/dev/providers/opencode/provider.toml)
- [旧免费模型的限流问题记录：不作为新模型配额承诺](https://github.com/anomalyco/opencode/issues/33495)
