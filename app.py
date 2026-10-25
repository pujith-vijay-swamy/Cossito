"""
app.py — Streamlit frontend for Cossito.

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
from pathlib import Path
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
    page_title="Cossito",
    page_icon=str(Path(__file__).resolve().parent / "assets" / "cossito-mark.png"),
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Custom CSS — premium dark UI
# ---------------------------------------------------------------------------

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Anton&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');

:root {
    --bg-primary: #0b1117;
    --bg-secondary: #121c25;
    --bg-card: #121c25;
    --border-main: 3px solid #263644;
    --border-accent: 3px solid #86e3c3;
    --border-black: 3px solid #263644;
    --accent-purple: #86e3c3;
    --accent-cyan: #8bbcf4;
    --accent-blue: #86e3c3;
    --accent-green: #86e3c3;
    --accent-yellow: #edcc8f;
    --accent-red: #f29aab;
    --shadow-hard: 4px 4px 0px #000000;
    --shadow-purple: 4px 4px 0px #86e3c3;
    --shadow-cyan: 4px 4px 0px #8bbcf4;
    --shadow-yellow: 4px 4px 0px #edcc8f;
    --text-primary: #edf3f5;
    --text-secondary: #a5b6c5;
}

/* Graphite surfaces with mint accents */
.stApp {
    font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
    background-color: var(--bg-primary);
    color: var(--text-primary);
}

/* Compact controls */
.stButton > button {
    font-family: 'Outfit', sans-serif !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    padding: 8px 14px !important;
    letter-spacing: 0.04em !important;
    text-transform: none !important;
    border: 1px solid #263644 !important;
    border-radius: 10px !important;
    background-color: #263644 !important;
    color: #edf3f5 !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    transition: all 0.12s ease !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 6px !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    background-color: #314757 !important;
    color: #edf3f5 !important;
}
.stButton > button:active {
    transform: translateY(-1px) !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}
.stButton > button[kind="primary"] {
    background: #86e3c3 !important;
    color: #000000 !important;
    border: 1px solid #263644 !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}
.stButton > button[kind="primary"]:hover {
    background: #b6a6ef !important;
    color: #000000 !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}

/* BaseWeb Input & Select Box Containers — Fix Double Border & Focus Ring */
div[data-baseweb="input"], 
div[data-baseweb="base-input"], 
div[data-baseweb="select"] > div,
div[data-baseweb="textarea"] {
    background-color: #121c25 !important;
    border: 1px solid #263644 !important;
    border-radius: 12px !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    transition: all 0.15s ease !important;
    outline: none !important;
}

div[data-baseweb="input"]:focus-within, 
div[data-baseweb="base-input"]:focus-within, 
div[data-baseweb="select"] > div:focus-within,
div[data-baseweb="textarea"]:focus-within {
    border-color: #86e3c3 !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}

/* Inner input text & textarea elements — Strip inner borders */
div[data-baseweb="input"] input, 
div[data-baseweb="base-input"] input, 
textarea {
    border: none !important;
    outline: none !important;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    background: transparent !important;
    color: #edf3f5 !important;
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
    color: #edf3f5 !important;
    font-weight: 600 !important;
}

/* Header Banner */
.agent-header {
    background: #121c25;
    border: 1px solid #86e3c3;
    border-radius: 16px;
    padding: 2.2rem 2.6rem;
    margin-bottom: 2rem;
    position: relative;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}
.agent-header h1 {
    font-family: 'Outfit', sans-serif;
    font-size: 2.8rem;
    font-weight: 700;
    letter-spacing: -0.03em;
    color: #edf3f5;
    text-transform: none;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 12px;
}
.agent-header h1 span.tag {
    font-size: 0.8rem;
    padding: 4px 12px;
    background: #86e3c3;
    color: #000000;
    border: 1px solid #263644;
    border-radius: 8px;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    font-weight: 600;
}
.agent-header p {
    color: #a5b6c5;
    font-size: 1.05rem;
    margin-top: 0.6rem;
    font-weight: 600;
    line-height: 1.5;
}

/* Semantic status badges */
.status-badge {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 10px 24px;
    border-radius: 12px;
    font-family: 'Outfit', sans-serif;
    font-size: 0.95rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: none;
    border: 1px solid #263644;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    margin-bottom: 1.5rem;
}
.status-idle       { background: #263644; color: #a5b6c5; }
.status-recon      { background: #8bbcf4; color: #000000; }
.status-executing  { background: #86e3c3; color: #000000; }
.status-paused     { background: #f29aab; color: #edf3f5; }
.status-done       { background: #86e3c3; color: #000000; }
.status-error      { background: #f29aab; color: #edf3f5; }

/* Task Cards */
.task-card {
    background: #121c25;
    border: 1px solid #263644;
    border-radius: 14px;
    padding: 1.3rem 1.6rem;
    margin-bottom: 1rem;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    transition: all 0.15s ease;
}
.task-card:hover {
    border-color: #86e3c3;
    transform: translateY(-1px);
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}
.task-card .task-id {
    font-family: 'Outfit', sans-serif;
    font-size: 0.85rem;
    font-weight: 700;
    text-transform: none;
    letter-spacing: 0.08em;
    color: #86e3c3;
    background: #263644;
    padding: 2px 10px;
    border: 1px solid #263644;
    border-radius: 6px;
}
.task-card .task-desc {
    font-size: 0.98rem;
    color: #edf3f5;
    font-weight: 600;
    margin-top: 10px;
    line-height: 1.5;
}
.task-status-icon { font-size: 1.25rem; }

/* Cognitive cards */
.cog-card {
    background: #121c25;
    border: 1px solid #263644;
    border-radius: 14px;
    padding: 1.4rem 1.6rem;
    margin-bottom: 1.2rem;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    transition: all 0.15s ease;
}
.cog-card:hover {
    border-color: #8bbcf4;
    transform: translateY(-1px);
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}
.cog-header {
    font-family: 'Outfit', sans-serif;
    font-size: 0.88rem;
    font-weight: 700;
    text-transform: none;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 8px;
    letter-spacing: 0.06em;
}
.cog-header-thinking { color: #8bbcf4; }
.cog-header-goal     { color: #86e3c3; }
.cog-header-memory   { color: #b6a6ef; }
.cog-header-actions  { color: #edcc8f; }

.cog-content {
    font-size: 0.96rem;
    line-height: 1.6;
    color: #edf3f5;
    font-weight: 500;
}
.cog-content-memory {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.88rem;
    line-height: 1.5;
    color: #8bbcf4;
    background: #0b1117;
    padding: 14px;
    border-radius: 10px;
    border: 1px solid #263644;
    white-space: pre-wrap;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}

/* Failed Injection Card */
.failed-injection-card {
    background: #121c25;
    border: 1px solid #edcc8f;
    border-radius: 14px;
    padding: 1.4rem 1.6rem;
    margin-top: 1rem;
    margin-bottom: 1.5rem;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}
.failed-injection-header {
    font-family: 'Outfit', sans-serif;
    font-size: 0.95rem;
    font-weight: 700;
    text-transform: none;
    color: #edcc8f;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 8px;
    letter-spacing: 0.05em;
}
.failed-injection-desc {
    font-size: 0.92rem;
    color: #c6d2dd;
    margin-bottom: 12px;
    line-height: 1.5;
    font-weight: 500;
}

/* Log Terminal Panel */
.terminal-header {
    background: #263644;
    border: 1px solid #263644;
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
    font-weight: 600;
    color: #edf3f5;
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
    border: 1.5px solid #263644;
}
.dot-red { background-color: #f29aab; }
.dot-yellow { background-color: #edcc8f; }
.dot-green { background-color: #86e3c3; }

.log-panel {
    background: #0b1117;
    border: 1px solid #263644;
    border-bottom-left-radius: 14px;
    border-bottom-right-radius: 14px;
    padding: 1.25rem;
    max-height: 480px;
    overflow-y: auto;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.88rem;
    line-height: 1.6;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}
.log-entry {
    padding: 4px 0;
    border-bottom: 1px solid #121c25;
}
.log-time {
    color: #91a3b3;
    margin-right: 10px;
    font-size: 0.8rem;
    font-weight: 600;
}
.log-info    { color: #8bbcf4; font-weight: 500; }
.log-success { color: #86e3c3; font-weight: 700; }
.log-warn    { color: #edcc8f; font-weight: 600; }
.log-error   { color: #f29aab; font-weight: 700; }

/* HITL Alert */
.hitl-alert {
    background: #121c25;
    border: 1px solid #f29aab;
    border-radius: 16px;
    padding: 1.8rem 2.2rem;
    text-align: center;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
    animation: neo-pulse 2s infinite;
}
@keyframes neo-pulse {
    0%, 100% { box-shadow: 0 8px 28px rgba(0, 0, 0, .16); }
    50%      { box-shadow: 0 8px 28px rgba(0, 0, 0, .16); }
}
.hitl-alert h3 {
    font-family: 'Outfit', sans-serif;
    color: #f29aab;
    margin: 0 0 0.6rem 0;
    font-size: 1.4rem;
    font-weight: 700;
    letter-spacing: 0.03em;
    text-transform: none;
}
.hitl-alert p {
    color: #c6d2dd;
    margin: 0;
    line-height: 1.5;
    font-weight: 500;
}

/* Progress bar */
.progress-bar-container {
    background: #121c25;
    border: 1px solid #263644;
    border-radius: 10px;
    height: 16px;
    overflow: hidden;
    margin: 1rem 0;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
}
.progress-bar-fill {
    height: 100%;
    border-radius: 8px;
    background: linear-gradient(90deg, #8bbcf4, #86e3c3, #86e3c3);
    border-right: 1px solid #263644;
    transition: width 0.4s ease;
}

/* Cossito / kinetic poster system. Presentation only. */
:root {
    --bg-primary: #101014; --bg-secondary: #19191f; --bg-card: #202027;
    --accent-purple: #c1a0ff; --accent-cyan: #94d5ff; --accent-blue: #e9ff69;
    --accent-green: #b9f887; --accent-yellow: #e9ff69; --accent-red: #ff8b98;
    --text-primary: #fffbed; --text-secondary: #b9b6c5;
}
.stApp {
    font-family: 'Space Grotesk', 'Segoe UI', sans-serif; color-scheme: dark; color: #fffbed;
    background: radial-gradient(ellipse at 95% 0%, #633e8c35, transparent 44%), radial-gradient(ellipse at 0% 65%, #ff794310, transparent 35%), #101014;
}
[data-testid="stHeader"] { background: #101014ee; border-bottom: 1px solid #3e3a47; }
[data-testid="stSidebar"] { background: #17171d; border-right: 2px solid #44404d; }
[data-testid="stSidebar"] > div:first-child { padding-top: 2rem; }
[data-testid="stMainBlockContainer"] { max-width: 1640px; padding: 2rem 3rem 4rem; }
[data-testid="stMarkdownContainer"] h3, [data-testid="stMarkdownContainer"] h4 {
    font-family: 'Space Grotesk', 'Segoe UI', sans-serif; font-weight: 700; letter-spacing: -.04em; color: #fffbed;
}
[data-testid="stWidgetLabel"] p { color: #d8d2e2; font-size: .83rem; font-weight: 600; }
[data-testid="stCaptionContainer"] { color: #b9b6c5; }
hr { border-color: #44404d !important; }
[data-testid="stMetric"] { background: #202027; border: 2px solid #6c6578; border-radius: 8px; padding: 18px 22px; box-shadow: 5px 5px 0 #050507; }
[data-testid="stMetricValue"] { color: #e9ff69; font-family: 'Space Grotesk', sans-serif; font-weight: 700; }
.agent-header {
    isolation: isolate; position: relative; overflow: hidden; padding: 2.4rem 2.7rem 2rem;
    border: 2px solid #fffbed; border-radius: 10px; margin-bottom: 2.4rem;
    background: linear-gradient(115deg, #ffb078 0%, #ff855e 48%, #f278b4 100%);
    box-shadow: 8px 8px 0 #050507, 9px 9px 0 #706273;
    animation: cossito-reveal .75s cubic-bezier(.16,1,.3,1) both;
}
.agent-header::before {
    content: ''; pointer-events: none; position: absolute; z-index: -1; width: 380px; height: 380px;
    right: -90px; top: -125px; border-radius: 50%; border: 1px solid #24192740;
    background: repeating-conic-gradient(from 0deg, #29152820 0deg 1deg, transparent 1deg 12deg);
    box-shadow: 0 0 0 28px #fffbed12, 0 0 0 29px #20102320, 0 0 0 66px #fffbed0c;
    animation: cossito-orbit 40s linear infinite;
}
.agent-header::after {
    content: ''; pointer-events: none; position: absolute; z-index: -1; right: 16%; top: -50%; width: 100px; height: 230%;
    background: #fffbed23; transform: rotate(25deg); animation: cossito-light 12s ease-in-out infinite alternate;
}
.agent-header h1 {
    font-family: 'Anton', 'Impact', 'Arial Narrow', sans-serif; text-transform: uppercase;
    color: #17131d; font-size: clamp(4.5rem, 8.5vw, 8.5rem); font-weight: 400;
    letter-spacing: -.035em; line-height: 1.02; margin: 0 0 1.1rem; display: block;
    text-shadow: 2px 2px 0 #fffbed, 5px 5px 0 #17131d30; transform: skewX(-3deg);
}
.agent-header p { color: #30202c; max-width: 620px; font-family: 'Space Grotesk', 'Segoe UI', sans-serif; font-size: .95rem; font-weight: 500; line-height: 1.7; margin: 0; }
.stButton > button {
    min-height: 44px; font-family: 'Space Grotesk', 'Segoe UI', sans-serif !important; font-size: .86rem !important; font-weight: 700 !important;
    text-transform: none !important; letter-spacing: -.015em !important; border: 2px solid #89808f !important;
    border-radius: 7px !important; background: #26242e !important; color: #fffbed !important;
    box-shadow: 4px 4px 0 #050507 !important; transition: transform .18s ease, box-shadow .18s ease, background .18s ease !important;
}
.stButton > button:hover { background: #38313f !important; color: #fffbed !important; transform: translate(-2px,-2px) rotate(-.6deg) !important; box-shadow: 6px 6px 0 #050507 !important; border-color: #c1a0ff !important; }
.stButton > button:active { transform: translate(3px,3px) !important; box-shadow: 1px 1px 0 #050507 !important; }
.stButton > button[kind="primary"] { background: #e9ff69 !important; color: #1a2010 !important; border-color: #e9ff69 !important; box-shadow: 4px 4px 0 #050507, 5px 5px 0 #778a40 !important; }
.stButton > button[kind="primary"]:hover { background: #f4ffb0 !important; color: #1a2010 !important; box-shadow: 6px 6px 0 #050507, 7px 7px 0 #e9ff69 !important; }
.stButton > button:disabled { opacity: .4; transform: none !important; box-shadow: none !important; }
.stButton > button:focus-visible { outline: 2px solid #fffbed !important; outline-offset: 4px !important; }
div[data-baseweb="input"], div[data-baseweb="select"] > div, div[data-baseweb="textarea"] { background: #101014 !important; border: 2px solid #5e566b !important; border-radius: 7px !important; box-shadow: 3px 3px 0 #050507 !important; }
div[data-baseweb="base-input"] { background: transparent !important; border: 0 !important; box-shadow: none !important; }
div[data-baseweb="input"] input, div[data-baseweb="base-input"] input, textarea { font-family: 'Space Grotesk', 'Segoe UI', sans-serif !important; color: #fffbed !important; font-weight: 400 !important; }
div[data-baseweb="input"]:focus-within, div[data-baseweb="select"] > div:focus-within, div[data-baseweb="textarea"]:focus-within { border-color: #c1a0ff !important; box-shadow: 3px 3px 0 #c1a0ff !important; }
div[data-baseweb="select"] input { caret-color: #c1a0ff !important; user-select: text !important; }
div[data-baseweb="popover"] [role="listbox"] { background: #202027; color: #fffbed; border: 2px solid #6c6578; }
.status-badge { padding: 8px 15px; font: 700 .78rem 'Space Grotesk', sans-serif; letter-spacing: .015em; border: 2px solid #050507; border-radius: 5px; box-shadow: 3px 3px 0 #050507; margin-bottom: 1rem; transform: rotate(-1deg); }
.status-idle { color: #d8d2e2; background: #302b37; }
.status-recon { color: #142b3b; background: #94d5ff; }
.status-executing { color: #242e0b; background: #e9ff69; animation: cossito-status 3s ease-in-out infinite; }
.status-done { color: #21162f; background: #c1a0ff; }
.status-paused { color: #361d0c; background: #ffb078; }
.status-error { color: #39151c; background: #ff8b98; }
.task-card, .cog-card { background: #202027; padding: 1.4rem 1.5rem; border: 2px solid #635a70; border-radius: 9px; box-shadow: 5px 5px 0 #050507; transition: transform .2s ease, box-shadow .2s ease, border-color .2s ease; }
.task-card:hover { border-color: #e9ff69; transform: translate(-2px,-3px); box-shadow: 7px 8px 0 #050507, 8px 9px 0 #e9ff6970; }
.cog-card:hover { border-color: #c1a0ff; transform: translate(-2px,-3px); box-shadow: 7px 8px 0 #050507, 8px 9px 0 #c1a0ff70; }
.task-card .task-id { font: 500 .7rem 'JetBrains Mono', monospace; background: #e9ff69; color: #242e0b; border: 1px solid #050507; border-radius: 4px; padding: 4px 9px; letter-spacing: .025em; }
.task-card .task-desc, .cog-content { font-family: 'Space Grotesk', 'Segoe UI', sans-serif; font-size: .94rem; line-height: 1.8; font-weight: 400; color: #eee7f3; overflow-wrap: anywhere; }
.cog-header { font: 700 .78rem 'Space Grotesk', sans-serif; letter-spacing: .025em; }
.cog-header-thinking { color: #94d5ff; }.cog-header-goal { color: #e9ff69; }.cog-header-memory { color: #c1a0ff; }.cog-header-actions { color: #ffb078; }
.cog-content-memory { background: #131117; color: #cfc0ef; border: 1px solid #5e566b; box-shadow: none; font-size: .8rem; }
.terminal-header { background: #c1a0ff; border: 2px solid #050507; border-bottom: 0; padding: 13px 18px; border-radius: 7px 7px 0 0; }
.terminal-title { font: 700 .67rem 'JetBrains Mono', monospace; color: #241535; letter-spacing: .08em; }
.terminal-dot { width: 8px; height: 8px; border: 1px solid #241535; }.dot-red { background: #ff8b98; }.dot-yellow { background: #e9ff69; }.dot-green { background: #b9f887; }
.log-panel { background: #131117; border: 2px solid #5e566b; border-top: 0; box-shadow: 5px 5px 0 #050507; font-size: .8rem; line-height: 1.8; border-radius: 0 0 7px 7px; overflow-wrap: anywhere; }
.log-time { color: #ada2ba; font-size: .73rem; font-weight: 400; }.log-info { color: #94d5ff; }.log-success { color: #b9f887; }.log-warn { color: #e9ff69; }.log-error { color: #ff8b98; }
.log-entry { padding: 7px 0; border-bottom-color: #3b3345; }
.hitl-alert, .failed-injection-card { background: #2e211f; border: 2px solid #ffb078; box-shadow: 5px 5px 0 #050507; animation: none; }
.hitl-alert h3, .failed-injection-header { color: #ffb078; font-family: 'Space Grotesk', sans-serif; font-weight: 700; }
.progress-bar-container { background: #28222f; height: 12px; border: 2px solid #635a70; border-radius: 4px; box-shadow: 3px 3px 0 #050507; }
.progress-bar-fill { border: none; border-radius: 1px; background: repeating-linear-gradient(115deg, #e9ff69 0 16px, #bbd344 16px 24px); background-size: 53px 100%; animation: cossito-flow 2s linear infinite; }
.stApp * { scrollbar-width: thin; scrollbar-color: #736182 #17171d; }
@keyframes cossito-reveal { from { opacity: 0; transform: translateY(18px) rotate(-.7deg); } to { opacity: 1; transform: none; } }
@keyframes cossito-orbit { to { transform: rotate(360deg); } }
@keyframes cossito-light { from { transform: translateX(-50px) rotate(25deg); } to { transform: translateX(150px) rotate(25deg); } }
@keyframes cossito-flow { to { background-position: 53px 0; } }
@keyframes cossito-status { 0%,100% { box-shadow: 3px 3px 0 #050507; } 50% { box-shadow: 3px 3px 0 #050507, 0 0 16px #e9ff6925; } }
@media (max-width: 760px) {
    [data-testid="stMainBlockContainer"] { padding: 1.25rem 1.1rem 2rem; }
    .agent-header { padding: 1.8rem 1.3rem; margin-bottom: 1.8rem; box-shadow: 5px 5px 0 #050507; }
    .agent-header h1 { font-size: clamp(3.5rem, 17vw, 6rem); }
    .agent-header p { font-size: .85rem; }.agent-header::before { right: -260px; }
    .task-card, .cog-card { padding: 1.1rem; }.terminal-title { letter-spacing: 0; }
}
/* Electric control desk. Header and workspace branding stay unchanged. */
[data-testid="stSidebar"] {
    background: radial-gradient(circle, #20142e12 1px, transparent 1px) 0 0 / 10px 10px,
                linear-gradient(145deg, #dfd0ff, #c1a5f4 48%, #efcfdf);
    background-size: 10px 10px, 180% 180%;
    animation: cossito-panel-grade 18s ease-in-out infinite alternate;
    border-right: 3px solid #08060d; box-shadow: 7px 0 0 #c1a0ff16;
    color: #271a39; color-scheme: dark;
}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p { color: #36234c; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h3 {
    color: #21132f; font-size: 1.24rem; font-weight: 700; letter-spacing: -.055em;
    padding: 8px 0 12px; border-bottom: 2px solid #392247; margin-bottom: 16px;
}
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p { font-weight: 700; font-size: .78rem; letter-spacing: .015em; }
[data-testid="stSidebar"] hr { border-color: #57396950 !important; margin: 1.6rem 0 !important; }
[data-testid="stSidebar"] code { background: #2b1f3a; color: #e9ff69; border: 1px solid #574366; border-radius: 4px; }
[data-testid="stSidebar"] a { color: #4b237b; text-decoration-color: #4b237b80; }
[data-testid="stSidebar"] div[data-baseweb="input"],
[data-testid="stSidebar"] div[data-baseweb="select"] > div,
[data-testid="stSidebar"] div[data-baseweb="textarea"] {
    background: #241b31 !important; border: 2px solid #160e20 !important;
    border-radius: 9px !important; box-shadow: 3px 4px 0 #8668ab !important;
    transition: box-shadow .2s ease, border-color .2s ease !important;
}
[data-testid="stSidebar"] div[data-baseweb="input"]:focus-within,
[data-testid="stSidebar"] div[data-baseweb="select"] > div:focus-within,
[data-testid="stSidebar"] div[data-baseweb="textarea"]:focus-within {
    border-color: #e9ff69 !important; box-shadow: 3px 4px 0 #281b39, 0 0 0 3px #e9ff6940 !important;
}
[data-testid="stSidebar"] input::placeholder,
[data-testid="stSidebar"] textarea::placeholder { color: #b8a6ce !important; opacity: 1; }
[data-testid="stSidebar"] [data-testid="stSidebarCollapseButton"] button { color: #271a39; }
[data-testid="stSidebar"] [data-testid="stTooltipIcon"] { color: #4d3264; }
.stButton > button {
    position: relative !important; isolation: isolate; overflow: hidden !important;
    background: #c1a0ff !important; color: #241535 !important;
    border: 2px solid #100b17 !important; border-radius: 9px !important;
    min-height: 46px; padding: 10px 18px !important; font-weight: 700 !important;
    box-shadow: 4px 5px 0 #050507 !important;
    transition: transform .2s cubic-bezier(.2,.9,.3,1.3), box-shadow .2s ease, background .2s ease !important;
}
.stButton > button > * { position: relative; z-index: 1; }
.stButton > button::before {
    content: ''; pointer-events: none; position: absolute; z-index: 0; inset: 0 auto 0 0; width: 5px;
    background: repeating-linear-gradient(135deg, #241535 0 3px, transparent 3px 6px); opacity: .55;
}
.stButton > button::after {
    content: ''; pointer-events: none; position: absolute; z-index: 0; top: -80%; left: -60%; width: 35%; height: 260%;
    background: linear-gradient(90deg, transparent, #ffffff65, transparent);
    transform: translateX(-150%) rotate(24deg);
}
.stButton > button:hover {
    background: #d9c4ff !important; color: #241535 !important;
    transform: translate(-2px,-3px) rotate(-.8deg) !important;
    box-shadow: 6px 8px 0 #050507 !important; border-color: #100b17 !important;
}
.stButton > button:hover::after { animation: cossito-button-sweep .8s ease-out; }
.stButton > button:active { transform: translate(3px,4px) rotate(0) !important; box-shadow: 1px 1px 0 #050507 !important; }
.stButton > button[kind="primary"] {
    background: linear-gradient(110deg, #e9ff69 0%, #c4f55d 55%, #a5ed9e 100%) !important;
    color: #1d2b0d !important; border-color: #1d2b0d !important;
    box-shadow: 4px 5px 0 #050507, 0 0 20px #e9ff6910 !important;
}
.stButton > button[kind="primary"]::after { animation: cossito-button-sweep 6s ease-in-out infinite; }
.stButton > button[kind="primary"]:hover { background: linear-gradient(110deg, #f1ff9c, #b2f79a) !important; color: #1d2b0d !important; box-shadow: 6px 8px 0 #050507, 0 0 25px #e9ff6920 !important; }
[data-testid="stSidebar"] .stButton > button { box-shadow: 4px 5px 0 #2b173f !important; }
[data-testid="stSidebar"] .stButton > button:hover { box-shadow: 6px 8px 0 #2b173f !important; }
[data-testid="stSidebar"] .stButton > button:active { box-shadow: 1px 1px 0 #2b173f !important; }
.stButton > button:focus-visible { outline: 3px solid #fffbed !important; outline-offset: 4px !important; }
[data-testid="stSidebar"] .stButton > button:focus-visible { outline-color: #532a7d !important; }
.stButton > button:disabled, [data-testid="stSidebar"] .stButton > button:disabled { background: #a89bb8 !important; color: #40344f !important; border-color: #756685 !important; opacity: .65 !important; transform: none !important; box-shadow: none !important; cursor: not-allowed !important; }
.stButton > button:disabled::before, .stButton > button:disabled::after { display: none !important; }
@keyframes cossito-panel-grade { from { background-position: 0 0, 0% 0%; } to { background-position: 0 0, 100% 100%; } }
@keyframes cossito-button-sweep { 0%,65% { transform: translateX(-150%) rotate(24deg); } 100% { transform: translateX(650%) rotate(24deg); } }
@media (prefers-reduced-motion: reduce) {
    [data-testid="stSidebar"] { animation: none !important; }
    .stApp *, .stApp *::before, .stApp *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
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
    if target_url_clean:
        from url_security import normalize_target_url
        try:
            target_url_clean = normalize_target_url(target_url_clean)
        except ValueError as exc:
            st.error(str(exc))
            target_url_clean = ""

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
    <h1>Cossito</h1>
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
                    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
                    animation: pulse-blue 1.8s infinite;
                }
                @keyframes pulse-blue {
                    0% { transform: scale(0.95); box-shadow: 0 8px 28px rgba(0, 0, 0, .16); }
                    70% { transform: scale(1); box-shadow: 0 8px 28px rgba(0, 0, 0, .16); }
                    100% { transform: scale(0.95); box-shadow: 0 8px 28px rgba(0, 0, 0, .16); }
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
                    box-shadow: 0 8px 28px rgba(0, 0, 0, .16);
                    animation: pulse-red 1.8s infinite;
                }
                @keyframes pulse-red {
                    0% { transform: scale(0.95); box-shadow: 0 8px 28px rgba(0, 0, 0, .16); }
                    70% { transform: scale(1); box-shadow: 0 8px 28px rgba(0, 0, 0, .16); }
                    100% { transform: scale(0.95); box-shadow: 0 8px 28px rgba(0, 0, 0, .16); }
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
            <span class="terminal-title">COSSITO // CORE EXECUTION LOGS</span>
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
