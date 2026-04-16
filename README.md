<table>
  <tr>
    <td width="150"><img src="logo/cortex-logo.svg" alt="Cortex Agent Framework" width="130" /></td>
    <td>
      <h1>Cortex Agent Framework</h1>
      <strong>One YAML file. One method call. A production AI agent.</strong><br/>
      Stop rebuilding the same agent plumbing. Define it once, deploy anywhere, let it learn.
    </td>
  </tr>
</table>

<p align="center">
  <a href="https://pypi.org/project/cortex-agent-framework/"><img src="https://img.shields.io/pypi/v/cortex-agent-framework.svg" alt="PyPI version" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+" /></a>
  <img src="https://img.shields.io/badge/LLMs-8_providers-green.svg" alt="8 LLM providers" />
  <img src="https://img.shields.io/badge/MCP-native-purple.svg" alt="MCP native" />
</p>

<p align="center">
  <a href="docs/OVERVIEW.md">Overview</a> •
  <a href="docs/ARCHITECTURE.md">Architecture</a> •
  <a href="docs/FEATURES.md">Features</a> •
  <a href="docs/GETTING_STARTED.md">Getting Started</a> •
  <a href="docs/USE_CASES.md">Use Cases</a> •
  <a href="docs/CONFIGURATION.md">Config</a> •
  <a href="docs/CLI.md">CLI</a> •
  <a href="docs/DEPLOYMENT.md">Deployment</a> •
  <a href="docs/FAQ.md">FAQ</a>
</p>

---

## The problem

Every AI team builds the same stack: task decomposition, parallel tool execution, streaming, retries, session management, quality scoring, multi-provider routing, deployment. Most teams rebuild it two or three times before shipping.

**Cortex is that stack.** Pre-built. Battle-tested. Driven by config, not code.

---

## 3 commands. A running agent.

```bash
pip install cortex-agent-framework
cortex setup            # visual wizard at localhost:7799
cortex publish ui       # chat UI at localhost:8090
```

You now have a working agent with a professional web interface, file upload support, streaming responses, and persistent chat history. No frontend to build. No backend to wire. No infrastructure to manage.

---

## Define your agent in YAML. Run it in Python.

```yaml
agent:
  name: ResearchAgent
  description: Searches the web and writes reports

llm_access:
  default:
    provider: anthropic
    model: claude-sonnet-4-5
    api_key_env_var: ANTHROPIC_API_KEY

task_types:
  - name: web_research
    capability_hint: web_search
    output_format: md
  - name: write_report
    capability_hint: document_generation
    depends_on: [web_research]
```

```python
from cortex.framework import CortexFramework

framework = CortexFramework("cortex.yaml")
await framework.initialize()

result = await framework.run_session(
    user_id="user_1",
    request="Research the latest vector DB benchmarks and write a report",
)
print(result.response)
```

Fan-out, tool calls, dependency resolution, synthesis, validation — all handled.

---

## Why teams choose Cortex

### Skip months of framework engineering

The orchestration, parallelism, tool integration, streaming, retries, validation, session persistence, and deployment pipeline — it's all in the box. Your Python code stays a thin wrapper. The agent's behavior lives in `cortex.yaml`, versioned, diffable, reviewable.

### Multi-agent composition for free

Any Cortex agent becomes an MCP server in one command. Other agents consume it as a tool. That's the entire inter-agent protocol — standard MCP, nothing custom.

```
Orchestrator → Research Agent (MCP :8081) → brave-search, wikipedia
             → Code Review Agent (MCP :8082) → github, filesystem
             → Writing Agent (MCP :8083) → document-gen
```

Each agent scales, deploys, and configures independently. Compose them by adding YAML lines.

### Built-in quality gates

Every response passes through a **Validation Agent** that scores intent match, completeness, and coherence. You set a floor (default: 0.75); responses below it are flagged and remediated. Bad intermediate outputs are retried with feedback before the pipeline moves on.

Catch regressions before users do, not after.

### Your agent gets smarter over time

The **Learning Engine** observes task patterns across sessions. When patterns recur, it stages a **delta proposal** — a concrete config change you review with `cortex delta review` and apply in one command. The agent doesn't silently drift; it surfaces what it learned and asks for approval.

---

## What's in the box

