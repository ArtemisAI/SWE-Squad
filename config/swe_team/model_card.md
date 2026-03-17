# SWE-Squad Model Card

All agents, models, and AI capabilities available to the autonomous SWE team.

---

## 1. Claude CLI (Primary Coding Engine)

Used by **InvestigatorAgent** and **DeveloperAgent** via `claude --print`.
These models actually read and write code — not the BASE_LLM proxy.

| Alias | Model ID | Use case |
|-------|----------|----------|
| `haiku` | `claude-haiku-4-5-20251001` | Docs, scanning, simple tasks (T3 fast) |
| `sonnet` | `claude-sonnet-4-6` | Routine HIGH bugs, feature implementation (T2 standard) |
| `opus` | `claude-opus-4-6` | CRITICAL bugs, orchestration of sub-agents (T1 heavy) |

**Routing rules:**
- Severity HIGH → Sonnet
- Severity CRITICAL → Opus (orchestrates sub-agents via `orchestrate.md`)
- After 2 Sonnet failures → auto-escalate to Opus
- Cached patches (distiller hit) → no model call

**Thinking variants** (`claude-opus-4-6-thinking`, `claude-opus-4-5-thinking`) available on BASE_LLM proxy but NOT used by Claude CLI directly.

---

## 2. BASE_LLM Proxy (`https://api.ai-automate.me/v1`)

Used for the **ticket system** — embeddings, extraction, semantic memory.
**NOT** used for coding tasks (those use Claude CLI above).

Configured via `BASE_LLM_API_KEY` in `.env`.

### Embeddings (semantic ticket memory)
| Model | Dims | Use |
|-------|------|-----|
| `bge-m3` | 1024 | Primary embedding (configured in `memory.embedding_model`) |
| `qwen3-embedding` | — | Fallback |
| `mxbai-embed-large` | — | Fallback |
| `nomic-embed-text` | — | Fallback |
| `ollama/nomic-embed-text` | — | Fallback |

### Chat/Extraction models
| Model | Notes |
|-------|-------|
| `gemini-2.5-flash-thinking` | Fast, good for extraction/classification |
| `gemini-3-flash` | Fast, low cost |
| `gemini-3-pro-high` | High quality, expensive |
| `gemini-2.5-pro` | Large context, deep reasoning |
| `qwen3:8b` | Lightweight, fast local |
| `qwen3:4b` | Ultra-fast local |
| `qwen3-coder:30b` | Code-aware mid-tier |
| `qwen3-coder:480b-cloud` | Large cloud coder |
| `deepseek-r1:14b` / `:32b` | Strong reasoning |
| `deepseek-v3.1:671b-cloud` | Very large, expensive |
| `kimi-k2.5:cloud` | Large context (1M tokens in cloud variant) |
| `cogito-2.1:671b-cloud` | Large reasoning |
| `mistral-large-3:675b-cloud` | Mistral large |
| `command-r-plus:104b-q4_0` | Cohere, good RAG |
| `gpt-4o` | OpenAI via proxy |
| `claude-opus-4-6` | Anthropic via proxy (uses proxy quota, not CLI) |

### Speech / Audio
| Model | Type |
|-------|------|
| `tts-1`, `tts-1-hd` | Text-to-speech (OpenAI compat) |
| `kanit-tts`, `kani-tts-2`, `kokoro` | Custom TTS voices |
| `whisper-1`, `whisper-large` | Speech-to-text |

### Vision
| Model | Notes |
|-------|-------|
| `llava:7b` | Local vision model |
| `qwen3-vl:235b-cloud` | Large vision-language cloud model |

---

## 3. Gemini CLI (Specialist Delegation)

Installed at `/usr/bin/gemini` (or similar). Logged in on this machine.

| Property | Value |
|----------|-------|
| Context window | **1M tokens** |
| Good for | Web search, large context summarisation, doc analysis |
| Data retention | **Caution** — Google data retention policies apply; do not send proprietary code or PII |
| Daily limits | Applies — check remaining quota before assigning large batches |
| Enabled in fallback | `enabled: false` in `swe_team.yaml` (off by default) |

**Safe tasks for Gemini delegation:**
- Web search for library docs, CVEs, RFC lookups
- Summarising large log dumps (non-proprietary)
- Comparing public API specs

**Do NOT delegate:**
- Source code from LinkedAi (proprietary)
- User PII or credentials
- Internal system architecture details

---

## 4. A2A Network Agents

Hub at `http://100.110.176.73:18790` (Tailscale node). 1040+ tasks processed.

| Agent name | Status | Role |
|-----------|--------|------|
| `openclaw` | healthy | Claude-backed coding/reasoning agent |
| `gemini` | healthy | Gemini model agent |
| `llm_proxy` | healthy | BASE_LLM proxy forwarding agent |

**Note:** The SWE-Squad runner communicates via A2A for event dispatch. Fallback delegation (when Claude CLI is rate-limited) also routes through this hub.

---

## 5. MCP Servers (Available to Claude in subprocesses)

Configured in `.mcp.json` (project) and `~/.claude.json` (global for all subprocesses).

| Server | Transport | Use |
|--------|-----------|-----|
| **DeepWiki** | HTTP (`https://mcp.deepwiki.com/mcp`) | Query any public GitHub repo's docs. Use for third-party library research. |
| **Playwright** | stdio (`npx @playwright/mcp@latest`) | Browser automation — reproduce UI bugs, test HTTP endpoints, take screenshots. |
| **GitHub** | Docker (`ghcr.io/github/github-mcp-server`) | GitHub issues, PRs, branches — all toolsets. |
| **Supabase** | stdio (npx) | Direct Supabase DB access for ticket store queries. |

---

## 6. Model Routing Decision Tree

```
Ticket severity?
├── CRITICAL  → Opus CLI (orchestrates sub-agents)
│                └── Sub-agents use Sonnet/Haiku per task type
├── HIGH      → Sonnet CLI
│   └── 2 failures → escalate to Opus
├── MEDIUM    → Sonnet CLI (if severity_filter allows)
└── LOW       → deferred (below severity_filter default="high")

Ticket has cached patch? → Distiller replay (no model call)
Rate limit hit?          → Exponential backoff → fallback_agents chain
```

---

## 7. Cost Guidance

| Task | Model | Approx cost |
|------|-------|-------------|
| Investigation (Sonnet, 1 ticket) | claude-sonnet-4-6 | ~$0.05–0.15 |
| Investigation (Opus orchestrated) | claude-opus-4-6 | ~$0.50–2.00 |
| Embedding (bge-m3, 1 ticket) | BASE_LLM proxy | ~$0.001 |
| Gemini delegation | Gemini CLI | free tier / quota |

Max daily budget (10 cycles × 3 investigations): ~$0.50–1.50 Sonnet / ~$5–20 Opus
