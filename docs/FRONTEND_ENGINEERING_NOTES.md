# 前端工程结构与后端接口映射

这份说明记录现有单页应用的真实边界。`docs/FRONTEND_PAGE_SPEC.md` 仍是页面行为的主要规格；本文件以 `studio/server.py` 当前路由和 `web/app.js` 调用为依据，不代表尚未实现的服务。

## 当前前端结构

- `web/index.html`：单页工作台外壳、8 个页面区块和新建项目弹窗。`#tabs` 是唯一导航，页面编号与状态保留现有 DOM ID，避免破坏业务绑定。
- `web/styles/design-system.css`：集中保存颜色、字体、间距、圆角、阴影和共享布局覆盖。`web/style.css` 保留既有画布、OCR、质检和业务组件样式。
- `web/components/app-shell.js`：项目列表行、顶部项目上下文、统一导航状态、统计卡片。
- `web/components/icon.js`、`web/assets/icons/`：共享图标索引和独立 SVG 源文件。
- `web/services/api.js`：HTTP、JSON 响应与网络错误处理。业务路由仍以 FastAPI 已存在的路由为准。
- `web/app.js`：既有项目状态、页面行为和业务操作。目前仍是较大的单文件控制器；逐步拆出各业务页面控制器时应保留现有 API 调用和状态语义。

## API 服务映射

| 前端职责 | 当前后端路由 | 真实能力与边界 |
|---|---|---|
| 项目列表、创建、读取 | `GET /api/projects`、`POST /api/projects`、`GET /api/projects/{pid}` | 项目和页面规划保存在本地工作区；创建后由前端进入逐页规划。 |
| 规划和 Design System | `PUT /api/projects/{pid}/slides/{sid}`、`POST /api/projects/{pid}/lock`、`POST /api/projects/{pid}/plan/offline`、`POST /api/projects/{pid}/plan/ai`、`PUT /api/projects/{pid}/design`、`POST /api/projects/{pid}/style/ai`、`GET /api/projects/{pid}/prompts`、`POST /api/projects/{pid}/slides/{sid}/prompts/ai`、`GET /api/projects/{pid}/prompts.zip` | 离线建议与 DeepSeek 建议是不同路径；模型 Key 在调用时由调试输入提供，不写进项目。 |
| 资产上传和移除 | `POST /api/projects/{pid}/upload`、`POST /api/projects/{pid}/upload-batch`、`POST /api/projects/{pid}/slides/{sid}/images/{role}/remove`、`POST /api/projects/{pid}/uploads/{token}/restore`、`GET /media/{pid}/{filename}` | Design/Clean/透明素材采用项目现有命名和配对规则；移除操作支持撤回。 |
| Agent、OCR 和重建 | `GET /api/projects/{pid}/reconstruction-progress`、`GET /api/projects/{pid}/slides/{sid}/reconstruction`、`POST /api/projects/{pid}/slides/{sid}/confirm-reconstruction`、`POST /api/projects/{pid}/slides/{sid}/ocr`、`POST /api/projects/{pid}/slides/{sid}/analyze-structure`、`POST /api/projects/{pid}/slides/{sid}/review-image-pair`、`POST /api/projects/{pid}/slides/{sid}/prompts/refine-clean` | OCR/结构识别按本地可用依赖和 DeepSeek Key 运行；上传/配对结果以服务响应为准。 |
| PPT 原生对象、公式和编辑器 | `PUT /api/projects/{pid}/slides/{sid}`、`POST /api/projects/{pid}/slides/{sid}/text-layout`、`POST /api/formula/preview`、`POST /api/projects/{pid}/slides/{sid}/inpaint`、`POST /api/projects/{pid}/slides/{sid}/extract-asset` | 文本、公式、形状和图片对象写入现有 slide 数据。浏览器画布是编辑草稿；保存才提交。 |
| OCR 背景修复与候选 | `PUT /api/projects/{pid}/slides/{sid}/regions`、`POST /api/projects/{pid}/slides/{sid}/clean-candidate`、`POST /api/projects/{pid}/slides/{sid}/accept-clean`、`POST /api/projects/{pid}/slides/{sid}/raster-first`、`POST /api/projects/{pid}/slides/{sid}/compare-render`、`GET /lab/{pid}/{sid}/{filename}` | Clean 修复先生成候选，用户确认后替换正式 clean；Mask/识别区域单独保存。 |
| QA、导出、校准和调试 | `GET /api/projects/{pid}/qa`、`POST /api/projects/{pid}/export`、`POST /api/projects/{pid}/slides/{sid}/visual-qa`、`POST /api/projects/{pid}/slides/{sid}/calibrate-text`、`GET/POST /api/projects/{pid}/calibration-memory`、`GET /api/projects/{pid}/debug`、`GET /api/health`、`POST /api/ai/test` | 普通导出与 AI 校准分开；实际渲染评分取自生成报告，不能用 UI 示例数替代。LibreOffice 是辅助渲染，WPS 导入验收另有路由。 |
| WPS 验收 | `GET /api/wps-environment`、`POST /api/projects/{pid}/wps-render`、`GET /api/projects/{pid}/wps-render/{import_id}`、`GET /wps-preview/{pid}/{import_id}/{filename}` | 用户提供 WPS 导出的 PDF/截图后记录对照；导入证据不会自动判定验收通过。 |
| 批量创建与导出 | `POST /api/batch/create`、`POST /api/batch/export`、`GET /batch-download/{filename}` | CSV 项目创建和 ZIP 导出由现有服务实现，逐项失败返回在批量结果中。 |

### 当前明确未接入

- ChatGPT Image / 通用图像生成 API：不存在；页面只生成、复制提示词并接收手工上传的图片。
- 后端尚无独立云端服务层或公开 API 版本协商；此应用通过同源 FastAPI 路由运行。
- 页面计划里的设计概览、流程图预览等示意 UI 不等于已经连接生图服务。

## 共享 UI 约定

页面导航固定为规划、上传与重建、检查与导出、视觉规范、原生对象编辑、OCR 与修复、批量生产、调试中心。侧栏宽度在桌面统一为 264px，顶部项目栏为 64px；小屏侧栏折叠为可横向滚动的导航，项目选择仍保留。Logo 与 8 个导航图标均以可维护 SVG 文件提供。

共享颜色由 `web/styles/design-system.css` 中的 `--color-*` Tokens 管理。状态颜色用于语义提示，正文和按钮文字仍明确说明当前状态。
