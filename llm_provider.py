"""
llm_provider.py — LLM factory for multiple providers (OpenRouter, OpenAI, Anthropic, Gemini).
"""

from __future__ import annotations

import os
import re
import json
from dotenv import load_dotenv

# Load local environment variables from .env file
load_dotenv()
from typing import Any, Mapping, Optional, Type, TypeVar
from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.google.chat import ChatGoogle
from langchain_core.language_models.chat_models import BaseChatModel


def repair_json(s: str) -> str:
    """A highly robust, state-machine based JSON repair function that can fix
    truncated JSON objects, lists, unescaped strings, and unclosed brackets.
    """
    s = s.strip()
    if not s:
        return "{}"
    
    # Find the first '{' or '['
    first_brace = s.find('{')
    first_bracket = s.find('[')
    if first_brace == -1 and first_bracket == -1:
        return "{}"
    
    start_idx = first_brace if (first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket)) else first_bracket
    s = s[start_idx:]
    
    # State tracking
    in_string = False
    escape = False
    stack = []  # keeps track of '{' and '['
    rebuilt = []
    
    i = 0
    while i < len(s):
        char = s[i]
        
        if in_string:
            if escape:
                rebuilt.append(char)
                escape = False
            elif char == '\\':
                rebuilt.append(char)
                escape = True
            elif char == '"':
                rebuilt.append(char)
                in_string = False
            else:
                rebuilt.append(char)
        else:
            if char == '"':
                rebuilt.append(char)
                in_string = True
                escape = False
            elif char in ('{', '['):
                rebuilt.append(char)
                stack.append(char)
            elif char in ('}', ']'):
                if char == '}' and stack and stack[-1] == '{':
                    rebuilt.append(char)
                    stack.pop()
                elif char == ']' and stack and stack[-1] == '[':
                    rebuilt.append(char)
                    stack.pop()
                if not stack:
                    # Closed outermost structure, ignore remaining trailing text/chatter
                    break
            elif char in (':', ','):
                rebuilt.append(char)
            elif char.isspace() or char in '-0123456789.tfn':  # tfn for true/false/null
                rebuilt.append(char)
            else:
                rebuilt.append(char)
        i += 1
        
    if in_string:
        if rebuilt and rebuilt[-1] == '\\':
            rebuilt.pop()
        rebuilt.append('"')
        in_string = False
        
    rebuilt_str = "".join(rebuilt).strip()
    
    while True:
        original_len = len(rebuilt_str)
        rebuilt_str = rebuilt_str.strip()
        
        if rebuilt_str.endswith(','):
            rebuilt_str = rebuilt_str[:-1]
        elif rebuilt_str.endswith(':'):
            rebuilt_str = rebuilt_str[:-1].strip()
            if rebuilt_str.endswith('"'):
                m = re.search(r'"[^"\\]*(?:\\.[^"\\]*)*"$', rebuilt_str)
                if m:
                    rebuilt_str = rebuilt_str[:m.start()].strip()
        elif re.search(r'\b(t|tr|tru|f|fa|fal|fals|n|nu|nul)$', rebuilt_str, re.IGNORECASE):
            rebuilt_str = re.sub(r'\b(t|tr|tru|f|fa|fal|fals|n|nu|nul)$', '', rebuilt_str, flags=re.IGNORECASE).strip()
        
        if len(rebuilt_str) == original_len:
            break
            
    final_stack = []
    final_in_string = False
    final_escape = False
    for char in rebuilt_str:
        if final_in_string:
            if final_escape:
                final_escape = False
            elif char == '\\':
                final_escape = True
            elif char == '"':
                final_in_string = False
        else:
            if char == '"':
                final_in_string = True
            elif char == '{':
                final_stack.append('{')
            elif char == '[':
                final_stack.append('[')
            elif char == '}':
                if final_stack and final_stack[-1] == '{':
                    final_stack.pop()
            elif char == ']':
                if final_stack and final_stack[-1] == '[':
                    final_stack.pop()
                    
    closing_chars = []
    for item in reversed(final_stack):
        if item == '{':
            closing_chars.append('}')
        elif item == '[':
            closing_chars.append(']')
            
    rebuilt_str += "".join(closing_chars)
    return rebuilt_str


