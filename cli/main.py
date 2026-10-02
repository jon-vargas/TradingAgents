from typing import Optional
import os
import json
import datetime
import typer
from pathlib import Path
from functools import wraps
from rich.console import Console
from rich.prompt import Prompt
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live
from rich.columns import Columns
from rich.markdown import Markdown
from rich.layout import Layout
from rich.text import Text
from rich.live import Live
from rich.table import Table
from collections import deque
import time
from rich.tree import Tree
from rich import box
from rich.align import Align
from rich.rule import Rule

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting import ResearchDatabase
from cli.models import AnalystType
from cli.utils import *
from tradingagents.reporting.report_diff import diff_reports

console = Console()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agent LLM Research Framework (local, research-only)",
    add_completion=True,  # Enable shell completion
)


# Create a deque to store recent messages with a maximum length
class MessageBuffer:
    def __init__(self, max_length=100):
        self.messages = deque(maxlen=max_length)
        self.tool_calls = deque(maxlen=max_length)
        self.current_report = None
        self.final_report = None  # Store the complete final report
        self.agent_status = {
            # Analyst Team
            "Market Analyst": "pending",
            "Social Analyst": "pending",
            "News Analyst": "pending",
            "Fundamentals Analyst": "pending",
            # Research Team
            "Bull Researcher": "pending",
            "Bear Researcher": "pending",
            "Research Manager": "pending",
            # Trading Team
            "Trader": "pending",
            # Risk Management Team
            "Risky Analyst": "pending",
            "Neutral Analyst": "pending",
            "Safe Analyst": "pending",
            # Portfolio Management Team
            "Portfolio Manager": "pending",
        }
        self.current_agent = None
        self.report_sections = {
            "market_report": None,
            "sentiment_report": None,
            "news_report": None,
            "fundamentals_report": None,
            "investment_plan": None,
            "trader_investment_plan": None,
            "final_trade_decision": None,
        }

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.messages.append((timestamp, message_type, content))

    def add_tool_call(self, tool_name, args):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.tool_calls.append((timestamp, tool_name, args))

    def update_agent_status(self, agent, status):
        if agent in self.agent_status:
            self.agent_status[agent] = status
            self.current_agent = agent

    def update_report_section(self, section_name, content):
        if section_name in self.report_sections:
            self.report_sections[section_name] = content
            self._update_current_report()

    def _update_current_report(self):
        # For the panel display, only show the most recently updated section
        latest_section = None
        latest_content = None

        # Find the most recently updated section
        for section, content in self.report_sections.items():
            if content is not None:
                latest_section = section
                latest_content = content
               
        if latest_section and latest_content:
            # Format the current section for display
            section_titles = {
                "market_report": "Market Analysis",
                "sentiment_report": "Social Sentiment",
                "news_report": "News Analysis",
                "fundamentals_report": "Fundamentals Analysis",
                "investment_plan": "Research Team Decision",
                "trader_investment_plan": "Trading Team Plan",
                "final_trade_decision": "Portfolio Management Decision",
            }
            self.current_report = (
                f"### {section_titles[latest_section]}\n{latest_content}"
            )

        # Update the final complete report
        self._update_final_report()

    def _update_final_report(self):
        report_parts = []

        # Analyst Team Reports
        if any(
            self.report_sections[section]
            for section in [
                "market_report",
                "sentiment_report",
                "news_report",
                "fundamentals_report",
            ]
        ):
            report_parts.append("## Analyst Team Reports")
            if self.report_sections["market_report"]:
                report_parts.append(
                    f"### Market Analysis\n{self.report_sections['market_report']}"
                )
            if self.report_sections["sentiment_report"]:
                report_parts.append(
                    f"### Social Sentiment\n{self.report_sections['sentiment_report']}"
                )
            if self.report_sections["news_report"]:
                report_parts.append(
                    f"### News Analysis\n{self.report_sections['news_report']}"
                )
            if self.report_sections["fundamentals_report"]:
                report_parts.append(
                    f"### Fundamentals Analysis\n{self.report_sections['fundamentals_report']}"
                )

        # Research Team Reports
        if self.report_sections["investment_plan"]:
            report_parts.append("## Research Team Decision")
            report_parts.append(f"{self.report_sections['investment_plan']}")

        # Trading Team Reports
        if self.report_sections["trader_investment_plan"]:
            report_parts.append("## Trading Team Plan")
            report_parts.append(f"{self.report_sections['trader_investment_plan']}")

        # Portfolio Management Decision
        if self.report_sections["final_trade_decision"]:
            report_parts.append("## Portfolio Management Decision")
            report_parts.append(f"{self.report_sections['final_trade_decision']}")

        self.final_report = "\n\n".join(report_parts) if report_parts else None


message_buffer = MessageBuffer()


def create_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_column(
        Layout(name="upper", ratio=3), Layout(name="analysis", ratio=5)
    )
    layout["upper"].split_row(
        Layout(name="progress", ratio=2), Layout(name="messages", ratio=3)
    )
    return layout


