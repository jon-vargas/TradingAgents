"""
Token Management Utilities for TradingAgents
Helps manage token usage and prevent rate limit errors
"""

import logging

import tiktoken
from typing import List, Dict, Any

logger = logging.getLogger("tradingagents.utils.token_management")


def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """
    Count the number of tokens in a text string.
    
    Args:
        text: The text to count tokens for
        model: The model name (for appropriate tokenizer)
        
    Returns:
        Number of tokens
    """
    if text is None:
        return 0
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        # Default to cl100k_base for newer models
        encoding = tiktoken.get_encoding("cl100k_base")
    
    return len(encoding.encode(text))


def truncate_to_token_limit(text: str, max_tokens: int = 10000, model: str = "gpt-4o") -> str:
    """
    Truncate text to fit within a token limit.
    
    Args:
        text: The text to truncate
        max_tokens: Maximum number of tokens
        model: The model name
        
    Returns:
        Truncated text
    """
    if text is None:
        return ""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    
    tokens = encoding.encode(text)
    
    if len(tokens) <= max_tokens:
        return text
    
    # Truncate and keep result strictly within max_tokens.
    truncation_suffix = "\n\n[... Content truncated due to length ...]"
    suffix_tokens = encoding.encode(truncation_suffix)
    if max_tokens > len(suffix_tokens):
        truncated_tokens = tokens[: max_tokens - len(suffix_tokens)] + suffix_tokens
    else:
        truncated_tokens = tokens[:max_tokens]
    return encoding.decode(truncated_tokens)


