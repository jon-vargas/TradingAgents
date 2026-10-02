"""
Setup script for the TradingAgents package.

Note: pyproject.toml is the primary dependency source.
This file exists for editable installs (pip install -e .) and
compatibility with tools that don't yet support PEP 621.
"""

from setuptools import setup, find_packages

setup(
    name="tradingagents",
    version="0.1.0",
    description="Multi-Agent LLM Financial Trading & Research Framework",
    author="TradingAgents Team",
    author_email="yijia.xiao@cs.ucla.edu",
    url="https://github.com/TauricResearch",
    packages=find_packages(include=["tradingagents*", "cli*"]),
    install_requires=[
        # LLM / LangChain
        "langchain-openai>=0.3.23",
        "langchain-experimental>=0.3.4",
        "langchain-anthropic>=0.3.15",
        "langchain-google-genai>=2.1.5",
        "langgraph>=0.4.8",
        # Data
        "pandas>=2.3.0",
        "numpy>=1.24.0",
        "yfinance>=0.2.63",
        "stockstats>=0.6.5",
        "finnhub-python>=2.4.23",
        "eodhd>=1.0.32",
        "akshare>=1.16.98",
        "tushare>=1.4.21",
        # News / social
        "praw>=7.8.1",
        "feedparser>=6.0.11",
        "requests>=2.32.4",
        "parsel>=1.10.0",
        # Web UI
        "fastapi>=0.115.0",
        "uvicorn>=0.30.0",
        "jinja2>=3.1.0",
        "python-multipart>=0.0.9",
        "aiofiles>=24.1.0",
        # PDF / reports
        "weasyprint>=60.0",
        "tiktoken>=0.5.0",
        # CLI
        "typer>=0.9.0",
        "rich>=14.0.0",
        "questionary>=2.1.0",
        # Utilities
        "python-dotenv>=1.0.0",
        "python-dateutil>=2.8.0",
        "pytz>=2025.2",
        "tqdm>=4.67.1",
        "typing-extensions>=4.14.0",
        "setuptools>=80.9.0",
        # Storage
        "chromadb>=1.0.12",
    ],
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "tradingagents=cli.main:app",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Financial and Insurance Industry",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Office/Business :: Financial :: Investment",
    ],
)
