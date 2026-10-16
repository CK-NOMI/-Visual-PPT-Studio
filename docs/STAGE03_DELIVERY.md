# Stage 03 MVP 交付记录 · 2026-10-13

本轮只改本地代码和隔离副本，没有修改 GitHub。原项目 `workspace/6d57a8e7389d` 仍为 r402，`project.json` SHA-256 为 `f97058b50db7722414dda5e3eb1e4119effe659ab5533a51eb6838757a9c8ca7`。隔离副本和追踪文件位于 `workspace/stage03-delivery-20261013/`。

## 可运行版本

- 本地网页：`http://127.0.0.1:8766/`，服务只监听本机；默认交付项目为隔离副本 `a03c01a03c01`，r405。
- 两栏对比台展示当前页 Design 和当前有效 LibreOffice PPTX 预览；问题按严重程度排序，低置信度模型备注不占主列表；诊断、哈希和兼容性证据默认折叠。
- 主按钮为“智能检查与修复”和“导出 PPTX”。有效导出后出现“下载当前 PPTX”链接；预览及下载均绑定 revision、artifact SHA-256。
- 03 智能修复只检查当前页；DeepSeek 批量提出结构化意图，服务端与本地证据白名单绑定后，才交给现有确定性引擎执行。没有直接应用模型坐标或内容改写。

## 真实 PPTX 结果

| 隔离副本 | 版本 | 实际改动 | 校验 |
| --- | --- | --- | --- |
| `a03b01a03b01` | r402→r404 | S001 新增原稿英文主标题和模块编号 1、2；3、4 保持；最终对象数 63 | Chromium、LibreOffice、PPTX 原生文本和保存布局通过 |
| `a03c01a03c01` | r404→r405 | S002 两项来源字重修复保留；3 项未改善动作回滚 | 目标局部 MAE 49.325→32.429、40.241→34.831；浏览器和 PPTX 通过 |

当前工作台选用第二个副本，最新文件为 `presentation_editable_r405_c71b30f00f79.pptx`，SHA-256：`c71b30f00f79e36d33cd1c30d4bf1b7a60fba2d823f00f1a78753cc9e6a19463`。独立文件 SHA 与报告相符；原生文本对象和保存布局契约通过，LibreOffice 预览为两页。

S001 强制可见变化的 r402→r404 对照：英文标题新增，圆形内 1、2 恢复；区域 MAE 分别从 48.819→41.971、36.455→27.968、65.559→47.044。S001 没有确认的高优先级待修复项，但对象完整性检查不等于整页设计内容完整认证。

S002 DeepSeek 提出了一项缺失正文候选，自动验收没有保留它；安全门仍判定 56/60 个 Design 文字区域可见，4 项待复核。T/1 歧义和内容差异不会被自动猜改。本轮不能宣称 S002 内容完整，也不能宣称 Office/WPS 与 LibreOffice 完全一致。

前后图：

- S001 原 r402：`data/a03b01a03b01/build/editable_r402_0b3d21e87caa_S001_preview.png`
- S001 修复后的 r405：`data/a03c01a03c01/build/editable_r405_c71b30f00f79_S001_preview.png`
- S002 两个已保留样式动作：`data/a03c01a03c01/build/repairs/bab2bac17be64cadae2e800a93b9defe/5e857549fdf8a8a25c59_before_after.png`、`78a8740c6f979a82d546_before_after.png`
- 各阶段 HTTP、revision、文件哈希、预览路径：`trace.json`；旧版端到端记录：`report.html`。

## 性能与回归

使用同一隔离项目观察到：Clean OCR 冷缓存 0.818 秒、命中 0.002 秒；图像对照冷缓存 12.698 秒、命中 0.009 秒（缓存命中后得分相同）；智能检查 S001 冷请求 11.38 秒、命中 2.22 秒，S002 冷请求 12.18 秒、命中 3.02 秒，命中请求不再调用 DeepSeek。缓存键绑定 revision、Design/Clean 与预览哈希、布局和字体、QA/渲染代码及引擎标识。

回归结果：`PYTHONPATH="$PWD" .venv/bin/python3 -m pytest -q` 为 **135 passed**；`node --check web/app.js`、Python 编译和 `git diff --check` 通过。最新前端在真实浏览器中检查了 S001/S002 切换、r405 预览、下载链接及默认折叠状态。