def update_display(layout, spinner_text=None):
    # Header with welcome message
    layout["header"].update(
        Panel(
            "[bold green]Welcome to TradingAgents CLI[/bold green]\n"
            "[dim]© [Tauric Research](https://github.com/TauricResearch)[/dim]",
            title="Welcome to TradingAgents",
            border_style="green",
            padding=(1, 2),
            expand=True,
        )
    )

    # Progress panel showing agent status
    progress_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        box=box.SIMPLE_HEAD,  # Use simple header with horizontal lines
        title=None,  # Remove the redundant Progress title
        padding=(0, 2),  # Add horizontal padding
        expand=True,  # Make table expand to fill available space
    )
    progress_table.add_column("Team", style="cyan", justify="center", width=20)
    progress_table.add_column("Agent", style="green", justify="center", width=20)
    progress_table.add_column("Status", style="yellow", justify="center", width=20)

    # Group agents by team
    teams = {
        "Analyst Team": [
            "Market Analyst",
            "Social Analyst",
            "News Analyst",
            "Fundamentals Analyst",
        ],
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Risky Analyst", "Neutral Analyst", "Safe Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    for team, agents in teams.items():
        # Add first agent with team name
        first_agent = agents[0]
        status = message_buffer.agent_status[first_agent]
        if status == "in_progress":
            spinner = Spinner(
                "dots", text="[blue]in_progress[/blue]", style="bold cyan"
            )
            status_cell = spinner
        else:
            status_color = {
                "pending": "yellow",
                "completed": "green",
                "error": "red",
            }.get(status, "white")
            status_cell = f"[{status_color}]{status}[/{status_color}]"
        progress_table.add_row(team, first_agent, status_cell)

        # Add remaining agents in team
        for agent in agents[1:]:
            status = message_buffer.agent_status[agent]
            if status == "in_progress":
                spinner = Spinner(
                    "dots", text="[blue]in_progress[/blue]", style="bold cyan"
                )
                status_cell = spinner
            else:
                status_color = {
                    "pending": "yellow",
                    "completed": "green",
                    "error": "red",
                }.get(status, "white")
                status_cell = f"[{status_color}]{status}[/{status_color}]"
            progress_table.add_row("", agent, status_cell)

        # Add horizontal line after each team
        progress_table.add_row("─" * 20, "─" * 20, "─" * 20, style="dim")

    layout["progress"].update(
        Panel(progress_table, title="Progress", border_style="cyan", padding=(1, 2))
    )

    # Messages panel showing recent messages and tool calls
    messages_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        expand=True,  # Make table expand to fill available space
        box=box.MINIMAL,  # Use minimal box style for a lighter look
        show_lines=True,  # Keep horizontal lines
        padding=(0, 1),  # Add some padding between columns
    )
    messages_table.add_column("Time", style="cyan", width=8, justify="center")
    messages_table.add_column("Type", style="green", width=10, justify="center")
    messages_table.add_column(
        "Content", style="white", no_wrap=False, ratio=1
    )  # Make content column expand

    # Combine tool calls and messages
    all_messages = []

    # Add tool calls
    for timestamp, tool_name, args in message_buffer.tool_calls:
        # Truncate tool call args if too long
        if isinstance(args, str) and len(args) > 100:
            args = args[:97] + "..."
        all_messages.append((timestamp, "Tool", f"{tool_name}: {args}"))

    # Add regular messages
    for timestamp, msg_type, content in message_buffer.messages:
        # Convert content to string if it's not already
        content_str = content
        if isinstance(content, list):
            # Handle list of content blocks (Anthropic format)
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get('type') == 'text':
                        text_parts.append(item.get('text', ''))
                    elif item.get('type') == 'tool_use':
                        text_parts.append(f"[Tool: {item.get('name', 'unknown')}]")
                else:
                    text_parts.append(str(item))
            content_str = ' '.join(text_parts)
        elif not isinstance(content_str, str):
            content_str = str(content)
            
        # Truncate message content if too long
        if len(content_str) > 200:
            content_str = content_str[:197] + "..."
        all_messages.append((timestamp, msg_type, content_str))

    # Sort by timestamp
    all_messages.sort(key=lambda x: x[0])

    # Calculate how many messages we can show based on available space
    # Start with a reasonable number and adjust based on content length
    max_messages = 12  # Increased from 8 to better fill the space

    # Get the last N messages that will fit in the panel
    recent_messages = all_messages[-max_messages:]

    # Add messages to table
    for timestamp, msg_type, content in recent_messages:
        # Format content with word wrapping
        wrapped_content = Text(content, overflow="fold")
        messages_table.add_row(timestamp, msg_type, wrapped_content)

    if spinner_text:
        messages_table.add_row("", "Spinner", spinner_text)

    # Add a footer to indicate if messages were truncated
    if len(all_messages) > max_messages:
        messages_table.footer = (
            f"[dim]Showing last {max_messages} of {len(all_messages)} messages[/dim]"
        )

    layout["messages"].update(
        Panel(
            messages_table,
            title="Messages & Tools",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # Analysis panel showing current report
    if message_buffer.current_report:
        layout["analysis"].update(
            Panel(
                Markdown(message_buffer.current_report),
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )
    else:
        layout["analysis"].update(
            Panel(
                "[italic]Waiting for analysis report...[/italic]",
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )

    # Footer with statistics
    tool_calls_count = len(message_buffer.tool_calls)
    llm_calls_count = sum(
        1 for _, msg_type, _ in message_buffer.messages if msg_type == "Reasoning"
    )
    reports_count = sum(
        1 for content in message_buffer.report_sections.values() if content is not None
    )

    stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats_table.add_column("Stats", justify="center")
    stats_table.add_row(
        f"Tool Calls: {tool_calls_count} | LLM Calls: {llm_calls_count} | Generated Reports: {reports_count}"
    )

    layout["footer"].update(Panel(stats_table, border_style="grey50"))


def get_user_selections():
    """Get all user selections before starting the analysis display."""
    # Display ASCII art welcome message
    with open("./cli/static/welcome.txt", "r", encoding="utf-8") as f:
        welcome_ascii = f.read()

    # Create welcome box content
    welcome_content = f"{welcome_ascii}\n"
    welcome_content += "[bold green]TradingAgents: Multi-Agents LLM Financial Trading Framework - CLI[/bold green]\n\n"
    welcome_content += "[bold]Workflow Steps:[/bold]\n"
    welcome_content += "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management\n\n"
    welcome_content += (
        "[dim]Built by [Tauric Research](https://github.com/TauricResearch)[/dim]"
    )

    # Create and center the welcome box
    welcome_box = Panel(
        welcome_content,
        border_style="green",
        padding=(1, 2),
        title="Welcome to TradingAgents",
        subtitle="Multi-Agents LLM Financial Trading Framework",
    )
    console.print(Align.center(welcome_box))
    console.print()  # Add a blank line after the welcome box

    # Create a boxed questionnaire for each step
    def create_question_box(title, prompt, default=None):
        box_content = f"[bold]{title}[/bold]\n"
        box_content += f"[dim]{prompt}[/dim]"
        if default:
            box_content += f"\n[dim]Default: {default}[/dim]"
        return Panel(box_content, border_style="blue", padding=(1, 2))

    # Step 1: Ticker symbol
    console.print(
        create_question_box(
            "Step 1: Ticker Symbol", "Enter the ticker symbol to analyze", "SPY"
        )
    )
    selected_ticker = get_ticker()

    # Step 2: Analysis date
    default_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(
        create_question_box(
            "Step 2: Analysis Date",
            "Enter the analysis date (YYYY-MM-DD)",
            default_date,
        )
    )
    analysis_date = get_analysis_date()

    # Step 3: Select analysts
    console.print(
        create_question_box(
            "Step 3: Analysts Team", "Select your LLM analyst agents for the analysis"
        )
    )
    selected_analysts = select_analysts()
    console.print(
        f"[green]Selected analysts:[/green] {', '.join(analyst.value for analyst in selected_analysts)}"
    )

    # Step 4: Research depth
    console.print(
        create_question_box(
            "Step 4: Research Depth", "Select your research depth level"
        )
    )
    selected_research_depth = select_research_depth()

    # Step 5: OpenAI backend
    console.print(
        create_question_box(
            "Step 5: OpenAI backend", "Select which service to talk to"
        )
    )
    selected_llm_provider, backend_url = select_llm_provider()
    
    # Step 6: Thinking agents
    console.print(
        create_question_box(
            "Step 6: Thinking Agents", "Select your thinking agents for analysis"
        )
    )
    selected_shallow_thinker = select_shallow_thinking_agent(selected_llm_provider)
    selected_deep_thinker = select_deep_thinking_agent(selected_llm_provider)

    # Step 7: Investment Profile (optional)
    console.print(
        create_question_box(
            "Step 7: Investment Profile",
            "Select an investment profile to tailor analysis (Enter to skip for generic)",
            "Auto / Default",
        )
    )
    profiles = DEFAULT_CONFIG.get("investment_profiles", {})
    profile_choices = ["auto"] + list(profiles.keys())
    profile_labels = ["Auto / Default (generic analysis)"]
    for key in profiles:
        p = profiles[key]
        profile_labels.append(f"{p.get('display_name', key)} — {p.get('company_context', '')}")
    for i, label in enumerate(profile_labels):
        console.print(f"  [{i}] {label}")
    profile_input = Prompt.ask(
        "Investment profile",
        default="0",
    )
    try:
        idx = int(profile_input)
        selected_profile_key = profile_choices[idx] if 0 <= idx < len(profile_choices) else "auto"
    except (ValueError, IndexError):
        selected_profile_key = profile_input if profile_input in profiles else "auto"
    selected_investment_profile = profiles.get(selected_profile_key) if selected_profile_key != "auto" else None
    if selected_investment_profile:
        console.print(f"[green]Investment profile:[/green] {selected_investment_profile.get('display_name', selected_profile_key)}")
    else:
        console.print("[dim]Investment profile: Auto / Default[/dim]")

    return {
        "ticker": selected_ticker,
        "analysis_date": analysis_date,
        "analysts": selected_analysts,
        "research_depth": selected_research_depth,
        "llm_provider": selected_llm_provider.lower(),
        "backend_url": backend_url,
        "shallow_thinker": selected_shallow_thinker,
        "deep_thinker": selected_deep_thinker,
        "investment_profile": selected_investment_profile,
        "investment_profile_key": selected_profile_key if selected_profile_key != "auto" else None,
    }


def display_complete_report(final_state):
    """Display the complete analysis report with team-based panels."""
    console.print("\n[bold green]Complete Analysis Report[/bold green]\n")

    # I. Analyst Team Reports
    analyst_reports = []

    # Market Analyst Report
    if final_state.get("market_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["market_report"]),
                title="Market Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    # Social Analyst Report
    if final_state.get("sentiment_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["sentiment_report"]),
                title="Social Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    # News Analyst Report
    if final_state.get("news_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["news_report"]),
                title="News Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    # Fundamentals Analyst Report
    if final_state.get("fundamentals_report"):
        analyst_reports.append(
            Panel(
                Markdown(final_state["fundamentals_report"]),
                title="Fundamentals Analyst",
                border_style="blue",
                padding=(1, 2),
            )
        )

    if analyst_reports:
        console.print(
            Panel(
                Columns(analyst_reports, equal=True, expand=True),
                title="I. Analyst Team Reports",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    # II. Research Team Reports
    if final_state.get("investment_debate_state"):
        research_reports = []
        debate_state = final_state["investment_debate_state"]

        # Bull Researcher Analysis
        if debate_state.get("bull_history"):
            research_reports.append(
                Panel(
                    Markdown(debate_state["bull_history"]),
                    title="Bull Researcher",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        # Bear Researcher Analysis
        if debate_state.get("bear_history"):
            research_reports.append(
                Panel(
                    Markdown(debate_state["bear_history"]),
                    title="Bear Researcher",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        # Research Manager Decision
        if debate_state.get("judge_decision"):
            research_reports.append(
                Panel(
                    Markdown(debate_state["judge_decision"]),
                    title="Research Manager",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        if research_reports:
            console.print(
                Panel(
                    Columns(research_reports, equal=True, expand=True),
                    title="II. Research Team Decision",
                    border_style="magenta",
                    padding=(1, 2),
                )
            )

    # III. Trading Team Reports
    if final_state.get("trader_investment_plan"):
        console.print(
            Panel(
                Panel(
                    Markdown(final_state["trader_investment_plan"]),
                    title="Trader",
                    border_style="blue",
                    padding=(1, 2),
                ),
                title="III. Trading Team Plan",
                border_style="yellow",
                padding=(1, 2),
            )
        )

    # IV. Risk Management Team Reports
    if final_state.get("risk_debate_state"):
        risk_reports = []
        risk_state = final_state["risk_debate_state"]

        # Aggressive (Risky) Analyst Analysis
        if risk_state.get("risky_history"):
            risk_reports.append(
                Panel(
                    Markdown(risk_state["risky_history"]),
                    title="Aggressive Analyst",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        # Conservative (Safe) Analyst Analysis
        if risk_state.get("safe_history"):
            risk_reports.append(
                Panel(
                    Markdown(risk_state["safe_history"]),
                    title="Conservative Analyst",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        # Neutral Analyst Analysis
        if risk_state.get("neutral_history"):
            risk_reports.append(
                Panel(
                    Markdown(risk_state["neutral_history"]),
                    title="Neutral Analyst",
                    border_style="blue",
                    padding=(1, 2),
                )
            )

        if risk_reports:
            console.print(
                Panel(
                    Columns(risk_reports, equal=True, expand=True),
                    title="IV. Risk Management Team Decision",
                    border_style="red",
                    padding=(1, 2),
                )
            )

        # V. Portfolio Manager Decision
        if risk_state.get("judge_decision"):
            console.print(
                Panel(
                    Panel(
                        Markdown(risk_state["judge_decision"]),
                        title="Portfolio Manager",
                        border_style="blue",
                        padding=(1, 2),
                    ),
                    title="V. Portfolio Manager Decision",
                    border_style="green",
                    padding=(1, 2),
                )
            )


def update_research_team_status(status):
    """Update status for all research team members and trader."""
    research_team = ["Bull Researcher", "Bear Researcher", "Research Manager", "Trader"]
    for agent in research_team:
        message_buffer.update_agent_status(agent, status)


_ANALYST_REPORT_KEYS = {
    "market": ("market_report", "Market Analyst"),
    "social": ("sentiment_report", "Social Analyst"),
    "news": ("news_report", "News Analyst"),
    "fundamentals": ("fundamentals_report", "Fundamentals Analyst"),
}


def mark_selected_analysts_in_progress(selections):
    for analyst in selections["analysts"]:
        _, label = _ANALYST_REPORT_KEYS[analyst.value]
        message_buffer.update_agent_status(label, "in_progress")


def all_selected_analyst_reports_present(chunk, selections) -> bool:
    for analyst in selections["analysts"]:
        report_key, _ = _ANALYST_REPORT_KEYS[analyst.value]
        if not chunk.get(report_key):
            return False
    return True


def maybe_start_research_team(chunk, selections):
    if all_selected_analyst_reports_present(chunk, selections):
        update_research_team_status("in_progress")

def extract_content_string(content):
    """Extract string content from various message formats."""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        # Handle Anthropic's list format
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get('type') == 'text':
                    text_parts.append(item.get('text', ''))
                elif item.get('type') == 'tool_use':
                    text_parts.append(f"[Tool: {item.get('name', 'unknown')}]")
            else:
                text_parts.append(str(item))
        return ' '.join(text_parts)
    else:
        return str(content)

def run_analysis():
    # First get all user selections
    selections = get_user_selections()
    
    # Get analysis mode configuration
    analysis_mode = selections["research_depth"]  # Now returns a dict with mode settings

    # Create config with selected analysis mode settings
    config = DEFAULT_CONFIG.copy()
    config["analysis_mode"] = analysis_mode["mode"]
    config["max_debate_rounds"] = analysis_mode["max_debate_rounds"]
    config["max_risk_discuss_rounds"] = analysis_mode["max_risk_discuss_rounds"]
    config["use_perplexity"] = analysis_mode["use_perplexity"]
    config["news_limit"] = analysis_mode.get("news_limit", 20)
    config["data_vendors"] = analysis_mode.get("data_vendors", config.get("data_vendors", {}))
    config["quick_think_llm"] = selections["shallow_thinker"]
    config["deep_think_llm"] = selections["deep_thinker"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    
    # Log analysis mode info
    console.print(f"\n[bold cyan]Analysis Mode:[/bold cyan] {analysis_mode['mode'].upper()}")
    console.print(f"[dim]{analysis_mode['description']}[/dim]")
    console.print(f"[dim]Estimated time: {analysis_mode['estimated_time']} | Cost: {analysis_mode['estimated_cost']}[/dim]")
    if analysis_mode["use_perplexity"]:
        console.print("[bold green]✓ Perplexity deep research enabled[/bold green]")
    console.print()

    # Initialize the graph
    graph = TradingAgentsGraph(
        [analyst.value for analyst in selections["analysts"]], config=config, debug=True
    )

    # Create result directory
    results_dir = Path(config["results_dir"]) / selections["ticker"] / selections["analysis_date"]
    results_dir.mkdir(parents=True, exist_ok=True)
    report_dir = results_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    log_file = results_dir / "message_tool.log"
    log_file.touch(exist_ok=True)

    def save_message_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, message_type, content = obj.messages[-1]
            content = content.replace("\n", " ")  # Replace newlines with spaces
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [{message_type}] {content}\n")
        return wrapper
    
    def save_tool_call_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, tool_name, args = obj.tool_calls[-1]
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [Tool Call] {tool_name}({args_str})\n")
        return wrapper

    def save_report_section_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(section_name, content):
            func(section_name, content)
            if section_name in obj.report_sections and obj.report_sections[section_name] is not None:
                content = obj.report_sections[section_name]
                if content:
                    file_name = f"{section_name}.md"
                    with open(report_dir / file_name, "w", encoding="utf-8") as f:
                        f.write(content)
        return wrapper

    message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
    message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
    message_buffer.update_report_section = save_report_section_decorator(message_buffer, "update_report_section")

    # Now start the display layout
    layout = create_layout()

    with Live(layout, refresh_per_second=4) as live:
        # Initial display
        update_display(layout)

        # Add initial messages
        message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
        message_buffer.add_message(
            "System", f"Analysis date: {selections['analysis_date']}"
        )
        message_buffer.add_message(
            "System",
            f"Selected analysts: {', '.join(analyst.value for analyst in selections['analysts'])}",
        )
        update_display(layout)

        # Reset agent statuses
        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "pending")

        # Reset report sections
        for section in message_buffer.report_sections:
            message_buffer.report_sections[section] = None
        message_buffer.current_report = None
        message_buffer.final_report = None

        mark_selected_analysts_in_progress(selections)
        update_display(layout)

        # Create spinner text
        spinner_text = (
            f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
        )
        update_display(layout, spinner_text)

        # Initialize state and get graph args
        from tradingagents.dataflows.instrument_identity import resolve_instrument_context_for_run

        ticker = selections["ticker"]
        from tradingagents.reporting import get_db
        from tradingagents.utils.investment_profile_resolution import (
            resolve_investment_profile_for_ticker,
        )

        _, resolved_profile, _ = resolve_investment_profile_for_ticker(
            ticker,
            explicit_key=selections.get("investment_profile_key"),
            explicit_profile=selections.get("investment_profile"),
            db=get_db("research.db"),
            config=getattr(graph, "config", DEFAULT_CONFIG),
        )
        identity, instrument_context = resolve_instrument_context_for_run(
            ticker, selections["analysis_date"]
        )
        init_agent_state = graph.propagator.create_initial_state(
            ticker, selections["analysis_date"],
            investment_profile=resolved_profile,
            instrument_context=instrument_context,
            instrument_identity=identity,
        )
        profile_key = (resolved_profile or {}).get("profile_key")
        with graph.checkpoint_scope(ticker, selections["analysis_date"], profile_key) as thread_id_value:
            args = graph._graph_invoke_args(thread_id_value)
            invoke_state = graph.checkpoint_input(init_agent_state)

            from tradingagents.agents.utils.tool_context import (
                reset_current_ticker,
                set_current_ticker,
            )
            from tradingagents.graph.analyst_context import bind_run_context, clear_run_context

            ticker_token = set_current_ticker(ticker)
            bind_run_context()
            trace = []
            try:
                for chunk in graph.graph.stream(invoke_state, **args):
                    trace.append(chunk)
                    messages = chunk.get("messages") or []
                    if messages:
                        last_message = messages[-1]
                        if hasattr(last_message, "content"):
                            content = extract_content_string(last_message.content)
                            msg_type = "Reasoning"
                        else:
                            content = str(last_message)
                            msg_type = "System"
                        message_buffer.add_message(msg_type, content)
                        if hasattr(last_message, "tool_calls"):
                            for tool_call in last_message.tool_calls:
                                if isinstance(tool_call, dict):
                                    message_buffer.add_tool_call(
                                        tool_call["name"], tool_call["args"]
                                    )
                                else:
                                    message_buffer.add_tool_call(
                                        tool_call.name, tool_call.args
                                    )

                    if chunk.get("market_report"):
                        message_buffer.update_report_section(
                            "market_report", chunk["market_report"]
                        )
                        message_buffer.update_agent_status("Market Analyst", "completed")
                        maybe_start_research_team(chunk, selections)

                    if chunk.get("sentiment_report"):
                        message_buffer.update_report_section(
                            "sentiment_report", chunk["sentiment_report"]
                        )
                        message_buffer.update_agent_status("Social Analyst", "completed")
                        maybe_start_research_team(chunk, selections)

                    if chunk.get("news_report"):
                        message_buffer.update_report_section(
                            "news_report", chunk["news_report"]
                        )
                        message_buffer.update_agent_status("News Analyst", "completed")
                        maybe_start_research_team(chunk, selections)

                    if chunk.get("fundamentals_report"):
                        message_buffer.update_report_section(
                            "fundamentals_report", chunk["fundamentals_report"]
                        )
                        message_buffer.update_agent_status(
                            "Fundamentals Analyst", "completed"
                        )
                        maybe_start_research_team(chunk, selections)

                    # Research Team - Handle Investment Debate State
                    if (
                        "investment_debate_state" in chunk
                        and chunk["investment_debate_state"]
                    ):
                        debate_state = chunk["investment_debate_state"]

                        # Update Bull Researcher status and report
                        if "bull_history" in debate_state and debate_state["bull_history"]:
                            # Keep all research team members in progress
                            update_research_team_status("in_progress")
                            # Extract latest bull response
                            bull_responses = debate_state["bull_history"].split("\n")
                            latest_bull = bull_responses[-1] if bull_responses else ""
                            if latest_bull:
                                message_buffer.add_message("Reasoning", latest_bull)
                                # Update research report with bull's latest analysis
                                message_buffer.update_report_section(
                                    "investment_plan",
                                    f"### Bull Researcher Analysis\n{latest_bull}",
                                )

                        # Update Bear Researcher status and report
                        if "bear_history" in debate_state and debate_state["bear_history"]:
                            # Keep all research team members in progress
                            update_research_team_status("in_progress")
                            # Extract latest bear response
                            bear_responses = debate_state["bear_history"].split("\n")
                            latest_bear = bear_responses[-1] if bear_responses else ""
                            if latest_bear:
                                message_buffer.add_message("Reasoning", latest_bear)
                                # Update research report with bear's latest analysis
                                message_buffer.update_report_section(
                                    "investment_plan",
                                    f"{message_buffer.report_sections['investment_plan']}\n\n### Bear Researcher Analysis\n{latest_bear}",
                                )

                        # Update Research Manager status and final decision
                        if (
                            "judge_decision" in debate_state
                            and debate_state["judge_decision"]
                        ):
                            # Keep all research team members in progress until final decision
                            update_research_team_status("in_progress")
                            message_buffer.add_message(
                                "Reasoning",
                                f"Research Manager: {debate_state['judge_decision']}",
                            )
                            # Update research report with final decision
                            message_buffer.update_report_section(
                                "investment_plan",
                                f"{message_buffer.report_sections['investment_plan']}\n\n### Research Manager Decision\n{debate_state['judge_decision']}",
                            )
                            # Mark all research team members as completed
                            update_research_team_status("completed")
                            # Set first risk analyst to in_progress
                            message_buffer.update_agent_status(
                                "Risky Analyst", "in_progress"
                            )

                    # Trading Team
                    if (
                        "trader_investment_plan" in chunk
                        and chunk["trader_investment_plan"]
                    ):
                        message_buffer.update_report_section(
                            "trader_investment_plan", chunk["trader_investment_plan"]
                        )
                        # Set first risk analyst to in_progress
                        message_buffer.update_agent_status("Risky Analyst", "in_progress")

                    # Risk Management Team - Handle Risk Debate State
                    if "risk_debate_state" in chunk and chunk["risk_debate_state"]:
                        risk_state = chunk["risk_debate_state"]

                        # Update Risky Analyst status and report
                        if (
                            "current_risky_response" in risk_state
                            and risk_state["current_risky_response"]
                        ):
                            message_buffer.update_agent_status(
                                "Risky Analyst", "in_progress"
                            )
                            message_buffer.add_message(
                                "Reasoning",
                                f"Risky Analyst: {risk_state['current_risky_response']}",
                            )
                            # Update risk report with risky analyst's latest analysis only
                            message_buffer.update_report_section(
                                "final_trade_decision",
                                f"### Risky Analyst Analysis\n{risk_state['current_risky_response']}",
                            )

                        # Update Safe Analyst status and report
                        if (
                            "current_safe_response" in risk_state
                            and risk_state["current_safe_response"]
                        ):
                            message_buffer.update_agent_status(
                                "Safe Analyst", "in_progress"
                            )
                            message_buffer.add_message(
                                "Reasoning",
                                f"Safe Analyst: {risk_state['current_safe_response']}",
                            )
                            # Update risk report with safe analyst's latest analysis only
                            message_buffer.update_report_section(
                                "final_trade_decision",
                                f"### Safe Analyst Analysis\n{risk_state['current_safe_response']}",
                            )

                        # Update Neutral Analyst status and report
                        if (
                            "current_neutral_response" in risk_state
                            and risk_state["current_neutral_response"]
                        ):
                            message_buffer.update_agent_status(
                                "Neutral Analyst", "in_progress"
                            )
                            message_buffer.add_message(
                                "Reasoning",
                                f"Neutral Analyst: {risk_state['current_neutral_response']}",
                            )
                            # Update risk report with neutral analyst's latest analysis only
                            message_buffer.update_report_section(
                                "final_trade_decision",
                                f"### Neutral Analyst Analysis\n{risk_state['current_neutral_response']}",
                            )

                        # Update Portfolio Manager status and final decision
                        if "judge_decision" in risk_state and risk_state["judge_decision"]:
                            message_buffer.update_agent_status(
                                "Portfolio Manager", "in_progress"
                            )
                            message_buffer.add_message(
                                "Reasoning",
                                f"Portfolio Manager: {risk_state['judge_decision']}",
                            )
                            # Update risk report with final decision only
                            message_buffer.update_report_section(
                                "final_trade_decision",
                                f"### Portfolio Manager Decision\n{risk_state['judge_decision']}",
                            )
                            # Mark risk analysts as completed
                            message_buffer.update_agent_status("Risky Analyst", "completed")
                            message_buffer.update_agent_status("Safe Analyst", "completed")
                            message_buffer.update_agent_status(
                                "Neutral Analyst", "completed"
                            )
                            message_buffer.update_agent_status(
                                "Portfolio Manager", "completed"
                            )

                    update_display(layout)
            finally:
                clear_run_context()
                reset_current_ticker(ticker_token)

            # Get final state and decision
            final_state = trace[-1]
            decision = graph.process_signal(final_state["final_trade_decision"])

            # Update all agent statuses to completed
            for agent in message_buffer.agent_status:
                message_buffer.update_agent_status(agent, "completed")

            message_buffer.add_message(
                "Analysis", f"Completed analysis for {selections['analysis_date']}"
            )

            # Update final report sections
            for section in message_buffer.report_sections.keys():
                if section in final_state:
                    message_buffer.update_report_section(section, final_state[section])

            # Display the complete final report
            display_complete_report(final_state)

            update_display(layout)


@app.command()
def analyze():
    run_analysis()


@app.command("movers-scan")
def movers_scan(
    top_n: int = typer.Option(25, min=1, max=100, help="Top N rows to return per horizon"),
    include_losers: bool = typer.Option(True, help="Include day_losers in movers universe"),
    db_path: str = typer.Option("research.db", help="Path to research.db"),
):
    """Run movers intelligence scan and print dual-horizon output."""
    from tradingagents.screening.movers import MoversIntelligenceService

    db = ResearchDatabase(db_path)
    service = MoversIntelligenceService(db=db, config=DEFAULT_CONFIG)
    result = service.run_scan(include_losers=include_losers, top_n=top_n)
    console.print_json(json.dumps(result, default=str))


@app.command()
def history(
    ticker: Optional[str] = typer.Option(None, help="Filter by ticker symbol"),
    limit: int = typer.Option(20, help="Max number of analyses"),
    db_path: str = typer.Option("research.db", help="Path to research.db"),
):
    """Show recent analysis history with snapshot availability."""
    ticker = normalize_ticker(ticker) if ticker else None
    db = ResearchDatabase(db_path)
    analyses = db.get_analyses_by_ticker(ticker, limit) if ticker else db.get_recent_analyses(limit)
    if not analyses:
        console.print("No analyses found.")
        return

    table = Table(title="Analysis History", box=box.SIMPLE_HEAVY)
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("Ticker")
    table.add_column("Date")
    table.add_column("Decision")
    table.add_column("Conf", justify="right")
    table.add_column("Quality", justify="right")
    table.add_column("QA", justify="right")
    table.add_column("SEC")
    table.add_column("Transcript")

    for analysis in analyses:
        confidence = f"{analysis.confidence:.1f}" if analysis.confidence else "0.0"
        sec_flag = "Yes" if analysis.sec_filings_snapshot else "No"
        transcript_flag = "Yes" if analysis.earnings_transcript_snapshot else "No"
        qa_count = 0
        if getattr(analysis, "report_warnings", ""):
            try:
                qa_count = len(json.loads(analysis.report_warnings))
            except json.JSONDecodeError:
                qa_count = 1
        quality_score = getattr(analysis, "data_quality_score", 0)
        table.add_row(
            str(analysis.id or ""),
            analysis.ticker,
            analysis.analysis_date,
            analysis.decision,
            confidence,
            f"{quality_score}",
            str(qa_count),
            sec_flag,
            transcript_flag,
        )

    console.print(table)


@app.command("export-history")
def export_history(
    filepath: str = typer.Argument(..., help="Output CSV path"),
    ticker: Optional[str] = typer.Option(None, help="Filter by ticker symbol"),
    db_path: str = typer.Option("research.db", help="Path to research.db"),
):
    """Export analysis history to CSV (includes snapshot columns)."""
    ticker = normalize_ticker(ticker) if ticker else None
    db = ResearchDatabase(db_path)
    path = db.export_to_csv(filepath, ticker)
    console.print(f"Exported history to {path}")


@app.command("export-provenance")
def export_provenance(
    filepath: str = typer.Argument(..., help="Output JSON/CSV path"),
    export_format: str = typer.Option("json", help="Export format: json|csv"),
    limit: int = typer.Option(100, help="Max analyses to include"),
    ticker: Optional[str] = typer.Option(None, help="Filter by ticker symbol"),
    db_path: str = typer.Option("research.db", help="Path to research.db"),
):
    """Export provenance events for analyses to JSON or CSV."""
    if export_format not in {"json", "csv"}:
        raise typer.BadParameter("export_format must be json or csv")
    ticker = normalize_ticker(ticker) if ticker else None
    db = ResearchDatabase(db_path)
    path = db.export_provenance(
        filepath,
        export_format=export_format,
        limit=limit,
        ticker=ticker,
    )
    console.print(f"Exported provenance to {path}")


@app.command("backtest-history")
def backtest_history(
    limit: int = typer.Option(20, help="Max backtest runs to show"),
    ticker: Optional[str] = typer.Option(None, help="Filter by ticker symbol"),
    run_start: Optional[str] = typer.Option(None, help="Filter runs on/after YYYY-MM-DD"),
    run_end: Optional[str] = typer.Option(None, help="Filter runs on/before YYYY-MM-DD"),
    db_path: str = typer.Option("research.db", help="Path to research.db"),
):
    """Show recent backtest runs with costs and date range."""
    ticker = normalize_ticker(ticker) if ticker else None
    run_start = normalize_date_option(run_start, label="run_start", allow_time=True)
    run_end = normalize_date_option(run_end, label="run_end", allow_time=True)
    validate_date_range(run_start, run_end, allow_time=True)
    db = ResearchDatabase(db_path)
    runs = db.get_backtest_runs(
        limit=limit,
        ticker=ticker,
        run_start_date=run_start,
        run_end_date=run_end,
    )
    if not runs:
        console.print("No backtest runs found.")
        return

    total = len(runs)
    avg_return = sum(run.avg_return for run in runs) / total if total else 0.0
    avg_win = sum(run.win_rate for run in runs) / total if total else 0.0
    avg_acc = sum(run.accuracy for run in runs) / total if total else 0.0
    console.print(
        f"Runs: {total} | Avg return: {avg_return:.2%} | "
        f"Avg win rate: {avg_win:.2%} | Avg accuracy: {avg_acc:.2%}"
    )

    table = Table(title="Backtest Runs", box=box.SIMPLE_HEAVY)
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("Run At")
    table.add_column("Ticker")
    table.add_column("Range")
    table.add_column("Lookahead")
    table.add_column("Costs")
    table.add_column("Updated/Skipped")
    table.add_column("Avg Ret")
    table.add_column("Win Rate")
    table.add_column("Accuracy")

    for run in runs:
        range_label = f"{run.start_date or '-'} → {run.end_date or '-'}"
        costs = f"{run.slippage_bps:.1f}/{run.transaction_cost_bps:.1f} bps"
        updated_skipped = f"{run.updated_count}/{run.skipped_count}"
        table.add_row(
            str(run.id or ""),
            run.run_at,
            run.ticker or "-",
            range_label,
            run.lookahead_days or "-",
            costs,
            updated_skipped,
            f"{run.avg_return:.2%}",
            f"{run.win_rate:.2%}",
            f"{run.accuracy:.2%}",
        )

    console.print(table)


@app.command("export-backtest-runs")
def export_backtest_runs(
    filepath: str = typer.Argument(..., help="Output CSV path"),
    limit: int = typer.Option(100, help="Max runs to export"),
    ticker: Optional[str] = typer.Option(None, help="Filter by ticker symbol"),
    run_start: Optional[str] = typer.Option(None, help="Filter runs on/after YYYY-MM-DD"),
    run_end: Optional[str] = typer.Option(None, help="Filter runs on/before YYYY-MM-DD"),
    db_path: str = typer.Option("research.db", help="Path to research.db"),
):
    """Export backtest runs to CSV."""
    ticker = normalize_ticker(ticker) if ticker else None
    run_start = normalize_date_option(run_start, label="run_start", allow_time=True)
    run_end = normalize_date_option(run_end, label="run_end", allow_time=True)
    validate_date_range(run_start, run_end, allow_time=True)
    db = ResearchDatabase(db_path)
    path = db.export_backtest_runs(
        filepath,
        limit=limit,
        ticker=ticker,
        run_start_date=run_start,
        run_end_date=run_end,
    )
    console.print(f"Exported backtest runs to {path}")


@app.command("report-diff")
def report_diff(
    ticker: Optional[str] = typer.Option(None, help="Ticker symbol"),
    date_a: Optional[str] = typer.Option(None, help="First report date YYYY-MM-DD"),
    date_b: Optional[str] = typer.Option(None, help="Second report date YYYY-MM-DD"),
    file_a: Optional[str] = typer.Option(None, help="First report HTML file"),
    file_b: Optional[str] = typer.Option(None, help="Second report HTML file"),
    output_dir: str = typer.Option("research_output", help="Report output directory"),
    output_path: Optional[str] = typer.Option(None, help="Optional path to save diff"),
):
    """Diff two report HTML files."""
    if ticker:
        ticker = normalize_ticker(ticker)
    diff_text = diff_reports(
        file_a=file_a,
        file_b=file_b,
        ticker=ticker,
        date_a=date_a,
        date_b=date_b,
        output_dir=output_dir,
    )
    if output_path:
        Path(output_path).write_text(diff_text, encoding="utf-8")
        console.print(f"Diff saved to {output_path}")
    else:
        console.print(diff_text)


@app.command("export-batch-summary")
def export_batch_summary(
    output_dir: str = typer.Option("research_output", help="Output directory for summary"),
    tickers: str = typer.Option(..., help="Comma-separated tickers"),
    date: str = typer.Option(..., help="Analysis date YYYY-MM-DD"),
    delay: int = typer.Option(30, help="Delay between analyses (seconds)"),
    mode: str = typer.Option("standard", help="Analysis mode (quick/standard/deep)"),
    investment_profile: str = typer.Option("", help="Investment profile key (e.g. large_cap_core, dividend_income, high_growth, commodity_cyclical, momentum_speculative). Empty for default."),
    snapshot_filter: str = typer.Option(
        "all", help="Filter summary rows: all|any|sec|transcript|none|multi"
    ),
    db_path: str = typer.Option("research.db", help="Path to research.db"),
):
    """Run a batch analysis and export summary CSV/HTML."""
    from tradingagents.default_config import get_config_for_mode
    from tradingagents.research import ResearchAgent

    ticker_list = normalize_ticker_list(tickers)
    date = normalize_date_option(date, label="analysis date", allow_future=False) or date
    if snapshot_filter not in {"all", "any", "sec", "transcript", "none", "multi"}:
        raise typer.BadParameter("snapshot_filter must be: all|any|sec|transcript|none|multi")

    # Resolve investment profile
    inv_profile = None
    if investment_profile:
        profiles = DEFAULT_CONFIG.get("investment_profiles", {})
        inv_profile = profiles.get(investment_profile)
        if not inv_profile:
            raise typer.BadParameter(f"Unknown investment profile: {investment_profile}. Available: {', '.join(profiles.keys())}")
        console.print(f"[bold cyan]Investment Profile:[/bold cyan] {inv_profile.get('display_name', investment_profile)}")

    config = get_config_for_mode(mode, DEFAULT_CONFIG)
    agent = ResearchAgent(
        config=config,
        output_dir=output_dir,
        db_path=db_path,
        auto_report=True,
        auto_save=True,
        debug=True,
    )
    def run_with_filter(filter_value: str, suffix: str = ""):
        summary_csv = os.path.join(output_dir, f"batch_summary_{date}{suffix}.csv")
        summary_html = os.path.join(output_dir, f"batch_summary_{date}{suffix}.html")
        agent.batch_analyze(
            ticker_list,
            date,
            delay_seconds=delay,
            save_summary=True,
            summary_path=summary_csv,
            summary_html_path=summary_html,
            summary_snapshot_filter=filter_value,
            investment_profile=inv_profile,
        )

    if snapshot_filter == "multi":
        results = agent.batch_analyze(ticker_list, date, delay_seconds=delay, save_summary=False, investment_profile=inv_profile)
        for filter_value, suffix in [("sec", "_sec"), ("transcript", "_transcript")]:
            filtered = agent._filter_batch_results(results, filter_value)
            summary_csv = os.path.join(output_dir, f"batch_summary_{date}{suffix}.csv")
            summary_html = os.path.join(output_dir, f"batch_summary_{date}{suffix}.html")
            summary_only = os.path.join(output_dir, f"batch_summary_only_{date}{suffix}.csv")
            summary_meta = os.path.join(output_dir, f"batch_summary_meta_{date}{suffix}.csv")

            agent._write_batch_summary(
                filtered,
                date,
                summary_path=summary_csv,
                summary_snapshot_filter=filter_value,
                batch_total=len(ticker_list),
            )
            agent._write_batch_summary_only(
                filtered,
                date,
                summary_path=summary_only,
                summary_snapshot_filter=filter_value,
                batch_total=len(ticker_list),
            )
            agent._write_batch_metadata_only(
                filtered,
                date,
                summary_path=summary_meta,
                summary_snapshot_filter=filter_value,
                batch_total=len(ticker_list),
            )
            agent._write_batch_summary_html(
                filtered,
                date,
                summary_path=summary_html,
                summary_snapshot_filter=filter_value,
                batch_total=len(ticker_list),
            )

            console.print(f"Summary CSV saved to {summary_csv}")
            console.print(f"Summary-only CSV saved to {summary_only}")
            console.print(f"Metadata-only CSV saved to {summary_meta}")
            console.print(f"Summary HTML saved to {summary_html}")
        return

    run_with_filter(snapshot_filter)


@app.command("export-batch-summary-only")
def export_batch_summary_only(
    output_dir: str = typer.Option("research_output", help="Output directory for summary"),
    tickers: Optional[str] = typer.Option(None, help="Comma-separated tickers"),
    date: Optional[str] = typer.Option(None, help="Analysis date YYYY-MM-DD"),
    delay: int = typer.Option(30, help="Delay between analyses (seconds)"),
    mode: str = typer.Option("standard", help="Analysis mode (quick/standard/deep)"),
    snapshot_filter: str = typer.Option(
        "all", help="Filter summary rows: all|any|sec|transcript|none|multi"
    ),
    from_summary: Optional[str] = typer.Option(
        None, help="Existing batch_summary_*.csv to extract summary-only without running analyses"
    ),
    output_path: Optional[str] = typer.Option(None, help="Output CSV path for summary-only export"),
    metadata_only: bool = typer.Option(False, help="Export metadata-only CSV"),
    db_path: str = typer.Option("research.db", help="Path to research.db"),
):
    """Run a batch analysis and export summary-only CSV."""
    from tradingagents.default_config import get_config_for_mode
    from tradingagents.research import ResearchAgent

    if from_summary:
        target_path = output_path or os.path.join(output_dir, "batch_summary_only.csv")
        ResearchAgent._write_summary_only_from_csv(from_summary, target_path)
        console.print(f"Summary-only CSV saved to {target_path}")
        return

    if not tickers or not date:
        console.print("Provide --tickers and --date, or use --from-summary.")
        return

    ticker_list = normalize_ticker_list(tickers)
    date = normalize_date_option(date, label="analysis date", allow_future=False) or date

    if snapshot_filter not in {"all", "any", "sec", "transcript", "none", "multi"}:
        raise typer.BadParameter("snapshot_filter must be: all|any|sec|transcript|none|multi")

    config = get_config_for_mode(mode, DEFAULT_CONFIG)
    agent = ResearchAgent(
        config=config,
        output_dir=output_dir,
        db_path=db_path,
        auto_report=True,
        auto_save=True,
        debug=True,
    )
    results = agent.batch_analyze(
        ticker_list,
        date,
        delay_seconds=delay,
        save_summary=False,
    )
    filtered_results = agent._filter_batch_results(results, snapshot_filter)
    if metadata_only:
        agent._write_batch_metadata_only(
            filtered_results,
            date,
            summary_path=output_path,
            summary_snapshot_filter=snapshot_filter,
            batch_total=len(ticker_list),
        )
    else:
        agent._write_batch_summary_only(
            filtered_results,
            date,
            summary_path=output_path,
            summary_snapshot_filter=snapshot_filter,
            batch_total=len(ticker_list),
        )


@app.command("search-kpis")
def search_kpis(
    ticker: str = typer.Option(None, "--ticker", "-t", help="Filter by ticker"),
    kpi_name: str = typer.Option(None, "--name", "-n", help="Filter by KPI name (partial match)"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum results"),
):
    """Search indexed KPIs from earnings snapshots."""
    from tradingagents.reporting.database import ResearchDatabase
    from rich.table import Table

    ticker = normalize_ticker(ticker) if ticker else None
    db = ResearchDatabase()
    results = db.search_kpis(ticker=ticker, kpi_name=kpi_name, limit=limit)

    if not results:
        console.print("[yellow]No KPIs found matching criteria.[/yellow]")
        return

    table = Table(title="Indexed KPIs")
    table.add_column("Ticker")
    table.add_column("Date")
    table.add_column("KPI Name")
    table.add_column("Value")
    table.add_column("Period")
    table.add_column("Context")
    for row in results:
        table.add_row(
            row["ticker"],
            row["analysis_date"],
            row["kpi_name"],
            row["kpi_value"] or "",
            row["kpi_period"] or "",
            (row["kpi_context"] or "")[:40],
        )
    console.print(table)


@app.command("search-guidance")
def search_guidance(
    ticker: str = typer.Option(None, "--ticker", "-t", help="Filter by ticker"),
    metric: str = typer.Option(None, "--metric", "-m", help="Filter by metric (partial match)"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum results"),
):
    """Search indexed guidance from earnings snapshots."""
    from tradingagents.reporting.database import ResearchDatabase
    from rich.table import Table

    ticker = normalize_ticker(ticker) if ticker else None
    db = ResearchDatabase()
    results = db.search_guidance(ticker=ticker, metric=metric, limit=limit)

    if not results:
        console.print("[yellow]No guidance found matching criteria.[/yellow]")
        return

    table = Table(title="Indexed Guidance")
    table.add_column("Ticker")
    table.add_column("Date")
    table.add_column("Metric")
    table.add_column("Range")
    table.add_column("Timeframe")
    table.add_column("Context")
    for row in results:
        table.add_row(
            row["ticker"],
            row["analysis_date"],
            row["metric"],
            row["guidance_range"] or "",
            row["timeframe"] or "",
            (row["context"] or "")[:40],
        )
    console.print(table)


@app.command("search-risks")
def search_risks(
    ticker: str = typer.Option(None, "--ticker", "-t", help="Filter by ticker"),
    keyword: str = typer.Option(None, "--keyword", "-k", help="Filter by keyword (partial match)"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum results"),
):
    """Search indexed risk factors from SEC snapshots."""
    from tradingagents.reporting.database import ResearchDatabase
    from rich.table import Table

    ticker = normalize_ticker(ticker) if ticker else None
    db = ResearchDatabase()
    results = db.search_risks(ticker=ticker, keyword=keyword, limit=limit)

    if not results:
        console.print("[yellow]No risks found matching criteria.[/yellow]")
        return

    table = Table(title="Indexed Risk Factors")
    table.add_column("Ticker")
    table.add_column("Date")
    table.add_column("Risk Text")
    table.add_column("Form")
    table.add_column("Filing Date")
    for row in results:
        table.add_row(
            row["ticker"],
            row["analysis_date"],
            (row["risk_text"] or "")[:60],
            row["source_form"] or "",
            row["filing_date"] or "",
        )
    console.print(table)


@app.command("kpi-deltas")
def kpi_deltas(
    ticker: str = typer.Argument(..., help="Ticker symbol"),
    analysis_id: int = typer.Option(None, "--id", help="Analysis ID to compare (uses latest if omitted)"),
):
    """Show KPI deltas between current and previous analysis."""
    from tradingagents.reporting.database import ResearchDatabase
    from rich.table import Table

    ticker = normalize_ticker(ticker)
    db = ResearchDatabase()

    if analysis_id is None:
        recent = db.get_analyses_by_ticker(ticker, limit=1)
        if not recent:
            console.print(f"[red]No analyses found for {ticker}[/red]")
            raise typer.Abort()
        analysis_id = recent[0].id

    deltas = db.compute_kpi_deltas(ticker, analysis_id)

    if not deltas:
        console.print("[yellow]No KPI data found for comparison.[/yellow]")
        return

    table = Table(title=f"KPI Deltas for {ticker} (Analysis #{analysis_id})")
    table.add_column("KPI Name")
    table.add_column("Current Value")
    table.add_column("Current Period")
    table.add_column("Previous Value")
    table.add_column("Previous Period")
    for d in deltas:
        table.add_row(
            d["kpi_name"],
            d["current_value"] or "",
            d["current_period"] or "",
            d["previous_value"] or "-",
            d["previous_period"] or "-",
        )
    console.print(table)


@app.command("guidance-shifts")
def guidance_shifts(
    ticker: str = typer.Argument(..., help="Ticker symbol"),
    analysis_id: int = typer.Option(None, "--id", help="Analysis ID to compare (uses latest if omitted)"),
):
    """Show guidance shifts between current and previous analysis."""
    from tradingagents.reporting.database import ResearchDatabase
    from rich.table import Table

    ticker = normalize_ticker(ticker)
    db = ResearchDatabase()

    if analysis_id is None:
        recent = db.get_analyses_by_ticker(ticker, limit=1)
        if not recent:
            console.print(f"[red]No analyses found for {ticker}[/red]")
            raise typer.Abort()
        analysis_id = recent[0].id

    shifts = db.compute_guidance_shifts(ticker, analysis_id)

    if not shifts:
        console.print("[yellow]No guidance data found for comparison.[/yellow]")
        return

    table = Table(title=f"Guidance Shifts for {ticker} (Analysis #{analysis_id})")
    table.add_column("Metric")
    table.add_column("Current Range")
    table.add_column("Current Timeframe")
    table.add_column("Previous Range")
    table.add_column("Previous Timeframe")
    for s in shifts:
        table.add_row(
            s["metric"],
            s["current_range"] or "",
            s["current_timeframe"] or "",
            s["previous_range"] or "-",
            s["previous_timeframe"] or "-",
        )
    console.print(table)


@app.command("index-snapshots")
def index_snapshots(
    ticker: str = typer.Option(None, "--ticker", "-t", help="Re-index snapshots for a specific ticker"),
    limit: int = typer.Option(100, "--limit", "-l", help="Maximum analyses to process"),
):
    """Re-index KPIs, guidance, and risks from existing snapshots."""
    from tradingagents.reporting.database import ResearchDatabase

    ticker = normalize_ticker(ticker) if ticker else None
    db = ResearchDatabase()

    with db._connect() as conn:
        cursor = conn.cursor()
        query = """
            SELECT id, ticker FROM analyses
            WHERE has_sec_snapshot = 1 OR has_transcript_snapshot = 1
        """
        params: list = []
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()

    total_kpis = 0
    total_guidance = 0
    total_risks = 0
    for row in rows:
        counts = db.index_snapshot_fields(row["id"])
        total_kpis += counts["kpis"]
        total_guidance += counts["guidance"]
        total_risks += counts["risks"]

    console.print(
        f"[green]Indexed {len(rows)} analyses: "
        f"{total_kpis} KPIs, {total_guidance} guidance items, {total_risks} risks[/green]"
    )


if __name__ == "__main__":
    app()
