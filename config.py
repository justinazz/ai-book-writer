"""Configuration for the book generation system."""
from typing import Dict


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "nemomix-unleashed-12b"
WEB_UI_PORT = 8001
OUTPUT_FOLDER = r"E:\AI\BookWriter\book_output"
WORD_COUNT_LOWER_TOLERANCE_RATIO = 0.25
WORD_COUNT_UPPER_TOLERANCE_RATIO = 0.50
MIN_WORD_COUNT_TOLERANCE_WORDS = 50
MAX_SENTENCE_WORDS = 80
MAX_ITERATIONS_LIMIT = 100
OUTLINE_MODEL_TEMPERATURE = 0.1
WRITER_MODEL_TEMPERATURE = 0.8


def get_config(
    local_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    extra_body: dict | None = None,
    temperature: float | None = None,
) -> Dict:
    """Get the configuration for the agents."""
    config_list = [{
        "model": model,
        "base_url": local_url,
        "api_key": "not-needed",
        # Local endpoint: force zero pricing so AutoGen does not emit cost warnings.
        "price": [0, 0],
    }]
    if reasoning_effort:
        config_list[0]["reasoning_effort"] = reasoning_effort
    if extra_body:
        config_list[0]["extra_body"] = extra_body

    config = {
        "config_list": config_list,
        "timeout": 600,
        "cache_seed": None,
    }
    if max_tokens is not None:
        config["max_tokens"] = max_tokens
    if temperature is not None:
        config["temperature"] = temperature
    return config
