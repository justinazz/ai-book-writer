import json
from types import SimpleNamespace

from book_generator import BookGenerator, MAX_PATCH_ATTEMPTS_PER_CHAPTER


class FakeAgent:
    def __init__(self, name: str):
        self.name = name
        self.system_message = ""


class ScriptedUserProxy:
    name = "user_proxy"
    system_message = ""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def initiate_chat(self, agent, clear_history=True, silent=True, max_turns=1, message=""):
        if not self.outputs:
            raise AssertionError(f"No scripted output left for {agent.name}")
        expected_agent, content = self.outputs.pop(0)
        assert agent.name == expected_agent
        self.calls.append({"agent": agent.name, "message": message, "content": content})
        return SimpleNamespace(chat_history=[{"name": agent.name, "content": content}])


REQUIRED_BEATS = [
    "Mara suffers through first sunlight and open sky panic.",
    "She follows the old manifest toward a pre-war service station.",
    "Jonah Rusk saves her from mutated coyotes with a jury-rigged sonic lure.",
    "Jonah warns that people pay for sealed-vault coordinates.",
    "Mara refuses Vault 70's location and trades repair work for directions.",
    "End with Jonah deciding to travel with her after hearing the words New Vegas and ceramic regulator in the same sentence.",
]


def _make_generator(tmp_path=None, outputs=None, max_iterations=3) -> BookGenerator:
    agents = {
        "user_proxy": ScriptedUserProxy(outputs or []),
        "writer": FakeAgent("writer"),
        "editor": FakeAgent("editor"),
        "memory_keeper": FakeAgent("memory_keeper"),
    }
    output_dir = str(tmp_path) if tmp_path is not None else "."
    generator = BookGenerator(
        agents=agents,
        agent_config={},
        outline=[{"chapter_number": 1, "title": "First Sunlight", "prompt": ""}],
        output_dir=output_dir,
        max_iterations=max_iterations,
    )
    generator._build_writer_agent = lambda name="writer": FakeAgent(name)
    generator._build_patch_writer_agent = lambda name="patch_writer_final": FakeAgent(name)
    return generator


def _chapter_prompt() -> str:
    return "\n".join(
        [
            "Required Chapter Details:",
            "Target Word Count: 100",
            "Beats:",
            *[f"{index}. {beat}" for index, beat in enumerate(REQUIRED_BEATS, start=1)],
        ]
    )


def _story_words(count: int) -> str:
    words = [f"word{index}" for index in range(count)]
    sentences = [" ".join(words[index:index + 20]) + "." for index in range(0, count, 20)]
    return " ".join(sentences)


BASE_SCENE = (
    "Mara stepped into the white glare and nearly folded under the open sky. "
    "She followed Eli's manifest to a dead service station, drank from the wrong cistern, "
    "and retched until her knees shook on the concrete. Jonah Rusk drove off the coyotes "
    "with a whining sonic lure and eyed her wrist terminal. He warned that sealed-vault "
    "coordinates bought a lot of trouble in the Mojave. Mara refused Vault 70's location "
    "and traded repair work for the southern road."
)

PATCHED_SCENE = (
    "Mara stepped into the white glare and nearly folded under the open sky. "
    "She followed Eli's manifest to a dead service station, drank from the wrong cistern, "
    "and retched until her knees shook on the concrete. Jonah Rusk drove off the coyotes "
    "with a whining sonic lure and eyed her wrist terminal. He warned that sealed-vault "
    "coordinates bought a lot of trouble in the Mojave. Mara refused Vault 70's location "
    "and traded repair work for the southern road. When she said New Vegas held the ceramic "
    "regulator, Jonah shut his toolbox and decided he was coming with her."
)


def _editor_json(failed=None, loop_result="PASS", sentence_result="PASS") -> str:
    failed = set(failed or [])
    return json.dumps(
        {
            "beat_check": [
                {
                    "index": index,
                    "beat": beat,
                    "status": "FAIL" if index in failed else "PASS",
                    "evidence": "missing" if index in failed else "present",
                }
                for index, beat in enumerate(REQUIRED_BEATS, start=1)
            ],
            "beat_check_result": "FAIL" if failed else "PASS",
            "loop_check_result": loop_result,
            "loop_check_notes": ["ok"],
            "sentence_length_check": ["ok"],
            "sentence_length_check_result": sentence_result,
            "word_count_advice": "No word-count changes are required.",
            "suggest": "Patch the failed items.",
        }
    )


def test_classifies_near_pass_as_patchable() -> None:
    generator = _make_generator()

    result = generator._classify_validation_result(
        _editor_json(failed={6}),
        BASE_SCENE,
        REQUIRED_BEATS,
        100,
    )

    assert result == "patchable"


def test_classifies_too_many_failed_beats_as_retryable() -> None:
    generator = _make_generator()

    result = generator._classify_validation_result(
        _editor_json(failed={1, 2, 3, 4}),
        BASE_SCENE,
        REQUIRED_BEATS,
        100,
    )

    assert result == "retryable"


