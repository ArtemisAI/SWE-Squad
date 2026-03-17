# CLI Framework Evaluation for SWE Squad

## Scope

This report compares the following frameworks requested in the issue:

- [clap](https://github.com/clap-rs/clap) (Rust)
- [oclif](https://github.com/oclif/oclif) (TypeScript/Node.js)
- [cobra](https://github.com/spf13/cobra) (Go)
- [typer](https://github.com/fastapi/typer) (Python)

Current CLI implementation is in `scripts/ops/swe_cli.py` and is written in Python with `argparse`.

---

## Decision Summary

**Recommended framework: Typer**

Typer is the most appropriate and solid choice for SWE Squad because it keeps the CLI in the existing Python ecosystem while improving maintainability, developer experience, typed command definitions, and help UX with minimal migration risk.

---

## Evaluation Criteria

1. **Language fit with current codebase**
2. **Migration cost from current `argparse` CLI**
3. **Long-term maintainability**
4. **Operational reliability for cron/automation usage**
5. **Testing ergonomics**
6. **Dependency and runtime footprint**
7. **Team onboarding and DX**

---

## Comparative Analysis

| Framework | Strengths | Risks / Tradeoffs | Fit for SWE Squad |
|---|---|---|---|
| **clap (Rust)** | Excellent performance; very mature parser ecosystem; strong type safety | Requires rewriting CLI in Rust and integrating with Python runtime/data model boundaries | **Low** — high rewrite and integration complexity for little operational gain |
| **oclif (Node.js)** | Plugin architecture; strong ecosystem for distributable CLIs; rich scaffolding | Requires introducing Node toolchain/runtime for a currently Python-first project | **Low-Medium** — good framework, but technology split adds maintenance overhead |
| **cobra (Go)** | Industry-proven in many infra tools; solid command structure and completions | Requires Go rewrite and cross-language orchestration with Python internals | **Low** — similar concerns as clap: strong framework, wrong ecosystem fit |
| **typer (Python)** | Native Python fit; typed arguments/options; automatic help/completion/docs; simple testing; incremental migration possible | Adds dependency on Click/Typer runtime; startup overhead slightly above bare argparse | **High** — best balance of capability, cost, and migration safety |

---

## Why Typer Wins for This Repository

### 1) Native ecosystem alignment

SWE Squad CLI and core services are Python-based. Typer keeps implementation, testing, packaging, and developer workflows in one language.

### 2) Incremental migration path

You can migrate command-by-command from `argparse` to Typer without disruptive rewrites of ticket/status/report logic.

### 3) Better command ergonomics with low risk

Typer provides:

- Cleaner command declaration via type hints
- Better auto-generated help/usage output
- Shell completion support
- Consistent command groups/subcommands for future growth

### 4) Better maintainability for growing CLI surface

Given commands already include `status`, `tickets`, `issues`, `repos`, `summary`, `report`, and `dashboard`, Typer’s command organization is better suited for continued growth than ad-hoc `argparse` expansion.

---

## Recommended Adoption Plan (Minimal Risk)

1. **Phase 1: Introduce Typer as optional CLI entrypoint**
   - Keep existing `argparse` command behavior unchanged
   - Add Typer wrapper for one low-risk command (e.g., `status`)
2. **Phase 2: Migrate remaining commands incrementally**
   - Port each command with parity tests
   - Preserve existing output contracts (`--json`, exit codes)
3. **Phase 3: Remove old parser once parity is complete**
   - Keep compatibility aliases if needed
   - Update README/cron examples to final command interface

---

## Final Recommendation

For improving SWE Squad CLI tools, **Typer is the most appropriate and solid framework** among the options evaluated.

It provides the best combination of:

- Strong capability for modern CLI UX
- Lowest migration and operational risk
- Maximum alignment with the existing Python architecture
- Sustainable long-term maintainability
