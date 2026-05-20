"""
cortex/prompts.py — Central registry of all LLM prompt templates used by the framework.

Every string the framework sends to an LLM lives here as a named constant.
Templates use {placeholder} syntax; callers fill them with .format() or f-strings.
No logic lives in this file — only text.

Sections
--------
1.  Code Generation         (sandbox/code_sandbox.py)
2.  Capability Routing      (modules/generic_mcp_agent.py)
3.  Task Execution          (modules/generic_mcp_agent.py)
4.  Bash Command Generation (modules/generic_mcp_agent.py)
5.  Decomposition           (modules/primary_agent.py  — build_system_prompt)
6.  Conversation            (modules/primary_agent.py  — converse)
7.  Synthesis               (modules/primary_agent.py  — synthesise)
8.  File Summary            (modules/primary_agent.py  — _summarise_file_for_synthesis)
9.  Remediation             (modules/primary_agent.py  — remediate)
10. Blueprint               (modules/primary_agent.py  — generate_blueprint_updates)
11. Task-output Validation  (modules/primary_agent.py  — validate_task_output)
12. Replanning              (modules/primary_agent.py  — replan)
13. Session Validation      (modules/validation_agent.py)
14. Intent Gate             (modules/intent_gate.py)
15. Capability Scout        (modules/capability_scout.py)
16. Ant Colony              (ants/ant_colony.py)
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Code Generation
# ─────────────────────────────────────────────────────────────────────────────

CODE_GEN_SYSTEM = (
    "You are an expert polyglot programmer fluent in Python, JavaScript/Node.js, "
    "Shell, Ruby, and Go. Write clean, focused code. "
    "Return ONLY raw source code — no markdown fences, no explanation."
)

CODE_GEN_USER = """\
You are writing a script or program to accomplish a task.

TASK NAME: {task_name}
TASK DESCRIPTION: {description}
INSTRUCTION: {instruction}
OUTPUT FORMAT: {output_format}

Choose the best language for the task. Add a header comment to declare it:
  # LANGUAGE: python   (default — use when in doubt)
  # LANGUAGE: node     (JavaScript/Node.js)
  # LANGUAGE: shell    (Bash shell script)
  # LANGUAGE: ruby
  # LANGUAGE: go

────────────────────────────────────────────────────────
IF LANGUAGE IS PYTHON:
────────────────────────────────────────────────────────
Write a script with exactly ONE function:

    def run(input: dict) -> str:
        ...

Rules:
- The function receives a dict called `input` with any context data.
- The function must return a string (the result).
- If output_format is "md", return Markdown. If "json", return valid JSON string.
- You MAY import any standard library module including subprocess.
- You MAY use third-party packages — declare them at the top:
    # REQUIREMENTS: pandas, requests
- You MAY write files of any type (source code, binaries, images) to:
    os.path.join(input.get("output_dir", "."), "filename.ext")
  Only write inside input.get("output_dir").
- You MAY use subprocess to run programs you have written to output_dir, or
  to call system interpreters (python3, node, bash, etc.).
- Return a summary string when done. For file outputs return "File written to: <path>".

────────────────────────────────────────────────────────
IF LANGUAGE IS node / shell / ruby / go:
────────────────────────────────────────────────────────
Write a complete, self-contained script. It will be executed directly.
- Read CORTEX_TASK_INPUT env var for JSON task input.
- Write outputs to the directory in CORTEX_OUTPUT_DIR env var.
- Print your result to stdout.
- Exit 0 on success, non-zero on failure.

────────────────────────────────────────────────────────
BUILDING APPS:
────────────────────────────────────────────────────────
If the task requires building a runnable application (GUI app, server, CLI tool):
- Write the app source files into output_dir.
- For Python: you may use tkinter, PyQt5/6, Flask, FastAPI, etc.
- For Node.js: you may use Express, Electron stubs, etc.
- Launch or run the app using subprocess (Python) or exec (Node.js/shell).
- Return a result describing what was built and where it lives.

Return ONLY the source code, no explanation, no markdown fences.
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1b. App Control
# ─────────────────────────────────────────────────────────────────────────────

APP_CONTROL_SYSTEM = (
    "You are an app-control dispatcher. Given a task instruction, emit structured "
    "action blocks telling the AppControl tool exactly what to do. "
    "Reply with ONLY the action blocks — no prose, no explanation."
)

