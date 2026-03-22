"""Configuration for the book generation system."""
from typing import Dict


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "nemomix-unleashed-12b"
OUTPUT_FOLDER = r"E:\AI\BookWriter\book_output"


def get_config(
    local_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    extra_body: dict | None = None,
) -> Dict:
    """Get the configuration for the agents."""
    config_list = [{
        "model": model,
        "base_url": local_url,
        "api_key": "not-needed",
        # Local endpoint: force zero pricing so AutoGen does not emit cost warnings.
        "price": [0, 0],
    }]

    config = {
        "seed": 42,
        "temperature": temperature,
        "config_list": config_list,
        "timeout": 600,
        "cache_seed": None,
    }
    if max_tokens is not None:
        config["max_tokens"] = max_tokens
    if extra_body:
        config["extra_body"] = extra_body
    return config
