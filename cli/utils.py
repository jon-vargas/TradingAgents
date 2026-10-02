import datetime
import questionary
import re
import typer
from typing import List, Optional, Tuple, Dict
from rich.console import Console

from cli.models import AnalystType

console = Console()
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")

ANALYST_ORDER = [
    ("Market Analyst", AnalystType.MARKET),
    ("Social Media Analyst", AnalystType.SOCIAL),
    ("News Analyst", AnalystType.NEWS),
    ("Fundamentals Analyst", AnalystType.FUNDAMENTALS),
]


def get_ticker() -> str:
    """Prompt the user to enter a ticker symbol."""
    def is_valid_ticker(value: str) -> bool:
        return bool(TICKER_PATTERN.match(value.strip().upper()))

    ticker = questionary.text(
        "Enter the ticker symbol to analyze:",
        validate=lambda x: is_valid_ticker(x)
        or "Ticker must be 1-8 chars (A-Z, 0-9, '.' or '-')",
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if not ticker:
        console.print("\n[red]No ticker symbol provided. Exiting...[/red]")
        raise typer.Abort()

    return normalize_ticker(ticker)


def get_analysis_date() -> str:
    """Prompt the user to enter a date in YYYY-MM-DD format."""
    def validate_date(date_str: str) -> bool:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return False
        try:
            parsed = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            return parsed <= datetime.date.today()
        except ValueError:
            return False

    date = questionary.text(
        "Enter the analysis date (YYYY-MM-DD):",
        validate=lambda x: validate_date(x.strip())
        or "Please enter a valid date in YYYY-MM-DD format.",
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if not date:
        console.print("\n[red]No date provided. Exiting...[/red]")
        raise typer.Abort()

    return date.strip()


def normalize_ticker(ticker: Optional[str]) -> Optional[str]:
    if ticker is None:
        return None
    value = ticker.strip().upper()
    if not value:
        raise typer.BadParameter("Ticker cannot be empty.")
    if not TICKER_PATTERN.match(value):
        raise typer.BadParameter("Ticker must be 1-8 chars (A-Z, 0-9, '.' or '-')")
    return value


def normalize_ticker_list(tickers: str) -> List[str]:
    items = [item.strip() for item in tickers.split(",") if item.strip()]
    if not items:
        raise typer.BadParameter("Provide at least one ticker.")
    return [normalize_ticker(item) for item in items]


def _parse_date(value: str, label: str, allow_future: bool, allow_time: bool) -> datetime.datetime:
    try:
        parsed = (
            datetime.datetime.fromisoformat(value)
            if allow_time and "T" in value
            else datetime.datetime.strptime(value, "%Y-%m-%d")
        )
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must be YYYY-MM-DD") from exc

    if not allow_future and parsed.date() > datetime.date.today():
        raise typer.BadParameter(f"{label} cannot be in the future.")
    return parsed


def normalize_date_option(
    value: Optional[str],
    *,
    label: str,
    allow_future: bool = True,
    allow_time: bool = False,
) -> Optional[str]:
    if value is None:
        return None
    _parse_date(value, label, allow_future, allow_time)
    return value


def validate_date_range(
    start: Optional[str],
    end: Optional[str],
    *,
    allow_time: bool = False,
) -> None:
    if not start or not end:
        return
    start_dt = _parse_date(start, "start date", True, allow_time)
    end_dt = _parse_date(end, "end date", True, allow_time)
    if start_dt > end_dt:
        raise typer.BadParameter("Start date must be on or before end date.")


def select_analysts() -> List[AnalystType]:
    """Select analysts using an interactive checkbox."""
    choices = questionary.checkbox(
        "Select Your [Analysts Team]:",
        choices=[
            questionary.Choice(display, value=value) for display, value in ANALYST_ORDER
        ],
        instruction="\n- Press Space to select/unselect analysts\n- Press 'a' to select/unselect all\n- Press Enter when done",
        validate=lambda x: len(x) > 0 or "You must select at least one analyst.",
        style=questionary.Style(
            [
                ("checkbox-selected", "fg:green"),
                ("selected", "fg:green noinherit"),
                ("highlighted", "noinherit"),
                ("pointer", "noinherit"),
            ]
        ),
    ).ask()

    if not choices:
        console.print("\n[red]No analysts selected. Exiting...[/red]")
        raise typer.Abort()

    return choices


def select_research_depth() -> dict:
    """Select research depth/analysis mode using an interactive selection.
    
    Returns:
        dict: Analysis mode configuration with keys:
            - mode: str ("quick", "standard", "deep")
            - max_debate_rounds: int
            - use_perplexity: bool
    """
    from tradingagents.default_config import ANALYSIS_MODES

    # Define research depth options mapped to new analysis modes
    DEPTH_OPTIONS = [
        (
            "🚀 Quick (~30 sec) - Fast scan: basic technicals + 5 headlines, no debates",
            "quick"
        ),
        (
            "📊 Standard (~3-4 min) - Full analysis: all data sources, 1 debate round",
            "standard"
        ),
        (
            "🔬 Deep (~5-6 min) - Research grade: Standard + Perplexity AI deep research, 2 debate rounds",
            "deep"
        ),
    ]

    choice = questionary.select(
        "Select Your [Analysis Mode]:",
        choices=[
            questionary.Choice(display, value=value) for display, value in DEPTH_OPTIONS
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select\n- Deep mode uses Perplexity for earnings, SEC filings, and competitive analysis",
        style=questionary.Style(
            [
                ("selected", "fg:yellow noinherit"),
                ("highlighted", "fg:yellow noinherit"),
                ("pointer", "fg:yellow noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print("\n[red]No analysis mode selected. Exiting...[/red]")
        raise typer.Abort()

    # Get the full mode configuration
    mode_config = ANALYSIS_MODES[choice]
    
    return {
        "mode": choice,
        "max_debate_rounds": mode_config["max_debate_rounds"],
        "max_risk_discuss_rounds": mode_config["max_risk_discuss_rounds"],
        "use_perplexity": mode_config["use_perplexity"],
        "news_limit": mode_config["news_limit"],
        "data_vendors": mode_config["data_vendors"],
        "description": mode_config["description"],
        "estimated_time": mode_config["estimated_time"],
        "estimated_cost": mode_config["estimated_cost"],
    }


def select_shallow_thinking_agent(provider) -> str:
    """Select shallow thinking llm engine using an interactive selection."""

    # Define shallow thinking llm engine options with their corresponding model names
    SHALLOW_AGENT_OPTIONS = {
        "openai": [
            ("GPT-5.6 Luna - High-volume tools (Recommended)", "gpt-5.6-luna"),
            ("GPT-5.4 nano - Budget tool calls", "gpt-5.4-nano"),
            ("GPT-4.1-mini - Fast tool calls", "gpt-4.1-mini"),
            ("GPT-4.1-nano - Ultra-fast, lowest cost", "gpt-4.1-nano"),
            ("GPT-4.1 - Best non-reasoning", "gpt-4.1"),
            ("GPT-5-nano - Fast reasoning at budget price", "gpt-5-nano"),
            ("GPT-4o-mini - Legacy fast model", "gpt-4o-mini"),
        ],
        "anthropic": [
            ("Claude 3.5 Haiku - Fast and efficient", "claude-3-5-haiku-20241022"),
            ("Claude 3.5 Sonnet - Powerful reasoning", "claude-3-5-sonnet-20241022"),
            ("Claude 4.5 Sonnet - Latest flagship model (Recommended)", "claude-4.5-sonnet-20251022"),
        ],
        "google": [
            ("Gemini 1.5 Flash - Fast and efficient", "gemini-1.5-flash"),
            ("Gemini 2.0 Flash - Next generation model", "gemini-2.0-flash"),
            ("Gemini 3.0 Flash - Latest fast model (Recommended)", "gemini-3-flash"),
            ("Gemini 3.0 Pro - Latest flagship model", "gemini-3-pro"),
        ],
        "openrouter": [
            ("xAI: Grok 3 - Latest xAI model (Recommended)", "xai/grok-3"),
            ("Meta: Llama 4 - Latest open source flagship", "meta-llama/llama-4"),
            ("Meta: Llama 3.3 70B - Powerful open source", "meta-llama/llama-3.3-70b-instruct:free"),
            ("DeepSeek: R1 - Efficient reasoning", "deepseek/deepseek-r1"),
        ],
        "ollama": [
            ("llama3.1 local", "llama3.1"),
            ("llama3.2 local", "llama3.2"),
        ]
    }

    choice = questionary.select(
        "Select Your [Quick-Thinking LLM Engine]:",
        choices=[
            questionary.Choice(display, value=value)
            for display, value in SHALLOW_AGENT_OPTIONS[provider.lower()]
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print(
            "\n[red]No shallow thinking llm engine selected. Exiting...[/red]"
        )
        raise typer.Abort()

    return choice


def select_deep_thinking_agent(provider) -> str:
    """Select deep thinking llm engine using an interactive selection."""

    # Define deep thinking llm engine options with their corresponding model names
    DEEP_AGENT_OPTIONS = {
        "openai": [
            ("GPT-5.6 Terra - Balanced reasoning (Recommended)", "gpt-5.6-terra"),
            ("GPT-5.6 Sol - Frontier reasoning", "gpt-5.6-sol"),
            ("GPT-5.5 - Previous flagship reasoning", "gpt-5.5"),
            ("GPT-5.4 - Prior production workhorse", "gpt-5.4"),
            ("GPT-5.2 - Previous flagship reasoning", "gpt-5.2"),
            ("GPT-5-mini - Deep reasoning (slower)", "gpt-5-mini"),
            ("o4-mini - Latest O-series reasoning", "o4-mini"),
            ("o3 - Previous O-series reasoning", "o3"),
            ("o3-mini - Fast O-series reasoning", "o3-mini"),
            ("GPT-4.1 - Best non-reasoning", "gpt-4.1"),
            ("GPT-4o - Legacy powerful model", "gpt-4o"),
        ],
        "anthropic": [
            ("Claude 3.5 Haiku - Fast and efficient", "claude-3-5-haiku-20241022"),
            ("Claude 3.5 Sonnet - Powerful reasoning", "claude-3-5-sonnet-20241022"),
            ("Claude 3 Opus - Previous flagship", "claude-3-opus-20240229"),
            ("Claude 4.5 Sonnet - Latest flagship model (Recommended)", "claude-4.5-sonnet-20251022"),
            ("Claude Opus 4.1 - Most advanced reasoning", "claude-opus-4.1-20250805"),
        ],
        "google": [
            ("Gemini 1.5 Flash - Fast and efficient", "gemini-1.5-flash"),
            ("Gemini 1.5 Pro - Powerful reasoning", "gemini-1.5-pro"),
            ("Gemini 2.0 Flash - Next generation", "gemini-2.0-flash"),
            ("Gemini 3.0 Flash - Latest fast model", "gemini-3-flash"),
            ("Gemini 3.0 Pro - 1M token context (Recommended)", "gemini-3-pro"),
            ("Gemini 3.0 Pro DeepThink - Complex reasoning", "gemini-3-pro-deepthink"),
        ],
        "openrouter": [
            ("xAI: Grok 3 - Latest xAI flagship (Recommended)", "xai/grok-3"),
            ("Meta: Llama 4 - Latest open source flagship", "meta-llama/llama-4"),
            ("DeepSeek: R1 - 671B parameters, highly efficient", "deepseek/deepseek-r1"),
            ("DeepSeek: V3.2 - 685B-parameter MoE model", "deepseek/deepseek-chat-v3.2"),
        ],
        "ollama": [
            ("llama3.1 local", "llama3.1"),
            ("llama4 local", "llama4"),
            ("qwen3", "qwen3"),
        ]
    }
    
    choice = questionary.select(
        "Select Your [Deep-Thinking LLM Engine]:",
        choices=[
            questionary.Choice(display, value=value)
            for display, value in DEEP_AGENT_OPTIONS[provider.lower()]
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print("\n[red]No deep thinking llm engine selected. Exiting...[/red]")
        raise typer.Abort()

    return choice

def select_llm_provider() -> tuple[str, str]:
    """Select the OpenAI api url using interactive selection."""
    # Define OpenAI api options with their corresponding endpoints
    BASE_URLS = [
        ("OpenAI", "https://api.openai.com/v1"),
        ("Anthropic", "https://api.anthropic.com/"),
        ("Google", "https://generativelanguage.googleapis.com/v1"),
        ("Openrouter", "https://openrouter.ai/api/v1"),
        ("Ollama", "http://localhost:11434/v1"),        
    ]
    
    choice = questionary.select(
        "Select your LLM Provider:",
        choices=[
            questionary.Choice(display, value=(display, value))
            for display, value in BASE_URLS
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()
    
    if choice is None:
        console.print("\n[red]No OpenAI backend selected. Exiting...[/red]")
        raise typer.Abort()
    
    display_name, url = choice
    print(f"You selected: {display_name}\tURL: {url}")
    
    return display_name, url