# Used when no scripting dictionary was found (generic free-form instructions)
APP_CONTROL_USER = """\
You are controlling a native application on the host machine.

TASK: {instruction}
PLATFORM: {platform}

Emit one or more action blocks. Separate multiple blocks with ---.
Each block uses these fields (omit unused ones):

ACTION: <launch_app | run_applescript | run_powershell | run_shell_command | screenshot | get_running_apps | get_window_text | copy_to_clipboard | paste_from_clipboard>
APP: <app name — for launch_app or get_window_text>
SCRIPT: <full AppleScript or PowerShell/shell script body>
COMMAND: <shell command — for run_shell_command>
TEXT: <text to copy — for copy_to_clipboard>
OUTPUT_PATH: <absolute .png path — for screenshot>

Rules:
- macOS: use run_applescript, or System Events UI scripting for apps without a scripting dict.
- Windows: use run_powershell for COM/UIA automation.
- Linux: use run_shell_command with xdotool / wmctrl.
- Keep SCRIPT compact — one logical block.
- For screenshots set OUTPUT_PATH inside the session output dir.
- Use copy_to_clipboard / paste_from_clipboard to move data between apps when scripting APIs are limited.
"""

# Used when a scripting dictionary was successfully discovered
APP_CONTROL_WITH_CAPS_USER = """\
You are controlling a native application on the host machine.
The application's scripting interface has been discovered for you below —
use it to generate precise, correct automation commands.

TASK: {instruction}
PLATFORM: {platform}

APPLICATION SCRIPTING INTERFACE:
{capabilities}

Using the interface above, emit one or more action blocks. Separate with ---.
Each block uses these fields (omit unused ones):

ACTION: <launch_app | run_applescript | run_powershell | run_shell_command | screenshot | get_running_apps | get_window_text | copy_to_clipboard | paste_from_clipboard>
APP: <app name — for launch_app or get_window_text>
SCRIPT: <full AppleScript or PowerShell/shell script body>
COMMAND: <shell command — for run_shell_command>
TEXT: <text to copy — for copy_to_clipboard>
OUTPUT_PATH: <absolute .png path — for screenshot>

Rules:
- Use only commands, classes, and properties listed in the scripting interface above.
- For macOS: wrap AppleScript in `tell application "{app_name}" ... end tell`.
  For UI scripting use `tell application "System Events" to tell process "{app_name}" ...`.
- For Windows: use the COM interface or UIA tree as shown above.
- If you need to read a result (e.g. a calculation output), use get_window_text or
  read a property from the scripting interface before returning.
- Chain steps with --- separators when order matters.
"""

# Used at each step of the screenshot vision loop
APP_CONTROL_VISION_USER = """\
You are controlling the application "{app_name}" by observing screenshots.

ORIGINAL TASK: {instruction}
PLATFORM: {platform}
STEP: {step} of {max_steps}

ACTIONS TAKEN SO FAR:
{history}

Look at the screenshot carefully. Decide the SINGLE next action to complete the task.

If the task is already complete (you can see the result), reply with:
  DONE: <the result or answer extracted from the screen>

Otherwise reply with exactly one action block:

ACTION: <run_applescript | run_powershell | run_shell_command | launch_app>
SCRIPT: <script body>     (for applescript / powershell / shell)
COMMAND: <command>        (alternative to SCRIPT for run_shell_command)
APP: <app name>           (for launch_app)

Rules:
- One action only — do not chain multiple steps in a single response.
- If you see an error on screen, adapt your next action to fix it.
- If you see a system dialog or permission prompt (e.g. "Allow access?", "Save changes?",
  unexpected modal), return an action that dismisses or responds to it before continuing
  with the original task. Common dismissals:
    * macOS: `tell application "System Events" to keystroke return`
    * Windows: `[System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}")`
- If the app is not open yet, emit a launch_app action first.
- macOS: prefer run_applescript with System Events UI scripting to click buttons or read values.
- Windows: prefer run_powershell with UI Automation or SendKeys for interaction.
"""

# ─────────────────────────────────────────────────────────────────────────────
# 2. Capability Routing
# ─────────────────────────────────────────────────────────────────────────────

CAPABILITY_ROUTER_SYSTEM = (
    "You are a capability router. Reply with a single capability name only."
)

# {capability_menu} — formatted capability list built at runtime
# {task_name}, {task_description}, {instruction_excerpt}
CAPABILITY_ROUTER_USER = """\
You are a capability router for an AI agent.

{capability_menu}

TASK NAME: {task_name}
TASK DESCRIPTION: {task_description}
INSTRUCTION (truncated): {instruction_excerpt}

Choose the single best capability from the list above for this task.
Rules:
- Prefer workspace_bash when the task reads, writes, or executes files inside the user's own project workspace.
- Prefer bash when the task needs a simple shell command outside the user's workspace.
- Prefer code_exec when the task requires generating and running Python code or producing output files in a sandbox.
- Prefer web_search when the task needs current information from the internet.
- Prefer llm_synthesis for writing, analysis, summarisation, or reasoning with no external tool needed.
- Prefer an MCP tool-server capability when a specialised registered server matches the task.
- If truly unsure, respond with llm_synthesis.

Respond with ONLY the capability name — a single word or short phrase, nothing else.\
"""