def test_classifies_hard_failures_and_invalid_editor_output_as_retryable() -> None:
    generator = _make_generator()

    assert generator._classify_validation_result(
        _editor_json(failed={6}, loop_result="FAIL"),
        BASE_SCENE,
        REQUIRED_BEATS,
        100,
    ) == "retryable"
    assert generator._classify_validation_result(
        "Review the draft again and maybe fix it.",
        BASE_SCENE,
        REQUIRED_BEATS,
        100,
    ) == "retryable"


def test_classifies_near_word_count_miss_as_patchable_but_far_miss_as_retryable() -> None:
    generator = _make_generator()

    assert generator._classify_validation_result(
        _editor_json(),
        _story_words(700),
        REQUIRED_BEATS,
        1000,
    ) == "patchable"
    assert generator._classify_validation_result(
        _editor_json(),
        _story_words(500),
        REQUIRED_BEATS,
        1000,
    ) == "retryable"


def test_deterministic_patch_hints_detect_same_sentence_ending_requirement() -> None:
    generator = _make_generator()

    hints = generator._build_deterministic_patch_hints(
        REQUIRED_BEATS,
        "Mara said New Vegas was far away. The ceramic regulator sounded fragile.",
    )
    clean_hints = generator._build_deterministic_patch_hints(
        REQUIRED_BEATS,
        "The road ended when Mara said New Vegas had the ceramic regulator.",
    )

    assert "New Vegas" in hints
    assert "ceramic regulator" in hints
    assert "ending section" in hints
    assert clean_hints == ""


def test_editor_format_retry_runs_once_for_incomplete_editor_output(tmp_path) -> None:
    outputs = [
        ("editor", "Review the draft for Chapter 1 and maybe revise it."),
        ("editor", _editor_json()),
    ]
    generator = _make_generator(tmp_path, outputs)

    feedback = generator._review_candidate_for_save(
        1,
        "First Sunlight",
        _chapter_prompt(),
        "No previous chapter summaries.",
        BASE_SCENE,
        REQUIRED_BEATS,
        100,
        1,
    )

    calls = generator.agents["user_proxy"].calls
    assert len(calls) == 2
    assert "previous editor response was not usable" in calls[1]["message"]
    assert generator._editor_feedback_is_complete(feedback, REQUIRED_BEATS)


def test_validate_prose_integrity_rejects_end_marker() -> None:
    generator = _make_generator()

    passed, message = generator._validate_prose_integrity(f"{BASE_SCENE}\n[End]")

    assert not passed
    assert "prohibited meta prose" in message


def test_patch_mode_accepts_near_pass_without_new_full_draft(tmp_path) -> None:
    outputs = [
        ("writer", f"SCENE:\n{BASE_SCENE}"),
        ("editor", _editor_json(failed={6})),
        ("writer_final", f"SCENE FINAL:\n{BASE_SCENE}"),
        ("editor", _editor_json(failed={6})),
        ("patch_writer_final_1", f"SCENE FINAL:\n{PATCHED_SCENE}"),
        ("editor", _editor_json()),
        ("memory_keeper", "MEMORY UPDATE:\nEVENT: Mara meets Jonah."),
    ]
    generator = _make_generator(tmp_path, outputs)

    result = generator.generate_chapter(1, _chapter_prompt())

    calls = generator.agents["user_proxy"].calls
    assert [call["agent"] for call in calls].count("writer") == 1
    assert [call["agent"] for call in calls].count("patch_writer_final_1") == 1
    assert "ceramic regulator" in result["final_scene"]
    assert (tmp_path / "chapter_01.txt").exists()


def test_patch_mode_exhausts_five_attempts_then_normal_retry_continues(tmp_path) -> None:
    logs = []
    outputs = [
        ("writer", f"SCENE:\n{BASE_SCENE}"),
        ("editor", _editor_json(failed={6})),
        ("writer_final", f"SCENE FINAL:\n{BASE_SCENE}"),
        ("editor", _editor_json(failed={6})),
    ]
    for patch_number in range(1, MAX_PATCH_ATTEMPTS_PER_CHAPTER + 1):
        outputs.extend(
            [
                (f"patch_writer_final_{patch_number}", f"SCENE FINAL:\n{BASE_SCENE}"),
                ("editor", _editor_json(failed={6})),
            ]
        )
    outputs.extend(
        [
            ("writer", f"SCENE:\n{PATCHED_SCENE}"),
            ("editor", _editor_json()),
            ("editor", _editor_json()),
            ("memory_keeper", "MEMORY UPDATE:\nEVENT: Patch exhausted, then retry passed."),
        ]
    )
    generator = _make_generator(tmp_path, outputs, max_iterations=2)
    generator.diagnostic_logger = logs.append

    result = generator.generate_chapter(1, _chapter_prompt())

    agents = [call["agent"] for call in generator.agents["user_proxy"].calls]
    assert agents.count("writer") == 2
    assert sum(agent.startswith("patch_writer_final_") for agent in agents) == MAX_PATCH_ATTEMPTS_PER_CHAPTER
    assert "Patch mode exhausted" in "\n".join(logs)
    assert "ceramic regulator" in result["final_scene"]
