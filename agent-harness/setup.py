from setuptools import setup, find_namespace_packages

setup(
    name="cli-anything-web-search-fast",
    version="0.1.0",
    description="AI-friendly CLI harness for web-search-fast — direct web search from the command line",
    author="DUDU & Cailleach",
    python_requires=">=3.10",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    install_requires=[
        "click>=8.0",
        "rich>=13.0",
    ],
    entry_points={
        "console_scripts": [
            "cli-anything-web-search-fast=cli_anything.web_search_fast.web_search_fast_cli:cli",
        ],
    },
    package_data={
        "cli_anything.web_search_fast": ["skills/SKILL.md"],
    },
)