# ─────────────────────────────────────────────────────────────────────────────
# 3. Task Execution  (GenericMCPAgent._call_llm)
# ─────────────────────────────────────────────────────────────────────────────

# {task_name}, {caps_note}, {output_format}, {description}
TASK_EXEC_SYSTEM = (
    "You are executing a '{task_name}' task as part of an AI agent.{caps_note} "
    "Output format: {output_format}. "
    "{description} "
    "Generate the requested content directly and completely. "
    "If the task involves creating a document, PDF, report, or any file, "
    "produce the full content as your output — the framework handles saving it to disk. "
    "Never refuse by saying you cannot create files or access the internet; "
    "just produce the best output you can for this task."
)

# Appended to TASK_EXEC_SYSTEM when agent.inject_session_context is enabled.
# {session_goal} — the original user request; {scratchpad_line} — either an
# empty string or a "\n\nSession reasoning so far:\n..." block.
TASK_EXEC_SESSION_CONTEXT = (
    "\n\n## Session Context\n"
    "Your task is one step of a larger session. The user's overall request is:\n"
    "{session_goal}\n"
    "Stay consistent with this goal and the surrounding work. Produce output "
    "for YOUR task only — do not attempt the whole request.{scratchpad_line}"
)

TASK_EXEC_SESSION_SCRATCHPAD_LINE = (
    "\n\nReasoning accumulated by the planner so far (confirmed facts, open "
    "questions, strategy) — treat as authoritative context:\n{scratchpad}"
)

TASK_EXEC_HITL_SUFFIX = (
    "\n\n## Human-in-the-Loop\n"
    "If anything in the task is ambiguous or you are missing information "
    "you need to proceed confidently, DO NOT GUESS. Instead, ask the user a "
    "single focused question by emitting EXACTLY this tag and then stopping "
    "your output immediately:\n"
    "<ask_human>your concise question here</ask_human>\n"
    "The system will pause execution, get the answer, and restart you with "
    "the answer included in the conversation. You may ask up to 3 questions "
    "per attempt. Only ask when necessary; prefer acting on clear instructions."
)

# ─────────────────────────────────────────────────────────────────────────────
# 3b. ReAct Loop  (modules/react_loop.py — driven by GenericMCPAgent)
# ─────────────────────────────────────────────────────────────────────────────

# One-line descriptions for the built-in actions a sub-agent can take inside
# the ReAct loop. MCP tool-server capabilities are appended to this menu at
# runtime. Keep each line short — the whole menu goes into every reasoning call.
REACT_BUILTIN_ACTIONS: dict[str, str] = {
    "llm_synthesis":  "Reason, write, summarise, or generate text/code/markdown with the LLM (no external tool).",
    "web_search":     "Search the web for current/live information and return results.",
    "code_exec":      "Generate and run code in a sandbox (computation, data processing, producing output files).",
    "bash":           "Run a single shell command in a restricted sandbox.",
    "workspace_bash": "Read, write, list, or execute files inside the user's own project workspace.",
    "app_control":    "Launch and drive a native desktop application.",
    "forge_mcp":      "Generate a new MCP tool-server script for a capability that does not exist yet.",
    "ask_user":       "Ask the user one focused clarification question and wait for the answer.",
}

# {task_name}, {description}, {output_format}, {action_menu}
REACT_SYSTEM = """\
You are a sub-agent executing ONE task inside a larger AI agent. The overall
request was already decomposed by a planner — your job is to fully complete the
single task below, nothing more.

You work in a reason -> act -> observe loop: think about the current state, take
exactly ONE action, read its observation, then repeat until the task is done.

TASK: {task_name}
WHAT THIS TASK IS: {description}
REQUIRED OUTPUT FORMAT: {output_format}

## Available actions
{action_menu}
  - finish: End the loop and return the completed task output.

## How to respond
Reply with EXACTLY ONE JSON object and nothing else — no prose, no markdown
fences. Two shapes are allowed.

To take an action:
{{"thought": "<what the observations so far tell you, and why this action next>",
  "action": "<one action name from the list above>",
  "action_input": "<a complete, self-contained instruction for that action>",
  "expectation": "<what you expect this action to produce or prove>"}}

When the task is fully complete:
{{"thought": "<why the task is now done and the answer is correct>",
  "action": "finish",
  "final_answer": "<the complete task output, in the required output format>"}}

## Rules
- One action per response. Never chain or batch actions.
- action_input must stand on its own — the action executor sees ONLY that
  string, not this conversation. Restate any path, query, or detail it needs.
- Each turn, first check the latest observation against the expectation you
  stated for it. If they diverge, adapt before moving on.
- If an observation reports an error, fix the input and retry, or pick a
  different action. Do not give up after one failure.
- Do not take an action just to re-confirm something you already know. Call
  "finish" as soon as the task is genuinely complete.
- final_answer is the ONLY thing the rest of the system keeps. It must be
  complete and correct on its own, in the required output format.
- Never refuse by claiming you cannot run code or reach the internet — use the
  actions above.
"""

