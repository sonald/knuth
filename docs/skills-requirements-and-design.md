# Skills 内置能力需求与设计

状态：Proposed
日期：2026-06-17
依据：[CONTEXT.md](../CONTEXT.md)、[ADR-003](decisions/ADR-003-system-preamble.md)、[ADR-004](decisions/ADR-004-runtime-control-and-agent-loop-boundaries.md)、[MessageMiddleware 需求与设计](message-middleware-requirements-and-design.md)、[Agent Skills Specification](https://agentskills.io/specification)

> 注意：[ADR-010](decisions/ADR-010-middleware-redesign.md) 更新了本文关于 `SkillReminderMiddleware` / `SkillChangeNoticeMiddleware` 的目标设计和迁移计划：动态 skill catalog reminder 迁移为 turn-level、cache-aware、durable projection event；`SkillChangeNoticeMiddleware` 目标态删除。相关目标行为以 ADR-010 为准。

本文定义 Knuth 的 Skill 文件格式和内置运行时能力。这里的关键边界是：

> Skill 是 Knuth 的内置 agent 能力，不能由 host 开关关闭；CLI 只负责把 host 侧可变配置传进 Knuth，例如从哪些目录加载、目录优先级、hot reload 参数和环境变量覆盖。

换句话说，skill 的 frontmatter 解析、目录扫描、`skill` 工具、可用 skill 列表提醒、skill 正文注入、与 MessageTape / ledger 的关系，都不属于 `knuth-cli` 私有实现。`knuth-cli` 只是当前 host，它读取 `knuth.yaml` / env 并调用 runtime builder。未来 `knuth-im`、daemon 或其他 host 应复用同一套内置 skill 能力，只传入不同的 `SkillRuntimeConfig`。

## 目标

- 支持读取本文定义的 `SKILL.md` 文件，并解析 YAML frontmatter。
- 支持多 root 加载和优先级去重：高优先级 root 中的同名 skill 覆盖低优先级 root。
- 将 `skill` 作为 Knuth 内置工具暴露给模型，而不是 CLI 特供工具。
- 每次模型请求都让模型看到当前可用 skill 列表；该列表是本轮是否可调用 skill 的唯一事实来源。
- 模型调用 `skill` 工具后，Knuth 在合法的 provider message 边界注入该 skill 的正文和调用参数，让下一次模型请求能使用该 skill。
- skill 文件变更不要求重启进程才能生效；v1 必须实现 hot reload watcher，并在 safe point 上刷新 snapshot。
- 保持 Knuth 的 durable run / replay / audit 语义：被调用的 skill 内容必须能解释当时模型看到的上下文，不能只依赖未来某个文件路径的当前内容。
- 让 CLI 可配置但不拥有 skill 语义：默认目录、env 名称、hot reload 参数属于 CLI；解析、校验、工具和注入属于 Knuth 内置层。

## Skill 文件格式

一个 skill 是一个目录，目录内必须包含 `SKILL.md`。文件名大小写不敏感，扫描器应接受 `SKILL.md` / `skill.md` 等形式，但规范写法是 `SKILL.md`。

规范目录结构：

```text
skill-name/
├── SKILL.md
├── scripts/
├── references/
├── assets/
└── ...
```

只有 `SKILL.md` 必须存在。`scripts/`、`references/`、`assets/` 和其他文件都是可选资源。

`SKILL.md` 必须以 YAML frontmatter 开头，frontmatter 后面的 Markdown 正文就是 skill prompt 内容。规范结构：

```markdown
---
name: example-skill
description: Use when the user asks for an example workflow.
license: MIT
compatibility: Requires standard POSIX shell tools.
allowed-tools: shell read_file
metadata:
  owner: docs
---

# Example Skill

Describe the workflow here. The model will read this body after invoking the
`skill` tool.
```

frontmatter 字段：

| 字段 | 必填 | 类型 | 说明 |
|---|---:|---|---|
| `name` | 是 | string | skill 标识，1 到 64 字符，只允许小写字母、数字和中划线，不以中划线开头或结尾，不包含连续中划线，并且必须匹配父目录名。 |
| `description` | 是 | string | 说明 skill 能做什么以及何时使用，非空，最长 1024。 |
| `license` | 否 | string | 许可证名称或 bundled license 文件引用。空字符串按 unset 处理。 |
| `compatibility` | 否 | string | 环境要求，最长 500。空字符串按 unset 处理。 |
| `metadata` | 否 | object | 自定义元数据。key 必须是字符串；Knuth v1 不解释其中内容，只保留。 |
| `allowed-tools` | 否 | string | 实验性字段。空格分隔的预批准工具名列表。Knuth v1 只解析和保留，不强制限制工具。 |

未知 frontmatter key 不阻止加载，但应产生 validation warning。自定义信息应放在 `metadata` 中。

正文规则：

- frontmatter 结束后的内容去除首尾空白后作为 `Skill.content`。
- 正文可以引用同目录下的文件；模型看到的 skill context 会包含 skill base directory。
- v1 不要求扫描或内联相对引用文件。若 skill 正文要求读取附加文件，模型应使用普通文件工具读取。
- 引用 skill 资源时，应使用相对 skill root 的路径，例如 `references/REFERENCE.md` 或 `scripts/extract.py`。
- 推荐保持 `SKILL.md` 主体简短，把长参考材料放入 `references/`，把可执行辅助逻辑放入 `scripts/`，把模板和静态资源放入 `assets/`。
- 空正文允许加载，但调用该 skill 时只会注入 base directory 和 arguments；建议 validator 给 warning。

## 非目标

- v1 不设计完整第三方 skill plugin ABI。
- v1 不实现 skill 安装、复制、升级或 marketplace。
- v1 不实现 `allowed_tools` 的强制沙箱；只解析并保留 metadata，后续可接入 policy。
- v1 不重新引入 `ask_user` / `WAITING_USER`。如果 skill 需要澄清，应由模型正常对用户提问，或未来单独设计用户输入暂停协议。
- v1 不让 skill 目录成为可写工作区。Skill 文件是配置/能力来源，不是任务产物目录。
- v1 不把 skill 列表写入 run history；当前可用列表是 build-time projection。只有实际调用的 skill 内容需要 durable/auditable。

## 分层边界

### knuth-core

`knuth-core` 只放跨包共享的数据契约，不读取文件系统，也不决定目录来源。

建议新增或扩展：

```python
class SkillSource(StrEnum):
    PROJECT = "project"
    USER = "user"
    BUILTIN = "builtin"
    HOST = "host"


class SkillMetadata(KnuthModel):
    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] | None = None
    allowed_tools: list[str] | None = None


class SkillInfo(KnuthModel):
    metadata: SkillMetadata
    source: SkillSource
    file_path: str
```

`SkillMetadata` 必须执行与“Skill 文件格式”章节一致的校验：

- `name` 为 1 到 64 字符，只允许小写字母、数字和中划线，不以中划线开头或结尾，不包含连续中划线，并且匹配父目录名。
- `description` 非空，最长 1024。
- `compatibility` 最长 500。
- `allowed-tools` 是空格分隔字符串，解析后规范化为 `allowed_tools: list[str]` 供 Knuth 内部使用；写入 `SKILL.md` 时仍使用 spec 字段名 `allowed-tools`。
- 未知 frontmatter key 只产生 warning；自定义数据建议放在 `metadata` 下。

`SkillInfo` 不重复定义 frontmatter 字段。它表示 manager 扫描后的发现结果：`metadata` 是 `SKILL.md` frontmatter 的规范化结果，`source` 和 `file_path` 是扫描过程补充的来源事实。渲染 skill 列表时可以读取 `SkillInfo.metadata.name` / `SkillInfo.metadata.description`。

同时为 system preamble 增加 skill 来源：

```python
class SystemSectionSource(StrEnum):
    ...
    SKILL = "skill"
```

v1 不需要为 skill 增加新的 `ToolResult` / `ToolExecutionResult` 字段。`skill` 工具读取到的正文、base directory 和调用参数直接放入普通 tool result observation。工具结果已经会成为 model-visible `tool_result` message，并作为 durable tool completion 写入 ledger；这足以解释模型当时看到的 skill 内容。

### knuth-toold

`knuth-toold` 拥有 skill 加载和 skill tool provider，因为它已经是工具发现与执行层。

建议新增 `knuth_toold.skills`：

- `SkillRoot`
- `SkillSnapshot`
- `SkillManager`
- `SkillHotReloadService`
- `SkillToolProvider`
- frontmatter validator
- `render_skill_system_section_text(...)`
- `render_skills_reminder_text(...)`
- `render_skill_change_notice_text(...)`
- `render_skill_tool_observation(...)`

`SkillRoot` 是 host 传给 Knuth 的显式配置：

```python
class SkillRoot(KnuthModel):
    source: SkillSource
    path: str
```

root 列表的顺序就是优先级顺序，first wins。不要让 `knuth-toold` 自己读取 `KNUTH_*`、HOME 或其他 host 约定；这些是 host 策略，由 CLI 或其他 host 解析后传入。

`SkillManager` 负责：

- 扫描 root 下所有大小写不敏感的 `SKILL.md`。
- 遍历目录 symlink，但用 inode cycle detection 防止循环。
- 跳过 `.git`。
- 解析 frontmatter 与正文。
- 对同名 skill 按 root 优先级去重。
- 维护当前 `SkillSnapshot(version, catalog_digest, skills)`。
- 提供 `invalidate(reason)`、`refresh_if_dirty()`、`current_snapshot()`、`skill_root_candidates()`、`list_skills()`、`get_skill()`、`to_skill_infos()`。

`SkillManager` 是 skill catalog 的唯一状态源。目录 watcher、lazy scan 或 host 显式刷新只调用 `invalidate(reason)` 设置 dirty flag，不直接通知 provider 或 middleware。`refresh_if_dirty()` 只能在 safe point 执行：如果 dirty，则重新扫描、递增 `version`、更新 `catalog_digest`；如果没有变化，返回现有 snapshot。

`SkillSnapshot.catalog_digest` 只覆盖会影响 skill 列表和 notice 文本的字段。v1 至少包含排序后的 skill name、description、source、file path；如果 reminder / notice 展示 `compatibility` 或 `allowed_tools`，也应把它们纳入 `catalog_digest`。它不包含 `SKILL.md` 正文 hash，也不递归 fingerprint `references/`、`scripts/`、`assets/`。

这让 `catalog_digest` 的计算成本只随已发现 skill 数量线性增长，不随 skill 正文体积或资源文件数量增长。即使有几百个 skill，也只需要摘要列表可见元数据；真正的 skill 正文在工具调用时读取并写入当次 tool result。

正文或资源文件变化仍会触发 `SkillManager.invalidate(...)`。下一次 safe point refresh 后，`version` 可以递增；但如果 list-visible metadata 没变，`catalog_digest` 不变，`SkillChangeNoticeMiddleware` 不插入新的 notice。真正调用 `skill` 时，`SkillToolProvider` 读取刷新后的正文并写入当次 tool result。

`SkillHotReloadService` 负责监听 skill root 和 skill 目录变化。它只调用 `SkillManager.invalidate(reason)`，不解析 skill、不 reload、不写 ledger、不调用 middleware。

`SkillToolProvider`、`SkillHotReloadService` 和 runtime middleware 共享同一个 `SkillManager` 实例：

```text
SkillManager
├── SkillToolProvider
├── SkillHotReloadService
└── SkillChangeNoticeMiddleware
```

这是一条 pull-based 边界：manager 不知道 run，也不调用 middleware；middleware 不扫目录，只读取 manager 的 versioned snapshot。

`SkillToolProvider` 暴露一个工具：

```text
name = "skill"
args = {
  "skill_name": string,
  "args": string
}
```

工具执行语义：

- 执行前在 safe point 尝试 `refresh_if_dirty()`。
- 如果 skill 不存在，返回 error observation，告诉模型该 skill 不可用，应该在不使用该 skill 的情况下继续。
- 如果 skill 存在，返回 success observation：`Skill '<name>' loaded successfully.`
- tool result observation 同时包含：

```text
Base directory for this skill: <base_dir>

<skill_content>

Skill arguments: <args>
```

这个 observation 会通过普通 tool-result message 进入模型上下文。它不是用户真实输入，也不需要额外的 user-role 注入。

v1 的大小限制按完整 tool-result observation 计算，而不是只按 `SKILL.md` 正文计算。默认上限应不超过默认 tool-result redaction 阈值，避免 `skill` tool 返回 success 后，正文又在下一次模型请求前被 context redaction 截断。超过上限时直接返回 `skill_content_too_large` error observation。

### knuth-runtime

`knuth-runtime` 拥有 skill 作为 agent 能力的 wiring 和 message 注入策略。

建议新增：

```python
class SkillRuntimeConfig(KnuthModel):
    roots: list[SkillRoot] = []
    hot_reload: bool = True
    hot_reload_debounce_ms: int = 1000
```

`build_sqlite_runtime(...)` / `build_memory_runtime(...)` 增加可选参数：

```python
skill_config: SkillRuntimeConfig | None = None
```

runtime builder 始终注册内置 skill 能力。若 host 未传入 `skill_config`，使用空 roots 的默认配置；这表示当前没有可发现的外部 skill，但不关闭 `skill` 工具、system section 或 reminder。

runtime builder：

1. 构造 `SkillManager` 并加载初始 snapshot。
2. 注册 `SkillToolProvider` 到 runtime-wide `ToolRegistry`。
3. 注册 `SkillSystemSectionProvider` 到 runtime section providers。
4. 注册 skill 相关 message middleware。
5. 当 `skill_config.hot_reload` 为 true 时，启动 `SkillHotReloadService`。
6. 在 runtime 关闭时停止 hot reload watcher，不能留下后台任务。
7. 不读取 CLI 配置文件、不读取环境变量、不决定默认目录。

#### Manager 与 middleware 的通信机制

runtime builder 必须只创建一个 `SkillManager`，然后把同一个实例传给 `SkillToolProvider` 和 skill middleware。变化检测和通知分两层：

- `SkillManager` 负责感知目录是否需要刷新，并维护全局 `SkillSnapshot(version, catalog_digest, skills)`。
- `SkillToolProvider` 在工具调用前通过 manager 读取当前 snapshot 和 skill 正文。
- `SkillHotReloadService` 监听文件系统变化，只负责标记 manager dirty。
- `SkillChangeNoticeMiddleware` 在 turn-end checkpoint 读取 manager 的 snapshot，并决定当前 run 是否需要追加一条新的 user-role notice。

不要用 callback、event bus 或 observer 让 manager / provider 主动调用 middleware。目录变化发生时，当前 run 可能有 open tool batch、approval 或其他不能插入消息的状态；合法插入点必须由 runtime checkpoint 决定。

dirty flag 只表示“manager 是否需要重新扫描目录”，不是“某个 run 是否已经通知过模型”。多个 run 可能共享同一个 manager，所以不能设计 `consume_changed()` 这类全局消费接口。per-run 去重由 middleware 基于当前 `MessageTape` 判断：找到上一条由 `SkillChangeNoticeMiddleware` 插入的 skill notice，比较其中的 catalog digest；如果 catalog digest 相同，返回空 patch；如果 catalog digest 不同，插入新 notice。

`SkillSystemSectionProvider` 只贡献稳定规则，不读取目录、不列出当前 skills。当前可用 skill 列表仍由 `SkillReminderMiddleware` 的 `<system-reminder>` 提供。

skill middleware 只返回普通 `InsertPatch`。rewrite/message identity 仍由 runtime middleware runner 和 ledger 负责，skill 设计不自定义 `rewrite_id`。

需要一个 system section provider 和两个 middleware。

#### SkillSystemSectionProvider

通过 `SystemSectionProvider` 贡献稳定的 system preamble fragment，source 使用 `SystemSectionSource.SKILL`。它不包含当前 skill 列表，也不随 hot reload 改变。推荐文本：

```markdown
## Skill
- **Skill** tool is used to invoke user-invocable skills to accomplish user's
  request. IMPORTANT: Only use Skill for skills listed in the current
  `<system-reminder>...</system-reminder>` user message for the current turn -
  do not guess or use built-in CLI commands. Skills can be hot-reloaded
  (added/removed/modified) during a session, and the current reminder is the
  single source of truth for the *current* turn; always re-check that the skill
  exists there right before invoking it, and do not rely on memory from earlier
  turns. If the user asks about the current available skills, answer from the
  current reminder and do not rely on memory from earlier turns. CAVEAT: user
  scope skills are stored under the app's configured skill directories. Do NOT
  create or modify files inside the skill or config directories. If the skill
  needs to generate, create, or write any files/directories, it must write only
  to a dedicated subdirectory under the current working directory (recommended
  examples: `./tmp`, `./artifacts`); do not write directly into the cwd root.
  Create the subdirectory if missing. If a tool or script accepts an output path
  (e.g. --path/--output/--dir), you must explicitly set it to a dedicated cwd
  subdirectory and never rely on defaults. If you cannot set a safe output path,
  ask the user before continuing.
```

#### SkillReminderMiddleware

在 `BEFORE_MODEL_REQUEST` 运行，产生 ephemeral insert。它把当前 skill snapshot 渲染为一条 user-role `<system-reminder>` 消息，插入在 conversation start：

```text
<system-reminder>
The following skills are available for use with the Skill tool:
Current skills count: N

- name: description
</system-reminder>
```

要求：

- 每次运行前执行 lazy refresh。
- 没有 skill 时仍可注入 `- none: No skills available`，也可以配置为不注入；v1 推荐注入，避免模型猜测。
- reminder 是 ephemeral，不写 ledger；它表达“当前可用能力”，不是历史事实。
- 模型若询问当前 skills，应基于当前 reminder 回答，不能凭旧上下文记忆。
- patch 形态是 `InsertPatch(durable=False, position=conversation_start, items=[InferenceMessage(role=USER, content=...)])`。
- patch metadata 只放 `content_hash`、`skill_count`、`catalog_digest` 等语义字段；不得放 `rewrite_id` 或其他 runtime-reserved key。

#### SkillChangeNoticeMiddleware

在 `AFTER_TURN_CLOSED` 运行，产生 durable insert。它的工作很简单：如果当前 skill snapshot 相比本 run 上一次 skill notice 发生变化，就重新组一条 user-role message，内容使用与 shell agent 类似的 skill 列表和使用提醒。

流程：

```text
AFTER_TURN_CLOSED
↓
manager.refresh_if_dirty()
↓
render_skill_change_notice_text(snapshot)
↓
从 MessageTape 找上一条 skill notice
↓
catalog digest 相同：返回空 patch
catalog digest 不同：在 conversation_end 插入新的 user message
```

patch 形态：

```python
InsertPatch(
    durable=True,
    position=TapePosition(kind="boundary", boundary="conversation_end"),
    items=[
        InferenceMessage(
            role=InferenceRole.USER,
            content=render_skill_change_notice_text(snapshot),
        )
    ],
    metadata={
        "reason": "skill_catalog_changed",
        "catalog_digest": snapshot.catalog_digest,
        "snapshot_version": snapshot.version,
    },
)
```

notice 文本应带稳定 header，方便 middleware 从当前 run 的可见消息中识别上一条 notice，例如：

```text
<knuth-skill-notice catalog-digest="<catalog_digest>">
Available skills have changed.
Current skills count: N

- name: description
</knuth-skill-notice>
```

v1 不要求新增 rewrite audit 查询面。middleware 可以通过稳定 header + catalog digest 在 `MessageTape.model_visible()` 中完成 per-run 去重。

## CLI 可变配置

`knuth-cli` 只做配置 adapter。

建议扩展 `AgentConfig`：

```python
class AgentConfig:
    ...
    skill_roots: list[SkillRoot] = []
    skill_hot_reload: bool = True
    skill_hot_reload_debounce_ms: int = 1000
```

CLI 默认 root 候选分两类，按以下优先级排序，first wins：

1. 项目级 root：`Path.cwd() / ".knuth" / "skills"`，source=`project`
2. 用户级 root：`Path.home() / ".agents" / "skills"`，source=`user`

其他兼容目录必须由 config file 或 env 显式配置，不作为 Knuth 的内置默认。显式配置的 roots 应保留用户给出的顺序；如果 host 同时启用默认 roots 和显式 roots，必须明确合并策略。v1 推荐：显式 `skill_roots` 覆盖默认 roots，避免隐式目录混入用户没有预期的技能。

环境变量建议：

- `KNUTH_SKILL_ROOTS=<pathsep-separated roots>`
- `KNUTH_SKILL_HOT_RELOAD=0|1`
- `KNUTH_SKILL_HOT_RELOAD_DEBOUNCE_MS=<milliseconds>`

`KNUTH_SKILL_ROOTS` 中的 root 使用 source=`host`，顺序就是优先级。env 覆盖 config。CLI 解析完后把 `SkillRuntimeConfig` 传给 runtime builder。除此之外，CLI 不 import skill parser，不直接注册 `skill` tool，不直接注入 skill prompt。

`knuth-im` 或未来 daemon 可以复用同一 `SkillRuntimeConfig`，也可以选择完全不同的 root 策略。

## Prompt 与工具说明

Knuth 的 skill 使用规则不应由 CLI 拼进私有 system prompt，而应由内置 skill 能力提供：

- `SkillSystemSectionProvider` 提供稳定的 system preamble 规则。
- `SkillToolProvider.manifest.description` 说明何时调用 `skill`、参数格式、不能猜测不存在的 skill。
- `SkillReminderMiddleware` 提供当前可用 skill 列表。

`SkillSystemSectionProvider` 使用上文推荐文本；动态 skill 列表不得放入 system preamble。

`SkillToolProvider.manifest.description` 推荐文本：

```text
Execute a Knuth skill within the main conversation.

Skills provide specialized instructions and domain knowledge. When the user's
task matches an available skill, invoke this tool before answering the task.

Important rules:
- Only invoke skills listed in the current <system-reminder> message for this
  model request.
- Do not guess skill names from memory or from earlier turns.
- If the current reminder says no skills are available, continue without using
  this tool.
- Do not invoke a skill that is already active in the current tool batch.
- If a skill is missing or no longer available, continue the task without it.
- Skill directories are read-only capability sources. Do not create or modify
  files inside skill roots.
- If a skill needs to create output files, write them under a dedicated
  subdirectory of the current working directory such as ./tmp or ./artifacts.
```

`SkillReminderMiddleware` 推荐文本：

```text
<system-reminder>
The following skills are available for use with the Skill tool.
This reminder is the source of truth for the current model request. Re-check it
before invoking any skill, because skills may change between turns.
Current skills count: <count>

<skills-list>
</system-reminder>
```

`<skills-list>` 格式：

```text
- <name>: <description>
```

没有可用 skill 时：

```text
- none: No skills available
```

推荐 v1 把动态列表放在 user-role `<system-reminder>`，把使用规则放在 tool description。这样：

- 当前列表随 refresh 改变。
- 不把 ephemeral list 变成 durable preamble。
- 当前 reminder 明确成为本次 model request 的 truth source。

## Durable 与 replay 语义

Skill 有三类信息：

1. 当前可用 skill 列表：build-time projection，不持久化。
2. 被模型实际调用的 skill 正文：通过普通 tool result observation 进入 ledger，必须 durable/auditable。
3. skill catalog 变更 notice：由 `SkillChangeNoticeMiddleware` 在 turn end 作为 durable user-role message 插入。

原因：如果 run 在 6 月 17 日调用了 `pdf` skill，而 6 月 18 日用户修改了 `pdf/SKILL.md`，继续或审计旧 run 时不能假装模型当时看到了新版 skill。

因此 `skill` tool 读取到的 skill 正文必须写进当次 tool result observation，而不是只写一个文件路径。tool completion 已经是 durable event，后续 resume / refold 可以从 ledger 重建模型当时收到的 tool result。

skill catalog notice 是另一件事：它不是某次 skill 调用的正文，而是“当前可用 skill 集合已经变化”的运行时提示。它由 middleware 在合法 checkpoint 上插入，避免 manager / watcher 在任意时刻直接改 conversation。

skill catalog notice 的恢复流程：

1. 目录 watcher、lazy scan 或 host 显式刷新调用 `SkillManager.invalidate(reason)`。
2. 下一次 `AFTER_TURN_CLOSED`，`SkillChangeNoticeMiddleware` 调用 `manager.refresh_if_dirty()`。
3. middleware 渲染当前 snapshot 对应的 notice，并从当前 run 的 `MessageTape` 找上一条 notice。
4. 如果上一条 notice 的 catalog digest 相同，返回空 patch。
5. 如果 catalog digest 不同，在 `conversation_end` 插入一条新的 durable user-role notice。

这样 replay 能解释两件事：模型实际调用 skill 时看到的正文来自 tool result；模型得知 skill 集合变化时看到的提示来自 durable notice。两者都不依赖当前文件系统中的新版 `SKILL.md`。

## 安全与权限

- Skill 文件和 skill root 目录不应被模型默认写入。内置 prompt/tool description 应说明：如果 skill 需要产物，应写到当前工作目录下的专用子目录，例如 `./tmp` 或 `./artifacts`。
- `allowed_tools` v1 只解析，不强制。未来可由 `PolicyEngine` 基于当前 active skill 限制工具使用；但这需要“active skill scope”设计，不能在 v1 半实现。
- Skill 内容进入 ledger 前仍经过现有 event redaction。若 skill 文件本身含 secret，redaction 只能尽力处理；用户不应把 secret 放入 skill 文档。
- Skill root 来自 host 配置。runtime 不应偷偷扫描任意全局目录。

## Hot Reload

v1 必须实现 built-in hot reload。设计目标是“及时发现、延迟生效”：

- `SkillManager.invalidate()` 标记 snapshot 过期。
- `SkillHotReloadService` 监听文件系统变化，只调用 `invalidate()`。
- `refresh_if_dirty()` 只在 safe point 执行，负责真正扫描和替换 snapshot。
- `SkillReminderMiddleware`、`SkillChangeNoticeMiddleware` 和 `SkillToolProvider` 调用前都可触发 refresh。

`SkillHotReloadService` 的 watch 范围：

- active roots：当前存在的 skill root。
- pending roots：配置中声明但当前不存在的 root。watcher 应监听最近存在的父目录，让新建 `.knuth/skills` 或用户级 skills 目录后能被发现。
- skill dirs：包含 `SKILL.md` 的目录，以及这些目录下的 `scripts/`、`references/`、`assets/` 等资源。
- symlinked skill dirs：目录 symlink 的目标需要单独 watch，因为多数 watcher 不会自动递归跟随目录 symlink。

watch 过滤规则：

- 忽略 `.git`。
- `SKILL.md` 大小写不敏感。
- root 级目录或 symlink 的新增、删除、替换需要触发 watch roots rebuild。
- skill 目录内任意资源变化都可以 invalidate；是否导致 notice 由 refresh 后的 catalog digest 决定。

运行语义：

- debounce 默认 1000ms，可由 `SkillRuntimeConfig.hot_reload_debounce_ms` 配置。
- watcher 不能直接 reload skill，也不能写 ledger 或插入 message。
- watcher 发现变化后调用 `SkillManager.invalidate(changed_path)`，然后继续监听。
- 如果 active root 被删除或替换，watcher invalidate 并 rebuild watch roots。
- runtime 关闭时必须停止 watcher，避免后台任务泄漏。

实现可使用 `watchfiles` 作为事件驱动 backend，也可以先落内置 polling backend；无论 backend 如何，公开语义都是 invalidate-only。

## 实现顺序

1. 在 `knuth-core` 增加 skill metadata 契约和 `SystemSectionSource.SKILL`。
2. 在 `knuth-toold` 增加 `knuth_toold.skills`：`SkillManager`、`SkillSnapshot`、`SkillHotReloadService`、validator、tool provider、render helpers。
3. 为 hot reload backend 增加事件驱动 watcher 或内置 polling fallback。v1 只要求公开语义是 invalidate-only，具体 backend 可按依赖成本选择。
4. 在 `knuth-runtime` 增加 `SkillRuntimeConfig`、`SkillSystemSectionProvider`、skill reminder middleware、skill change notice middleware，并在 builders 中共享同一个 `SkillManager` 实例。
5. 在 runtime lifecycle 中按 `SkillRuntimeConfig.hot_reload` 启动和停止 `SkillHotReloadService`。
6. 在 `knuth-cli` 扩展 config/env parsing，只传 `SkillRuntimeConfig`。
7. 为 `knuth-im` runtime factory 保持可复用入口；是否默认启用由 host policy 决定。

## 验收测试

需要覆盖：

- frontmatter validation：合法 name、非法 name、空 description、`allowed-tools` 字符串、未知 key warning。
- spec validation：`name` 必须匹配父目录名，拒绝连续中划线，拒绝大写字符，`allowed-tools` 按空格分隔字符串解析。
- root priority：多个 root 中同名 skill 只保留高优先级。
- CLI default roots：当前目录 `.knuth/skills` 中的 skill 优先于 `~/.agents/skills` 中的同名 skill。
- symlink scan：目录 symlink 可遍历，循环不会无限扫描，`.git` 被跳过。
- CLI config：配置文件/env 能配置 skill roots 和 hot reload，并把 `SkillRuntimeConfig` 传给 runtime builder；CLI 不提供 skill 总开关。
- tool registry：runtime 始终包含内置 `skill` tool；没有 roots 或没有 skill 文件时，当前列表为空。
- system section：runtime 始终包含 `SkillSystemSectionProvider` 的稳定规则，动态 skill 列表仍来自 reminder。
- manager dirty flag：目录变化调用 `SkillManager.invalidate(reason)` 后，不立刻扫描；下一次 safe point `refresh_if_dirty()` 更新 snapshot version / catalog digest。
- hot reload service：修改、新增、删除 `SKILL.md` 会 invalidate manager；不会立即 reload。
- pending root watch：启动时 root 不存在，后续创建 root 和 skill 后能 invalidate 并在 safe point 加载。
- symlink watch：symlinked skill directory 的目标文件变化能 invalidate；新增或替换目录 symlink 会 rebuild watch roots。
- root rebuild：active root 删除、替换、root 级 skill 目录增删会 invalidate 并 rebuild watch roots。
- debounce：短时间内多次文件变化合并为一次 invalidate 或一次 refresh。
- watcher lifecycle：runtime 关闭后 watcher 停止，不留下后台任务。
- shared manager：`SkillToolProvider` 和 `SkillChangeNoticeMiddleware` 使用同一个 `SkillManager` 实例，不通过 callback 或 event bus 通信。
- reminder：首轮 model request 能看到当前 skill 列表；skill 变更后 safe point refresh 生效。
- skill invocation：模型调用 `skill` 后，tool result observation 能看到 base_dir、skill content、args。
- skill invocation durability：调用 skill 后修改原文件，继续 run 仍能从 durable tool result 看到调用时的 skill 内容。
- skill size limit：被调用 skill 渲染后的完整 tool-result observation 超过 v1 上限时，`skill` tool 返回 error observation，且不会把正文部分写入 observation。
- change notice：skill catalog digest 变化后，下一次 `AFTER_TURN_CLOSED` 插入一条 user-role notice。
- per-run dedup：同一 run 已有相同 catalog digest 的 skill notice 时，不重复插入；不同 run 共享同一个 manager 时不会互相吞掉通知。

## 已定决策

- v1 不新增独立 workspace package。Skill 能力先放在 `knuth-toold.skills`，因为 skill 对模型表现为工具。
- `SkillSource` 保持封闭枚举，v1 只允许 `project | user | builtin | host`。
- v1 设置合理的 skill tool-result observation 大小上限。被调用 skill 渲染后的完整 observation 超过上限时，`skill` tool 返回 error observation；v1 不做 artifact offload。
