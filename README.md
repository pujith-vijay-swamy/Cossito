# OmniTasker

An autonomous browser agent that navigates to a target URL, identifies all coding problems on the page, solves them sequentially, verifies results, and pauses for human intervention on login/CAPTCHA walls.

Built with **browser-use**, **Playwright**, **LangChain**, and **Streamlit**.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure your API key

Copy the template and fill in your OpenRouter key:

```bash
cp .env.example .env
# Edit .env and replace sk-or-v1-your-key-here with your actual key
```

Or you can enter it directly in the Streamlit sidebar at runtime.

### 3. Run the application

```bash
streamlit run app.py
```

### 4. Use the agent

1. Enter your **OpenRouter API Key** in the sidebar (if not set via `.env`).
2. Enter the **Target URL** of the page with coding problems.
3. Click **▶️ Start Agent**.
4. Watch the agent:
   - **Reconnaissance**: Analyzes the page and builds a task queue.
   - **Execution**: Solves each problem one by one.
   - **Verification**: Checks output after each solution.
5. If a login wall or CAPTCHA appears, the agent **pauses automatically** — log in manually in the browser window, then click **🔓 Resume Agent**.

---

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                      Streamlit UI                      │  ← User interaction, live logs, HITL resume
│                        (app.py)                        │
└───────────────────────────┬────────────────────────────┘
                            │ queue.Queue + threading
┌───────────────────────────▼────────────────────────────┐
│                    AgentOrchestrator                   │  ← Central orchestrator & lifecycle manager
│                     (agent_core.py)                    │
└───────┬──────────────┬──────────────┬──────────────┬───┘
        │              │              │              │
┌───────▼──────┐┌──────▼──────┐┌──────▼──────┐┌──────▼──────┐
│ ReconEngine  ││ Controller  ││ DOM Extract ││  Overlay    │
│(recon_engine)││  Actions    ││(dom_extract)││  Manager    │
└───────┬──────┘└──────┬──────┘└──────┬──────┘└─────────────┘
        │              │              │
┌───────▼──────┐┌──────▼──────┐┌──────▼──────┐
│ Platform     ││ Editor      ││ Verification│
│ Adapters     ││ Adapters    ││ Engine      │
└──────────────┘└─────────────┘└─────────────┘
```

### Modular Workspace Organization

| Module                   | Purpose                                                                |
| ------------------------ | ---------------------------------------------------------------------- |
| `agent_core.py`          | Central orchestrator, state transitions, task execution loop           |
| `recon_engine.py`        | Fast-track reconnaissance, progressive hydration, task queue building  |
| `controller_actions.py`  | Custom browser actions: safe write, submission, HITL, reference images |
| `dom_extraction.py`      | CDP DOM tree extraction, section header detection, chrome filtering   |
| `overlay_manager.py`     | In-page floating HUD injection, real-time status and live thinking sync|
| `platform_adapters.py`   | Platform detection, submit button handlers, terminal result parsing    |
| `editor_adapters.py`     | Monaco, CodeMirror, Ace, textarea detection, safe injection readback   |
| `verification.py`        | Test case result parsing, submission verification, outcome assertion   |
| `state_machine.py`       | Deterministic task state machine and transition enforcement            |
| `browser_manager.py`     | Persistent browser lifecycle, anti-detection flags, profile handling   |
| `human_loop.py`          | HITL pause/resume controller, suggestion ingestion                     |
| `llm_provider.py`        | Multi-provider LLM factory (OpenRouter, Gemini, OpenAI, Claude)        |
| `runtime_evolution.py`   | Failure categorization, bounded recovery, autonomous skill distillation|
| `skill_registry.py`      | Skill persistence, indexing, and execution                             |
| `skill_validation.py`    | AST static analysis and safety validation for learned skills           |
| `models.py`              | Strict Pydantic models for tasks, queues, snapshots, and states        |
| `prompts.py`             | System instructions and prompt templates for recon and execution agents|
| `tests/`                 | Complete test suite covering models, adapters, overlay, state machine  |

---

## Key Design Principles

- **Zero Hardcoding**: All platforms, problem formats, code editors, and section hierarchies are detected dynamically using robust semantic heuristics, structural DOM querying, and standard protocols.
- **Accurate Reconnaissance**: Strict separation between page chrome / syllabus navigation and real problem statements. Auto-expands section accordions (e.g. `L4 - Practice Problems`) to catalogue all child problems.
- **Progressive Hydration**: Waits for single-page applications to hydrate problem statements; auto-clicks active question tabs when descriptions are empty.
- **Safe Code Writing**: Multi-layered editor adapters with verification readback and hash-based integrity checking before submit.
- **Single Shared Browser**: Preserves cookies, session state, and auth tokens across reconnaissance and execution phases.
- **Deterministic State Transitions**: Validated state machine ensuring predictable progression (`QUEUED` → `RECONCILING` → `READY` → `PLANNING` → `CODING` → `SUBMITTING` → `VERIFYING` → `PASSED`).

---

## Configuration

| Setting             | Default                        | Override                |
| ------------------- | ------------------------------ | ----------------------- |
| LLM Model           | `google/gemini-2.5-flash`      | `llm_provider.py`       |
| LLM Temperature     | `0.2`                          | `llm_provider.py`       |
| Browser Headless    | `False`                        | `AgentOrchestrator()`   |
| Max Recon Steps     | `10`                           | `recon_engine.py`       |
| Max Exec Steps/Task | `60`                           | `agent_core.py`         |
| Task Retries        | `3`                            | `models.py`             |
| Browser Profile Dir | `./browser_profile`            | `browser_manager.py`    |

---

## License

MIT