# {instruction}
REACT_USER_INITIAL = """\
Complete this task:

{instruction}

Begin the reason -> act -> observe loop. Respond with your first JSON object.\
"""

# {step}, {action}, {expectation}, {observation}
REACT_OBSERVATION = """\
[Observation — step {step}, action: {action}]
You expected: {expectation}

Result:
{observation}

Check this result against your expectation, then decide the next step. If the
task is now fully complete, respond with action "finish" and a complete
final_answer. Otherwise choose the next action.\
"""

# {step}, {action}, {valid_actions}
REACT_UNKNOWN_ACTION = """\
[Error — step {step}]
"{action}" is not a valid action. Choose exactly one of: {valid_actions}.
Respond again with a single valid JSON object.\
"""

# {max_iterations}
REACT_FORCE_FINAL = """\
You have reached the maximum number of steps ({max_iterations}) for this task.
Stop calling actions. Using everything observed so far, respond NOW with a
single JSON object using action "finish" and the most complete, correct
final_answer you can produce in the required output format.\
"""

# Prepended to the digest of compacted older steps when the running context
# grows past the configured budget.
REACT_COMPACTION_HEADER = (
    "\n\n## Earlier steps (compacted to save context)\n"
    "These reason/act/observe steps already happened — treat them as settled "
    "history:\n"
)

# ─────────────────────────────────────────────────────────────────────────────
# 4. Bash Command Generation
# ─────────────────────────────────────────────────────────────────────────────

BASH_CODEGEN_SYSTEM = "Output only the bash command, nothing else."

# {instruction}
BASH_CODEGEN_USER = (
    "Convert the following task description to a single executable "
    "bash command (no explanation, just the command):\n\n{instruction}"
)

# ─────────────────────────────────────────────────────────────────────────────
# 5. Decomposition  (build_system_prompt static fragments)
# ─────────────────────────────────────────────────────────────────────────────

DECOMP_PREBUILT_SCRIPTS_HEADER = "## Pre-built Agent Scripts"
DECOMP_PREBUILT_SCRIPTS_INTRO = (
    "These task names have tested, persisted code that runs without LLM generation. "
    "Prefer them over other options when they match the request:"
)

DECOMP_TASK_TYPES_HEADER = "## Available Task Types"

DECOMP_MCP_TOOLS_HEADER = "## Discovered MCP Tools"
DECOMP_MCP_NO_TYPES_INTRO = (
    "No predefined task types are configured. "
    "Use the tool names below as task names when decomposing."
)
DECOMP_MCP_WITH_TYPES_INTRO = (
    "The following tools are available in addition to the predefined task types above."
)

DECOMP_CAPABILITIES_HEADER = "## Available Capabilities"
DECOMP_CAPABILITIES_SELECT = "Choose the capability that best matches each task's needs:"

# Descriptions shown next to each capability name in the decomposition prompt.
DECOMP_CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    "web_search":          "Search the internet, look up live data (weather, news, prices, etc.)",
    "llm_synthesis":       "Reason, write, summarise, generate text/code/documents — no live data",
    "workspace_bash":      "Read, write, or execute files in the user's workspace directory",
    "bash":                "Run shell commands in a sandboxed environment",
    "code_exec":           "Generate and run Python code in a sandbox",
    "document_generation": "Create structured documents (PDF, DOCX, reports)",
    "image_generation":    "Generate or manipulate images",
}

DECOMP_CAPABILITIES_GUIDANCE = (
    "IMPORTANT: Use 'web_search' for ANY task needing live/current information "
    "(weather, news, prices, recent events). Use 'workspace_bash' for creating "
    "or editing files. Use 'llm_synthesis' only for pure reasoning/writing with "
    "no live data or file I/O needed."
)

DECOMP_BLUEPRINT_HEADER = "## Task Blueprints"
DECOMP_BLUEPRINT_INTRO = (
    "The following blueprints capture dos/don'ts, clarifications, and "
    "lessons from prior runs of these tasks. Treat them as authoritative "
    "guidance unless the user's request explicitly overrides them."
)

DECOMP_FORMAT_HEADER = "## Decomposition Output Format"
DECOMP_FORMAT_INTRO = "Decompose the user request into tasks. For each task, output a block:"

