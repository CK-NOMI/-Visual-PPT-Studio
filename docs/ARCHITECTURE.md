# 架构与模块契约 v0.1

## 总数据流

```
创建项目 -> S001…S0NN编号计划 -> 锁定 -> 提示词ZIP
                      |                     |
                      |         人在ChatGPT网页版生成设计稿
                      |                     |
                      +----------- 设计稿+无字底图按编号上传
                                          |
                                  原生布局场景编辑
                                          |
                    optional DeepSeek Vision 坐标建议（不自动覆盖）
                                          |
                         PptxGenJS -> editable.pptx
                                          |
                       python-pptx结构核对 / LO渲染
                                          |
                        规则QA / 可选DeepSeek视觉QA
                                          |
                             PDF / PNG / ZIP 批量
```

## 核心 JSON 约束

`workspace/<项目12位hexID>/project.json`：
- `locked`：确定页数、ID和顺序后才允许上传。
- `slides[].id`：严格 S001、S002…，永不依赖文件上传顺序。
- `slides[].images`：角色到图片文件名的映射。
- `slides[].elements`：以 `box=[x,y,w,h]` 记录 0..1 的几何坐标。有效对象类型：`text/rect/ellipse/line/image`。
- `slides[].title/body`：可信原始文本。最终 title、body 对象的文本必须从此字段读取，禁止从设计图片OCR回写。
- `slides[].masks`：本地去字的辅助选区，非AI语义遮罩。

## API 约定

| 路由 | 方法 | 功能 |
|---|---|---|
| `/api/projects` | GET/POST | 项目列表/创建 |
| `/api/projects/{id}` | GET | 项目JSON |
| `/api/projects/{id}/lock` | POST | 锁定编号 |
| `/api/projects/{id}/slides/{sid}` | PUT | 更新当前页文案、几何元素 |
| `/api/projects/{id}/plan/ai` | POST | DeepSeek建议，**不自动保存** |
| `/api/projects/{id}/style/ai` | POST | DeepSeek颜色和风格建议 |
| `/api/projects/{id}/prompts.zip` | GET | 逐页提示词zip |
| `/api/projects/{id}/upload` | POST multipart | 映射单个图片到明确的页码/角色 |
| `/api/projects/{id}/upload-batch` | POST multipart | 严格图片名，防错页 |
| `/api/projects/{id}/slides/{sid}/analyze` | POST | 可选VLM建议标题/正文坐标 |
| `/api/projects/{id}/slides/{sid}/analyze-structure` | POST | DeepSeek Vision 返回可审核的页面对象草案，不自动保存 |
| `/api/projects/{id}/slides/{sid}/inpaint` | POST | OpenCV实验性mask局部去字 |
| `/api/projects/{id}/qa` | GET | 本地规则审计 |
| `/api/projects/{id}/export?mode=editable` | POST | PPTX+PDF+PNG（LibreOffice可用时） |
| `/api/projects/{id}/slides/{sid}/visual-qa` | POST | 可选VLM compare |
| `/api/batch/create` | POST CSV | 批量创建项目 |
| `/api/batch/export` | POST | 批量输出ZIP |
| `/api/health` | GET | Python/Node/LO/DeepSeek配置诊断 |

## 可编辑性与回退规则

- **可编辑模式**：使用clean作为背景+原生文字/形状+独立位图。没有clean则使用纯色背景，**不偷偷使用设计图**。
- **栅格模式**：整页design作为唯一图片，不能声称可编辑。
- 优先保证内容正确，后保证视觉相似。若无法同时满足，则通过界面展示缺少clean/文字冲突，而不是自动静默降级。
- QA分`error`（越界、丢失素材、导出失败）与`warning`（缺clean、可能溢出/重叠/低对比度）。人工审核仍然必要。

## 下一版本建议接口（尚未实现）

1. **智能分层**：输出 `segmentation_manifest.json`，包含文字遮罩、复杂艺术区域、独立背景图片hash，接入人工确认流程。
2. **复杂图表**：`chart`及`table`对象转换为 PowerPoint 的 chart/table 原生对象，并强制对照原始数字。
3. **批量调度**：每个 page task 有 `pending/running/review_needed/done/failed` 状态，基于Redis+Celery并发，错误可重试而不重做成功页。
4. **生产安全**：用户授权、队列隔离、API密钥保管、任务审计、数据清理和请求资源上限。
5. **设计分数**：真实渲染与目标图按区域比对；区分可编辑对象检验与审美评价，避免伪造单一质量分数。
