import re

from book_generator import BookGenerator, MAX_UNBROKEN_TOKEN_CHARS


def _make_generator() -> BookGenerator:
    return BookGenerator(agents={}, agent_config={}, outline=[])


def test_compact_text_for_prompt_truncates_overlong_unbroken_tokens() -> None:
    generator = _make_generator()
    long_token = "x" * (MAX_UNBROKEN_TOKEN_CHARS + 75)

    compacted = generator._compact_text_for_prompt(
        f"Intro {long_token} outro",
        50,
        "draft scene",
    )

    assert long_token not in compacted
    assert "long unbroken token truncated" in compacted
    assert max(len(token) for token in re.findall(r"\S+", compacted)) <= MAX_UNBROKEN_TOKEN_CHARS


def test_validate_prose_integrity_rejects_degenerate_unbroken_tokens() -> None:
    generator = _make_generator()
    long_token = "y" * (MAX_UNBROKEN_TOKEN_CHARS + 30)

    passed, message = generator._validate_prose_integrity(f"SCENE:\n{long_token}")

    assert not passed
    assert "degenerate unbroken token" in message