# Task block formats
DECOMP_FORMAT_BLOCK_WITH_AMR = [
    "```",
    "<task>",
    "  <name>task_type_name</name>",
    "  <capability>capability_name</capability>",
    "  <model_tier>low|medium|high</model_tier>",
    "  <instruction>specific instruction for this task</instruction>",
    "  <depends_on>comma_separated_task_names_or_empty</depends_on>",
    "</task>",
    "```",
    "Set <capability> to the best matching capability from the Available Capabilities list.",
    "",
    "## Model Tier Assessment",
    "For <model_tier>, assess each task's inherent complexity independently and objectively:",
    "  low    — direct retrieval, format conversion, short text generation, single-fact lookup,",
    "           simple translation, data extraction from a clearly structured source.",
    "  medium — multi-step reasoning, moderate code generation (< ~100 lines), single-document",
    "           analysis, structured writing with a defined template, data aggregation.",
    "  high   — complex architecture design, multi-file code generation, deep research synthesis,",
    "           long-form content (> 1000 words), advanced algorithms, cross-domain reasoning.",
    "Assess based solely on the task's own requirements — not on the capabilities of any model.",
]

DECOMP_FORMAT_BLOCK_WITHOUT_AMR = [
    "```",
    "<task>",
    "  <name>task_type_name</name>",
    "  <capability>capability_name</capability>",
    "  <instruction>specific instruction for this task</instruction>",
    "  <depends_on>comma_separated_task_names_or_empty</depends_on>",
    "</task>",
    "```",
    "Set <capability> to the best matching capability from the Available Capabilities list.",
]

DECOMP_FORMAT_SUFFIX = "Output ALL task blocks before any other text."

DECOMP_CLARIFICATION_HEADER = "## Clarification"
DECOMP_CLARIFICATION_INTRO = (
    "If the request is ambiguous and you need clarification before proceeding, output:\n"
    "<clarification>Your question here</clarification>\n"
    "Wait for the user's response before proceeding with decomposition."
)

DECOMP_SYNTHESIS_GUIDANCE_HEADER = "## Synthesis Guidance"

# ─────────────────────────────────────────────────────────────────────────────
# 6. Conversation  (primary_agent.converse)
# ─────────────────────────────────────────────────────────────────────────────

CONVERSE_INTRO = (
    "You are replying to a conversational turn from the user. "
    "Answer directly and concisely. Do not pretend to execute a task. "
    "If the user asks what you can do, describe the capabilities and "
    "task types listed below in plain language."
)

# ─────────────────────────────────────────────────────────────────────────────
# 7. Synthesis  (primary_agent.synthesise)
# ─────────────────────────────────────────────────────────────────────────────

# {agent_name}
SYNTHESIS_SYSTEM_WITH_RESULTS = (
    "You are {agent_name}. "
    "Synthesise the task results into a complete, coherent response for the user. "
    "Use the task summaries provided — do not invent information not present in the summaries. "
    "Do NOT cite internal task IDs, task names, or task summary references in your response. "
    "If you reference a source, use the actual URL, document title, or resource name — "
    "never internal labels like task IDs."
)

# {agent_name}, {agent_description}
SYNTHESIS_SYSTEM_DIRECT = (
    "You are {agent_name}. {agent_description} "
    "Respond directly and concisely to the user's request below."
)

# ─────────────────────────────────────────────────────────────────────────────
# 8. File Summary  (primary_agent._summarise_file_for_synthesis)
# ─────────────────────────────────────────────────────────────────────────────

FILE_SUMMARY_SYSTEM = (
    "You are a precise summariser. Output only the summary, no preamble."
)

# {task_label}, {instruction_excerpt}, {file_path}
FILE_SUMMARY_USER = (
    "Task: {task_label}\n"
    "Instruction: {instruction_excerpt}\n\n"
    "File path: {file_path}\n\n"
    "Summarise the content of this file in the context of the task above. "
    "Surface the most decision-relevant facts only. Be concise (≤150 words)."
)

# ─────────────────────────────────────────────────────────────────────────────
# 9. Remediation  (primary_agent.remediate)
# ─────────────────────────────────────────────────────────────────────────────

# {agent_name}
REMEDIATE_SYSTEM = "You are {agent_name}. Improve the response as directed."

# {original_request}, {original_response}, {findings_text}, {prior_attempts_block}
# prior_attempts_block is empty on the first pass; on later passes it carries
# the earlier remediation attempt(s) and the findings each one still failed,
# so the model does not repeat a correction that already proved insufficient.
REMEDIATE_USER = (
    "The following response to the user request needs improvement:\n\n"
    "USER REQUEST:\n{original_request}\n\n"
    "ORIGINAL RESPONSE:\n{original_response}\n\n"
    "QUALITY ISSUES FOUND:\n{findings_text}\n"
    "{prior_attempts_block}\n"
    "Please provide a corrected response that addresses all the issues above. "
    "Return only the corrected response, no meta-commentary."
)

# Rendered into REMEDIATE_USER once per earlier remediation pass.
# {n}, {attempt_response}, {attempt_findings}
REMEDIATE_PRIOR_ATTEMPT = (
    "\nREMEDIATION ATTEMPT {n} (still did not pass — do not repeat its mistakes):\n"
    "{attempt_response}\n"
    "Issues that remained after attempt {n}:\n{attempt_findings}\n"
)