class InterceptingCompletions:
    def __init__(self, original_completions: Any) -> None:
        self._original = original_completions
        
    async def create(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._original.create(*args, **kwargs)
        if response and response.choices:
            choice = response.choices[0]
            if choice.message and choice.message.content:
                raw_text = choice.message.content
                cleaned_text = self._clean_json(raw_text)
                choice.message.content = cleaned_text
        return response
        
    def _clean_json(self, text: str) -> str:
        if not text:
            return text
        try:
            return repair_json(text)
        except Exception:
            # Fallback to simple clean if repair fails
            trimmed = text.strip()
            # Remove markdown code blocks
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", trimmed, re.DOTALL)
            if m:
                return m.group(1).strip()
            # Match outermost curly braces
            m = re.search(r"\{.*\}", trimmed, re.DOTALL)
            if m:
                return m.group(0).strip()
            return trimmed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class InterceptingChat:
    def __init__(self, original_chat: Any) -> None:
        self._original = original_chat
        self.completions = InterceptingCompletions(original_chat.completions)
        
    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class InterceptingClient:
    def __init__(self, original_client: Any) -> None:
        self._original = original_client
        self.chat = InterceptingChat(original_client.chat)
        
    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


class RobustChatOpenAI(ChatOpenAI):
    """Transparent wrapper for browser-use ChatOpenAI that intercepts API responses
    and safely extracts pure JSON output before the strict Pydantic validation runs.
    """
    def get_client(self) -> Any:
        original_client = super().get_client()
        return InterceptingClient(original_client)


def get_llm(
    model: str = "google/gemma-4-31b-it:free",
    temperature: float = 0.2,
    api_key: str | None = None,
    provider: str = "OpenRouter",
    base_url: str | None = None,
) -> BaseChatModel:
    """Create a configured chat model instance based on the selected provider and credentials.

    Args:
        model: The model identifier (e.g. google/gemma-4-31b-it:free, gpt-4o-mini).
        temperature: Sampling temperature.
        api_key: Explicit API key; falls back to environment variables.
        provider: API Provider name ("OpenRouter", "OpenAI", "Anthropic", "Google (Gemini)", "Ollama (Local)", "LM Studio / Local (vLLM / llama.cpp)", "Hugging Face (Inference API)").
        base_url: Optional custom API base URL for local or hosted endpoints.

    Returns:
        A ready-to-use LangChain-compatible ChatModel instance.

    Raises:
        ValueError: If no API key is found for a provider that requires one.
    """
    # 1. Direct OpenAI Provider
    if provider == "OpenAI":
        resolved_key = api_key or os.environ.get("OPENAI_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "OpenAI API key is required. "
                "Set the OPENAI_API_KEY env var or pass api_key explicitly."
            )
        return ChatOpenAI(
            model=model if model else "gpt-4o-mini",
            api_key=resolved_key,
            temperature=temperature,
        )

    # 2. Direct Anthropic Provider
    elif provider == "Anthropic":
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "Anthropic API key is required. "
                "Set the ANTHROPIC_API_KEY env var or pass api_key explicitly."
            )
        return ChatAnthropic(
            model=model if model else "claude-3-5-sonnet-latest",
            api_key=resolved_key,
            temperature=temperature,
        )

    # 3. Direct Google Gemini Provider
    elif provider == "Google (Gemini)":
        resolved_key = api_key or os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "Gemini API key is required. "
                "Set the GEMINI_API_KEY env var or pass api_key explicitly."
            )
        return ChatGoogle(
            model=model if model else "gemini-2.5-flash",
            api_key=resolved_key,
            temperature=temperature,
        )

    # 4. Ollama (Local) Provider
    elif provider == "Ollama (Local)":
        resolved_base = base_url or "http://localhost:11434/v1"
        return RobustChatOpenAI(
            model=model if model else "nemotron-3-super:cloud",
            api_key=api_key if api_key else "ollama",
            base_url=resolved_base,
            temperature=temperature,
            max_completion_tokens=4096,
            dont_force_structured_output=True,
            add_schema_to_system_prompt=True,
        )

    # 5. LM Studio / Local (vLLM / llama.cpp) Provider
    elif provider == "LM Studio / Local (vLLM / llama.cpp)":
        resolved_base = base_url or "http://localhost:1234/v1"
        return RobustChatOpenAI(
            model=model if model else "meta-llama-3-8b-instruct",
            api_key=api_key if api_key else "local",
            base_url=resolved_base,
            temperature=temperature,
            max_completion_tokens=4096,
            dont_force_structured_output=True,
            add_schema_to_system_prompt=True,
        )

    # 6. Hugging Face (Inference API) Provider
    elif provider == "Hugging Face (Inference API)":
        resolved_key = api_key or os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "Hugging Face Access Token (HF_TOKEN) is required for serverless Inference. "
                "Set the HF_TOKEN env var or pass key explicitly."
            )
        resolved_base = base_url or "https://api-inference.huggingface.co/v1/"
        return RobustChatOpenAI(
            model=model if model else "Qwen/Qwen2.5-Coder-32B-Instruct",
            api_key=resolved_key,
            base_url=resolved_base,
            temperature=temperature,
            max_completion_tokens=4096,
            dont_force_structured_output=True,
            add_schema_to_system_prompt=True,
        )

    # 7. OpenRouter Provider (Default)
    else:
        resolved_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "OpenRouter API key is required. "
                "Set the OPENROUTER_API_KEY env var or pass api_key explicitly."
            )

        # OpenRouter uses our robust structured output bypass parameters to enable free tier models cleanly
        return ChatOpenAI(
            model=model if model else "google/gemma-4-31b-it:free",
            api_key=resolved_key,
            temperature=temperature,
            base_url="https://openrouter.ai/api/v1",
            default_headers={"HTTP-Referer": "http://localhost:8501"},
            dont_force_structured_output=True,
            add_schema_to_system_prompt=True,
        )


def get_vision_llm(
    model: str = "google/gemma-4-31b-it:free",
    api_key: str | None = None,
    provider: str = "OpenRouter",
) -> BaseChatModel:
    """Instantiate standard LangChain ChatOpenAI pointing ONLY to the Google Gemini API
    OpenAI compatibility endpoint, using the Gemma 4 vision model.
    """
    from langchain_openai import ChatOpenAI as LangChainChatOpenAI

    # Find the Gemini API key (prioritize .env/OS environment variable first, then sidebar API key)
    resolved_key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
    if not resolved_key:
        resolved_key = api_key or ""
        
    if not resolved_key:
        raise ValueError(
            "A Google Gemini API key (GEMINI_API_KEY) is required to load the Gemma 4 vision assistant. "
            "Please add GEMINI_API_KEY to your '.env' file or enter it in the sidebar."
        )

    # Use Google's standard Gemini API OpenAI compatibility endpoint
    return LangChainChatOpenAI(
        model="gemma-4-31b-it",  # Gemma 4 via Gemini API
        api_key=resolved_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        temperature=0.0,
    )
