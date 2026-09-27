"""
app.py — Streamlit frontend for OmniTasker.

Provides the configuration & launch surface for the agent.  Real-time
monitoring also happens via an in-browser glassmorphism overlay panel
that is injected directly into the target webpage DOM (see overlay_panel.html).

Streamlit responsibilities:
  - Entering a target URL and API key
  - Starting / stopping the agent
  - Viewing the task queue discovered during recon
  - Live log streaming
  - Human-in-the-loop pause & resume
"""

from __future__ import annotations

import asyncio
import html
import os
import queue
import threading
from copy import deepcopy
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

# Load local environment variables from .env file
load_dotenv()

from agent_core import AgentOrchestrator
from human_loop import HumanInTheLoopController
from models import AgentPhase, AgentState, TaskStatus

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="OmniTasker",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Custom CSS — premium dark UI
# ---------------------------------------------------------------------------

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700;800&display=swap');

:root {
    --bg-primary: #09090b;
    --bg-secondary: #18181b;
    --bg-card: #121215;
    --border-main: 3px solid #27272a;
    --border-accent: 3px solid #a855f7;
    --border-black: 3px solid #000000;
    --accent-purple: #a855f7;
    --accent-cyan: #38bdf8;
    --accent-green: #4ade80;
    --accent-yellow: #facc15;
    --accent-red: #f43f5e;
    --shadow-hard: 4px 4px 0px #000000;
    --shadow-purple: 4px 4px 0px #a855f7;
    --shadow-cyan: 4px 4px 0px #38bdf8;
    --shadow-yellow: 4px 4px 0px #facc15;
    --text-primary: #ffffff;
    --text-secondary: #a1a1aa;
}

/* Global resets & Brutalist background */
.stApp {
    font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
    background-color: var(--bg-primary);
    color: var(--text-primary);
}

/* Resized & Compact Neo-Brutalist Buttons */
.stButton > button {
    font-family: 'Outfit', sans-serif !important;
    font-weight: 800 !important;
    font-size: 0.82rem !important;
    padding: 8px 14px !important;
    letter-spacing: 0.04em !important;
    text-transform: uppercase !important;
    border: 2.5px solid #000000 !important;
    border-radius: 10px !important;
    background-color: #27272a !important;
    color: #ffffff !important;
    box-shadow: 3px 3px 0px #000000 !important;
    transition: all 0.12s ease !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 6px !important;
}
.stButton > button:hover {
    transform: translate(-1.5px, -1.5px) !important;
    box-shadow: 4.5px 4.5px 0px #000000 !important;
    background-color: #3f3f46 !important;
    color: #ffffff !important;
}
.stButton > button:active {
    transform: translate(1.5px, 1.5px) !important;
    box-shadow: 1.5px 1.5px 0px #000000 !important;
}
.stButton > button[kind="primary"] {
    background: #a855f7 !important;
    color: #000000 !important;
    border: 2.5px solid #000000 !important;
    box-shadow: 3px 3px 0px #000000 !important;
}
.stButton > button[kind="primary"]:hover {
    background: #c084fc !important;
    color: #000000 !important;
    box-shadow: 4.5px 4.5px 0px #000000 !important;
}

/* BaseWeb Input & Select Box Containers — Fix Double Border & Focus Ring */
div[data-baseweb="input"], 
div[data-baseweb="base-input"], 
div[data-baseweb="select"] > div,
div[data-baseweb="textarea"] {
    background-color: #18181b !important;
    border: 2.5px solid #27272a !important;
    border-radius: 12px !important;
    box-shadow: 3px 3px 0px #000000 !important;
    transition: all 0.15s ease !important;
    outline: none !important;
}

div[data-baseweb="input"]:focus-within, 
div[data-baseweb="base-input"]:focus-within, 
div[data-baseweb="select"] > div:focus-within,
div[data-baseweb="textarea"]:focus-within {
    border-color: #a855f7 !important;
    box-shadow: 3px 3px 0px #a855f7 !important;
}

/* Inner input text & textarea elements — Strip inner borders */
div[data-baseweb="input"] input, 
div[data-baseweb="base-input"] input, 
textarea {
    border: none !important;
    outline: none !important;
    box-shadow: none !important;
    background: transparent !important;
    color: #ffffff !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    font-weight: 600 !important;
    font-size: 0.88rem !important;
}

