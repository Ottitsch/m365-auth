"""Rough token estimates for the OpenAI/Anthropic usage fields."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimated_usage(prompt: str, output: str) -> dict[str, int]:
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(output)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def estimated_anthropic_usage(prompt: str, output: str) -> dict[str, int]:
    return {
        "input_tokens": estimate_tokens(prompt),
        "output_tokens": estimate_tokens(output),
    }