# ─────────────────────────────────────────────────────────────────────────────
# 10. Blueprint  (primary_agent.generate_blueprint_updates)
# ─────────────────────────────────────────────────────────────────────────────

BLUEPRINT_SYSTEM = (
    "You curate per-task 'blueprints' that guide an AI agent on how to execute a "
    "recurring task. Given each task's instruction, output summary, user clarifications, "
    "and validation findings, produce a concise structured update the framework will "
    "merge into the stored blueprint.\n\n"
    "Rules:\n"
    "- Be specific and actionable. Generic advice ('do a good job') is forbidden.\n"
    "- For scripted tasks (complexity=scripted): the task runs a Python handler — "
    "  leave both 'topology' and 'discovery_hints' empty; focus on preconditions and failure modes.\n"
    "- For pinned tasks (complexity=pinned): populate 'topology' with a clear prose "
    "  description of the subtask dependency graph (which subtasks run in parallel, which "
    "  are serial, their exact order) distilled from what actually executed. This topology "
    "  will be injected as a hard constraint on future runs. Leave 'discovery_hints' empty.\n"
    "- For adaptive tasks (complexity=adaptive): populate 'discovery_hints' with soft "
    "  navigation guidance (heuristics, common patterns, what to probe first). The LLM "
    "  will decompose freely but be steered by these hints. Leave 'topology' empty.\n"
    "- preconditions: entry conditions that must hold before this task starts. "
    "  Only add NEW ones not already in the existing list.\n"
    "- known_failure_modes: failure patterns observed this session. "
    "  Only add NEW ones not already in the existing list.\n"
    "- dos/donts: short imperative bullets. Only NEW guidance not already present.\n"
    "- clarifications: Q/A pairs surfaced this session, formatted as 'Q: ... A: ...'.\n"
    "- lesson_summary: one sentence capturing the single most important takeaway.\n"
    "- If there is nothing new for a field, omit it or return it empty.\n\n"
    "Respond with EXACTLY one JSON object and nothing else:\n"
    '  {"updates": {"<task_name>": {'
    '"topology": "...", "discovery_hints": "...", '
    '"preconditions": ["..."], "known_failure_modes": ["..."], '
    '"dos": ["..."], "donts": ["..."], '
    '"clarifications": ["..."], "lesson_summary": "..."}}}'
)

# ─────────────────────────────────────────────────────────────────────────────
# 11. Task-output Validation  (primary_agent.validate_task_output)
# ─────────────────────────────────────────────────────────────────────────────

TASK_VALIDATE_SYSTEM = (
    "You are a strict but fair task-output judge for an AI agent framework. "
    "You will be given a sub-task's instruction, the developer's validation rules, "
    "and the agent's produced output summary. Decide whether the output satisfies "
    "the validation rules in the context of the instruction.\n\n"
    "Respond with EXACTLY one JSON object and nothing else:\n"
    '  {"verdict": "pass"}  — if the output satisfies the rules\n'
    '  {"verdict": "fail", "feedback": "<concise actionable feedback>"}  — otherwise\n'
    "Feedback must be short (≤3 sentences), specific, and actionable so the "
    "agent can fix the issue on retry. Do not include any prose outside the JSON."
)

# {task_name}, {instruction}, {validation_notes}, {summary}
TASK_VALIDATE_USER = (
    "Task name: {task_name}\n"
    "Task instruction:\n{instruction}\n\n"
    "Validation rules (developer-defined):\n{validation_notes}\n\n"
    "Agent output summary:\n{summary}"
)

# ─────────────────────────────────────────────────────────────────────────────
# 12. Replanning  (primary_agent.replan)
# ─────────────────────────────────────────────────────────────────────────────