| | |
|---|---|
| **8 LLM providers** | Anthropic, OpenAI, Gemini, Grok, Mistral, DeepSeek, AWS Bedrock, Azure AI — swap with one YAML line |
| **Fan-out / fan-in** | LLM-generated DAG with parallel execution; independent tasks run simultaneously |
| **MCP-native tools** | First-class SSE, stdio, and streamable-HTTP MCP tool servers |
| **Multi-agent mesh** | Publish any agent as an MCP server — compose specialist agents into an orchestrator |
| **Quality validation** | Every response scored and gated; per-task validation inside the execution loop |
| **Delta learning** | Agent proposes improvements; human-in-the-loop review before apply |
| **Blueprints** | Reusable workflow knowledge loaded into context, auto-updated with consent |
| **Streaming** | Typed event classes (`StatusEvent`, `ResultEvent`, `ClarificationEvent`) for any UI |
| **Per-task LLM routing** | Route decomposition to a fast model, synthesis to flagship |
| **Session persistence** | Memory / SQLite / Redis with WAL replay and resumable sessions |
| **Built-in chat UI** | Web frontend with file uploads, streaming, and conversation history |
| **4 deploy targets** | `publish docker`, `publish package`, `publish mcp`, `publish ui` |
| **Visual setup wizard** | Configure everything from a browser — `cortex setup` |
| **Security** | Input sanitisation, credential scrubbing, sandboxed code execution, MCP output guard |
| **Observability** | OpenTelemetry, audit logs, anomaly detection, token budgets |

---

## How Cortex compares

| Capability | Cortex | Typical agent frameworks |
|---|---|---|
| **Configuration** | Single `cortex.yaml` drives everything | Scattered code, env vars, multiple config files |
| **Task orchestration** | LLM-generated DAG with parallel fan-out/fan-in | Sequential chain or hand-coded state machine |
| **Tool protocol** | Native MCP (SSE, stdio, streamable-HTTP) | Custom tool wrappers per integration |
| **Multi-agent** | Any agent becomes an MCP tool in one command | Bespoke inter-agent protocols |
| **Quality gates** | Built-in validation with scoring + remediation | Manual testing or nothing |
| **Learning** | Delta proposals + blueprints with human review | Prompt tweaking by hand |
| **LLM providers** | 8 built-in, swap via config | Usually 1-2, hard-coded |
| **Deployment** | 4 targets, one command each | Write your own Dockerfile |

---

## Who is Cortex for?

| You are... | Cortex gives you... |
|---|---|
| **Startup founder** shipping an AI product | A production agent runtime in an afternoon — skip 3-6 months of plumbing |
| **Platform team** at a larger company | A governed agent runtime with audit trails, quality gates, and per-user isolation |
| **Enterprise architect** | Multi-agent meshes with independent scaling and compliance-friendly history encryption |
| **Solo developer** | Prototype to production with one YAML file |
| **Researcher** | Swap providers, models, and tools from config — run experiments without touching code |
| **MLOps engineer** | Validation scores, session replay, token accounting, and OpenTelemetry out of the box |

---

## What Cortex is *not*

- **Not a low-code builder.** It's a Python library. The config replaces boilerplate, not code.
- **Not an LLM gateway.** Bring your own API key.
- **Not a vector database.** It calls MCP tools that do RAG — it doesn't implement retrieval itself.
- **Not a web framework.** Cortex runs *inside* FastAPI/Django/Flask/Click.

---

## Documentation

| Document | Read this if you want to... |
|---|---|
| **[Overview](docs/OVERVIEW.md)** | ...understand what Cortex is, who it's for, and why it exists |
| **[Architecture](docs/ARCHITECTURE.md)** | ...see the internals: primary agent, task graph, MCP agents, validation |
| **[Features](docs/FEATURES.md)** | ...scan the full feature matrix |
| **[Getting Started](docs/GETTING_STARTED.md)** | ...build your first agent with working code |
| **[Use Cases](docs/USE_CASES.md)** | ...see real-world scenarios and reference architectures |
| **[Configuration](docs/CONFIGURATION.md)** | ...look up every `cortex.yaml` field |
| **[CLI Reference](docs/CLI.md)** | ...look up every `cortex` subcommand |
| **[Deployment](docs/DEPLOYMENT.md)** | ...ship to production |
| **[FAQ](docs/FAQ.md)** | ...find answers to common gotchas |
| **[Contributing](CONTRIBUTING.md)** | ...report bugs or submit PRs |

---

## Community & support

- **Issues**: file bugs and feature requests on [GitHub Issues](https://github.com/kritird/Cortex-Agent-Framework/issues)
- **Discussions**: ask questions on [GitHub Discussions](https://github.com/kritird/Cortex-Agent-Framework/discussions)
- **Security**: report vulnerabilities privately, not in public issues

---

## License

MIT — see [LICENSE](LICENSE). Use it commercially, fork it, ship it.

**Define once. Deploy anywhere. Let it learn.**
