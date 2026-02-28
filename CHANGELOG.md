# Changelog

All notable changes to nanobot are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added

- **Kaizen continuous improvement loop** — after every memory consolidation, the agent scans
  the conversation for repeatable tasks that could be automated as scripts or skills and appends
  candidates to `~/.nanobot/workspace/memory/KAIZEN.md`. On a configurable interval
  (`agents.defaults.kaizen_review_interval_days`, default `1` day) the top candidates are
  selected and the agent autonomously creates the corresponding skills or scripts in the
  workspace. This self-improvement cycle requires no user interaction.

- **Per-channel model routing** — each channel config (Telegram, Discord, WhatsApp, etc.) now
  accepts an optional `model` field. When set, that channel uses the specified model instead of
  the global `agents.defaults.model`. Useful for routing high-volume channels to a cheaper model
  while keeping interactive channels on a premium one.

- **Subagents now inherit MCP tools and cron scheduling** — `SubagentManager` now receives
  `mcp_servers` and `cron_service` from the parent `AgentLoop`. Subagents spawned via the
  `spawn` tool can use the same MCP-registered tools and schedule cron jobs, making them
  first-class citizens of the agent ecosystem.

- **Session cleanup CLI** — `nanobot sessions list` and `nanobot sessions cleanup --days N`
  commands for managing JSONL session files. Sessions older than N days (default 30) are
  removed. `--dry-run` shows what would be deleted without touching the filesystem.

- **GitHub Actions CI** (`.github/workflows/ci.yml`) — two-job pipeline:
  - Python: `ruff check`, `pytest`, `pip-audit`
  - Node.js: `npm audit --audit-level=high`, `npm run build`

- **CONTRIBUTING.md** — developer guide covering how to add channels, tools, providers, and
  skills, including the ABC interfaces and registry patterns.

- **bridge/README.md** — documents the Node.js WhatsApp bridge: WebSocket protocol, auth token
  negotiation, QR login, environment variables, and troubleshooting.

### Changed

- **Per-session concurrency** — the global `_processing_lock` that serialised every message from
  every channel has been replaced with per-session locks (`_session_locks`). Messages for
  different users/channels are now processed concurrently; only messages within the *same*
  session are still serialised (to prevent session corruption).

- **`_TOOL_RESULT_MAX_CHARS` raised from 500 → 5 000** — tool results stored in session JSONL
  files now retain up to 5 000 characters. This eliminates silent context loss between resumed
  sessions when tool output exceeded the old limit.

- **Sensitive arguments scrubbed from logs** — tool call arguments whose key contains
  `password`, `token`, `api_key`, `secret`, `auth`, `credential`, `private_key`, or
  `access_key` are replaced with `***` before being written to log files.

- **Workspace path in system prompt** — the agent's system prompt now shows `~/.nanobot/workspace`
  (home-relative) instead of the full absolute path, reducing username/path leakage.

- **MCP HTTP client now has a 30-second timeout** — previously `httpx.AsyncClient(timeout=None)`
  could hang the agent loop indefinitely on a slow or unresponsive MCP server.

- **Docker image runs as non-root** — a dedicated `nanobot` system user is created and the
  `USER nanobot` instruction added to the `Dockerfile`. The config directory is now at
  `/home/nanobot/.nanobot` inside the container. Update Docker volume mounts accordingly:
  ```
  -v ~/.nanobot:/home/nanobot/.nanobot
  ```

- **Gateway warns (or refuses) when running as root** — `nanobot gateway` now prints a prominent
  warning when `os.getuid() == 0` on Linux/macOS.

- **`allowFrom` open-access warning** — the gateway prints a `SECURITY WARNING` at startup for
  every enabled channel that has an empty `allowFrom` list, reminding operators to restrict
  access for production deployments.

- **WhatsApp bridge token passed via temp file** — the bridge authentication token is now
  written to a `0600` temporary file (`BRIDGE_TOKEN_FILE`) at startup instead of being passed
  as a plain environment variable, preventing exposure in `/proc/<pid>/environ` and `ps e`
  output.

### Fixed

- **Memory consolidation race condition** — background consolidation tasks now take a snapshot
  of `session.messages` *before* the first `await`, preventing a race with concurrent
  `_save_turn()` calls that could cause messages to be included in the consolidated summary
  non-deterministically.

### Documentation

- **README.md** — added "🧠 How It Works Internally" section with collapsible subsections on
  the Skills system, Session storage (JSONL format), Heartbeat (two-phase LLM decision),
  and the Memory system (MEMORY.md + HISTORY.md consolidation).
- **SECURITY.md** — updated to reflect all security improvements listed above; added guidance
  on the bridge token file, root-detection, log scrubbing, and MCP timeout.

---

## [0.1.4.post2] — 2026-02-24

_Reliability-focused release with a redesigned heartbeat, prompt cache optimization, and
hardened provider & channel stability._

### Added

- **Virtual tool-call heartbeat** — Heartbeat is now implemented as a virtual tool call,
  improving reliability and observability of the wake-up cycle.

### Changed

- **Prompt cache optimization** — Reduced latency and cost by improving how the prompt cache
  is populated and reused across turns.
