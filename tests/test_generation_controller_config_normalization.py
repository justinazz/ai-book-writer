import re
import threading

from generation_controller import GenerationController


def _make_controller() -> GenerationController:
    return GenerationController.__new__(GenerationController)


def test_prompt_characters_accept_string_or_array() -> None:
    controller = _make_controller()

    string_sections = controller._normalize_prompt_sections_payload(
        {"characters": "Mara: scout\nTobin: mechanic"}
    )
    array_sections = controller._normalize_prompt_sections_payload(
        {"characters": ["Mara: scout", "Tobin: mechanic"]}
    )

    assert string_sections.characters == "Mara: scout\nTobin: mechanic"
    assert array_sections.characters == "Mara: scout\nTobin: mechanic"


def test_chapter_characters_accept_string_or_array() -> None:
    controller = _make_controller()

    string_details = controller._normalize_chapter_details(
        {"1": {"characters": "Mara\nTobin"}},
        1,
    )
    array_details = controller._normalize_chapter_details(
        {"1": {"characters": ["Mara", "Tobin"]}},
        1,
    )

    assert string_details[1]["characters"] == "Mara\nTobin"
    assert array_details[1]["characters"] == "Mara\nTobin"


def test_output_log_timestamps_each_append_and_preserves_multiline_blocks(tmp_path) -> None:
    controller = _make_controller()
    controller._diagnostic_log_lock = threading.RLock()
    controller._diagnostic_log_path = None

    controller._initialize_output_log(str(tmp_path))
    controller._append_output_log("single line")
    controller._append_output_log("block header\nblock body\nblock footer")

    lines = (tmp_path / "outputlog.txt").read_text(encoding="utf-8").splitlines()

    timestamp_pattern = r"\[20\d\d-\d\d-\d\d \d\d:\d\d:\d\d UTC\]"
    assert re.fullmatch(r"BookWriter diagnostic log started 20\d\d-\d\d-\d\d \d\d:\d\d:\d\d UTC", lines[0])
    assert re.fullmatch(fr"{timestamp_pattern} single line", lines[1])
    assert re.fullmatch(fr"{timestamp_pattern} block header", lines[2])
    assert lines[3] == "block body"
    assert lines[4] == "block footer"
