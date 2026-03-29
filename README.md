<p align="center">
  <img src="assets/swe_squad_banner.png" alt="SWE Squad Banner" width="100%">
</p>

<h1 align="center">SWE Squad</h1>

<p align="center">
  <em>An autonomous, provider-agnostic software engineering team that monitors, triages, and fixes bugs while you sleep.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/tests-passing-22C55E?style=for-the-badge" alt="Tests Passing">
  <img src="https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white&style=for-the-badge" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-MIT-6366F1?style=for-the-badge" alt="MIT License">
  <img src="https://img.shields.io/badge/A2A-Protocol-F97316?style=for-the-badge" alt="A2A Protocol">
</p>

<p align="center">
  <a href="https://github.com/ArtemisAI/SWE-Squad/stargazers">
    <img src="https://img.shields.io/github/stars/ArtemisAI/SWE-Squad?style=social" alt="GitHub Stars">
  </a>
  &nbsp;&nbsp;
  <a href="https://github.com/ArtemisAI/SWE-Squad/network/members">
    <img src="https://img.shields.io/github/forks/ArtemisAI/SWE-Squad?style=social" alt="GitHub Forks">
  </a>
</p>

---

> [!WARNING]
> **Run in a VM or container — do not run on your host machine.**
>
> SWE Squad is an agentic AI system with full access to your filesystem, shell, and git history. Like any tool in this class (Claude Code, Devin, OpenHands), it reads, writes, and executes files autonomously.
>
> **Recommended setup:**
> - **Docker** — use the provided `Dockerfile` / `docker-compose.yml` (see [Quick Start](#quick-start))
> - **VM** — a dedicated Linux VM with scoped credentials
> - **Cloud sandbox** — a fresh VPS or GitHub Codespace with only the keys it needs
>
> Scope your API keys and GitHub tokens to the minimum required permissions. Never give the agent a token with org-wide write access.

---

## What is SWE Squad?

SWE Squad is a team of AI agents that autonomously monitors your production systems, detects issues, and fixes them — with human-in-the-loop escalation at every critical decision point.

Unlike single-agent coding tools, SWE Squad operates as a **coordinated pipeline** where each agent has a specialized role: monitoring ingests logs and GitHub issues, triage classifies severity and routes work, investigation performs root-cause analysis, and the developer agent attempts fixes on isolated git branches — discarding any that fail tests. A stability gate (Ralph Wiggum) blocks new feature work until the error backlog is clear.

The system is built around a **provider-agnostic plugin architecture**: every external dependency — coding engine, notification channel, issue tracker, sandbox, vector store — is behind a swappable interface. You bring your own tools; SWE Squad orchestrates them.

---

## Key Features

- **Provider-agnostic plugin architecture** — 12 domain interfaces, 20+ built-in implementations. Swap any component without touching core logic.
- **Multi-team support** — multiple squads share a Supabase backend with full isolation via `team_id` and assignee-based issue pickup. Alpha and beta squads never collide.
- **Session lifecycle management** — investigation and development sessions persist across daemon cycles. Developer sessions fork from investigator sessions, carrying full context forward.
- **Parallel execution with git worktree isolation** — each fix attempt runs in its own worktree; tests pass → commit, tests fail → auto-revert. No broken code reaches main.
- **Semantic memory** — resolved tickets are embedded (bge-m3, 1024-dim) and stored in a pgvector knowledge base. Top-5 similar past fixes are injected into every investigation prompt, confidence-weighted.
- **Knowledge graph scoring** — graph-based relationship scoring between tickets surfaces non-obvious connections across modules and error classes.
- **GitHub OAuth dashboard** — optional WebUI with user management, live metrics, and control plane API for runtime configuration (sessions, projects, model tiers).
- **A2A inter-agent protocol** — JSON-RPC 2.0 event bus for cross-agent coordination. Includes server, client, and adapters for Gemini CLI, OpenCode, and generic CLI agents.
- **Circuit breaker + exponential backoff** — if development failures exceed 80%, the daemon pauses for 30 minutes. All LLM calls use capped exponential backoff.
- **Automated code review** — every fix PR is reviewed before merge with a fail-closed policy. Code review failures block the merge.
- **Credential scanner** — pre-commit hook and inline scanner detect secrets before they reach git history.
- **Model routing** — Haiku for cheap tasks, Sonnet for routine fixes, Opus as orchestrator-only for critical tickets. After two Sonnet failures, auto-escalate to Opus.
- **Deterministic replay** — successful fix trajectories are cached by error fingerprint for zero-cost instant replay.

---

## Architecture

The pipeline flows from log ingestion through triage, investigation, development, and governance:

```mermaid
flowchart TD
    subgraph entry ["Entry Point"]
        Runner(["SWE Squad Runner\ncron · daemon · one-shot"])
    end

    subgraph ingest ["Ingestion"]
        direction LR
        Monitor["Monitor Agent\nLog scanning & fingerprinting"]
        GitHub["GitHub Scanner\nAssignee-filtered issues"]
        Remote["Remote Logs\nSSH / rsync collection"]
    end

    subgraph analysis ["Analysis & Routing"]
        Triage["Triage Agent\nSeverity classification"]
        Distiller["Trajectory Distiller\nCached fix replay"]
        Investigator["Investigator Agent\nRoot-cause analysis"]
        Memory["Semantic Memory\npgvector + knowledge graph"]
    end

    subgraph resolution ["Resolution"]
        Developer["Developer Agent\nKeep / discard fix loop"]
        Reviewer["Code Reviewer\nFail-closed merge gate"]
    end

    subgraph governance ["Governance & Output"]
        direction LR
        Ralph["Stability Gate\nBugs before features"]
        Creative["Creative Agent\nProactive proposals"]
        A2A["A2A Dispatch\nInter-agent event bus"]
    end

    Runner --> Monitor & GitHub & Remote
    Monitor & GitHub & Remote --> Triage
    Triage --> Distiller & Investigator
    Investigator <--> Memory
    Distiller & Investigator --> Developer
    Developer --> Reviewer --> Ralph
    Ralph -->|stable| Creative
    Ralph --> A2A

    classDef entryNode fill:#6366f1,stroke:#4338ca,color:#fff,stroke-width:2px
    classDef ingestNode fill:#10b981,stroke:#059669,color:#fff,stroke-width:1.5px
    classDef analysisNode fill:#f59e0b,stroke:#d97706,color:#fff,stroke-width:1.5px
    classDef resolveNode fill:#ef4444,stroke:#dc2626,color:#fff,stroke-width:2px
    classDef gateNode fill:#8b5cf6,stroke:#7c3aed,color:#fff,stroke-width:1.5px
    classDef outputNode fill:#06b6d4,stroke:#0891b2,color:#fff,stroke-width:1.5px

    class Runner entryNode
    class Monitor,GitHub,Remote ingestNode
    class Triage,Distiller,Investigator,Memory analysisNode
    class Developer,Reviewer resolveNode
    class Ralph gateNode
    class Creative,A2A outputNode
```

### Fix Loop

Each fix attempt runs on a git branch. Tests pass → commit. Tests fail → `git reset --hard`. No broken code ever reaches main.

```mermaid
flowchart TD
    Start(["New Ticket"]) --> Cache{"Trajectory\ncache hit?"}
    Cache -->|hit| Replay["Replay cached fix\nzero cost, instant"]
    Replay --> T0{"Tests?"}
    T0 -->|pass| Keep0(["KEEP — commit"])
    T0 -->|fail| A1
    Cache -->|miss| A1

    subgraph attempts ["Escalating Fix Attempts"]
        A1["Attempt 1 — Sonnet\nroutine fix"] --> T1{"Tests?"}
        T1 -->|pass| Keep1(["KEEP"])
        T1 -->|fail| A2["Attempt 2 — Sonnet\nwith error context"]
        A2 --> T2{"Tests?"}
        T2 -->|pass| Keep2(["KEEP"])
        T2 -->|fail| A3["Attempt 3 — Opus\norchestrates sub-agents"]
        A3 --> T3{"Tests?"}
        T3 -->|pass| Keep3(["KEEP"])
        T3 -->|fail| HITL
    end

    HITL(["HITL Escalation\nHuman notified"])

    classDef decisionNode fill:#f59e0b,stroke:#d97706,color:#fff
    classDef successNode fill:#10b981,stroke:#059669,color:#fff
    classDef failNode fill:#ef4444,stroke:#dc2626,color:#fff
    classDef sonnetNode fill:#3b82f6,stroke:#2563eb,color:#fff
    classDef opusNode fill:#8b5cf6,stroke:#7c3aed,color:#fff

    class Cache,T0,T1,T2,T3 decisionNode
    class Keep0,Keep1,Keep2,Keep3 successNode
    class HITL failNode
    class A1,A2 sonnetNode
    class A3 opusNode
```

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/ArtemisAI/SWE-Squad.git
cd SWE-Squad
pip install python-dotenv pyyaml

# 2. Configure
cp .env.example .env
# Edit .env — set SWE_TEAM_ENABLED, SWE_TEAM_ID, GH_TOKEN, SWE_GITHUB_ACCOUNT, SWE_GITHUB_REPO

# 3. Bootstrap (acknowledge pre-existing errors on first run)
python scripts/ops/swe_team_runner.py --bootstrap -v

# 4. Run a single scan cycle
python scripts/ops/swe_team_runner.py -v

# 5. Start the daemon (continuous 30-minute cycles)
python scripts/ops/swe_team_runner.py --daemon -v

# 6. Run tests
python -m pytest tests/unit/ -q
```

For Docker:

```bash
docker compose up -d
```

---

## Configuration

### Required Environment Variables

| Variable | Description |
|----------|-------------|
| `SWE_TEAM_ENABLED` | Kill switch — must be `true` to run |
| `SWE_TEAM_ID` | Unique team identifier for ticket scoping |
| `SWE_GITHUB_ACCOUNT` | Dedicated GitHub bot account for issue pickup |
| `SWE_GITHUB_REPO` | Target repository (`owner/repo`) |
| `GH_TOKEN` | GitHub PAT with `repo` scope |

### Optional Environment Variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Alert notifications |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | Shared multi-team ticket store |
| `BASE_LLM_API_URL` / `BASE_LLM_API_KEY` | OpenAI-compatible proxy for embeddings |
| `EMBEDDING_MODEL` | Embedding model name (default: `bge-m3`) |
| `SWE_MODEL_T1/T2/T3` | Override model tiers (default: haiku/sonnet/opus) |
| `SWE_REMOTE_NODES` | JSON array of SSH worker nodes |

See `.env.example` for the full list. Runtime configuration is in `config/swe_team.yaml`.

---

## Provider Architecture

SWE Squad is provider-agnostic: every external service is behind a swappable interface. New provider = new file in `src/swe_team/providers/<domain>/` + entry in `swe_team.yaml`. Nothing else changes.

| Domain | Interface | Default Implementation | Alternatives |
|--------|-----------|----------------------|--------------|
| Coding engine | `CodingEngine` | Claude Code CLI | Gemini CLI, OpenCode |
| Notification | `NotificationProvider` | Telegram | Slack, PagerDuty, webhook |
| Issue tracker | `IssueTracker` | GitHub Issues | GitLab, Jira, Linear |
| Sandbox | `SandboxProvider` | Docker / local subprocess | Proxmox, cloud VM |
| Auth | `AuthProvider` | GitHub OAuth | Custom JWT, API key |
| Embeddings | `EmbeddingProvider` | bge-m3 via BASE_LLM | OpenAI, local sentence-transformers |
| Vector store | `VectorStore` | Supabase pgvector | Qdrant, Weaviate, Chroma |
| Workspace | `WorkspaceProvider` | git-worktree | Docker volume, noop |
| Repo map | `RepoMapProvider` | ctags | tree-sitter, file listing |
| Task queue | `TaskQueueProvider` | In-memory (heapq) | Redis, RabbitMQ, SQS |
| Usage governor | `UsageGovernor` | Built-in token budget | Custom rate limiter |
| Log query | `LogQueryProvider` | Local file scanner | CloudWatch, Loki, Datadog |

---

## Dashboard (Optional)

The WebUI is an optional plugin (`src/swe_team/control_plane_api.py`). When enabled, it provides:

- Live ticket queue with severity and status filters
- Session browser — active and suspended Claude Code sessions
- Project configuration editor (model tiers, priority weights, sandbox paths)
- User management with role-based access control (GitHub OAuth)
- Control plane API for runtime reconfiguration without restart

To enable:

```bash
python scripts/ops/swe_team_runner.py --daemon --enable-dashboard --port 8080
```

The dashboard is not required for the agent pipeline to function.

---

## Multi-Team Support

Multiple squads can operate on shared infrastructure with full isolation:

- Each squad has its own `SWE_TEAM_ID` — all tickets are scoped to it in Supabase
- Issue pickup is **assignee-based only**: a squad only processes issues assigned to its bot account
- Squads share the semantic memory knowledge base — cross-team patterns improve investigation quality
- Independent daemon processes, independent `.env` files, independent GitHub bot accounts

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines, branch conventions, and the test requirement (`make test` must pass with zero failures).

Areas that would benefit from community input:

- Additional provider implementations (Slack notifications, Linear issue tracker, Qdrant vector store)
- CI/CD integration (GitHub Actions, GitLab CI)
- Agent prompt optimization and benchmarking
- Documentation and tutorials

---

## License

[MIT](LICENSE) — use it, fork it, build on it.

---

## Community

- [GitHub Discussions](https://github.com/ArtemisAI/SWE-Squad/discussions) — questions, ideas, show-and-tell
- [Issues](https://github.com/ArtemisAI/SWE-Squad/issues) — bug reports and feature requests
- [Contributing Guide](CONTRIBUTING.md) — how to submit a PR

<p align="center">
  <sub>Made with care by <a href="https://github.com/ArtemisAI">ArtemisAI</a></sub>
</p>
