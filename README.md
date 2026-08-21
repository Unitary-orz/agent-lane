# agent-lane

`agent-lane` 让助手调用 Codex 执行编码任务，同时把每一次 coding 会话保留下来，
方便查找、查看和继续。Human 想直接处理时，也可以在 Codex App 中打开并接管同一个
会话。

V1 首先支持 Codex coding agent。

[English](https://github.com/Unitary-orz/agent-lane/blob/main/README.en.md) ·
[架构说明](https://github.com/Unitary-orz/agent-lane/blob/main/docs/architecture.md) ·
[变更记录](https://github.com/Unitary-orz/agent-lane/blob/main/CHANGELOG.md)

> 当前版本：`1.0.0-rc.4`。Python 打包工具可能显示等价版本 `1.0.0rc4`。

## 看一段真实的使用过程

假设前面已经跑过几个 coding 任务。Human 不需要记住它们的 Codex task ID，也不
需要记得当时用了什么命令：

> **Human：**查看最近的 coding 会话。
>
> **助手：**
> 1. `login-flow` — 测试还有失败
> 2. `settings-page` — 设计审查已完成
> 3. `api-cleanup` — 实现已完成
>
> **Human：**继续 2，开始落地已经确认的设置页改造。

助手通过 agent-lane 找到第 2 个会话，把新要求发给原来的 Codex 会话。之前的对话、
决策和工作目录上下文都继续保留。

```bash
agent-lane codex session list --scope all --limit 10
agent-lane codex send \
  --thread-id "<第-2-项的-thread-id>" \
  --prompt "落地已经确认的设置页改造，并运行相关测试。"
```

这就是 agent-lane 的核心作用：助手可以发起 coding 任务，把现有会话展示给 Human，
查看其中发生了什么，并准确续接指定会话，而不是不断创建彼此割裂的新任务。

## 常见使用场景

### “开始落地新的登录流程”

助手创建一个有名字的 task，再把任务交给 Codex；正常创建路径不需要 lane ID：

```bash
agent-lane codex run \
  --title login-flow \
  --cwd /path/to/project \
  --commit-signing off \
  --prompt "落地新的登录流程，并运行相关测试。"
```

agent-lane 会生成并保存内部稳定 lane ID。显式传入的 `--title login-flow` 会把
`login-flow` 保存为 `custom_title`，同时作为新 Codex task 的初始名称。省略
`--title` 时不会保存自定义标题，lane 会跟随最近观察到的 Codex task 名称。结果用
`lane_title = custom_title ?? codex_title ?? lane_id` 与 `lane_title_source` 报告
最终标题；Human 不需要创建或记住 lane ID。

如果希望固定用户级 reasoning effort，只需配置一次 agent-lane：

```bash
agent-lane config effort set xh
agent-lane config effort status
```

`xh` 会归一化为 `xhigh`。之后的 `run`、`send` 和 `goal run` 默认使用该值；
命令显式传入的 `--effort` 始终优先。JSON 会报告 `effective_effort` 和
`effective_effort_source`。配置保存在 `~/.agent-lane/config.json`，不会修改
Codex 自身配置。

### “登录流程这个会话做了什么？”

助手可以查看会话，而不发起新一轮编码：

```bash
agent-lane codex status --thread-id "<选定的-thread-id>"
agent-lane codex session outline --thread-id "<选定的-thread-id>"
agent-lane codex session read --thread-id "<选定的-thread-id>" --include-turns
agent-lane codex closeout --thread-id "<选定的-thread-id>"
```

助手可以据此回答实际问题：

- Codex 还在执行、正在等待，还是已经结束？
- 改了什么，测试结果怎样？
- 是否还有未完成工作或需要处理的 Git 状态？

单纯查看会话不会暗中启动新的工作。

带状态的结果统一以 `execution` 作为判定对象。`execution.state` 只取
`active`、`inactive` 或 `unknown`；`execution.evidence` 分别记录 thread、
本地 runner、last turn 和 Goal 证据，`execution.conflicts` 保留冲突。活跃
thread 或存活 runner 的正向证据优先于缓存的终态字段和 Goal 生命周期状态。
`runner_status` 表示有效 turn 状态，`local_runner_status` 只表示本地
runner/缓存状态。因此 Goal 已 blocked 或 complete 时，也不能把实际仍在运行的
turn 判为 inactive。

### “继续这个会话，把测试补完”

助手把后续要求发给原来的 lane：

```bash
agent-lane codex send \
  --thread-id "<选定的-thread-id>" \
  --prompt "修复剩余测试失败，并重新运行相关测试。"
```

Codex 会沿用同一段对话和工作目录上下文。`run` 本身也采用“没有就创建、已有就恢复”
的行为。已有 lane ID 的自动化仍可传 `--lane-id`；正常交互可直接使用选定 thread。

### Task 目标与 fail-closed 选择

针对单个 task 的命令接受四种互斥目标之一：`--thread-id` 表示发现结果中的精确
session，`--target-title` 表示已绑定 task 的精确已知自定义标题或 Codex 标题，
`--current` 表示保存的工作目录与当前进程目录相同的唯一已绑定 task，`--lane-id`
则保留给兼容自动化。`run` 和 `goal set` 可以完全省略目标，此时创建新 task 并生成
内部 lane ID。

标题匹配不区分大小写但必须完整相等；`--current` 不表示“最近一个”。标题或当前
目录命中多个候选时，命令以 `CODEX_TARGET_AMBIGUOUS` 失败，并返回
`choices[].target_argv`，绝不猜测。成功结果在 `target_resolution` 中报告请求与
解析后的身份；选择完成到控制开始之间若绑定变化，则以 `CODEX_TARGET_CHANGED`
失败。

## Codex 支持

集成边界是 `codex app-server` 暴露的 JSON-RPC。`independent` 模式通过 stdio
连接；`app-sync` 通过本地 WebSocket 连接托管的共用运行时。agent-lane 不依赖
Codex 私有数据库，也不自动操作 App UI。

### Task 执行与观察

| agent-lane 子命令 | Codex 接口 | 实现方式 |
| --- | --- | --- |
| `doctor --mode independent --probe` | app-server `initialize` | 在执行 task 前验证 Codex CLI 和独立 stdio JSON-RPC 链路。 |
| `codex run` | `thread/start`、`thread/resume`、`turn/start` | 创建或恢复 lane，持久化 Codex task 绑定，再发起一个 turn。 |
| `codex send` | `thread/resume`、`turn/start` | 在 lane 已绑定的原 task 上发起后续 turn。 |
| `codex steer` | `thread/read`、`turn/steer` | 使用预期 turn ID，向一个已经确认的 App Sync 活跃 turn 补充输入。 |
| `codex status` | `thread/read` | 合并实时 task 状态、持久化 lane 状态和 runner 状态。 |
| `codex wait` | task 和 turn 状态观察 | 轮询到当前 turn 进入终态，或观察超时。 |
| `codex watch` | task 和 turn 状态观察 | 以 JSONL 快照持续输出同一套观察结果。 |
| `codex checkpoint` | `thread/read` | 等待一次后返回单个 lane 快照，供定时或有界流程使用。 |
| `codex closeout` | `thread/read` 加本地 Git 状态 | 返回 task 完成情况、最终输出、worktree 和 Git 收尾信息。 |
| `codex cleanup` | task 活跃状态和托管 worktree 元数据 | 完成安全检查后，仅删除 agent-lane 记录为自己所有且已停止使用的 worktree。 |

### Session 访问

| agent-lane 子命令 | Codex 接口 | 实现方式 |
| --- | --- | --- |
| `codex session list` | `thread/list` | 列出近期主 task 或全部 task thread；默认自动读取 live 状态并返回 compact 投影。 |
| `codex session find` | `thread/list` 搜索和本地匹配 | 搜索标题、prompt、lane 元数据、工作目录信息和 task 摘要。 |
| `codex session attach` | `thread/read` | 校验已有 task 的公开工作区证据（优先最近命令，缺失时使用 task cwd），再绑定到内部稳定 lane ID 和指定执行模式；调用方可不提供 lane ID。 |
| `codex session name get` | `thread/read` | 读取 stored 或 live Codex task 名称。 |
| `codex session name set` | `thread/name/set`，然后 `thread/read` | 可带冲突检查更新 task 名称，并精确读回确认。 |
| `codex custom-title get/set/clear` | 本地 lane alias | 读取、设置或清除 `lane_title` 使用的显式本地覆盖；不会重命名 Codex task。 |
| `codex session outline` | `thread/read` | 返回 task 身份、prompt 和历史 turn 状态的紧凑投影。 |
| `codex session read` | `thread/read` | 读取完整 task、全部 turn，或指定的一个 turn。 |

`session list --detail metadata`、`session list --detail summary` 和
`session read` 会返回机器可读的 `control` 对象；默认 compact 列表只保留其中的
`requires_attach`。尚未绑定的 task 仍然只能读取，并报告
`requires_explicit_attach: true` 与建议的
`attach_argv`。stored 观察建议 `independent`，live 观察建议 `app-sync`。只有显式
attach 后才取得控制权：

```bash
agent-lane codex session read --thread-id "<task-id>" --include-turns
agent-lane codex session attach \
  --thread-id "<task-id>"
agent-lane codex send \
  --thread-id "<task-id>" \
  --prompt "继续处理已经核实的 task。"
```

只读 session 查询默认使用 `--observe auto`；`session list` 和
`session find` 还默认使用 `--detail compact`。auto 会优先读取 App Sync 的 live
状态；若 live 不可用，仍返回持久化历史证据，但当前执行状态固定为
`state: unknown`、`stale: true` 并附明确 warning，不会把持久化 turn 状态提升为
权威终态。`outline` 中的 turn 状态也只能按该 warning 解释为历史记录。列表需要更大
的诊断投影时使用 `--detail metadata` 或 `--detail summary`。

attach 缺省使用 `independent`；确实需要 App 共用控制时再显式传
`--mode app-sync`。首次 attach 未传 lane ID 时会生成内部稳定 ID；同一 thread
重复 attach 会复用它；显式重复 attach 也可切换该 lane 的执行模式，而不会创建
第二条绑定。发现和读取都不会创建 lane 绑定。只读的 `status`、`wait`、
`checkpoint`、`closeout` 和 `goal get` 可直接检查未绑定的精确 thread；控制命令
则以 `CODEX_TARGET_ATTACH_REQUIRED` 返回分离的 `attach_argv` 与
`after_attach_argv`，后者保留完整的原控制请求；只读投影不会虚构后续 prompt。
写入绑定前，attach 会把请求的 cwd 与公共历史中最近的
`commandExecution.cwd` 比较；没有 command cwd 时，回退到公开的 `thread.cwd`。
若属于不同工作区，会以
`CODEX_ATTACH_WORKSPACE_DRIFT` 失败：首次绑定或 App-adopted binding 返回精确的
attach 重试；已有受管 lane 则要求走现有 `run` task-replacement 路径。只有两个来源
都没有 cwd 时，`workspace_preflight.status` 才为 `unavailable`。运行时工作区漂移检查
仍作为最终保护。

### Goal 与运行参数

| agent-lane 子命令或选项 | Codex 接口 | 实现方式 |
| --- | --- | --- |
| `codex goal set` | `thread/goal/set` | 创建或更新 objective、status 和可选 token budget。 |
| `codex goal run` | `thread/goal/get`、`turn/start` | 在 turn 数、运行时间等边界内持续推进 active goal。 |
| `codex goal get` | `thread/goal/get` | 读取当前 task goal。 |
| `codex goal complete` | `thread/goal/set` | 将当前 goal 标记为 complete。 |
| `codex goal clear` | `thread/goal/clear` | 从 Codex task 移除 goal。 |
| `--sandbox`、`--model`、`--profile`、`--effort`、`--add-dir`、`--config` | app-server 启动参数以及 thread、turn 参数 | 将支持的运行选择和配置传入 Codex 执行链路。 |
| `config effort set/status/clear` | agent-lane 用户配置 | 管理默认 turn effort，不改写 Codex 配置；显式 `--effort` 优先。 |
| `codex run --worktree` | Git worktree 和 `runtimeWorkspaceRoots` | 创建隔离工作目录、记录所有权，并绑定到 Codex task。 |

普通命令返回一个结构化 JSON envelope；`codex watch` 输出 JSONL。命令 timeout
只限制调用方观察 task 的时间，不会重新定义持久化 Codex task 的完成状态。
timeout 错误也会返回同一份 `execution` 证据；如果 turn 仍在运行或状态未知，
建议继续观察，而不是重复 `send`。

### App Sync

App Sync 直接提供以下 Codex App 集成功能：

| 子命令或选项 | 支持的功能 | 实现方式 |
| --- | --- | --- |
| `config app-sync enable` | 在登录后启用 App 与 Agent 共用 task。 | 安装并载入用户级托管运行时，向新打开的 App 进程提供登录环境。 |
| `config app-sync status` | 报告持久化 App Sync 就绪状态。 | 检查托管运行时、socket、兼容 Codex CLI 和登录配置。 |
| `doctor --mode app-sync --probe` | 验证端到端共用控制。 | 打开本地 WebSocket，并完成 JSON-RPC `initialize` 探测。 |
| `codex run --mode app-sync` | 创建或恢复 Agent 与 Codex App 都能访问的 task。 | 使用共用运行时 transport，并将 `app-sync` 持久化为 lane 的执行模式。 |
| `codex session list`、`codex session find` | App Sync 可用时列出或搜索 App 当前可见的 task。 | 默认使用 `--observe auto`，优先查询共用控制面；持久化 fallback 会标记为非权威。 |
| `codex session name get --observe live`、`codex session outline --observe live`、`codex session read --observe live` | 读取 App 当前可见的 task 元数据、消息和 turn 状态。 | 通过共用控制面查询 `thread/read`。 |
| `codex session attach --mode app-sync` | 将 App 创建的 Codex task 纳入 lane 管理。 | 校验 task，获取 task/lane 锁，再保存 lane 绑定。 |
| `codex steer` | 向当前共用 turn 补充输入。 | 仅在识别到唯一且明确的 active turn 后发送 `turn/steer`。 |
| `config app-sync disable` | 停止未来登录时自动激活 App Sync。 | 移除未来登录激活，但不终止可能仍有客户端连接的运行时。 |

App Sync 共用 task、消息和实时 turn，不控制 Codex App 显示哪个页面，也不允许双方
安全地同时发起相互冲突的 turn。该能力可选，仅支持 macOS，并使用可能随 Codex
版本变化的实验能力。

### Commit 签名注入（Beta，显式启用）

| 子命令或选项 | 支持的功能 | 实现方式 |
| --- | --- | --- |
| `signing init --generate` | 创建托管签名身份。 | 生成专用 Ed25519 key，并在 `~/.agent-lane/signing` 下启动隔离 SSH agent。 |
| `signing status` | 报告 key 和 agent 状态。 | 返回公钥路径、fingerprint、agent 状态以及 key 是否已载入。 |
| `signing test` | 在 Codex task 使用该身份前验证签名。 | 创建临时 Git 仓库并执行本地签名 commit smoke test。 |
| `signing stop` | 停止托管 SSH agent。 | 停止 agent 并移除 socket 和环境记录，不删除 key。 |
| `--commit-signing agent` | 为 `run`、`send`、`goal set` 或 `goal run` 启用 Beta 托管签名。 | 提供 SSH agent socket、公钥路径和临时 Git config，再探测 Codex task 的实际 shell 环境。 |
| `--commit-signing off` | lane 不使用托管签名。 | 不注入任何 agent-lane 签名环境。 |

lane alias 不保存私钥材料，也不会重写仓库或全局 Git config。不兼容的已有签名设置，
不会在缺少 `--allow-signing-replacement` 时被替换。

该能力目前为 Beta，必须显式启用。新 lane 以及没有保存签名模式的 lane 默认使用
`off`；已有 lane 继续沿用已保存的模式。选择 `agent` 后，如果签名身份或 Codex task
的实际 shell 环境无法验证，命令会失败关闭。向已经载入的 App Sync task 应用签名时，
可能需要创建替代 task，并显式提供 `--allow-signing-replacement`。

## 安装

使用条件：

- macOS
- Python 3.11 或更高版本
- 已安装并完成认证的 `codex` CLI

### 安装 CLI

```bash
git clone https://github.com/Unitary-orz/agent-lane.git
cd agent-lane
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install .
agent-lane --version
agent-lane --help
agent-lane doctor --mode independent --probe
```

最后一个命令会在创建首个 lane 前，通过已认证的 Codex app-server 验证默认
`independent` 执行链路。

### 安装助手 Skill（可选）

CLI 与助手 Skill 需要分别安装。若助手运行时支持仓库附带的 Skill 布局，请在同一份
源码检出中执行：

```bash
./integrations/hermes/install-skill
```

安装器会创建指向当前源码目录的符号链接，因此源码目录需要保留在原位置。Python
wheel 只安装 CLI，不会自动注册可选 Skill。目标目录、覆盖方式和验证步骤见
[助手集成说明](https://github.com/Unitary-orz/agent-lane/blob/main/integrations/hermes/README.md)。

使用 `agent-lane <surface> --help` 查看所有参数。完整输出契约和运行设计见
[架构说明](https://github.com/Unitary-orz/agent-lane/blob/main/docs/architecture.md)。

## 开发

```bash
python3.11 -m pip install -e ".[dev]"
python3.11 -m ruff check src tests
python3.11 -m pytest
python3.11 -m build
```

贡献约定见
[CONTRIBUTING.md](https://github.com/Unitary-orz/agent-lane/blob/main/CONTRIBUTING.md)，
私密漏洞报告方式见
[SECURITY.md](https://github.com/Unitary-orz/agent-lane/blob/main/SECURITY.md)。项目使用
[MIT License](https://github.com/Unitary-orz/agent-lane/blob/main/LICENSE)。
