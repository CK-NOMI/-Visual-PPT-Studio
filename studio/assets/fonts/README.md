# Packaged font families

The app bundles these variable-font families so its LibreOffice renderer and DeepSeek calibration agent can use a known, repeatable font set:

- `Noto Sans` — Latin sans serif
- `Noto Serif` — Latin serif
- `Noto Sans SC` — Simplified Chinese sans serif
- `Noto Serif SC` — Simplified Chinese serif; use as an open, redistributable Songti-style option

The matching `OFL-*.txt` files contain each family's SIL Open Font License 1.1 text. The fonts are from the official Google Fonts repository; retain the license files when packaging or redistributing this project. System fonts such as SimSun, Microsoft YaHei, and PingFang are only offered to the agent when detected on the current host; they are not copied into this app because they are not bundled under the project's redistributable font license.

The exported PPTX names the selected family but does not embed font binaries. To get identical typography on a separate computer, install the selected font there or render through the app's bundled font directory.