- **Provider & channel stability** — Hardened error handling and reconnection logic across
  LLM providers and chat channel integrations.

### Fixed

- **Slack mrkdwn rendering** — Fixed incorrect markdown rendering in Slack messages.
- **Slack thread isolation** — Slack replies now correctly stay within their originating thread.
- **Discord typing indicator** — Fixed the typing indicator not clearing correctly in Discord.

---

## [0.1.4.post1] — 2026-02-21

_New providers, media support across channels, and major stability improvements._

### Added

- **Feishu multimodal files** — Feishu channel now receives images and other files sent by
  users.
- **Slack file sending** — The agent can now send files to users via the Slack channel.
- **Discord long-message splitting** — Discord messages that exceed the character limit are
  automatically split across multiple messages.
- **VolcEngine provider** — Added VolcEngine (ByteDance) as a supported LLM provider.
- **MCP custom auth headers** — MCP HTTP client now accepts custom authentication headers per
  server configuration.
- **Anthropic prompt caching** — Added support for Anthropic's prompt-caching API to reduce
  cost on long system prompts.

### Changed

- **Memory reliability** — Background memory consolidation is more robust under concurrent
  message load.

### Fixed

- **Subagents in CLI mode** — Subagents spawned via the `spawn` tool now function correctly
  when running in CLI (`nanobot agent`) mode.

---

## [0.1.4] — 2026-02-17

_MCP support, progress streaming, new providers, and multiple channel improvements._

### Added

- **MCP (Model Context Protocol)** — nanobot now supports MCP tool servers; tools are
  auto-discovered and registered at startup alongside built-in tools.
- **OpenAI Codex provider** — Added OpenAI Codex as a supported LLM provider with OAuth login
  flow.
- **ClawHub skill** — Integrated the ClawHub skill, allowing the agent to search and install
  public agent skills from [clawhub.ai](https://clawhub.ai).

---

## [0.1.3.post7] — 2026-02-13

_Security hardening and multiple improvements. Upgrade recommended to address security issues._

### Added

- **MiniMax provider** — Added MiniMax as a supported LLM provider.

### Changed

- **Memory system redesign** — Simplified and more reliable two-layer memory consolidation
  with less code and fewer edge cases.
- **CLI experience** — Enhanced command-line interface with improved output formatting and
  usability.

### Security

- Multiple hardening improvements across providers and channels; see
  [release notes](https://github.com/HKUDS/nanobot/releases/tag/v0.1.3.post7) for details.

---

## [0.1.3.post6] — 2026-02-10

### Added

- **Slack channel** — Added Slack as a supported chat platform.
- **Email channel** — Added Email (IMAP/SMTP) as a supported chat platform.
- **QQ channel** — Added QQ (QQ单聊) as a supported chat platform.

### Changed

- **Provider refactor** — Refactored the provider layer so that adding a new LLM provider
  requires only 2 steps.

---

## [0.1.3.post5] — 2026-02-07

_Qwen support and several key improvements._

### Added

- **Qwen provider** — Added Alibaba Cloud Qwen as a supported LLM provider.
- **Moonshot/Kimi provider** — Added Moonshot (Kimi) as a supported LLM provider.
- **Discord channel** — Added Discord as a supported chat platform.
- **Feishu channel** — Added Feishu (飞书) as a supported chat platform.
- **DeepSeek provider** — Added DeepSeek as a supported LLM provider.
- **Scheduled tasks enhancements** — Improved natural language parsing and reliability for
  periodic/cron task configuration.

### Security

- Enhanced security hardening across channel integrations.

---

## [0.1.3.post4] — 2026-02-04

_Multi-provider & Docker support._

### Added

- **vLLM support** — Integrated vLLM for local LLM inference via the OpenAI-compatible API.
- **Docker support** — Added Dockerfile and Docker Compose configuration for containerised
  deployment.
- **Multi-provider support** — Multiple LLM providers can now be configured and selected per
  agent or channel.

### Changed

- **Natural language scheduling** — Improved natural language task scheduling reliability and
  expression coverage.

---

## [0.1.3] — 2026-02-02

_Initial public launch of nanobot._

---

[Unreleased]: https://github.com/HKUDS/nanobot/compare/v0.1.4.post2...HEAD
[0.1.4.post2]: https://github.com/HKUDS/nanobot/compare/v0.1.4.post1...v0.1.4.post2
[0.1.4.post1]: https://github.com/HKUDS/nanobot/compare/v0.1.4...v0.1.4.post1
[0.1.4]: https://github.com/HKUDS/nanobot/compare/v0.1.3.post7...v0.1.4
[0.1.3.post7]: https://github.com/HKUDS/nanobot/compare/v0.1.3.post6...v0.1.3.post7
[0.1.3.post6]: https://github.com/HKUDS/nanobot/compare/v0.1.3.post5...v0.1.3.post6
[0.1.3.post5]: https://github.com/HKUDS/nanobot/compare/v0.1.3.post4...v0.1.3.post5
[0.1.3.post4]: https://github.com/HKUDS/nanobot/compare/v0.1.3...v0.1.3.post4
[0.1.3]: https://github.com/HKUDS/nanobot/releases/tag/v0.1.3
