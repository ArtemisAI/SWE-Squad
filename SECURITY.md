# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in SWE Squad, please report it responsibly:

1. **Do NOT** open a public GitHub issue
2. Email the maintainers or use [GitHub Security Advisories](https://github.com/ArtemisAI/SWE-Squad/security/advisories/new)
3. Include a description of the vulnerability, steps to reproduce, and potential impact

We will acknowledge receipt within 48 hours and provide a timeline for a fix.

## Security Considerations

SWE Squad executes code via Claude Code CLI and runs shell commands. When deploying:

- **Isolate the runtime** — run agents in a sandboxed VM or container
- **Use dedicated GitHub accounts** — never share your personal credentials with agents
- **Scope PAT permissions** — use the minimum required GitHub token scopes
- **Review agent output** — agents propose fixes on branches; always review before merging
- **Protect secrets** — never commit `.env` files; use the `.env.example` template
- **Restrict network access** — limit agent VM's network to only required endpoints

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest `main` | Yes |
| Older commits | No |
