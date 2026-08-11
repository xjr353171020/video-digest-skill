# 网页视频提取与总结现成方案调研

> 调研日期：2026-08-11（美国太平洋时间以外的本机日期为 2026-08-11）  
> 范围：Codex app / Codex CLI 可用的 Skill、Plugin、MCP、CLI，以及能处理 YouTube、哔哩哔哩（B站）网页视频的字幕、转写、视觉信息和摘要路径。  
> 原则：优先读取官方文档、官方仓库、候选项目源码和发布信息；没有安装任何第三方 Skill，也没有调用候选项目的真实账号或 Cookie。

## 结论先行

1. **YouTube 现在已有官方、最省事的路径，但它不是 Skill：** ChatGPT/Codex 的官方 Chrome 集成可读取“有字幕的 YouTube 视频”的带时间戳 transcript，并据此回答问题、总结和跳转时间点。官方没有承诺同等能力覆盖 B 站。
2. **最像“真正视频理解 Skill”的社区方案是 [`bradautomates/claude-video`](https://github.com/bradautomates/claude-video)：** 它有 `.codex-plugin`、标准 `SKILL.md` 和可执行脚本；`yt-dlp` 抓字幕，缺字幕时把音频送到 Groq/OpenAI Whisper，并用 `ffmpeg` 抽帧让 Agent 读取视觉信息。它对 Codex 可安装，但当前脚本把字幕语言请求写成 `en.*`，所以中文 YouTube/B站视频通常会依赖 Whisper；不读取登录 Cookie，受限视频会失败。
3. **B站最完整的现成基础设施是 [`XZXZZX-Ai/bilibili-mcp`](https://github.com/XZXZZX-Ai/bilibili-mcp)：** 它不是 Skill，而是可在 Codex app/CLI 注册的本地 MCP。它优先取 B站原生字幕，没字幕时可在用户显式开启 `fallback_to_asr` 后用本地 `faster-whisper`，并返回可定位的时间戳；需要本地配置 B站登录凭证。摘要由 Codex 根据 MCP 返回内容完成。
4. **最接近“一句话输入、YouTube+B站都能转写并摘要”的单一 Skill 是 [`imlewc/video-to-subtitle-summary-skill`](https://github.com/imlewc/video-to-subtitle-summary-skill)：** 支持多平台、YouTube 字幕优先、本地 `faster-whisper` 回退和 AI 总结；但 README 的运行流程偏 Bash/`jq`/`/tmp`，B站默认推荐第三方解析服务，原生 Windows PowerShell 不是开箱即用。
5. **如果只想低成本做 YouTube 字幕提炼，** [`wjgoarxiv/youtube-digest-skill`](https://github.com/wjgoarxiv/youtube-digest-skill) 的 Triage（“值不值得看”）、TL;DR、要点、时间线很贴合需求；代价是仅依赖字幕，不下载音频、不做视觉分析，也没有无字幕 ASR 回退。
6. **没有找到一个由 OpenAI 官方维护、同时覆盖 YouTube+B站、并可直接完成“网页视频→高密度摘要”的专用 Skill/Plugin。** 官方旧 `openai/skills` 已标记 deprecated；其中的 `transcribe` 只处理本地音视频文件，不负责网页下载。

因此，对本机 Windows/Codex 的实际建议是：

- **YouTube 快速筛选：先用官方 Chrome 集成。** 直接要求“先给我 30 秒内的值不值得看判断、只列新增信息和关键时间点”。
- **B站：优先考虑 `bilibili-mcp`。** 它的原生字幕、登录边界和本地 ASR 回退比“把 Cookie 粘给网页服务”更可控。
- **需要画面文字、演示步骤、图表等视觉信息时：** 再考虑 `claude-video` 的 `/watch`；先把语言选择和 Windows 清理命令审查/适配好。
- **如果坚持只装一个 Skill：** `video-to-subtitle-summary-skill` 功能覆盖最广，但需要接受依赖和第三方服务配置；它不是我会在未审查前直接自动安装的方案。

## 先区分几个概念

| 类型 | 本质 | 在 Codex 中的作用 | 本次代表 |
|---|---|---|---|
| **Skill** | `SKILL.md` 指令 + 可选脚本/资源 | 告诉 Agent 何时触发、如何调用本地工具、如何组织摘要 | `claude-video`、`youtube-digest`、`video-to-subtitle-summary`、Baoyu YouTube transcript |
| **Plugin** | 可打包 Skill、MCP、UI/元数据的安装包 | 比单个 Skill 更完整；可能带 manifest 和生命周期 | `claude-video` 的 `.codex-plugin` |
| **MCP** | 常驻/按需启动的工具服务器 | 给 Codex 暴露结构化工具；摘要仍由 Codex 完成 | `bilibili-mcp`、`jkawamoto/mcp-youtube-transcript` |
| **CLI** | 独立命令行程序 | Skill 或 Agent 通过 shell 调用；本身不一定会总结 | `bilibili-cli`、`yt-dlp` |
| **网站/浏览器扩展** | 外部产品或浏览器控制层 | 不是可复制到 `~/.codex/skills` 的 Skill；通常依赖登录态或服务商 | 官方 Chrome、TranscriptAPI、TranscriptGenerate |

`npx skills add` 是社区的 Agent Skills 安装器，不是 OpenAI 官方目录的安全背书。它支持 Codex 的事实可由 [`vercel-labs/skills`](https://github.com/vercel-labs/skills) 的支持列表和安装说明确认；安装第三方 Skill 前仍应审查脚本、网络请求、Cookie/API Key 读写和清理命令。

## 候选总览

“无字幕”列描述候选本身的能力，不代表 Agent 不能把用户另外提供的音频交给别的转写工具。维护信号中的 star 是 2026-08-11 的 GitHub 仓库快照，且 monorepo 的 star 不等于该子 Skill 的使用量。

| 候选 | 类型/安装 | YouTube | B站 | 字幕与无字幕 | 视觉信息 | 登录/数据外发 | Windows 与维护判断 |
|---|---|---|---|---|---|---|---|
| [OpenAI Chrome](https://learn.chatgpt.com/docs/chrome-extension) | 官方 Apps + Chrome 扩展；不是 Skill | **官方明确支持有字幕视频** | 未承诺 | 有字幕 transcript；无官方 ASR 回退 | 未承诺逐帧视频理解 | 使用 Chrome 当前页面/登录态；官方能力 | 最省事；官方更新日志 2026-07-30 加入 YouTube 支持 |
| [bradautomates/claude-video](https://github.com/bradautomates/claude-video) | Agent Skill + `.codex-plugin`；`npx skills add ... -g` | 强 | 间接（任何 `yt-dlp` 支持的公开 URL，未专门保证 B站） | 原生字幕优先；无字幕→Groq/OpenAI Whisper | **有**：场景/关键帧，Agent 读取图片 | 不读平台 Cookie；缺字幕时音频外发到 Groq/OpenAI | MIT；v0.2.0，2026-07-01 发布；脚本成熟但是 Claude/Unix 风格，需 Windows 适配 |
| [XZXZZX-Ai/bilibili-mcp](https://github.com/XZXZZX-Ai/bilibili-mcp) | 本地 MCP；Codex Settings/MCP 或 `codex mcp add` | 否 | **强** | 原生字幕优先；显式 `fallback_to_asr`→本地 faster-whisper | 无 | B站 Cookie 在本地交互式配置；项目称不发给 CDN/ASR 子进程；配置文件不是 OS 级加密 | GPL-3.0；npm 1.11.4 / v1.11.4，2026-08-09；有测试与 Codex 指南 |
| [imlewc/video-to-subtitle-summary-skill](https://github.com/imlewc/video-to-subtitle-summary-skill) | Codex/Claude Skill；手动复制到 `~/.codex/skills` | **强** | **明确支持** | YouTube `yt-dlp` 字幕优先；B站/其他平台下载后本地 faster-whisper；可选火山引擎 | 无 | B站默认推荐 AI Douyin/TikHub；可选火山云；本地 ASR 可不外发 | MIT；157 stars；功能全但 Bash/`jq`/`/tmp`/第三方 API 使原生 PowerShell 不够顺滑 |
| [wjgoarxiv/youtube-digest-skill](https://github.com/wjgoarxiv/youtube-digest-skill) | Codex/Claude/Gemini Skill；复制目录 | **强（有字幕时）** | 否 | InnerTube/`youtube-transcript-api`；无字幕只提示用户提供 transcript | 无 | 不需要 Cookie；字幕来自 YouTube | MIT；6 stars；2026-03-15 代码 push，轻量但维护信号弱 |
| [JimLiu/baoyu-skills](https://github.com/JimLiu/baoyu-skills/tree/main/skills/baoyu-youtube-transcript) | Agent Skill；`npx skills add ... --skill baoyu-youtube-transcript` | **强（提取层）** | 否 | InnerTube 多语言/翻译/章节；被拦时回退 `yt-dlp`，可选浏览器 Cookie；无 ASR | 无 | 默认不需 API；Cookie 仅用于本地 `yt-dlp` 回退 | MIT；总仓库 24k stars、活跃；它提取 transcript，不直接给高密度摘要 |
| [public-clis/bilibili-cli](https://github.com/public-clis/bilibili-cli) | Python CLI + `SKILL.md`；`uv tool install`/`npx skills add` | 否 | **强（字幕/站内 AI 摘要）** | `--subtitle`/时间线；无字幕可导出 ASR-ready 音频，但不内置本地 ASR | 无 | 自动读 Chrome/Firefox/Edge/Brave Cookie 或 QR 登录；凭证保存本地 | Apache-2.0；v0.6.2、Alpha；代码 push 较早但测试和结构化输出完整 |
| [ZeroPointRepo/youtube-skills](https://github.com/ZeroPointRepo/youtube-skills) | Agent Skill；`npx skills add ... --skill youtube-full` | **强（API transcript）** | 否 | TranscriptAPI 远程字幕；无字幕通常 404，无本地 ASR | 无 | URL/请求/字幕发给 TranscriptAPI；100 免费 credits 后付费 | MIT；502 stars；活跃，但供应商依赖明显 |
| [jkawamoto/mcp-youtube-transcript](https://github.com/jkawamoto/mcp-youtube-transcript) | Python MCP；`uvx`/Docker/Claude bundle，可映射到 Codex MCP | **强（提取层）** | 否 | transcript、timed transcript、语言列表；长文本分页；无 ASR | 无 | 不需 Cookie；可配置 HTTP/住宅代理 | MIT；463 stars；2026-08-08 代码 push，维护活跃 |

## 详细核验

### 1. 官方 OpenAI 路径：Chrome（YouTube 首选）

官方 Chrome 文档的“Working with videos and transcripts”明确描述：对**有字幕的 YouTube 视频**，Codex 可以取得带时间戳的 transcript，用户可以要求总结/提问，回答里的时间戳可跳回视频位置。官方更新日志把这项能力标为 2026-07-30 的更新。

安装/使用边界：在 ChatGPT Desktop 的 Apps（或 Settings → Apps）中安装 Chrome 集成，按官方说明安装扩展并保持桌面端运行，然后新建 Codex chat。官方文档只保证 Google Chrome；没有对 Bilibili 提供专门字幕、音轨或视觉分析承诺。内置 Browser 只是通用网页导航/点击/读取工具，也不能当成视频总结 Skill。

官方仓库核对：

- [`openai/plugins`](https://github.com/openai/plugins)：截至本次检查，没有专门的 YouTube/Bilibili 网页视频总结插件。
- [`openai/skills`](https://github.com/openai/skills)：README 已标注 deprecated；当前维护方向是 `openai/plugins`。
- [`transcribe`](https://github.com/openai/skills/tree/main/skills/.curated/transcribe)：只接收本地音频/视频文件，调用 OpenAI 转写 API；不负责从网页下载媒体，也不直接总结 URL。

### 2. `bradautomates/claude-video`：真正的视频/视觉 Skill

这是目前最接近“贴 URL 就让 Codex 读懂视频”的现成项目：仓库有 [`skills/watch/SKILL.md`](https://github.com/bradautomates/claude-video/blob/main/skills/watch/SKILL.md)、[`skills/watch/scripts/`](https://github.com/bradautomates/claude-video/tree/main/skills/watch/scripts) 和 [`.codex-plugin/plugin.json`](https://github.com/bradautomates/claude-video/blob/main/.codex-plugin/plugin.json)。README 明确列出 Codex 和 `npx skills add bradautomates/claude-video -g` 安装方式。

管线：

1. `yt-dlp` 先尝试人工/自动字幕；有字幕时可跳过视频下载。
2. 没字幕时用 `ffmpeg` 提取音频，并发送到 Groq `whisper-large-v3` 或 OpenAI `whisper-1`。
3. `ffmpeg` 按时长/场景抽帧，Agent 读取 JPEG 后把画面和 transcript 对齐，输出摘要、时间线、引述和视觉备注。

关键限制：

- 当前 `download.py` 的字幕参数是 `--sub-langs en.*`（[第 78 行](https://github.com/bradautomates/claude-video/blob/main/skills/watch/scripts/download.py#L78)、[第 135 行](https://github.com/bradautomates/claude-video/blob/main/skills/watch/scripts/download.py#L135)）。中文 YouTube/B站字幕不会被优先拉取；如果没有英文字幕，通常会走 Whisper。
- SKILL 明确不访问平台账号/Session Cookie；YouTube 年龄限制、会员、地区限制或 B站登录态字幕可能失败。
- Whisper 是云端回退，不是本地隐私方案；应把 API Key 放在项目外的本地配置中，不要贴到聊天。
- README 给了 Windows `winget`/`pip` 提示，但 SKILL 示例大量使用 Bash 变量、`python3` 和 `rm -rf`。Windows Codex 应将其翻译成 PowerShell，并对临时目录做路径确认后再清理。

结论：**视觉信息最强、YouTube 公开视频最实用；B站/中文视频需要先修语言选择和登录边界。**

### 3. `XZXZZX-Ai/bilibili-mcp`：B站首选基础设施

它是本地 stdio MCP，不是 `SKILL.md`。项目文档提供 Codex app、Codex CLI 和手动 `config.toml` 配置：

```toml
[mcp_servers.bilibili-mcp]
command = "npx"
args = ["-y", "@xzxzzx/bilibili-mcp@latest"]
```

读取链路是“原生 B站字幕优先 → 无字幕且用户显式 `fallback_to_asr: true` 时本地 `faster-whisper`”。README 给出的边界包括：单 P 最长 2 小时、音频 128 MiB、转录超时 30 分钟；ASR 模型必须先由本地 `setup` 安装，MCP 调用本身不会偷偷下载模型。返回内容带时间区间和可跳转 B站时刻链接，适合 Codex 进一步压缩成“值不值得看/关键论点/章节”。

认证与隐私：字幕、搜索和部分内容读取通常需要 B站 Cookie。项目要求在本地隐藏提示符输入，Windows 配置路径为 `%USERPROFILE%\\.bilibili-mcp\\config.json`，并明确禁止把 Cookie 粘到聊天、MCP 配置、日志或截图中。它声称 Cookie 只发给 B站官方接口，不发给 CDN 或本地 ASR 子进程；但配置文件不是操作系统级加密，仍应保护账户目录。项目许可证为 GPL-3.0，若以后把代码嵌入闭源发布物，需要另行评估许可证边界。

结论：**如果目标是 B站高信息密度摘要，这是目前最值得先评估的社区方案；它缺的是“摘要模板 Skill”和视觉帧，不是字幕/ASR 基础能力。**

### 4. `imlewc/video-to-subtitle-summary-skill`：单 Skill 覆盖最广

README 和 `SKILL.md` 明确写了 Codex/Claude、B站、YouTube、抖音、小红书等 URL；YouTube 优先用 `yt-dlp` 直接抓人工/自动字幕，只有失败才下载音视频并走 `faster-whisper`。B站默认流程使用 AI Douyin/TikHub 获取直链，再本地 ASR；也可切换火山引擎云端字幕服务。

优点：多平台、能处理本地文件、无字幕有本地 ASR、最终摘要由当前 Agent 生成而非强制交给第三方 LLM。缺点：

- B站默认建议第三方解析代理，解析一次扣积分；这会把 URL/媒体交给外部服务并引入额度、服务条款和失效风险。
- 文档主要面向 Bash：`export`、`jq`、`/tmp`、`python3`，Windows PowerShell 需要重写变量读取、路径和依赖检查。
- 无视觉抽帧；画面里的代码、图表、字幕样式不会进入摘要。

结论：**功能覆盖最像用户想要的“网页视频→摘要”，但应先做 Windows 原生化和服务依赖审查。**

### 5. `wjgoarxiv/youtube-digest-skill`：最贴合“是否值得看”的字幕型 Skill

它的输出顺序是 TL;DR、3–7 个要点、带时间戳的核心主张、Topic Timeline、Notable Quotes，并有专门的 **Triage Mode**。README 明确给出 Codex 路径 `~/.codex/skills/youtube-digest/`；脚本默认只用 Python 标准库，通过 YouTube InnerTube 抓 caption，也可选 `youtube-transcript-api`/`yt-dlp` 补充元数据。

它是一个很好的摘要模板和提示词参考，但不是完整的视频理解器：无字幕时没有音频下载/ASR/视觉回退，年龄限制和字幕关闭的视频只能让用户提供 transcript。仓库体量小（6 stars、2026-03-15 代码 push），应把它视为轻量方案/模板，而不是高可用抓取基础设施。

### 6. `JimLiu/baoyu-skills` 的 `baoyu-youtube-transcript`：成熟的 YouTube 提取层

该 Skill 通过 InnerTube 直接抓人工/自动字幕，支持语言优先级、翻译、章节、SRT、缓存和说话人后处理；被 YouTube 拦截时回退到 `yt-dlp`，并允许用 `YOUTUBE_TRANSCRIPT_COOKIES_FROM_BROWSER` 指定浏览器 Cookie。它不下载音频、不做 Whisper、不抽视频帧，因此需要再让 Codex 对生成的 Markdown/transcript 做摘要。

它适合“先可靠得到多语言 transcript，再按不同模板提炼”的架构；不适合单独解决无字幕或强视觉依赖视频。仓库总 star 很高，但那是多技能 monorepo 的信号，不应等同于该子 Skill 的单独成熟度。

### 7. B站 CLI 与其他 MCP

#### `public-clis/bilibili-cli`

这是 Python CLI，同时附带 `SKILL.md`。核心命令：

```text
bili video BV... --subtitle
bili video BV... --subtitle-timeline
bili video BV... --ai
bili audio BV... --segment 25
```

它有 YAML/JSON 结构化输出，明确建议 Agent 总结时先拉字幕；无字幕时 `bili audio` 可输出 ASR-ready WAV，但 CLI 本身不含 Whisper 转写。认证支持本地保存凭证、Chrome/Firefox/Edge/Brave Cookie 和 QR 登录；Windows 用 `uv tool`/`pipx` 可行。它是“B站读取器 + 音频准备器”，不是完整的无字幕视频总结器。

#### `jkawamoto/mcp-youtube-transcript`

这是维护活跃的 YouTube transcript MCP，提供普通 transcript、带时间戳 transcript、视频信息和可用语言，并把超长结果分页。它不做 ASR、视觉抽帧或总结；更适合作为 Codex 的轻量字幕工具。README 给出的 `uvx` 配置可改写到 Codex MCP 配置中。YouTube 被云 IP 限制时支持 HTTP/住宅代理配置。

#### `ZeroPointRepo/youtube-skills`

它把 YouTube transcript/search/channel/playlist 包成多个 Skill，但所有 transcript 请求都走 TranscriptAPI，需 `TRANSCRIPT_API_KEY`，免费 100 credits，之后按量/订阅。没有字幕通常返回 404；无本地 ASR、无视觉、无 B站。优点是安装后不需要 `yt-dlp`/浏览器/FFmpeg，缺点是外部供应商、额度和数据外发。

## 不建议直接作为首选的候选

- [`ET06731/bilibili-summary`](https://github.com/ET06731/bilibili-summary)：是一个 B站 Skill，但 README 要用户复制 `SESSDATA` 等 Cookie，`SKILL.md` 没有无字幕 ASR，示例脚本把字幕截断到 8,000 字符；项目规模和维护信号很弱。不要把凭证贴到聊天。
- [`Allenyan07/video-to-text`](https://github.com/Allenyan07/video-to-text)：B站本地 API + Whisper 备用路径有价值，但主路径逆向 TranscriptGenerate 网站的登录、AES 加密和内部接口，要求邮箱/密码，README 也承认这不是官方 API；不适合默认交给 Agent 自动执行。
- [`Mocooa/bili-content-organizer`](https://github.com/Mocooa/bili-content-organizer)：适合“已有字幕→Obsidian 知识库”整理，不触发普通“总结一个视频”请求；无字幕时会跳过，且依赖另一个 B站 CLI。它是后处理 Skill，不是抓取/转写 Skill。
- [`ericgandrade/claude-superskills` 的 youtube-summarizer](https://github.com/ericgandrade/claude-superskills/tree/main/skills/youtube-summarizer)：只做 YouTube 字幕型摘要，README/SKILL 版本和平台描述有不一致，依赖旧式 `youtube-transcript-api` 调用方式；无 B站、无 ASR、无视觉，不如上面的候选稳妥。

## 依赖与技术风险

### `yt-dlp`

[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) 是最常见的本地抓取层，官方 README 同时列出：

- `--write-subs` / `--write-auto-subs` / `--list-subs`；
- `--cookies-from-browser`（Chrome、Edge、Firefox 等）；
- Bilibili extractor；
- Windows 路径示例。

截至 2026-08-11，仓库仍活跃，最新 release 为 `2026.07.04`。但它依赖各站点私有/易变接口，遇到反爬、登录、地区限制或站点改版时，任何上层 Skill 都可能失效。不要把某个 Skill README 中“B站返回 HTTP 412、yt-dlp 不可用”的旧说法当作永久事实；应以当前版本的实测结果为准。

### `youtube-transcript-api`

[`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api) API 简洁、支持多语言和翻译，但官方 README 明确提醒：云 IP 可能触发 `RequestBlocked`/`IpBlocked`，需要代理；年龄限制的 Cookie 认证因 YouTube API 变化目前不可靠；它还依赖 YouTube 未公开的 web-client API。因此它适合本机低频字幕抓取，不应被误认为稳定的官方 API。

### `faster-whisper`

[`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) 支持 Python 3.9+、CPU INT8 和 CUDA GPU，PyAV 自带 FFmpeg 库（不必安装系统 FFmpeg 才能解码音频）。Windows 本地 ASR 可行，但首次模型下载、磁盘/RAM/显存占用和长视频耗时仍要纳入用户体验；这也是 `bilibili-mcp` 和 `video-to-subtitle-summary` 把 ASR 设成显式回退的原因。

## 本机实测与现有初稿审计（2026-08-11）

这些结果来自当前 Windows/Codex 机器的真实运行，不是仅依据 README 推断。测试没有安装任何第三方 Skill，也没有把 Cookie、API Key 或字幕内容发送给第三方总结服务。

### 实测环境

- Python 3.12.13。
- 系统已有 `ffmpeg 7.0.1` / `ffprobe`。
- 全局原先没有 `yt-dlp`、`whisper`；调研仅在临时虚拟环境安装 `yt-dlp 2026.07.04`、`youtube-transcript-api 1.2.4` 和 `requests`。
- 测试视频：YouTube `aircAruvnKk`；B站 `BV1X7411F744?p=5`。

### YouTube：轻量字幕 Skill 已真实跑通

- `wjgoarxiv/youtube-digest-skill` 在**不安装任何第三方 Python 包**的情况下走 stdlib/InnerTube 路径成功：标题和时长正确，得到 286 个带时间段落。
- `TylonHH/codex_skill_youtube-transcripter` 在临时环境中也成功写出约 18.7 KiB 的纯文本 transcript。
- 这说明“有字幕的 YouTube → Codex 高密度摘要”已经不需要下载整段视频；官方 Chrome transcript 或轻量 InnerTube Skill 都可作为第一路径。

### B站：Chrome 页面字幕可读，CLI 登录态受数据库锁影响

- 未登录的 `yt-dlp --list-subs` 只能看到弹幕轨道，并提示字幕需要登录；但同一视频的音频直链可以解析，因此无字幕时的“只下载音频 → 本地 ASR”路径可行。
- 用户明确授权读取 Chrome 登录态后，`yt-dlp --cookies-from-browser chrome` 定位到了正确的数据库，但 Chrome 正在占用 `Default\Network\Cookies`，Windows 返回 `Permission denied`。普通文件复制和 SQLite 只读备份都因独占锁失败；没有尝试管理员权限、卷影复制或关闭 Chrome。
- 改用已连接的 Chrome 页面会话验证，同一 B站视频的播放器显示“主字幕：中文”，启用后字幕 DOM 可直接读取；在约 `00:42.8` 读取到了实际中文字幕。说明浏览器内的 B站字幕路径是可行的，只是它不像 YouTube 官方 transcript 那样直接一次性返回整段时间线。
- 临时复制过程中只成功复制过 `Local State`，Cookie 数据库没有复制成功；该临时敏感副本随后已送入回收站，未留下 Cookie 明文或导出文件。

### 当前工作区初稿：方向正确，但还不宜直接安装

审计对象：`网页ChatGPT生成-video-digest-codex-skill-v3/video-digest`。

优点：

- 字幕优先、音频转写回退、视觉按需、事实/观点分离、“值不值得看”和频道 digest 的产品方向都很贴合目标。
- 使用 `--cookies-from-browser`，比要求用户把 `SESSDATA`/完整 Cookie 粘到聊天或明文配置更安全。
- B站测试中元数据和音频源均可解析，说明通用 `yt-dlp` 基础并非不可用。

已确认的问题：

1. **YouTube 字幕语言表达式会请求过多轨道。** `--sub-langs "zh-Hans,zh-CN,zh,zh-Hant,zh-TW,en.*,en"` 中的 `en.*` 被 `yt-dlp` 扩展为大量自动翻译字幕；实测一次请求数十种语言并在第一个中文字幕下载处触发 HTTP 429。应该先列轨道，再只选一个最优字幕。
2. **字幕失败原因被吞掉。** `run(sub_cmd, check=False)` 后没有保存 stderr，报告把 429/认证/格式错误统一写成 `unavailable`，导致诊断失真。
3. **缺少可靠的 YouTube 轻量回退。** 在 `yt-dlp` 字幕失败后直接准备走 Whisper；应先接入已经实测成功的 `youtube-transcript-api` 或 stdlib/InnerTube 路径。
4. **当前 `yt-dlp` 的 YouTube JS 运行时边界没有处理。** 实测发出“没有受支持 JavaScript runtime”的警告；Skill 应显式检测/配置 Node 或 Deno，并把警告记录进 ingest report。
5. **读取顺序浪费上下文。** 本次 YouTube `metadata.json` 约 776 KiB，而 `metadata-summary.json` 仅约 500 字节；SKILL 却要求先读完整 `metadata.json`，其中大量格式/签名 URL 对摘要无用。应只读精简元数据。
6. **重复运行可能误用旧 transcript。** 工作目录由标题和 ID 固定生成，脚本不清理旧 `transcript.txt`；若一次成功后下一次抓取失败，最终仅检查文件是否存在，可能把旧结果误报为本次成功。
7. **`--frames auto` 实际与 `on` 相同。** 脚本没有自动判断视觉需求，都会下载视频流；“是否需要画面”应由上层分析后再显式触发。
8. **频道状态提交过早。** `channel_digest.py` 在详细转写/摘要之前就把发现的视频标记为 seen，后续处理失败的视频可能在下一次扫描中被跳过。
9. **Windows 本地 ASR 方案偏重。** 当前只支持 `openai-whisper` CLI；更适合本机的默认回退应评估 `faster-whisper` CPU INT8，并把模型下载和预计耗时作为显式步骤。

综合判断：**不建议丢掉这份初稿。最划算的路线是保留它的摘要模板、价值判断、事实核查和频道 digest，替换/加固证据获取层。**

## 给当前项目的落地建议

如果要继续维护当前 `video-digest-skill`，比较稳妥的产品边界是把“获取证据”和“摘要模板”拆开：

1. **YouTube evidence adapter**：优先官方 Chrome transcript；本地/CLI 场景再用 `yt-dlp` 或 Baoyu extractor；无字幕才询问是否允许云 Whisper 或本地 faster-whisper。
2. **Bilibili evidence adapter**：调用 `bilibili-mcp`/`bilibili-cli`，原生字幕优先；无字幕必须显式同意本地 ASR；登录凭证只走本地隐藏输入或现有浏览器 Cookie，不接受聊天粘贴。
3. **统一摘要模板**：先给“是否值得看”结论，再给 3–7 个高信息密度要点、关键时间点、证据/不确定性、可跳过区段；只有用户需要时才输出完整 transcript。
4. **视觉按需启用**：只有标题/字幕不足以回答问题，或用户明确关心屏幕内容时，才启用 `claude-video` 式抽帧，避免把长视频每帧都塞进上下文。
5. **验证标准**：不能只看 Skill 是否被加载；要用真实的公开视频分别验证“有字幕、无字幕、中文、长视频、需登录”五条路径，并记录字幕来源、是否外发音频、时间戳是否可定位。

## 主要一手来源

- [OpenAI Chrome extension 文档](https://learn.chatgpt.com/docs/chrome-extension)；[官方更新日志](https://learn.chatgpt.com/docs/changelog)
- [OpenAI plugins（当前维护仓库）](https://github.com/openai/plugins)；[已弃用的 openai/skills](https://github.com/openai/skills)；[`transcribe` Skill](https://github.com/openai/skills/tree/main/skills/.curated/transcribe)
- [Codex build skills 文档](https://developers.openai.com/codex/build-skills)
- [Agent Skills CLI（Vercel Labs）](https://github.com/vercel-labs/skills)；[Agent Skills 规范](https://agentskills.io)
- [`bradautomates/claude-video` README](https://github.com/bradautomates/claude-video)；[`watch` SKILL.md](https://github.com/bradautomates/claude-video/blob/main/skills/watch/SKILL.md)；[Codex plugin manifest](https://github.com/bradautomates/claude-video/blob/main/.codex-plugin/plugin.json)
- [`XZXZZX-Ai/bilibili-mcp` README](https://github.com/XZXZZX-Ai/bilibili-mcp)；[Codex/MCP 客户端配置](https://github.com/XZXZZX-Ai/bilibili-mcp/blob/master/docs/client-setup.md)；[MCP registry `server.json`](https://github.com/XZXZZX-Ai/bilibili-mcp/blob/master/server.json)
- [`imlewc/video-to-subtitle-summary-skill` README](https://github.com/imlewc/video-to-subtitle-summary-skill)；[`SKILL.md`](https://github.com/imlewc/video-to-subtitle-summary-skill/blob/main/SKILL.md)
- [`wjgoarxiv/youtube-digest-skill` README](https://github.com/wjgoarxiv/youtube-digest-skill)；[`SKILL.md`](https://github.com/wjgoarxiv/youtube-digest-skill/blob/main/SKILL.md)
- [`JimLiu/baoyu-skills` 的 YouTube transcript Skill](https://github.com/JimLiu/baoyu-skills/tree/main/skills/baoyu-youtube-transcript)
- [`public-clis/bilibili-cli` README](https://github.com/public-clis/bilibili-cli)；[`SKILL.md`](https://github.com/public-clis/bilibili-cli/blob/main/SKILL.md)
- [`jkawamoto/mcp-youtube-transcript`](https://github.com/jkawamoto/mcp-youtube-transcript)；[`ZeroPointRepo/youtube-skills`](https://github.com/ZeroPointRepo/youtube-skills)
- [`yt-dlp` README](https://github.com/yt-dlp/yt-dlp)；[`youtube-transcript-api` README](https://github.com/jdepoix/youtube-transcript-api)；[`faster-whisper` README](https://github.com/SYSTRAN/faster-whisper)
