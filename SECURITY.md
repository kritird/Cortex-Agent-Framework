# Security Policy

## Supported versions

Security fixes are applied to the latest minor release line. Older releases are supported on a best-effort basis.

| Version | Supported |
| ------- | --------- |
| 1.0.x   | ✅        |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security reports.**

If you believe you've found a security vulnerability in Cortex Agent Framework, report it privately by emailing:

**kritiranjan.das@gmail.com**

Include as much of the following as you can:

- A clear description of the issue and its impact
- Steps to reproduce, ideally a minimal proof of concept
- The affected version(s) and platform
- Any suggested remediation you're aware of

You should receive an acknowledgement within **72 hours**. I'll work with you to confirm the issue, develop a fix, and agree on a coordinated disclosure timeline before any public write-up.

## Scope

The following areas are in scope and particularly worth scrutiny:

- **Code Sandbox** (`cortex/sandbox/`) — sandbox escapes, import blocklist bypasses, resource-limit bypasses.
- **Input Sanitiser** (`cortex/security/`) — path traversal, MIME bypass, null-byte injection, oversized input handling.
- **Credential Scrubber** (`cortex/security/`) — patterns that leak credentials through task outputs or history records.
- **Tool Server Registry** / **MCP servers** — untrusted tool-server responses, command-injection via stdio server argv, SSRF via SSE endpoints.
- **External MCP Registry** — auto-discovery accepting malicious server descriptors.
- **Storage backends** — unauthenticated Redis access, SQLite path traversal, file-system escapes in the result envelope store.
- **LLM providers** — credential leakage in error messages or logs.
- **Session resume / WAL** — unauthenticated access to resume tokens.

## Out of scope

- Vulnerabilities in upstream dependencies (report those to the upstream project; I'll pick up fixes via version bumps).
- Denial-of-service from deliberately crafted LLM prompts in an authenticated local-dev context.
- Issues requiring pre-existing local code execution on the host running Cortex.

## Handling process

1. **Triage** (within 72 hours): acknowledge and classify severity.
2. **Fix** (timeline depends on severity): develop a patch on a private branch.
3. **Disclosure**: coordinate a release and public advisory with the reporter credited (unless anonymity is requested).
4. **CVE**: request a CVE for issues affecting released versions where appropriate.