/* Hide blinking text cursor / caret inside Selectbox dropdowns */
div[data-baseweb="select"] input {
    caret-color: transparent !important;
    cursor: pointer !important;
    user-select: none !important;
}
div[data-baseweb="select"] span {
    color: #ffffff !important;
    font-weight: 600 !important;
}

/* Header Banner */
.agent-header {
    background: #18181b;
    border: 3.5px solid #a855f7;
    border-radius: 16px;
    padding: 2.2rem 2.6rem;
    margin-bottom: 2rem;
    position: relative;
    box-shadow: 6px 6px 0px #000000;
}
.agent-header h1 {
    font-family: 'Outfit', sans-serif;
    font-size: 2.8rem;
    font-weight: 900;
    letter-spacing: -0.03em;
    color: #ffffff;
    text-transform: uppercase;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 12px;
}
.agent-header h1 span.tag {
    font-size: 0.8rem;
    padding: 4px 12px;
    background: #a855f7;
    color: #000000;
    border: 2.5px solid #000000;
    border-radius: 8px;
    box-shadow: 3px 3px 0px #000000;
    font-weight: 800;
}
.agent-header p {
    color: #a1a1aa;
    font-size: 1.05rem;
    margin-top: 0.6rem;
    font-weight: 600;
    line-height: 1.5;
}

