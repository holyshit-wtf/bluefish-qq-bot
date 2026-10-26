# 蓝色大肥鱼 QQ 群养成机器人

BlueFish is a group-scoped QQ companion bot built with NoneBot2, OneBot V11,
NapCat, DeepSeek and SQLite. It supports isolated group memory, persona-driven
chat, commands, usage limits and scheduled maintenance.

本项目是一个基于 NoneBot2、OneBot V11、NapCat、DeepSeek 和 SQLite 的 QQ
群养成机器人。它不包含 Web 管理页，也不会自动登录 QQ。

## 功能

- 按 QQ 群隔离的上下文记忆，并使用 SQLite FTS5 检索较早的相关消息。
- DeepSeek 聊天与官方联网搜索，适合新闻、攻略、行情、价格和天气等信息型问题。
- 群管理员可配置触发概率、回复冷却、人格、禁忌词和养成规则。
- 全局发送限流、安全过滤、用量统计、单实例锁和启动自检。
- 每日提炼、过期数据清理和数据库备份任务。
- 模拟 NapCat 与 DeepSeek 的自动化闭环测试，不连接真实 QQ 或消耗真实 API。

## 环境要求

- Windows 10/11 和 PowerShell。
- Python 3.12；推荐使用 Anaconda。
- 一个已获授权使用、用于测试的 QQ 小号。
- 已登录该 QQ 的 [NapCat](https://napneko.github.io/)，并开放 OneBot V11
  正向 WebSocket。
- 可用的 DeepSeek API Key。

## 快速开始

```powershell
git clone https://github.com/<你的 GitHub 用户名>/bluefish-qq-bot.git
Set-Location bluefish-qq-bot

.\scripts\Create-Environment.ps1
Copy-Item .env.example .env
```

编辑 `.env`，至少填写：

```dotenv
DEEPSEEK_API_KEY=你的 DeepSeek API Key
OWNER_QQ=你的测试 QQ 号
NAPCAT_WS_URL=ws://127.0.0.1:3001
```

若 NapCat 启用了 Token，同时填写 `NAPCAT_ACCESS_TOKEN`。随后启动：

```powershell
.\scripts\Start-Bot.ps1
```

启动脚本会检查 Python 3.12、依赖、配置、数据库、FTS5 和单实例锁，并通过
OneBot WebSocket 握手确认 NapCat 与 QQ 登录状态。

停止机器人：

```powershell
.\scripts\Stop-Bot.ps1
.\scripts\HealthCheck.ps1
```

## 首次使用

机器人启动后，先由主人私聊启用测试群：

```text
/鱼 群 启用 <测试群号>
/鱼 群 列表
```

然后在测试群发送 `/鱼 帮助`。主人管理命令只允许私聊执行，群管理员可在群内
调整概率、冷却、必答、养成、人格和禁忌词。

## 完整命令

所有机器人命令都以 `/鱼` 开头。群内发送的玩家命令只影响当前群；私聊中的玩家
命令使用最近绑定群。私聊前应先在已启用群中发言，再发送 `/鱼 绑定` 查看绑定
结果。未启用群中的消息和命令不会被处理。

### 玩家命令

| 命令 | 作用 |
| --- | --- |
| `/鱼 帮助` | 列出玩家命令并显示当前群人格 |
| `/鱼 状态` | 查看好感、成长等级、经验、饥饿、心情和 Token |
| `/鱼 画像` | 查看机器人在当前群形成的个人画像摘要 |
| `/鱼 签到` | 每日首次签到获得 3 枚 Token，余额上限 999 |
| `/鱼 投喂 白饭` | 免费投喂；增加群体饥饿、本人好感和成长经验，冷却 30 分钟 |
| `/鱼 投喂 Token` | 消耗 5 枚 Token；提高群体饥饿、本人好感和成长经验 |
| `/鱼 排行` | 查看本群好感前 10 名 |
| `/鱼 绑定` | 查看当前私聊绑定的群 |

饥饿达到 100 时会拒绝投喂；Token 投喂被拒绝时不会扣除 Token。

### 群管理员命令

群管理员命令必须在目标群内发送，发送者需要是 QQ 群主或管理员。这些设置只影响
当前群。

| 命令 | 允许值或作用 |
| --- | --- |
| `/鱼 配置 概率 <数值>` | 调整普通消息的随机插话概率，范围 0 至 100 |
| `/鱼 配置 冷却 <秒数>` | 调整随机插话最短间隔，范围 60 至 86400 秒 |
| `/鱼 配置 必答 开` | 恢复对 @ 和回复的必答 |
| `/鱼 配置 必答 关` | 关闭本群必答路径 |
| `/鱼 配置 养成 开` | 启用签到、投喂、状态、画像和排行 |
| `/鱼 配置 养成 关` | 关闭养成，保留基础聊天和人格 |
| `/鱼 配置 人格 标准贪吃鲸娘` | 切换为标准贪吃鲸娘人格 |
| `/鱼 配置 人格 活力鲸娘` | 切换为更主动、短句的活力鲸娘人格 |
| `/鱼 配置 人格 安静鲸娘` | 切换为更克制、少主动插话的安静鲸娘人格 |
| `/鱼 禁忌 添加 <词语>` | 添加 1 至 32 字禁忌词，每群最多 100 条 |
| `/鱼 禁忌 删除 <词语>` | 删除本群禁忌词 |
| `/鱼 禁忌 列表` | 查看本群全部禁忌词 |

### 主人命令

主人命令只能由 `.env` 中 `OWNER_QQ` 对应的账号私聊机器人。`OWNER_QQ` 应填写
真人主账号，不能填写机器人 QQ 号；在群内发送主人命令会被拒绝。

| 命令 | 作用 |
| --- | --- |
| `/鱼 群 启用 <群号>` | 将目标群加入白名单并启用机器人 |
| `/鱼 群 停用 <群号>` | 停用目标群但保留已有数据 |
| `/鱼 群 列表` | 查看全部群及启用状态 |
| `/鱼 全局 暂停` | 暂停所有群的自动回复和普通用户私聊 |
| `/鱼 全局 恢复` | 恢复自动回复 |
| `/鱼 用户 查看 <群号> <QQ号>` | 查看指定用户在指定群的养成和画像 |
| `/鱼 用户 好感 <群号> <QQ号> <增减值>` | 调整好感，最终值限制在 0 至 100 |
| `/鱼 用户 删除 <群号> <QQ号>` | 删除指定用户在该群的相关数据 |
| `/鱼 系统 状态` | 查看运行时长、连接状态、发送计数和本月成本估算 |
| `/鱼 审计 <页码>` | 每页 20 条查看最近 30 天审计摘要 |

首次接入群时可参考以下配置：

```text
/鱼 群 启用 <群号>
/鱼 群 列表
/鱼 帮助
/鱼 配置 概率 20
/鱼 配置 冷却 300
/鱼 配置 必答 开
/鱼 配置 养成 开
```

## 配置

完整默认值见 [`.env.example`](.env.example)。常用配置包括：

| 配置 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek 聊天与搜索 API Key |
| `OWNER_QQ` | 允许执行主人命令的 QQ 号 |
| `NAPCAT_WS_URL` | OneBot V11 正向 WebSocket 地址 |
| `NAPCAT_ACCESS_TOKEN` | NapCat 访问令牌，未启用时留空 |
| `GROUP_HISTORY_RETENTION` | 每个群保留的历史消息数 |
| `GROUP_CONTEXT_MESSAGES` | 每次请求注入的最近消息数 |
| `GROUP_CONTEXT_FTS_LIMIT` | FTS 检索补充消息数 |
| `SEND_LIMIT_PER_MINUTE` | 全局每分钟发送上限 |
| `SEND_LIMIT_PER_HOUR` | 全局每小时发送上限 |
| `SEND_LIMIT_PER_DAY` | 全局每天发送上限 |

运行日志位于 `logs/bluefish.log`，数据库位于 `data/bluefish.db`，每日备份位于
`data/backups`。日志按天轮转并保留 30 天；数据库备份保留最近 7 份。

## 测试

```powershell
conda run --no-capture-output -n bluefish-bot python -m pytest -q
```

测试使用模拟 NapCat 与模拟 DeepSeek HTTP 接口，不会连接真实 QQ。最终联调仍
应使用专用小号和测试群。

## 安全与隐私

- 不要提交 `.env`、SQLite 数据库、日志、备份或包含这些内容的部署包。
- 仓库的 `.gitignore` 已排除本地配置和运行数据，但它不能替代提交前检查。
- 不要把 API Key、NapCat Token、QQ 号或聊天记录粘贴到 Issue、日志分享和截图中。
- 如果令牌可能已经公开，应立即在对应平台撤销并重新生成，不能只删除 Git 文件。
- 使用机器人处理群消息前，应确认你有权处理这些消息，并遵守适用法律和平台规则。

## 腾讯风控与账号风险

自动化方式接入 QQ 可能触发腾讯风控，造成功能限制、临时封禁或账号封禁。请使用
专用测试账号，遵守 QQ 平台规则并合理设置发送频率。使用者应自行评估并承担账号
限制、封禁、数据丢失、API 费用及其他相关风险；项目作者及贡献者不对由此产生的
损失负责。

## 项目结构

| 目录 | 用途 |
| --- | --- |
| `bot.py` | NoneBot 应用入口 |
| `plugins/bluefish` | OneBot 事件与发送适配 |
| `services` | 触发、人格、搜索、记忆、命令和主业务链路 |
| `storage` | SQLite 架构、迁移和仓储 |
| `jobs` | 每日提炼、清理和备份 |
| `scripts` | 环境、启动、停止、健康检查、部署与迁移 |
| `tests` | 自动化测试 |

## 参与贡献

欢迎通过 Issue 报告问题和提出建议，也欢迎提交 Pull Request。提交代码前请确保
相关测试通过，并移除所有本地密钥、账号信息和运行数据。

## 许可证

本项目采用 [MIT License](LICENSE)。