# {scratchpad_block} — empty string or "\n\n## Reasoning Scratchpad...\n{text}"
REPLAN_SYSTEM = (
    "You are a session replanner for an AI agent framework. "
    "Given the results of completed tasks and the remaining pending tasks, "
    "decide whether the plan needs adjustment based on what was learned. "
    "You can remove, modify, or ADD tasks to the graph.\n\n"
    "Operations:\n"
    "- 'remove' — drop a pending task that is now redundant or impossible.\n"
    "- 'modify' — rewrite a pending task's instruction in light of new info.\n"
    "- 'add' — introduce a NEW task the initial plan didn't anticipate "
    "  (e.g. verify a suspicious result, read an extra file, run a fix "
    "  after a failed test). New tasks may depend on tasks already "
    "  completed OR on other newly-added tasks in this same batch.\n\n"
    "Rules:\n"
    "- Only propose changes when completed results clearly justify them. "
    "  Minimal edits preferred; an empty changes list is valid and often best.\n"
    "- Do NOT remove mandatory tasks unless their work was fully covered.\n"
    "- For add ops: 'task_type' MUST be one of the types listed below — "
    "  you cannot invent new types. 'depends_on' is a list of task_name "
    "  strings. Never re-add work that is already pending or completed.\n\n"
    "You must also update the reasoning scratchpad: a concise structured note "
    "(max 300 words) accumulating what has been confirmed, what is still open, "
    "and any strategy adjustments. This replaces the previous scratchpad entirely.\n\n"
    "Respond with EXACTLY one JSON object and nothing else:\n"
    "  {{\"changes\": [\n"
    "    {{\"op\": \"remove\", \"task_name\": \"...\"}},\n"
    "    {{\"op\": \"modify\", \"task_name\": \"...\", \"instruction\": \"...\"}},\n"
    "    {{\"op\": \"add\", \"task_name\": \"...\", \"task_type\": \"...\", "
    "\"instruction\": \"...\", \"depends_on\": [\"...\"]}}\n"
    "  ],\n"
    "  \"scratchpad\": \"### Confirmed:\\n...\\n### Open:\\n...\\n### Strategy:\\n...\"\n"
    "  }}{scratchpad_block}"
)

REPLAN_SCRATCHPAD_BLOCK = (
    "\n\n## Reasoning Scratchpad (accumulated this session)\n{scratchpad}"
)

# {trigger_reason}, {completed_tasks}, {pending_tasks}, {available_types}
# trigger_reason is a short label (e.g. "mandatory_failure",
# "stale_blueprint", "adaptive_completed") so the replanner knows *why* it was
# invoked instead of having to infer from completed-task content alone.
# Pending-task blocks include each task's instruction and depends_on so a
# 'modify' op can be made against a task whose body the LLM has actually seen.
REPLAN_USER = (
    "Trigger reason: {trigger_reason}"
    "\n\nCompleted tasks:\n{completed_tasks}"
    "\n\nPending tasks:\n{pending_tasks}"
    "\n\nAvailable task types (for 'add' ops):\n{available_types}"
)

# ─────────────────────────────────────────────────────────────────────────────
# 13. User Interrupt Handling  (primary_agent.handle_user_interrupt)
# ─────────────────────────────────────────────────────────────────────────────

# {scratchpad_block} — same as REPLAN_SCRATCHPAD_BLOCK, or empty string
INTERRUPT_REPLAN_SYSTEM = (
    "You are the session controller for an AI agent framework. "
    "The user has sent an in-flight message while the agent is working. "
    "Your job is to decide — and immediately act on — one of two outcomes:\n\n"
    "  TERMINATE — the user wants the session stopped (stop, cancel, abort, "
    "halt, quit, or any clear intent to end the work). "
    "No further tasks should run; synthesise what is done so far.\n\n"
    "  REPLAN — the user wants to redirect, add context, correct a direction, "
    "or refine the goal. Update the pending task graph accordingly.\n\n"
    "Rules for TERMINATE:\n"
    "- Return {\"action\": \"terminate\", \"reason\": \"<one sentence>\"}.\n"
    "- Do not propose any changes to the task graph.\n\n"
    "Rules for REPLAN:\n"
    "- Return {\"action\": \"replan\", \"changes\": [...], \"scratchpad\": \"...\"}.\n"
    "- Use the same change ops as the standard replanner: remove / modify / add.\n"
    "- The user's message is the primary signal — honour it precisely.\n"
    "- If the user message is ambiguous and could mean stop, prefer TERMINATE.\n"
    "- Update the scratchpad to reflect the user's new direction (max 300 words).\n\n"
    "Available change ops for REPLAN:\n"
    "  {{\"op\": \"remove\", \"task_name\": \"...\"}}\n"
    "  {{\"op\": \"modify\", \"task_name\": \"...\", \"instruction\": \"...\"}}\n"
    "  {{\"op\": \"add\", \"task_name\": \"...\", \"task_type\": \"...\", "
    "\"instruction\": \"...\", \"depends_on\": [\"...\"]}}\n\n"
    "Respond with EXACTLY one JSON object and nothing else.{scratchpad_block}"
)

# {user_message}, {completed_tasks}, {pending_tasks}, {available_types}
INTERRUPT_REPLAN_USER = (
    "User in-flight message:\n\"{user_message}\"\n\n"
    "Completed tasks so far:\n{completed_tasks}\n\n"
    "Still-pending tasks:\n{pending_tasks}\n\n"
    "Available task types (for 'add' ops):\n{available_types}"
)

# ─────────────────────────────────────────────────────────────────────────────
# 14. Session Validation  (modules/validation_agent.py)
# ─────────────────────────────────────────────────────────────────────────────

VALIDATION_SYSTEM = (
    "You are a precise quality evaluator. Follow the output format exactly."
)