def _summarize_stock_price_csv(stock_data: str, max_tokens: int, model: str = "gpt-4o") -> str:
    """
    Keep the most recent OHLCV rows when stock CSV is oversized.

    The prior generic truncation kept the beginning of long CSV payloads,
    which can surface stale prices from years ago in downstream analysis.
    """
    lines = stock_data.splitlines()
    if not lines:
        return stock_data

    csv_header_idx = None
    metadata_lines = []
    for idx, line in enumerate(lines):
        if line.startswith("#"):
            metadata_lines.append(line)
            continue
        if line.strip().startswith("Date,") and "Close" in line:
            csv_header_idx = idx
            break

    if csv_header_idx is None:
        return truncate_to_token_limit(stock_data, max_tokens, model=model)

    csv_header = lines[csv_header_idx].strip()
    rows = [line.strip() for line in lines[csv_header_idx + 1 :] if line.strip()]
    if not rows:
        return truncate_to_token_limit(stock_data, max_tokens, model=model)

    latest_fields = rows[-1].split(",")
    latest_date = latest_fields[0] if latest_fields else "N/A"
    latest_close = latest_fields[4] if len(latest_fields) > 4 else "N/A"
    metadata_lines = metadata_lines + [f"# Latest close in payload: {latest_date} = {latest_close}"]

    # Compute a row budget and preserve the newest rows (tail), not the oldest.
    scaffold = "\n".join(metadata_lines + ["", csv_header])
    scaffold_tokens = count_tokens(scaffold, model=model)
    sample_rows = rows[-min(30, len(rows)) :]
    sample_tokens = count_tokens("\n".join(sample_rows), model=model)
    avg_row_tokens = max(2, sample_tokens // max(1, len(sample_rows)))
    available_tokens = max(0, max_tokens - scaffold_tokens)
    row_budget = max(15, available_tokens // avg_row_tokens) if available_tokens > 0 else 15
    kept_rows = rows[-row_budget:]

    compact = "\n".join(metadata_lines + ["", csv_header] + kept_rows)
    # Keep complete rows (no mid-line truncation) by shrinking tail window as needed.
    while kept_rows and count_tokens(compact, model=model) > max_tokens:
        kept_rows = kept_rows[1:]  # drop oldest row from kept tail window
        compact = "\n".join(metadata_lines + ["", csv_header] + kept_rows)

    if kept_rows:
        return compact
    return truncate_to_token_limit(stock_data, max_tokens, model=model)


def summarize_news_items(news_data: str, max_items: int = 10, max_tokens_per_item: int = 500) -> str:
    """
    Summarize news data to reduce token count.
    
    Args:
        news_data: Raw news data (JSON string or formatted text)
        max_items: Maximum number of news items to keep
        max_tokens_per_item: Maximum tokens per news article
        
    Returns:
        Summarized news data
    """
    import json
    
    try:
        # Try to parse as JSON
        data = json.loads(news_data)
        
        if isinstance(data, dict) and "feed" in data:
            # Alpha Vantage format
            feed = data["feed"][:max_items]  # Keep only first N items
            
            summarized_feed = []
            for item in feed:
                # Keep only essential fields
                summarized_item = {
                    "title": item.get("title", "")[:200],  # Truncate title
                    "summary": item.get("summary", "")[:300],  # Truncate summary
                    "source": item.get("source", ""),
                    "time_published": item.get("time_published", ""),
                    "overall_sentiment_label": item.get("overall_sentiment_label", ""),
                    "overall_sentiment_score": item.get("overall_sentiment_score", 0),
                }
                
                # Add ticker sentiment if exists
                if "ticker_sentiment" in item and item["ticker_sentiment"]:
                    summarized_item["ticker_sentiment"] = item["ticker_sentiment"][0] if item["ticker_sentiment"] else {}
                
                summarized_feed.append(summarized_item)
            
            return json.dumps({"feed": summarized_feed}, indent=2)
        
    except (json.JSONDecodeError, KeyError):
        pass
    
    # If not JSON or different format, do simple truncation
    return truncate_to_token_limit(news_data, max_items * max_tokens_per_item)


def optimize_analyst_input(data: str, data_type: str = "news", max_tokens: int = 8000) -> str:
    """
    Optimize input data for analyst consumption.
    
    Args:
        data: Raw data
        data_type: Type of data (news, fundamentals, technical, etc.)
        max_tokens: Maximum tokens allowed
        
    Returns:
        Optimized data
    """
    if data is None:
        return ""
    current_tokens = count_tokens(data)
    
    if current_tokens <= max_tokens:
        return data
    
    logger.warning("%s data exceeds token limit: %d > %d. Optimizing to fit within %d tokens...",
                   data_type.upper(), current_tokens, max_tokens, max_tokens)
    
    if data_type == "news":
        # Special handling for news data.
        # Use the budget to determine how many items to keep, then enforce a hard final cap.
        max_items = max(4, min(15, max_tokens // 250))
        optimized = summarize_news_items(data, max_items=max_items, max_tokens_per_item=220)
    elif (
        data_type in {"technical", "technicals"}
        and "# Stock data for" in data
        and "Date,Open,High,Low,Close" in data
    ):
        # Preserve newest OHLCV rows to avoid stale "current price" leakage.
        optimized = _summarize_stock_price_csv(data, max_tokens=max_tokens)
    else:
        # Generic truncation for other data types
        optimized = truncate_to_token_limit(data, max_tokens)

    # Always hard-enforce the requested cap, regardless of summarization path.
    optimized = truncate_to_token_limit(optimized, max_tokens)
    
    optimized_tokens = count_tokens(optimized)
    logger.info("Reduced to %d tokens (%d tokens saved)", optimized_tokens, current_tokens - optimized_tokens)
    
    return optimized


def estimate_request_tokens(messages: List[Dict[str, Any]], model: str = "gpt-4o") -> int:
    """
    Estimate total tokens for a request including messages.
    
    Args:
        messages: List of message dictionaries
        model: Model name
        
    Returns:
        Estimated token count
    """
    if messages is None:
        return 8  # Overhead only
    total = 0
    
    for message in messages:
        # Count role tokens (roughly 4 tokens per message for overhead)
        total += 4
        
        if isinstance(message, dict):
            content = message.get("content", "")
        elif isinstance(message, tuple):
            content = message[1]
        else:
            content = str(message)
        
        total += count_tokens(str(content), model)
    
    # Add overhead for request structure
    total += 8
    
    return total


def compact_messages_for_budget(
    messages: List[Any],
    max_tokens: int = 16000,
    max_messages: int = 18,
    model: str = "gpt-4o",
) -> List[Any]:
    """
    Compact message history to fit within a token/message budget.

    Strategy:
    - Keep latest messages first (most relevant for tool loops)
    - Prefer preserving at least one early user/context message when possible
    - Drop older messages when over budget
    """
    if not messages:
        return []

    if max_tokens <= 0 or max_messages <= 0:
        return [messages[-1]]

    def _clone_with_truncated_content(message: Any, token_cap: int) -> Any:
        truncated_content = truncate_to_token_limit(
            str(getattr(message, "content", "")),
            token_cap,
            model=model,
        )
        # LangChain messages are usually pydantic models supporting model_copy/copy(update=...).
        if hasattr(message, "model_copy"):
            try:
                return message.model_copy(update={"content": truncated_content})
            except Exception:
                pass
        if hasattr(message, "copy"):
            try:
                return message.copy(update={"content": truncated_content})
            except Exception:
                pass
        try:
            from langchain_core.messages import HumanMessage
            return HumanMessage(content=truncated_content)
        except Exception:
            return message

    selected: List[Any] = []
    selected_tokens = 0

    for message in reversed(messages):
        if len(selected) >= max_messages:
            break
        content = str(getattr(message, "content", ""))
        msg_tokens = count_tokens(content, model) + 4  # approximate per-message overhead

        # If a single latest message exceeds the entire budget, keep a truncated clone.
        if msg_tokens > max_tokens:
            message = _clone_with_truncated_content(message, token_cap=max_tokens - 8)
            content = str(getattr(message, "content", message))
            msg_tokens = count_tokens(content, model) + 4

        if selected and (selected_tokens + msg_tokens) > max_tokens:
            continue
        selected.append(message)
        selected_tokens += msg_tokens

    selected = list(reversed(selected))

    # Preserve initial context when feasible and not already included.
    first_msg = messages[0]
    if selected and selected[0] is not first_msg:
        first_tokens = count_tokens(str(getattr(first_msg, "content", "")), model) + 4
        if (
            len(selected) < max_messages
            and (selected_tokens + first_tokens) <= max_tokens
        ):
            selected = [first_msg] + selected

    if not selected:
        return [messages[-1]]

    return selected


def split_into_chunks(text: str, max_tokens_per_chunk: int = 8000, model: str = "gpt-4o") -> List[str]:
    """
    Split text into chunks that fit within token limits.
    
    Args:
        text: Text to split
        max_tokens_per_chunk: Maximum tokens per chunk
        model: Model name
        
    Returns:
        List of text chunks
    """
    if text is None:
        return []
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    
    tokens = encoding.encode(text)
    chunks = []
    
    for i in range(0, len(tokens), max_tokens_per_chunk):
        chunk_tokens = tokens[i:i + max_tokens_per_chunk]
        chunks.append(encoding.decode(chunk_tokens))
    
    return chunks


if __name__ == "__main__":
    # Test the utilities
    test_text = "This is a test. " * 1000
    
    print(f"Original tokens: {count_tokens(test_text)}")
    
    truncated = truncate_to_token_limit(test_text, max_tokens=100)
    print(f"Truncated tokens: {count_tokens(truncated)}")
    
    chunks = split_into_chunks(test_text, max_tokens_per_chunk=200)
    print(f"Split into {len(chunks)} chunks")

