# 前端改版验证记录

日期：2026-10-12

## 浏览器检查

- 使用 Codex 应用内 Chromium 打开本机 `http://127.0.0.1:8765/`，检查了 8 个导航页：规划、上传与重建、检查与导出、视觉规范、对象编辑器、OCR 修复、批量生产、调试中心。
- 视口尺寸：1586×992、1440×900、1024×900、390×900 CSS px。每个尺寸都逐一切换 8 个页面，并核对页面选择状态、共享导航、侧栏宽度和文档横向溢出。
- 桌面侧栏：1586px / 1440px 为 264px；1024px 为 236px。桌面项目栏：64px。390px 下转换为横向导航，项目切换入口仍可见。
- 修复了 QA 双图对照、提示词工具栏和 OCR 校对表在 390px 下导致的页面横向溢出；修复前发现的问题已复测为无溢出。
- 新建项目弹窗通过打开、空表单必填拦截和取消关闭检查；未创建测试项目，也未写入真实 API Key。
- 项目列表通过现有 `/api/projects` 成功读取，浏览器控制台没有 error 级错误。
- JS 语法检查：`node --check` 通过（应用、AppShell、Icon 与 API Service）。`git diff --check` 通过。

## 仓库测试

运行：`PYTHONPATH=. ./.venv/bin/pytest -q`

- 结果：77 passed，2 failed，7 warnings。
- `tests/test_api.py::test_calibration_agent_uses_current_render_and_only_suggestions`：测试构造的 `report_editable.json` 没有 PPTX 文件名，当前服务拒绝它并返回“导出报告中的PPTX文件名无效，请重新导出”。
- `tests/test_v03_regression.py::test_ocr_candidate_not_auto_selected`：OCR 测试返回 5 个重复区域，断言只预期 1 个区域。

两个失败都位于本次前端改版之前已有的后端/重建代码改动；本次没有修改后端，也没有尝试放宽这些断言。

## 截图

在应用内浏览器完成了桌面 QA 页和手机上传页的视觉复核。当前浏览器截图接口仅返回会话内预览，没有提供将 PNG 写入工作区的导出路径；本次未向项目新增人工截图文件。