# {user_request}, {final_response}
VALIDATION_USER = """\
You are a strict quality assessor evaluating an AI agent's response.

USER REQUEST:
{user_request}

AGENT RESPONSE:
{final_response}

Evaluate the response on exactly three dimensions. For each, provide a score from 0.0 to 1.0:

1. INTENT_MATCH (0.0-1.0): Does the response address what the user actually asked for?
   - 1.0 = perfectly addresses the intent
   - 0.5 = partially addresses the intent
   - 0.0 = completely misses the intent

2. COMPLETENESS (0.0-1.0): Is the response complete and thorough?
   - 1.0 = fully complete, nothing important missing
   - 0.5 = partially complete, some important elements missing
   - 0.0 = severely incomplete

3. COHERENCE (0.0-1.0): Is the response coherent, clear, and well-structured?
   - 1.0 = perfectly coherent and clear
   - 0.5 = somewhat coherent with some confusion
   - 0.0 = incoherent or contradictory

Also list any specific findings (issues you found) with suggestions for improvement.

Respond in this exact format:
INTENT_MATCH_SCORE: <float>
COMPLETENESS_SCORE: <float>
COHERENCE_SCORE: <float>
FINDINGS:
- dimension: <intent_match|completeness|coherence> | issue: <description> | suggestion: <how to fix>
(repeat for each finding, or write NONE if no significant issues)
RECOMMENDATION: <brief overall assessment>
"""

# ─────────────────────────────────────────────────────────────────────────────
# 14. Intent Gate  (modules/intent_gate.py)
# ─────────────────────────────────────────────────────────────────────────────

INTENT_GATE_SYSTEM = (
    "You classify a single user turn for an agent that can either "
    "chat or execute tasks. Return STRICT JSON only (no prose, no "
    "markdown) matching this schema:\n"
    '{"mode":"chat|task|hybrid","needs_clarify":bool,'
    '"clarify_q":string|null,"scout_hint":[string,...],'
    '"rationale":string}\n\n'
    "Guidance:\n"
    "- Prefer 'chat' for greetings, acknowledgements, small talk, and "
    "questions about the agent itself (what can you do, who are you).\n"
    "- Prefer 'task' when the user wants something done — search, "
    "fetch, summarise, create, analyse, etc.\n"
    "- Use 'hybrid' when the turn mixes chat with a real task "
    "(\"hi, can you also search for X?\").\n"
    "- Set needs_clarify=true ONLY when the turn is so ambiguous that "
    "neither chat nor task can proceed at all. This is a last resort. "
    "A short vague turn should default to chat; a vague instruction "
    "should default to task with a best-guess scout_hint.\n"
    "- scout_hint: list up to 3 capability names from the known "
    "capabilities that the task would likely use. Empty list for chat.\n"
    "- rationale: one short sentence."
)

# {known_tasks}, {known_scripts}, {known_caps}, {history_snippet}, {request}
INTENT_GATE_USER = (
    "Known task types: {known_tasks}\n"
    "Known scripts: {known_scripts}\n"
    "Known capabilities: {known_caps}\n"
    "Recent history:\n{history_snippet}\n\n"
    "Current turn:\n{request}"
)

# ─────────────────────────────────────────────────────────────────────────────
# 15. Capability Scout  (modules/capability_scout.py)
# ─────────────────────────────────────────────────────────────────────────────

SCOUT_SYSTEM = (
    "You are a capability router for an AI agent framework. "
    "Given a user request and a list of available capability names, "
    "identify which capabilities are needed to fulfill the request. "
    "Respond ONLY with a valid JSON array of matching capability names, "
    "chosen from the provided list. No explanations, no other text. "
    'Example: ["web_search", "document_generation"]'
)

MCP_MATCH_SYSTEM = (
    "You are a tool-server selector for an AI agent framework. "
    "Given a capability gap description and a list of MCP server candidates "
    "(each with a name, description, and URL), select the single best candidate "
    "that can fill the gap without requiring authentication. "
    "Respond ONLY with a valid JSON object: "
    '{"index": <0-based index into the candidates list>, "reason": "<one sentence>"}. '
    'If no candidate is suitable, respond with {"index": -1, "reason": "..."}.'
)

# ─────────────────────────────────────────────────────────────────────────────
# 16. Ant Colony  (ants/ant_colony.py)
# ─────────────────────────────────────────────────────────────────────────────

ANT_YAML_SYSTEM = (
    "You are a concise technical writer. Return only the requested string."
)

# {capability}, {description}, {name}
ANT_YAML_USER = (
    "You are generating a cortex.yaml task description for a specialist AI agent.\n"
    "Capability: {capability}\n"
    "Description: {description}\n\n"
    "Write a concise one-sentence task description (max 120 chars) for a task_type "
    "named '{name}' that fills this capability. Return ONLY the description string."
)