/* Neo-Brutalist Status Badge */
.status-badge {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 10px 24px;
    border-radius: 12px;
    font-family: 'Outfit', sans-serif;
    font-size: 0.95rem;
    font-weight: 900;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    border: 3.5px solid #000000;
    box-shadow: 5px 5px 0px #000000;
    margin-bottom: 1.5rem;
}
.status-idle       { background: #27272a; color: #a1a1aa; }
.status-recon      { background: #38bdf8; color: #000000; }
.status-executing  { background: #4ade80; color: #000000; }
.status-paused     { background: #f43f5e; color: #ffffff; }
.status-done       { background: #a855f7; color: #000000; }
.status-error      { background: #f43f5e; color: #ffffff; }

/* Task Cards */
.task-card {
    background: #18181b;
    border: 3px solid #27272a;
    border-radius: 14px;
    padding: 1.3rem 1.6rem;
    margin-bottom: 1rem;
    box-shadow: 4px 4px 0px #000000;
    transition: all 0.15s ease;
}
.task-card:hover {
    border-color: #a855f7;
    transform: translate(-2px, -2px);
    box-shadow: 6px 6px 0px #a855f7;
}
.task-card .task-id {
    font-family: 'Outfit', sans-serif;
    font-size: 0.85rem;
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #a855f7;
    background: #27272a;
    padding: 2px 10px;
    border: 2px solid #000000;
    border-radius: 6px;
}
.task-card .task-desc {
    font-size: 0.98rem;
    color: #ffffff;
    font-weight: 600;
    margin-top: 10px;
    line-height: 1.5;
}
.task-status-icon { font-size: 1.25rem; }

/* Cognitive cards */
.cog-card {
    background: #18181b;
    border: 3px solid #27272a;
    border-radius: 14px;
    padding: 1.4rem 1.6rem;
    margin-bottom: 1.2rem;
    box-shadow: 4px 4px 0px #000000;
    transition: all 0.15s ease;
}
.cog-card:hover {
    border-color: #38bdf8;
    transform: translate(-2px, -2px);
    box-shadow: 6px 6px 0px #38bdf8;
}
.cog-header {
    font-family: 'Outfit', sans-serif;
    font-size: 0.88rem;
    font-weight: 900;
    text-transform: uppercase;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 8px;
    letter-spacing: 0.06em;
}
.cog-header-thinking { color: #38bdf8; }
.cog-header-goal     { color: #4ade80; }
.cog-header-memory   { color: #c084fc; }
.cog-header-actions  { color: #facc15; }

.cog-content {
    font-size: 0.96rem;
    line-height: 1.6;
    color: #ffffff;
    font-weight: 500;
}
.cog-content-memory {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.88rem;
    line-height: 1.5;
    color: #38bdf8;
    background: #09090b;
    padding: 14px;
    border-radius: 10px;
    border: 2px solid #27272a;
    white-space: pre-wrap;
    box-shadow: inset 2px 2px 0px #000000;
}

/* Failed Injection Card */
.failed-injection-card {
    background: #18181b;
    border: 3.5px solid #f59e0b;
    border-radius: 14px;
    padding: 1.4rem 1.6rem;
    margin-top: 1rem;
    margin-bottom: 1.5rem;
    box-shadow: 5px 5px 0px #f59e0b;
}
.failed-injection-header {
    font-family: 'Outfit', sans-serif;
    font-size: 0.95rem;
    font-weight: 900;
    text-transform: uppercase;
    color: #f59e0b;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 8px;
    letter-spacing: 0.05em;
}
.failed-injection-desc {
    font-size: 0.92rem;
    color: #d4d4d8;
    margin-bottom: 12px;
    line-height: 1.5;
    font-weight: 500;
}

/* Log Terminal Panel */
.terminal-header {
    background: #27272a;
    border: 3.5px solid #000000;
    border-bottom: none;
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
    padding: 12px 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.terminal-title {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.8rem;
    font-weight: 800;
    color: #ffffff;
    letter-spacing: 0.08em;
}
.terminal-dots {
    display: flex;
    gap: 8px;
}
.terminal-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    border: 1.5px solid #000000;
}
.dot-red { background-color: #f43f5e; }
.dot-yellow { background-color: #facc15; }
.dot-green { background-color: #4ade80; }

.log-panel {
    background: #09090b;
    border: 3.5px solid #000000;
    border-bottom-left-radius: 14px;
    border-bottom-right-radius: 14px;
    padding: 1.25rem;
    max-height: 480px;
    overflow-y: auto;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.88rem;
    line-height: 1.6;
    box-shadow: 5px 5px 0px #000000;
}
.log-entry {
    padding: 4px 0;
    border-bottom: 1px solid #18181b;
}
.log-time {
    color: #71717a;
    margin-right: 10px;
    font-size: 0.8rem;
    font-weight: 600;
}
.log-info    { color: #38bdf8; font-weight: 500; }
.log-success { color: #4ade80; font-weight: 700; }
.log-warn    { color: #facc15; font-weight: 600; }
.log-error   { color: #f43f5e; font-weight: 700; }

/* HITL Alert */
.hitl-alert {
    background: #18181b;
    border: 3.5px solid #f43f5e;
    border-radius: 16px;
    padding: 1.8rem 2.2rem;
    text-align: center;
    box-shadow: 6px 6px 0px #f43f5e;
    animation: neo-pulse 2s infinite;
}
@keyframes neo-pulse {
    0%, 100% { box-shadow: 6px 6px 0px #f43f5e; }
    50%      { box-shadow: 8px 8px 0px #facc15; }
}
.hitl-alert h3 {
    font-family: 'Outfit', sans-serif;
    color: #f43f5e;
    margin: 0 0 0.6rem 0;
    font-size: 1.4rem;
    font-weight: 900;
    letter-spacing: 0.03em;
    text-transform: uppercase;
}
.hitl-alert p {
    color: #d4d4d8;
    margin: 0;
    line-height: 1.5;
    font-weight: 500;
}

/* Progress bar */
.progress-bar-container {
    background: #18181b;
    border: 3px solid #000000;
    border-radius: 10px;
    height: 16px;
    overflow: hidden;
    margin: 1rem 0;
    box-shadow: 3px 3px 0px #000000;
}
.progress-bar-fill {
    height: 100%;
    border-radius: 8px;
    background: linear-gradient(90deg, #38bdf8, #a855f7, #4ade80);
    border-right: 2px solid #000000;
    transition: width 0.4s ease;
}
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Session state initialization
# ---------------------------------------------------------------------------

_DEFAULTS: dict = {
    "agent_state": AgentState(),
    "hitl_controller": HumanInTheLoopController(),
    "log_queue": queue.Queue(),
    "running_flag": {"value": False},  # mutable container for cross-thread flag inside session state
    "control_flags": {
        "stop_requested": False, 
        "suggestions": [],
        "pause_requested": False,
        "pause_click_time": None,
        "pause_elapsed_time": None,
    },  # mutable control flags inside session state
    "agent_thread": None,
    "agent_loop": None,
    "is_running": False,
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default


def _drain_queue() -> None:
    """Pull the latest AgentState from the queue into session_state."""
    if "log_queue" not in st.session_state:
        # Ensure all defaults are initialized to prevent fragment race conditions
        for key, default in _DEFAULTS.items():
            if key not in st.session_state:
                st.session_state[key] = default

    q = st.session_state.log_queue
    latest: Optional[AgentState] = None
    while not q.empty():
        try:
            latest = q.get_nowait()
        except queue.Empty:
            break
    if latest is not None:
        st.session_state.agent_state = latest


# ---------------------------------------------------------------------------
# Background thread runner
# ---------------------------------------------------------------------------

def _run_agent_in_thread(
    url: str,
    api_key: str,
    model: str,
    provider: str,
    use_vision: bool,
    hitl_controller: HumanInTheLoopController,
    log_q: queue.Queue,
    running_flag: dict,
    control_flags: dict,
    base_url: Optional[str] = None,
) -> None:
    """Entry-point for the background thread that runs the async agent."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    orchestrator = AgentOrchestrator(
        url=url,
        api_key=api_key,
        on_state_change=lambda state: log_q.put(deepcopy(state)),
        hitl_controller=hitl_controller,
        headless=False,
        model=model,
        provider=provider,
        use_vision=use_vision,
        control_flags=control_flags,
        base_url=base_url,
    )

    try:
        print("Starting orchestrator.run()...")
        loop.run_until_complete(orchestrator.run())
        print("orchestrator.run() finished successfully.")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Exception in _run_agent_in_thread: {e}\n{tb}")
        err_state = AgentState()
        err_state.phase = AgentPhase.ERROR
        err_state.error_message = f"Agent startup/execution failed: {e}\n{tb}"
        log_q.put(err_state)
    finally:
        loop.close()
        running_flag["value"] = False


# ---------------------------------------------------------------------------
# Helper renderers
# ---------------------------------------------------------------------------

def _render_status_badge(phase: AgentPhase) -> str:
    labels = {
        AgentPhase.IDLE: ("⚪", "Idle", "idle"),
        AgentPhase.RECON: ("🔍", "Reconnaissance", "recon"),
        AgentPhase.EXECUTING: ("⚡", "Executing", "executing"),
        AgentPhase.PAUSED_HITL: ("⏸️", "Paused — Human Action Required", "paused"),
        AgentPhase.DONE: ("✅", "Completed", "done"),
        AgentPhase.ERROR: ("❌", "Error", "error"),
    }
    icon, label, cls = labels.get(phase, ("❓", "Unknown", "idle"))
    return f'<span class="status-badge status-{cls}">{icon} {label}</span>'


def _task_status_icon(status: TaskStatus) -> str:
    return {
        TaskStatus.PENDING: "◻️",
        TaskStatus.IN_PROGRESS: "🔄",
        TaskStatus.SUCCESS: "✅",
        TaskStatus.FAILED: "❌",
        TaskStatus.RETRYING: "🔁",
    }.get(status, "❓")


def _render_log_entry(entry) -> str:
    cls = f"log-{entry.level}"
    escaped_msg = html.escape(str(entry.message))
    return (
        f'<div class="log-entry">'
        f'<span class="log-time">[{entry.timestamp}]</span>'
        f'<span class="{cls}">{escaped_msg}</span>'
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Layout & Fragments
# ---------------------------------------------------------------------------

@st.fragment(run_every=1.5)
def render_sidebar_controls(api_key, target_url, model_name, provider, custom_base_url):
    """Reactive fragment for real-time sidebar controls (Pause/Resume/Stop/Suggestions)."""
    _drain_queue()
    
    st.divider()

    # Clean URL and resolve API key from user input
    target_url_clean = target_url.strip() if target_url else ""
    if target_url_clean and not (target_url_clean.startswith("http://") or target_url_clean.startswith("https://") or target_url_clean.startswith("file://")):
        target_url_clean = "https://" + target_url_clean

    effective_api_key = (api_key or "").strip()
    if not effective_api_key and ("Local" in provider or "Ollama" in provider):
        effective_api_key = "local"

    if not st.session_state.is_running:
        # Agent is NOT running — show ONLY Start Agent button
        start_disabled = not bool(effective_api_key) or not bool(target_url_clean)
        if st.button("🚀 Start Agent", disabled=start_disabled, use_container_width=True, type="primary"):
            st.session_state.is_running = True
            st.session_state.running_flag["value"] = True
            st.session_state.control_flags["stop_requested"] = False
            st.session_state.control_flags["pause_requested"] = False
            st.session_state.control_flags["pause_click_time"] = None
            st.session_state.control_flags["pause_elapsed_time"] = None
            st.session_state.agent_state = AgentState()
            
            # Clear log queue
            while not st.session_state.log_queue.empty():
                try:
                    st.session_state.log_queue.get_nowait()
                except queue.Empty:
                    break

            hitl = HumanInTheLoopController()
            st.session_state.hitl_controller = hitl
            log_q = st.session_state.log_queue
            running_flag = st.session_state.running_flag
            control_flags = st.session_state.control_flags
            thread = threading.Thread(
                target=_run_agent_in_thread,
                args=(target_url_clean, effective_api_key, model_name.strip(), provider, False, hitl, log_q, running_flag, control_flags, custom_base_url),
                daemon=True,
            )
            st.session_state.agent_thread = thread
            thread.start()
            try:
                st.rerun(scope="app")
            except TypeError:
                st.rerun()
    else:
        # Agent IS running — show Pause and Stop buttons side-by-side
        col_pause, col_stop = st.columns(2)
        with col_pause:
            current_phase = st.session_state.agent_state.phase
            if current_phase == AgentPhase.PAUSED_HITL:
                if st.button("⚡ Resume Agent", use_container_width=True, type="primary"):
                    st.session_state.hitl_controller.resume()
                    st.rerun()
            else:
                if st.session_state.control_flags.get("pause_requested"):
                    st.button("⏳ Pausing...", disabled=True, use_container_width=True)
                else:
                    if st.button("⏸️ Pause Agent", use_container_width=True):
                        import time
                        st.session_state.control_flags["pause_requested"] = True
                        st.session_state.control_flags["pause_click_time"] = time.time()
                        st.session_state.control_flags["pause_elapsed_time"] = None
                        st.rerun()

        with col_stop:
            if st.button("🛑 Stop Agent", use_container_width=True):
                st.session_state.is_running = False
                if "control_flags" not in st.session_state or st.session_state.control_flags is None:
                    st.session_state.control_flags = {"stop_requested": False, "suggestions": []}
                st.session_state.control_flags["stop_requested"] = True
                st.session_state.control_flags["pause_requested"] = False
                st.session_state.running_flag["value"] = False
                st.rerun()

    st.divider()
    st.markdown("### 💬 Human Suggestion")
    
    def _inject_suggestion_callback():
        sugg = st.session_state.get("human_suggestion_input", "").strip()
        if sugg:
            if "control_flags" not in st.session_state or st.session_state.control_flags is None:
                st.session_state.control_flags = {"stop_requested": False, "suggestions": []}
            if "suggestions" not in st.session_state.control_flags:
                st.session_state.control_flags["suggestions"] = []
            st.session_state.control_flags["suggestions"].append(sugg)
            # Wiping widget state before rerun inside the callback prevents StreamlitAPIException
            st.session_state.human_suggestion_input = ""

    st.text_area(
        "Inject instructions directly into the AI agent's current task context.",
        placeholder="e.g. Name the class Solution, fix the imports...",
        key="human_suggestion_input",
        height=80,
    )
    has_suggestion = bool(st.session_state.get("human_suggestion_input", "").strip())
    st.button(
        "📨 Inject Suggestion",
        disabled=not has_suggestion,
        on_click=_inject_suggestion_callback,
        use_container_width=True,
    )

    st.divider()
    st.markdown(
        f"**Model:** `{model_name}`  \n"
        f"**Provider:** {provider}  \n"
        f"**Vision Assistant:** Gemma 4 / Gemini (Active)  \n"
        f"**Browser:** Using headed Chromium"
    )


# Pull latest state from background thread.
_drain_queue()
state: AgentState = st.session_state.agent_state

# ---- Header ----
st.markdown(
    """
<div class="agent-header">
    <h1>OmniTasker <span class="tag">NEO-BRUTAL V2</span></h1>
    <p>Autonomous browser coding engine with real-time DOM injection & Human-in-the-Loop orchestration.</p>
</div>
""",
    unsafe_allow_html=True,
)

# ---- Sidebar: Configuration ----
with st.sidebar:
    st.markdown("### ⚙️ Configuration")
    
    # 1. API Provider Dropdown
    provider = st.selectbox(
        "API Provider",
        options=[
            "OpenRouter", 
            "OpenAI", 
            "Anthropic", 
            "Google (Gemini)", 
            "Ollama (Local)", 
            "LM Studio / Local (vLLM / llama.cpp)", 
            "Hugging Face (Inference API)"
        ],
        index=0,
        help="Select the LLM gateway or provider you want to use."
    )
    
    # 2. Dynamic API Key & Base URL Fields
    custom_base_url = None
    if provider == "OpenRouter":
        api_key = st.text_input(
            "OpenRouter API Key",
            type="password",
            placeholder="sk-or-v1-…",
            help="Enter your OpenRouter API key.",
        )
        default_model = "google/gemma-4-31b-it:free"
    elif provider == "OpenAI":
        api_key = st.text_input(
            "OpenAI API Key",
            type="password",
            placeholder="sk-…",
            help="Enter your OpenAI API key.",
        )
        default_model = "gpt-4o-mini"
    elif provider == "Anthropic":
        api_key = st.text_input(
            "Anthropic API Key",
            type="password",
            placeholder="sk-ant-…",
            help="Enter your Anthropic API key.",
        )
        default_model = "claude-3-5-sonnet-latest"
    elif provider == "Google (Gemini)":
        api_key = st.text_input(
            "Gemini API Key",
            type="password",
            placeholder="AIzaSy…",
            help="Enter your Google Gemini API key.",
        )
        default_model = "gemini-2.5-flash"
    elif provider == "Ollama (Local)":
        api_key = st.text_input(
            "Ollama API Key (Optional)",
            type="password",
            value="ollama",
            help="Leave as 'ollama' or standard value unless auth is required.",
        )
        custom_base_url = st.text_input(
            "Ollama API Base URL",
            value="http://localhost:11434/v1",
            help="Standard Ollama API local endpoint.",
        )
        default_model = "nemotron-3-super:cloud"
    elif provider == "LM Studio / Local (vLLM / llama.cpp)":
        api_key = st.text_input(
            "Local API Key (Optional)",
            type="password",
            value="local",
            help="Leave as 'local' unless auth is required.",
        )
        custom_base_url = st.text_input(
            "Local API Base URL",
            value="http://localhost:1234/v1",
            help="LM Studio default: http://localhost:1234/v1. vLLM default: http://localhost:8000/v1.",
        )
        default_model = "meta-llama-3-8b-instruct"
    else:  # Hugging Face (Inference API)
        api_key = st.text_input(
            "Hugging Face Access Token (HF_TOKEN)",
            type="password",
            placeholder="hf_…",
            help="Enter your Hugging Face access token.",
        )
        custom_base_url = st.text_input(
            "Hugging Face Base URL (Optional)",
            value="https://api-inference.huggingface.co/v1/",
            help="Hugging Face serverless OpenAI-compatible Inference URL.",
        )
        default_model = "Qwen/Qwen2.5-Coder-32B-Instruct"
        
    # 3. Target URL Input
    target_url = st.text_input(
        "Target URL",
        placeholder="https://example.com/problems",
        help="The web page containing coding problems to solve.",
    )
    
    # 4. Model Name Input
    model_name = st.text_input(
        "Model Name",
        value=default_model,
        key=f"model_name_{provider}",
        help="The exact model identifier to use.",
    )

    # Reactive controls render inside sidebar
    render_sidebar_controls(api_key, target_url, model_name, provider, custom_base_url)

# ---- Main area: two-column layout (Real-time Fragment) ----
@st.fragment(run_every=1.5)
def render_dashboard():
    # Pull latest logs and thoughts from background thread queue
    _drain_queue()
    state = st.session_state.agent_state

    # Check if the background task has finished, and trigger a full rerun to activate sidebar controls
    if st.session_state.is_running and not st.session_state.running_flag["value"]:
        st.session_state.is_running = False
        st.rerun()

    left_col, right_col = st.columns([1.2, 1], gap="large")

    with left_col:
        # Status badge
        st.markdown(_render_status_badge(state.phase), unsafe_allow_html=True)

        # ---- Failed Code Injection Panel ----
        if getattr(state, "failed_injection_code", None):
            st.markdown(
                """
                <div class="failed-injection-card">
                    <div class="failed-injection-header">
                        <span>❌</span> Manual Paste Required: Injection failed
                    </div>
                    <div class="failed-injection-desc">
                        The agent tried to inject the code solution 3 times but could not find a supported editor container. Please click on the box below to view, copy, and paste the code manually.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            with st.popover("💻 CODE (Click to view/copy solution)", use_container_width=True):
                st.code(state.failed_injection_code, language="python")
            
        elif getattr(state, "current_solution_code", None):
            st.markdown(
                """
                <div class="info-card" style="background: rgba(139, 92, 246, 0.1); border: 1px solid rgba(139, 92, 246, 0.25); border-radius: 12px; padding: 12px 14px; margin-bottom: 14px;">
                    <div style="font-weight: 700; color: #a78bfa; margin-bottom: 4px;">
                        <span>🧠</span> The code to be implemented is:
                    </div>
                    <div style="font-size: 0.85rem; color: #d1d5db;">
                        The agent is currently injecting this code solution. Click below to view the source.
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            with st.popover("💻 CODE (Click to view current solution)", use_container_width=True):
                st.code(state.current_solution_code, language="python")

        # Progress bar
        if state.phase == AgentPhase.EXECUTING and state.task_queue:
            pct = int(state.progress_pct * 100)
            st.markdown(
                f"""
    <div class="progress-bar-container">
        <div class="progress-bar-fill" style="width: {pct}%;"></div>
    </div>
    <div style="text-align:right; font-size:0.78rem; color:var(--text-secondary);">
        {pct}% — Task {state.current_task_index + 1}/{len(state.task_queue.tasks)}
    </div>
    """,
                unsafe_allow_html=True,
            )

        # ---- HITL Alert ----
        if state.phase == AgentPhase.PAUSED_HITL:
            reason = state.hitl_reason or "Authentication or CAPTCHA detected."
            elapsed_time_html = ""
            elapsed_time = st.session_state.control_flags.get("pause_elapsed_time")
            if elapsed_time is not None:
                elapsed_time_html = f"""
                <div style="margin-top: 10px; padding: 6px 12px; background: rgba(88, 166, 255, 0.15); border: 1px solid rgba(88, 166, 255, 0.3); border-radius: 8px; font-size: 0.85rem; font-weight: 600; color: #58a6ff; display: inline-block;">
                    ⏱️ Task completed in {elapsed_time:.2f} seconds after clicking pause
                </div>
                """
            st.markdown(
                f"""
    <div class="hitl-alert">
        <h3>⚠️ HUMAN ACTION REQUIRED</h3>
        <p>{reason}</p>
        {elapsed_time_html}
        <p style="margin-top:0.75rem; font-size:0.85rem; color:var(--text-secondary);">
            Please check the headed browser, log in, inject hints if needed, then click <b>Resume Agent</b> below.
        </p>
    </div>
    """,
                unsafe_allow_html=True,
            )
            st.markdown("")  # spacing
            if st.button("🔓 Resume Agent", use_container_width=True, type="primary"):
                st.session_state.control_flags["pause_elapsed_time"] = None
                st.session_state.hitl_controller.resume()
                st.rerun()

        # ---- Task Queue ----
        if state.task_queue and state.task_queue.tasks:
            st.markdown("#### 📋 Task Queue")
            if state.task_queue.recon_summary:
                st.caption(state.task_queue.recon_summary)

            for task in state.task_queue.tasks:
                icon = _task_status_icon(task.status)
                is_current = (
                    state.phase == AgentPhase.EXECUTING
                    and state.task_queue.tasks.index(task) == state.current_task_index
                )
                border_extra = "border-left: 3px solid var(--accent-blue);" if is_current else ""
                desc = html.escape(str(task.description))
                st.markdown(
                    f"""
    <div class="task-card" style="{border_extra}">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <span class="task-id">Task #{task.task_id}</span>
            <span class="task-status-icon">{icon}</span>
        </div>
        <div class="task-desc">{desc}</div>
    </div>
    """,
                    unsafe_allow_html=True,
                )

    with right_col:
        # ---- Live AI Thought Process ----
        st.markdown("#### 🧠 Live AI Thought Process")
        
        # Active status badge
        if state.phase in [AgentPhase.RECON, AgentPhase.EXECUTING]:
            st.markdown(
                """
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 16px; background: rgba(88, 166, 255, 0.1); border: 1px solid rgba(88, 166, 255, 0.25); border-radius: 8px; padding: 10px 14px;">
                    <span class="pulse-dot"></span>
                    <span style="font-size: 0.85rem; font-weight: 500; color: #58a6ff; font-family: 'Inter', sans-serif;">Agent is actively thinking and executing browser actions...</span>
                </div>
                <style>
                .pulse-dot {
                    display: inline-block;
                    width: 8px;
                    height: 8px;
                    background-color: #58a6ff;
                    border-radius: 50%;
                    box-shadow: 0 0 8px #58a6ff;
                    animation: pulse-blue 1.8s infinite;
                }
                @keyframes pulse-blue {
                    0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(88, 166, 255, 0.7); }
                    70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(88, 166, 255, 0); }
                    100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(88, 166, 255, 0); }
                }
                </style>
                """,
                unsafe_allow_html=True
            )
        elif state.phase == AgentPhase.PAUSED_HITL:
            st.markdown(
                """
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 16px; background: rgba(248, 81, 73, 0.1); border: 1px solid rgba(248, 81, 73, 0.25); border-radius: 8px; padding: 10px 14px;">
                    <span class="pulse-dot-red"></span>
                    <span style="font-size: 0.85rem; font-weight: 500; color: #f85149; font-family: 'Inter', sans-serif;">Agent is paused waiting for user input.</span>
                </div>
                <style>
                .pulse-dot-red {
                    display: inline-block;
                    width: 8px;
                    height: 8px;
                    background-color: #f85149;
                    border-radius: 50%;
                    box-shadow: 0 0 8px #f85149;
                    animation: pulse-red 1.8s infinite;
                }
                @keyframes pulse-red {
                    0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(248, 81, 73, 0.7); }
                    70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(248, 81, 73, 0); }
                    100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(248, 81, 73, 0); }
                }
                </style>
                """,
                unsafe_allow_html=True
            )
        else:
            st.markdown(
                """
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 16px; background: rgba(139, 148, 158, 0.08); border: 1px solid rgba(139, 148, 158, 0.2); border-radius: 8px; padding: 10px 14px;">
                    <span style="display: inline-block; width: 8px; height: 8px; background-color: #8b949e; border-radius: 50%;"></span>
                    <span style="font-size: 0.85rem; font-weight: 500; color: #8b949e; font-family: 'Inter', sans-serif;">Agent is currently idle.</span>
                </div>
                """,
                unsafe_allow_html=True
            )

        # Render thoughts
        if state.model_thinking or state.model_next_goal or state.model_memory or state.model_last_actions:
            if state.model_thinking:
                escaped_thinking = html.escape(str(state.model_thinking))
                st.markdown(
                    f"""
                    <div class="cog-card">
                        <div class="cog-header cog-header-thinking">
                            <span>🧠</span> Model Thinking
                        </div>
                        <div class="cog-content" style="white-space: pre-wrap;">{escaped_thinking}</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            if state.model_next_goal:
                escaped_goal = html.escape(str(state.model_next_goal))
                st.markdown(
                    f"""
                    <div class="cog-card">
                        <div class="cog-header cog-header-goal">
                            <span>🎯</span> Next Goal
                        </div>
                        <div class="cog-content">{escaped_goal}</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            if state.model_memory:
                escaped_memory = html.escape(str(state.model_memory))
                st.markdown(
                    f"""
                    <div class="cog-card">
                        <div class="cog-header cog-header-memory">
                            <span>💾</span> Memory
                        </div>
                        <div class="cog-content-memory">{escaped_memory}</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

            if state.model_last_actions:
                actions_badges = "".join(f'<span style="display:inline-block; font-size:0.75rem; font-family:\'JetBrains Mono\', monospace; padding:3px 8px; margin:2px; background:rgba(6,182,212,0.12); border:1px solid rgba(6,182,212,0.3); border-radius:4px; color:var(--accent-cyan);">{html.escape(str(act))}</span>' for act in state.model_last_actions)
                st.markdown(
                    f"""
                    <div class="cog-card">
                        <div class="cog-header cog-header-actions">
                            <span>⚡</span> Last Action(s) executed
                        </div>
                        <div style="margin-top: 4px;">{actions_badges}</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
        else:
            st.markdown(
                """
                <div class="cog-card" style="border-style: dashed; text-align: center; color: var(--text-secondary); padding: 2rem 1rem;">
                    <div style="font-size: 1.8rem; margin-bottom: 8px;">🧠</div>
                    <div style="font-size: 0.85rem;">Thought processes and execution states will appear here in real-time once the agent starts processing.</div>
                </div>
                """,
                unsafe_allow_html=True
            )

        st.divider()

        # ---- Live Logs ----
        st.markdown("#### 📜 Live Logs")
        terminal_header_html = """
        <div class="terminal-header">
            <span class="terminal-title">OMNITASKER // CORE EXECUTION LOGS</span>
            <div class="terminal-dots">
                <span class="terminal-dot dot-red"></span>
                <span class="terminal-dot dot-yellow"></span>
                <span class="terminal-dot dot-green"></span>
            </div>
        </div>
        """
        if state.logs:
            log_html = "".join(_render_log_entry(e) for e in state.logs)
            st.markdown(
                f'{terminal_header_html}<div class="log-panel">{log_html}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'{terminal_header_html}<div class="log-panel" style="color:var(--text-secondary); text-align:center; padding:2rem;">'
                "Waiting for agent to start…</div>",
                unsafe_allow_html=True,
            )

        # ---- Error details ----
        if state.error_message:
            with st.expander("🔍 Error Details", expanded=True):
                st.code(state.error_message, language="text")

# Render the dynamic dashboard fragment
render_dashboard()
