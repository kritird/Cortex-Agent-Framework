# Use Cases

[← Back to README](../README.md)

Real-world scenarios where Cortex earns its place in your stack. This document has two parts:

- **[Validated Industry Use Cases](#validated-industry-use-cases)** — working examples run against a live LLM (Gemma 4 via Ollama) as part of the framework's User Acceptance Test suite. Each includes the exact `cortex.yaml` task shape and the request that was sent.
- **[Architecture Patterns](#architecture-patterns)** — deployment shapes for common integration scenarios.

---

## Validated Industry Use Cases

These examples are drawn directly from [`tests/test_uat_industry_acceptance.py`](../tests/test_uat_industry_acceptance.py) — every config, task description, and assertion was executed against a live LLM and passed. Copy and adapt them as starting points for your own agents.

---

### Education — Adaptive Tutoring Agent

**Scenario:** A high-school student asks a tutoring agent to explain Python lists and loops, then work through a practice exercise.

**What was tested:** multi-task pipeline (assess → explain → exercise → solution), code sandbox execution (Fibonacci to n=20), multi-student session isolation, history persistence across sessions.

**Task layout:**

```yaml
task_types:
  - name: assess_prior_knowledge
    description: >
      Assess the student's prior knowledge based on their question.
      Identify what they already know and what gaps exist.
      Output a brief structured assessment.

  - name: explain_concept
    description: >
      Provide a clear, age-appropriate explanation of the concept.
      Use analogies and concrete examples.
      Break complex ideas into digestible steps.

  - name: generate_practice_exercise
    description: >
      Create a practical coding exercise that reinforces the concept.
      Include problem statement, expected input/output, and a worked example.
      The exercise must be completable in Python in under 20 lines.

  - name: write_solution_code
    description: >
      Write a complete, runnable Python solution to the exercise.
      The code must be self-contained, print the result, and include
      inline comments explaining each step.
```

**Example request:**

```
I'm a high school student learning Python for the first time.
Can you help me understand what a list is and how loops work?
I want to practice with a real coding exercise.
```

**Verified output:** Response covered `list`, `loop`, `python`, `exercise`, `example`, `code`. Three tasks completed in sequence. Session history written and retrievable.

**Full cortex.yaml for code execution variant:**

```yaml
agent:
  name: AdaptiveTutorAgent
  description: Personalized Python tutoring with code execution
  intent_gate:
    enabled: false
  time:
    default_max_wait_seconds: 300
    default_task_timeout_seconds: 240

llm_access:
  default:
    provider: local          # or openai / anthropic
    model: gemma4:e4b        # swap for any model
    base_url: http://localhost:11434/v1
    max_tokens: 1024

code_sandbox:
  enabled: true
  timeout_seconds: 90
  allow_network: false

storage:
  base_path: ./cortex_storage

history:
  enabled: true
  max_sessions_in_context: 5
  retention_days: 90

task_types:
  - name: design_algorithm
    description: >
      Design and describe the algorithm for the coding problem.
      Explain the approach, time complexity, and edge cases.

  - name: write_and_run_code
    description: >
      Write a complete Python solution implementing the algorithm.
      Return the exact code that was written and executed.
```

---

### Healthcare — Clinical Triage & Decision Support

**Scenario:** A nurse practitioner submits a patient presentation; the agent triages urgency, recommends a care pathway, and produces a clinical handoff summary.

**What was tested:** urgency classification for acute neurological symptoms (thunderclap headache, meningism), cardiac chest pain triage, diabetes crisis management, validation gate enforcing clinical quality floor (≥ 0.65), markdown artifact output for handoff docs.

**Task layout:**

```yaml
task_types:
  - name: symptom_analysis
    description: >
      Analyse the presented symptoms systematically.
      Identify red flags, likely differential diagnoses (top 3), and
      the most critical condition to rule out first.
      Output: RED_FLAGS: ..., DIFFERENTIALS: ..., PRIORITY_DDX: ...

  - name: urgency_classification
    description: >
      Classify the urgency level: CRITICAL / HIGH / MODERATE / LOW.
      Justify the classification with reference to specific symptoms.
      Output: URGENCY: <level>, RATIONALE: ...
    depends_on: [symptom_analysis]

  - name: care_pathway
    description: >
      Recommend an immediate care pathway: investigations, interventions,
      and escalation criteria. Be specific and actionable.
    depends_on: [urgency_classification]

  - name: clinical_handoff_summary
    description: >
      Write a structured clinical handoff summary suitable for the
      receiving team. Include: patient demographics, presenting complaint,
      assessment, plan, and pending results.
    depends_on: [care_pathway]
    output_format: md
    validation_notes: >
      Summary must include urgency level, at least two differential
      diagnoses, and specific next steps.
```

**Example request:**

```
Patient: 45-year-old male. Symptoms: sudden onset severe headache
'worst of his life', neck stiffness, sensitivity to light,
temperature 38.8°C. Onset: 2 hours ago. No prior history. Please triage.
```

**Verified output:** Response contained `urgent`/`critical`/`emergency` signals. ≥2 tasks completed. Validation gate scored clinical outputs and enforced quality threshold.

**Key config for regulated environments:**

```yaml
validation:
  enabled: true
  threshold: 0.65       # reject responses below this quality floor
  timeout_seconds: 30

history:
  enabled: true
  retention_days: 365   # full-year audit trail

learning:
  enabled: true
  require_user_identity: true   # only learn from authenticated clinicians
  confidence_threshold: high
```

---

### Financial Analysis — Portfolio Risk & Stock Screening

**Scenario:** A wealth management firm wants an agent that assesses portfolio concentration risk, runs VaR calculations in Python, and produces a client-ready executive report.

**What was tested:** four-task financial pipeline (risk assessment → VaR code execution → rebalancing recommendations → executive report), stock screening model with Python scoring, token usage accounting per session.

**Task layout:**

```yaml
task_types:
  - name: portfolio_risk_assessment
    description: >
      Analyse the portfolio composition and assess concentration risk.
      Calculate sector weightings. Identify top 3 risk factors.
      Output: SECTOR_WEIGHTS: ..., TOP_RISKS: ..., CONCENTRATION_SCORE: 0-10.

  - name: compute_var_metrics
    description: >
      Write and execute Python code to compute a simplified Value-at-Risk (VaR)
      using historical simulation. Portfolio value $1M, tech 68%, healthcare 20%,
      cash 12%. Daily volatility 2.1%.
      Compute 1-day VaR at 95% and 99% confidence. Print results clearly.

  - name: investment_recommendation
    description: >
      Based on the risk assessment, provide 3 actionable rebalancing
      recommendations with expected impact on portfolio risk.
      Format as a numbered list.

  - name: executive_report
    description: >
      Write a concise executive investment report (300-500 words) summarising
      the portfolio risk, VaR metrics, and rebalancing recommendations.
      Address it to a wealth management client.
    output_format: md
    validation_notes: >
      Report must mention VaR, at least two specific percentage figures,
      and provide numbered recommendations.
```

**Example request:**

```
Analyse our tech-heavy portfolio: 68% technology (AAPL, MSFT, NVDA, META),
20% healthcare (JNJ, PFE), 12% cash. Total value $1M.
Assess concentration risk, compute VaR, and recommend rebalancing.
Produce an executive summary for our investment committee.
```

**Verified output:** VaR figures at 95% and 99% confidence computed and printed. Executive report produced as `.md` artifact. Token usage per session populated for cost attribution.

---

### Scientific Research — Climate & Weather Modelling

**Scenario:** A research team needs an agent that synthesises climate literature, builds a Python temperature projection model, and produces a structured research report.

**What was tested:** literature synthesis pipeline, numerical Python code producing temperature projections, research report task completion metadata, multi-task dependency chains (research → model → report).

**Task layout:**

```yaml
task_types:
  - name: literature_synthesis
    description: >
      Synthesise key findings on climate change projections.
      Cover: current global temperature anomaly, IPCC AR6 scenarios,
      tipping points, and regional impacts. Cite major concepts.

  - name: build_projection_model
    description: >
      Write Python code that models temperature increase under two scenarios:
      business-as-usual (+3.5°C by 2100) and aggressive mitigation (+1.5°C).
      Compute decade-by-decade projections from 2020 to 2100.
      Print a formatted table of results.
    depends_on: [literature_synthesis]

  - name: research_report
    description: >
      Write a structured research report with sections: Abstract,
      Key Findings, Projection Results, Policy Implications.
      Include numerical projections from the model.
    depends_on: [build_projection_model]
    output_format: md
```

**Example request:**

```
Research the climate crisis and build a temperature projection model.
Synthesise IPCC findings, model temperature trajectories under
business-as-usual vs. aggressive mitigation scenarios (2020-2100),
and produce a research report with policy recommendations.
```

---

### Software Engineering — Design → Implement → Test → Execute

**Scenario:** A senior engineering team uses an agent to design a data structure, implement it in Python, run it in a sandbox, and write unit tests — all in one pipeline session.

**What was tested:** BST implementation pipeline (plan → implement → execute → test), Python code sandbox execution, CI failure analysis and automated fix recommendations, code store persistence across sessions.

**Task layout:**

```yaml
task_types:
  - name: plan_solution
    description: >
      Design the algorithm and data structure for the coding task.
      Describe the approach, classes/functions needed, and test cases.

  - name: implement_solution
    description: >
      Write a complete, runnable Python implementation of a Binary Search Tree
      with insert(), search(), and inorder_traversal() methods.
      Include a __main__ block that inserts [5,3,7,1,4,6,8] and prints
      the inorder traversal (expected output: 1 3 4 5 6 7 8).
    depends_on: [plan_solution]

  - name: execute_and_validate
    description: >
      Execute the BST implementation. Capture the exact output.
      Verify the inorder traversal is correct (1 3 4 5 6 7 8).
      Return: EXECUTION_STATUS: PASS or FAIL, ACTUAL_OUTPUT: ..., VALIDATION: correct/incorrect.
    depends_on: [implement_solution]

  - name: write_test_suite
    description: >
      Write Python unit tests using unittest covering: insert, search
      (existing + missing keys), and inorder traversal correctness.
      Execute the tests and report results.
    depends_on: [implement_solution]
```

**Example request:**

```
You are a senior software engineer. Build a Binary Search Tree in Python.
Phase 1: Plan the algorithm and data structure.
Phase 2: Implement BST with insert, search, and inorder_traversal.
Phase 3: Execute it and verify output is: 1 3 4 5 6 7 8
Phase 4: Write and run unit tests. Report PASS/FAIL.
```

**Verified output (E1–E4 all passed):** Design, implementation, and test suite produced. Code executed in sandbox. CI failure analysis (`E3`) diagnosed a broken test and recommended a targeted fix. Code store populated and persisted after session end.

**Config for code-heavy agents:**

```yaml
code_sandbox:
  enabled: true
  timeout_seconds: 120
  allow_network: false

agent:
  concurrency:
    max_parallel_tasks: 2   # execute_and_validate + write_test_suite run in parallel
```

---

### Legal & Compliance — Contract Risk Analysis

**Scenario:** A legal team wants an agent that reviews contract clauses for risk, flags GDPR violations in data retention language, and produces a risk assessment within an SLA.

**What was tested:** contract risk identification pipeline, GDPR violation detection in retention clauses, session completion within defined time SLA, structured risk output format.

**Task layout:**

```yaml
task_types:
  - name: clause_extraction
    description: >
      Extract all clauses related to: liability, indemnification, termination,
      data retention, IP ownership, and governing law.
      Format each clause as: CLAUSE_TYPE | SECTION | VERBATIM_TEXT.

  - name: risk_identification
    description: >
      For each extracted clause, identify legal risks.
      Rate each risk: CRITICAL / HIGH / MEDIUM / LOW.
      Flag any clauses that may violate GDPR Article 5 (data minimisation,
      storage limitation) or standard commercial practice.
    depends_on: [clause_extraction]

  - name: negotiation_recommendations
    description: >
      For each CRITICAL or HIGH risk clause, propose specific redline language
      that mitigates the risk. Format as: ORIGINAL → PROPOSED REDLINE.
    depends_on: [risk_identification]

  - name: risk_summary
    description: >
      Write a one-page risk summary for the legal partner. Include:
      overall risk rating (RED/AMBER/GREEN), top 3 critical risks,
      and recommended negotiation priority.
    depends_on: [negotiation_recommendations]
    output_format: md
```

**Example request:**

```
Review this SaaS vendor contract for legal risks. Pay particular attention
to: unlimited liability clauses, auto-renewal terms, data retention
beyond 7 years, and any GDPR storage limitation violations.
Flag all CRITICAL and HIGH risks with proposed redline language.
```

---

### Synapse UI — Chat Interface with SSE Streaming

**Scenario:** A product team deploys the Cortex Synapse UI so business users can interact with the agent through a browser, with real-time streaming updates.

**What was tested (G1–G3):** POST `/api/session` creates a session and returns `ui_session_id`, SSE stream at `GET /api/session/{id}/events` emits live events during a Gemma4 run, session history retrievable via `GET /api/history` after completion.

**How to start the UI:**

```bash
cortex publish ui
# → http://localhost:7433
```

**Or embed in an existing aiohttp app:**

```python
from cortex.framework import CortexFramework
from cortex.ui.server import build_app
from aiohttp import web

fw = CortexFramework("cortex.yaml")
await fw.initialize()
app = build_app(fw)
web.run_app(app, port=7433)
```

**API surface:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/session` | POST (multipart) | Start a session; returns `ui_session_id` |
| `/api/session/{id}/events` | GET (SSE) | Stream live task events |
| `/api/history` | GET | Paginated session history |
| `/api/history/{session_id}` | GET | Single session detail |

**SSE event shape:**

```
data: {"type": "task_dispatch", "task_name": "portfolio_risk_assessment", ...}
data: {"type": "task_complete", "task_name": "portfolio_risk_assessment", "duration_ms": 55000}
data: {"type": "session_end", "validation_score": 0.82, "tasks_completed": 4}
data: {"type": "done"}
```

**Auth config:**

```yaml
agent:
  auth:
    mode: token          # or basic / none
    token: "${API_TOKEN}"
```

---

### ANT Colony — Specialist Agent Mesh

**Scenario:** A pharmaceutical research platform runs a mesh of specialist agents: one for target identification, one for molecular dynamics simulation. The orchestrator hatches them as ANTs and routes tasks to the right specialist.

**What was tested (H1–H6):** hatch and register specialist ANTs, event emission on hatch, persistence to `ants.yaml`, stop lifecycle, max-ANTs limit enforcement, multi-specialist parallel pipeline.

**Hatch ANTs programmatically:**

```python
from cortex.ants.ant_colony import AntColony

colony = AntColony(
    base_path="./ants",
    base_port=19600,
    max_ants=10,
    auto_restart=True,
)

await colony.hatch(
    name="drug-discovery-ant",
    capability="pharmaceutical_research",
    description="Specialist for target identification and lead optimisation",
)

await colony.hatch(
    name="molsim-ant",
    capability="molecular_dynamics",
    description="Specialist for MD simulation and binding affinity prediction",
)
```

**Or declare in cortex.yaml:**

```yaml
ants:
  base_path: ./ants
  base_port: 19600
  max_ants: 10
  auto_restart: true
  colony:
    - name: drug-discovery-ant
      capability: pharmaceutical_research
      description: Specialist for target identification

    - name: molsim-ant
      capability: molecular_dynamics
      description: Specialist for MD simulation
```

**When to use ANTs vs. task_types:**
- Use `task_types` for tasks within a single domain that share one LLM config.
- Use ANTs when specialists need different models, different tool servers, or should scale independently.

---

### Autonomic Learning — Agent That Improves Itself

**Scenario:** After each session, the framework automatically scores complexity, detects novel task patterns, and stages a delta proposal. Operators review and apply deltas to evolve the agent's blueprint without redeployment.

**What was tested (I1–I4):** `LearningEngine.stage_delta()` writes a `pending.yaml` proposal, `apply_delta()` creates a `.bak` backup before modifying `cortex.yaml`, learning events emitted in the session event queue, multiple sessions accumulate proposals.

**Config:**

```yaml
learning:
  enabled: true
  auto_apply_delta: false       # keep human-in-the-loop; set true for autonomous
  validation_threshold: 0.7     # only learn from high-quality sessions
  complexity_threshold: 0.4     # only learn from sufficiently complex sessions
  require_user_identity: true   # anonymous sessions never trigger learning
  confidence_threshold: high    # high | medium | low
```

**Review and apply staged deltas:**

```bash
# See what the agent learned
cortex delta list

# Preview a specific proposal
cortex delta show pending.yaml

# Apply to cortex.yaml (creates .bak backup automatically)
cortex delta apply pending.yaml --min-confidence high
```

**Learning lifecycle events in the session queue:**

```python
result, events = await fw.run_session(user_id="analyst_1", request="...", event_queue=q)

# After session ends, history_record.learned_action tells you what happened:
# "staged"              — new delta written to pending.yaml
# "applied"             — delta auto-applied (auto_apply_delta: true)
# "skipped_complexity"  — session was too simple to learn from
# "skipped_validation"  — validation score too low
# "skipped_rpc_anon"    — anonymous user, learning disabled
print(result.history_record.learned_action)
```

---

### Cross-Domain Policy Analysis — Multi-Industry Pipeline

**Scenario (Z5 — crown jewel test):** A G20 policy analyst asks an agent to simultaneously research AI's impact on Education, Financial Inclusion, and Healthcare Access; compute a Python-based AI Equity Index; and synthesise everything into a government policy brief.

**What was tested:** 5-task pipeline with fan-out (3 parallel domain analyses) → sequential synthesis → policy brief; code sandbox execution computing numerical scores; validation gate on the final brief; learning engine firing post-session; history written with full metadata.

**Task layout:**

```yaml
task_types:
  - name: education_impact_analysis
    description: >
      Analyse the impact of AI on education systems for underserved populations.
      Cover: personalised learning, teacher augmentation, equity gaps,
      and measurable outcomes from existing deployments.

  - name: financial_inclusion_analysis
    description: >
      Analyse AI's role in financial inclusion.
      Cover: credit scoring for unbanked populations, fraud detection,
      robo-advisory for low-income investors, and regulatory challenges.

  - name: healthcare_access_analysis
    description: >
      Analyse AI's impact on healthcare access.
      Cover: diagnostic AI in low-resource settings, telemedicine,
      drug discovery acceleration, and WHO digital health strategy.

  - name: compute_equity_score
    description: >
      Write and execute Python code that computes a simple AI Equity Index
      for three sectors (Education, Finance, Healthcare). Score each 0-100
      using: access_improvement (0-40), cost_reduction (0-30), risk_mitigation (0-30).
      Print the final scores and rank the sectors.
    depends_on:
      - education_impact_analysis
      - financial_inclusion_analysis
      - healthcare_access_analysis

  - name: policy_brief
    description: >
      Write a 500-word policy brief for government stakeholders on deploying AI
      equitably across education, finance, and healthcare. Include: problem statement,
      evidence-based recommendations (one per sector), AI Equity Index results,
      implementation roadmap, and risks.
    depends_on: [compute_equity_score]
    output_format: md
    validation_notes: >
      Brief must reference all three sectors, include numbered recommendations,
      and mention equity or inclusion.

agent:
  concurrency:
    max_parallel_tasks: 3   # the three domain analyses run simultaneously
```

**Example request:**

```
Analyse AI's impact across three critical domains — Education, Financial Inclusion,
and Healthcare Access — for underserved populations. Compute an AI Equity Index
scoring each sector. Produce a comprehensive policy brief for G20 ministers
recommending equitable AI deployment strategies.
```

**Verified output (Z5 passed):** All 5 tasks completed (3 in parallel, 2 sequential). Policy brief contained `education`, `finance`, `healthcare`, `equity`, `recommend`. Numerical equity scores computed by executed Python code. Learning engine fired post-session. History record written with `session_id`, `original_request`, `learned_action`.

---

## Architecture Patterns

### Pattern matching: which usage mode fits you?

| If you need… | Usage mode |
|---|---|
| A chat UI for end users | **Synapse UI** (`cortex publish ui` → port 7433) |
| An agent other agents call as a tool | **MCP Server** (`cortex publish mcp`) |
| A one-shot CLI tool for devs/ops | **CLI** (`cortex dev` or Click wrapper) |
| Batch processing of a job queue | **Background worker** (Celery/SQS + `fw.run_session()`) |
| AI feature inside an existing web app | **Embedded library** (`pip install cortex-agent-framework`) |
| Multi-tenant production service | **Docker + Redis storage** |
| A pre-configured agent for users to install | **Python package** (`cortex publish package`) |
| Specialist agents at different scales | **ANT Colony** (`AntColony.hatch()`) |

---

### 1. Customer Support Triage

**Agent layout:**
```
Inbound ticket → CortexFramework → triage result → ticket system
```

**Task types:** `classify` → `retrieve_docs` (MCP) + `severity_score` → `draft_reply`

**Why Cortex:** classify + retrieve_docs + severity_score run in parallel; draft_reply synthesises them. Validation catches bad drafts. Delta learning surfaces new ticket patterns automatically.

---

### 2. Competitive Intelligence Report

**Agent layout:**
```
Scheduled trigger → CortexFramework → Markdown report → Slack / email
```

**Task types:** `fetch_competitor_news` (fan-out N-ways via MCP) → `extract_features` → `compare_with_ours` → `write_brief`

**Why Cortex:** N-way fan-out over competitors runs in parallel. Session history lets PMs compare week-over-week. `cortex replay` gives a reproducible audit trail.

---

### 3. Internal Developer Tool ("Ask the Monorepo")

**Agent layout:**
```
Developer in IDE → Cortex MCP → answer with file:line references
```

**Task types:** `search_code` + `search_git_history` + `search_docs` → `synthesise_answer`

**Why Cortex:** Published as an MCP server (`cortex publish mcp`), it plugs into Claude Desktop, VS Code, Cursor, or any MCP-capable IDE. One YAML, every developer productive.

---

### 4. Compliance-Gated Enterprise Agent

**Why Cortex fits regulated industries (finance, healthcare, legal):**
- Every session is persisted with input, output, token usage, and validation scores.
- `cortex replay SESSION_ID --user-id USER_ID` reconstructs any past session exactly.
- Validation thresholds enforce a quality floor; low-scoring responses route to human review.
- Learning Engine is identity-gated — only learns from authenticated users.
- Delta learning is human-in-the-loop — no config changes go live without `cortex delta apply`.

**Deployment:** Docker + Redis storage in your regulated cloud, behind your existing audit logging pipeline.

---

### 5. Data Pipeline Enrichment Worker

**Agent layout:**
```
Job queue (SQS/Celery) → Cortex worker → enriched rows → data warehouse
```

**Task types:** `extract_specs` + `categorise` → `score_quality`

**Why Cortex:** Per-user concurrency limits prevent one job from starving the queue. Redis storage lets you run 20 workers in parallel. Validation catches hallucinated specs before they hit the warehouse. Token accounting tells finance what each row costs.

---

## Further reading

- [Getting Started](GETTING_STARTED.md) — first agent in 5 minutes
- [Configuration Reference](CONFIGURATION.md) — every YAML key explained
- [Architecture](ARCHITECTURE.md) — internal component diagram
- [Synapse UI](CORTEX_SYNAPSE.md) — full UI deployment guide
- [Deployment](DEPLOYMENT.md) — Docker, multi-agent, and cloud patterns
- [UAT Test Suite](../tests/test_uat_industry_acceptance.py) — 46 runnable acceptance tests
