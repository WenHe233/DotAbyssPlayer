# DotAbyssPlayer

`DotAbyssPlayer` collects the catalog download flow, bundle download flow, bundle-to-player extraction tools, and the web ADV player source in one repo-shaped folder.

This folder intentionally does not include:

- Downloaded catalogs or bundles
- Extracted player data
- Third-party binary tools such as `vgmstream-cli.exe`
- Local absolute paths or machine-specific build output

## Layout

- `src/DotAbyssClient`: .NET 8 downloader for maintenance lookup, catalog fetch, catalog parse, manifest generation, and bundle download
- `src/AdvPlayer`: static web ADV player source (`src/client.js` = desktop client shell UI)
- `tools`: Python extraction and post-processing tools (`pipeline.py` = streaming download→extract→clean orchestration)
- `scripts`: convenience scripts + `serve_advplayer.py` (desktop backend: static + translation/LLM proxy + setup/update/repair API)
- `desktop`: Tauri (WebView2) desktop shell
- `build`: packaging scripts (dotnet publish + PyInstaller + tauri build)
- `docs`: workflow notes

## 桌面客户端（一键下载 / 解密 / 更新）

面向一般用户的一体化客户端：双击即用，内置「一键下载 → 本地解密提取 → 播放」，支持增量更新，无需手动装工具链或跑命令。定位为**自用 / 小圈子**分发（Windows x64）。

**架构**：Tauri/WebView2 壳（`desktop/`）启动时拉起冻结的 Python 后端 sidecar（`scripts/serve_advplayer.py`），后端负责静态服务 + 编排。首次运行时：C# 下载器 `--dry-run` 完成加密 catalog 握手并产出 `download_manifest.tsv`（不下载任何 bundle），随后 `tools/pipeline.py` **流式**逐篇「下载 → `extract_story` → vgmstream 解码 → wav→ogg → 立即删除 bundle/wav/acb」，峰值磁盘 ~5–6 GB、终态 ~4 GB（旧批处理峰值 ~30 GB）。翻译不打包，运行时从 `github.com/s88037zz/dotabyss-translation` 拉取；缺口回退日文原文「未译」，高级用户可在「译」面板填自己的大模型 API key 开 AI 补全。

**数据目录**（可配置）：`$DOTABYSS_DATA_DIR` > 便携（同目录有 `portable.txt` → `./data-store`）> 默认 `%LOCALAPPDATA%/DotAbyssPlayer/data`。

**开发运行**（不打包，用源码 + venv）：

```powershell
# 后端（会拉起 .NET 下载器 / venv 内跑提取）
.venv/Scripts/python.exe scripts/serve_advplayer.py --port 8777 --data-dir <数据目录>
# 另开壳（会自动 spawn 上面的后端；或直接浏览器开 http://127.0.0.1:8777）
cd desktop/src-tauri && cargo tauri dev
```

**打包成安装包 / 便携版**：

```powershell
pwsh -File build/build.ps1            # NSIS 安装包
pwsh -File build/build.ps1 -Portable  # 追加 build/DotAbyssPlayer-portable.zip
```

后端编排 API：`GET /api/state|progress|update/check`、`POST /api/setup|update/apply|repair|llm-config`。前端 QoL：首运向导、下载/更新进度、更新横幅、最近 / 收藏 / 续播、一键修复单篇。

## Requirements

- .NET 8 SDK
- Python 3.10+
- `pip install -r requirements.txt`
- 音频（语音/SE）提取需要 `vgmstream-cli.exe`，放在 `tools/bin/vgmstream/` 或 `PATH`（从 https://vgmstream.org/ 下载 win64 版）
- **Windows 长路径（重要）**：全量提取前请启用长路径支持，否则深层 sound bundle 路径超过 260 字符会导致语音提取失败（`audioCueCount=0`）。管理员 PowerShell 执行后重开终端：

  ```powershell
  Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -Value 1 -Type DWord
  ```

## Quick start

For the full Android DMM R18 workflow, run the one-shot script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_full_r18.ps1
```

It creates `.venv`, installs pinned Python dependencies, builds `DotAbyssClient`, downloads the full bundle set, extracts all r18 novel stories, extracts shared assets, converts audio to OGG, and verifies Live2D motion files.

For a lightweight connectivity test without downloading bundle payloads:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_full_r18.ps1 -DryRun
```

Manual steps are still available.

1. Download the remote catalog and bundle set:

```powershell
dotnet run --project src/DotAbyssClient -- download --profile android-dmm-r18 -o workspace/bundles/android-dmm-r18 --write-catalog-json
```

2. Extract story bundles into the player data folder:

```powershell
.\.venv\Scripts\python.exe tools/adv_extract.py --scan-all --bundle-root workspace/bundles/android-dmm-r18 --output src/AdvPlayer/data_r18_all
```

3. Start the player:

```powershell
python scripts/serve_advplayer.py
```

Then open `http://127.0.0.1:8777/`.

## 翻译 / Translation

播放器已集成 [dotabyss-translation](https://github.com/s88037zz/dotabyss-translation) 的官方繁体（zh_Hant）译文，并支持用 OpenAI 兼容 LLM 端点补翻未收录的句子。

**前置条件**

- 将 `dotabyss-translation` 仓库 clone 到本仓库根目录（工作树内）。服务器通过 `/translations/` 路由映射读取，无需复制文件。
- 前端 vendor 库放在 `src/AdvPlayer/vendor/`：`pixi.min.js`（PixiJS 6）、`cubism4.min.js`（pixi-live2d-display）、`live2dcubismcore.min.js`（Live2D Cubism Core）、`opencc.min.js`（opencc-js UMD，简繁转换）。

**工作方式**

- 提取器 `adv_extract.py` 会保留 story 前缀（`evs_/hmn_/hmr_/mas_/men_`），与翻译仓库的带前缀 id 对齐（`hmn_`/`men_` 数字 id 会碰撞，必须靠前缀区分）。
- 进入每个 story 时，播放器按 id 加载 `translations/novels/<id>/zh_Hant.json`：命中即用官方译文；未命中的句子在后台批量发给本地 LLM 代理（结合全脚本上下文与角色名字典翻译），结果落盘缓存到 `src/AdvPlayer/data_r18_all/llm_cache/<id>.json`，二次进入直接读缓存。
- 右下角「译」按钮打开设置面板：主语言（简体/繁体，opencc 实时转换）、显示布局（仅中文 / 中日双语 / 仅日文）、角色名翻译开关、AI 译文标记 ⚡。设置存 localStorage，可导出/导入 `config.json`。

**启用 LLM 补翻（可选）**

复制模板并填入你的 OpenAI 兼容端点凭据：

```powershell
Copy-Item config/llm.example.json config/llm.json
# 编辑 config/llm.json，填写 base_url / api_key / model（默认示例为 DeepSeek）
```

未配置 `config/llm.json` 时，未收录的句子回退显示日文原文并标注「未译」，其余翻译功能照常工作。

## Legacy Workflow

The older helper remains available, but the full deployment path above is preferred:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_r18_workflow.ps1
```

More detail lives in [docs/workflow.md](docs/workflow.md).
