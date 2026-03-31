"""Background generation controller for the browser UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
import threading
import traceback
from typing import Callable, Dict, List, Optional
from urllib.error import URLError
from urllib.request import urlopen

from agents import BookAgents
from book_generator import BookGenerator, GenerationPauseRequested
from config import DEFAULT_BASE_URL, DEFAULT_MODEL, MAX_ITERATIONS_LIMIT, OUTPUT_FOLDER, get_config
from outline_generator import OutlineGenerator


CONFIG_DIR = "saved_configs"
MEMORY_SNAPSHOT_FILE = "memory.txt"


def _utc_timestamp() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_exception(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return f"{type(exc).__name__} (no error message)"


@dataclass
class PromptSections:
    premise: str = ""
    storylines: str = ""
    setting: str = ""
    characters: str = ""
    writing_style: str = ""
    tone: str = ""
    plot_beats: str = ""
    constraints: str = ""


@dataclass
class ChapterStatus:
    number: int
    title: str
    status: str = "pending"


@dataclass
class ChapterArtifacts:
    memory_update: str = ""
    draft_scene: str = ""
    editor_feedback: str = ""
    final_scene: str = ""


@dataclass
class ChapterReviewState:
    improvement_notes: str = ""
    saved_text: str = ""
    alternates: List[str] = field(default_factory=list)
    artifacts: ChapterArtifacts = field(default_factory=ChapterArtifacts)


@dataclass
class ProgressState:
    chapter_number: int = 0
    chapter_title: str = ""
    current_agent: str = ""
    current_step: str = ""
    iteration: int = 0
    max_iterations: int = 0
    output_stage: str = ""
    detail: str = ""


@dataclass
class ContinuityState:
    chapter_summaries: List[str] = field(default_factory=list)
    characters: List[str] = field(default_factory=list)
    world_details: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)


@dataclass
class ChapterMemoryRecord:
    prompt_entry: str = ""
    source_text: str = ""
    summary: str = ""
    characters: List[str] = field(default_factory=list)
    world_details: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)


@dataclass
class RunSnapshot:
    status: str = "idle"
    phase: str = "Idle"
    mode: str = "keep_going"
    waiting_for_input: bool = False
    run_active: bool = False
    busy: bool = False
    stop_requested: bool = False
    prompt: str = ""
    num_chapters: int = 10
    current_chapter: int = 0
    total_chapters: int = 0
    latest_error: str = ""
    latest_advice: str = ""
    outline_text: str = ""
    current_checkpoint_title: str = ""
    current_checkpoint_body: str = ""
    outline_model: str = DEFAULT_MODEL
    writer_model: str = DEFAULT_MODEL
    endpoint_url: str = DEFAULT_BASE_URL
    token_limit_enabled: bool = True
    max_tokens: int = 8192
    reduce_thinking: bool = False
    max_iterations: int = 5
    chapter_target_word_count: int = 0
    output_folder: str = OUTPUT_FOLDER
    config_name: str = ""
    chapter_details: Dict[int, Dict[str, object]] = field(default_factory=dict)
    phase_version: int = 0
    prompt_sections: PromptSections = field(default_factory=PromptSections)
    chapters: List[ChapterStatus] = field(default_factory=list)
    chapter_reviews: Dict[int, ChapterReviewState] = field(default_factory=dict)
    available_models: List[str] = field(default_factory=list)
    model_fetch_error: str = ""
    outline_approved: bool = False
    awaiting_outline_approval: bool = False
    outline_feedback: str = ""
    outline_regeneration_requested: bool = False
    progress: ProgressState = field(default_factory=ProgressState)
    continuity: ContinuityState = field(default_factory=ContinuityState)
    recent_events: List[str] = field(default_factory=list)
    progress_events: List[str] = field(default_factory=list)
    saved_configs: List[str] = field(default_factory=list)
    resume_available: bool = False
    resume_chapter_number: int = 0


class GenerationController:
    """Runs outline and chapter generation in the background with pause points."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._diagnostic_log_lock = threading.RLock()
        self._diagnostic_log_path: Optional[str] = None
        self._state = RunSnapshot()
        self._chapter_memory_records: Dict[int, ChapterMemoryRecord] = {}
        self._chapter_detail_versions: Dict[int, int] = {}
        self._resume_requested = False
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self._refresh_saved_configs()
        self.refresh_models()

    def _initialize_output_log(self, output_folder: str) -> None:
        os.makedirs(output_folder, exist_ok=True)
        path = os.path.join(output_folder, "outputlog.txt")
        with self._diagnostic_log_lock:
            self._diagnostic_log_path = path
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"BookWriter diagnostic log started {_utc_timestamp()}\n")

    def _append_output_log(self, message: str) -> None:
        with self._diagnostic_log_lock:
            path = self._diagnostic_log_path
        if not path:
            return
        text = message if message.endswith("\n") else message + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()

    def _log_runtime(self, message: str) -> None:
        print(message)
        self._append_output_log(message)

    def _log_runtime_block(self, header: str, content: str) -> None:
        body = (content or "").rstrip() or "[empty]"
        divider = "=" * 20
        self._log_runtime(f"{divider} {header} {divider}\n{body}\n{divider} END {header} {divider}")

    def _reset_chapter_detail_versions(
        self,
        chapter_details: Dict[int, Dict[str, object]],
        num_chapters: int,
    ) -> None:
        versions: Dict[int, int] = {}
        for chapter_number in range(1, max(0, num_chapters) + 1):
            details = chapter_details.get(chapter_number, {})
            beats = str(details.get("beats", "")).strip() if isinstance(details, dict) else ""
            target_word_count = 0
            if isinstance(details, dict):
                try:
                    target_word_count = int(details.get("target_word_count", 0) or 0)
                except (TypeError, ValueError):
                    target_word_count = 0
            if beats or target_word_count > 0:
                versions[chapter_number] = 1
        self._chapter_detail_versions = versions

    def _build_basic_saved_summary(self, chapter_number: int, chapter_text: str) -> str:
        cleaned = " ".join((chapter_text or "").split())
        words = cleaned.split()
        excerpt = " ".join(words[:50]).strip() if words else ""
        if not excerpt:
            excerpt = "[no usable summary]"
        return f"Chapter {chapter_number} Summary: {excerpt}..."

    def _build_memory_record(
        self,
        chapter_number: int,
        result: Dict,
        chapter_text: str,
    ) -> ChapterMemoryRecord:
        memory_text = str(result.get("memory_update", "") or "").strip()
        if memory_text:
            prompt_entry = f"Chapter {chapter_number} Memory:\n{memory_text}"
            summary = f"Chapter {chapter_number}: {memory_text[:300].strip()}"
        else:
            prompt_entry = self._build_basic_saved_summary(chapter_number, chapter_text)
            summary = prompt_entry

        characters: List[str] = []
        world_details: List[str] = []
        alerts: List[str] = []
        for line in memory_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("CHARACTER:"):
                characters.append(stripped.replace("CHARACTER:", "", 1).strip())
            elif stripped.startswith("WORLD:"):
                world_details.append(stripped.replace("WORLD:", "", 1).strip())
            elif stripped.startswith("CONTINUITY ALERT:"):
                alerts.append(stripped.replace("CONTINUITY ALERT:", "", 1).strip())

        return ChapterMemoryRecord(
            prompt_entry=prompt_entry.strip(),
            source_text=memory_text or prompt_entry,
            summary=summary.strip(),
            characters=[item for item in characters if item],
            world_details=[item for item in world_details if item],
            alerts=[item for item in alerts if item],
        )

    def _rebuild_continuity_from_memory_records(self) -> None:
        ordered_records = [
            self._chapter_memory_records[number]
            for number in sorted(self._chapter_memory_records)
            if self._chapter_memory_records[number].prompt_entry.strip()
        ]
        self._state.continuity = ContinuityState(
            chapter_summaries=[record.summary for record in ordered_records if record.summary],
            characters=[
                item
                for record in ordered_records
                for item in record.characters
                if item
            ][-40:],
            world_details=[
                item
                for record in ordered_records
                for item in record.world_details
                if item
            ][-40:],
            alerts=[
                item
                for record in ordered_records
                for item in record.alerts
                if item
            ][-20:],
        )
        self._state.continuity.chapter_summaries = self._state.continuity.chapter_summaries[-20:]

    def _build_initial_chapter_memory(self, before_chapter: int = 0) -> Dict[int, str]:
        with self._lock:
            records = {
                chapter_number: record.prompt_entry
                for chapter_number, record in self._chapter_memory_records.items()
                if record.prompt_entry.strip() and (before_chapter <= 0 or chapter_number < before_chapter)
            }
        return dict(sorted(records.items()))

    def _get_resume_chapter_number(self) -> int:
        with self._lock:
            if not self._state.resume_available:
                return 0
            return int(self._state.resume_chapter_number or 0)

    def _clear_resume_state(self) -> None:
        self._state.resume_available = False
        self._state.resume_chapter_number = 0

    def _next_resume_chapter(self, chapter_number: int) -> int:
        total_chapters = int(self._state.total_chapters or self._state.num_chapters or 0)
        next_chapter = int(chapter_number) + 1
        return next_chapter if 1 <= next_chapter <= total_chapters else 0

    def _should_stop_chapter_generation(self) -> bool:
        with self._lock:
            return bool(self._state.stop_requested)

    def _render_memory_snapshot(self) -> tuple[str, str]:
        with self._lock:
            output_folder = self._state.output_folder or OUTPUT_FOLDER
            chapter_memory_updates = [
                (chapter_number, self._chapter_memory_records[chapter_number].source_text.strip())
                for chapter_number in sorted(self._chapter_memory_records)
                if self._chapter_memory_records[chapter_number].source_text.strip()
            ]
            continuity = ContinuityState(
                chapter_summaries=list(self._state.continuity.chapter_summaries),
                characters=list(self._state.continuity.characters),
                world_details=list(self._state.continuity.world_details),
                alerts=list(self._state.continuity.alerts),
            )

        lines: List[str] = [
            "BookWriter Memory Snapshot",
            f"Updated: {_utc_timestamp()}",
            "",
            "Prompt Memory Window (latest 3 chapter memory entries used for upcoming chapter context):",
        ]
        recent_updates = chapter_memory_updates[-3:]
        if recent_updates:
            for chapter_number, memory_update in recent_updates:
                lines.append(f"Chapter {chapter_number} Memory:")
                lines.append(memory_update)
                lines.append("")
        else:
            lines.append("No prompt memory entries yet.")
            lines.append("")

        lines.append("Raw Chapter Memory Updates:")
        if chapter_memory_updates:
            for chapter_number, memory_update in chapter_memory_updates:
                lines.append(f"Chapter {chapter_number} Memory:")
                lines.append(memory_update)
                lines.append("")
        else:
            lines.append("No chapter memory updates yet.")
            lines.append("")

        lines.extend([
            "Continuity Summary:",
            "",
            "Chapter Summaries:",
            *(continuity.chapter_summaries or ["None yet."]),
            "",
            "Characters:",
            *(continuity.characters or ["None yet."]),
            "",
            "World Details:",
            *(continuity.world_details or ["None yet."]),
            "",
            "Continuity Alerts:",
            *(continuity.alerts or ["None yet."]),
        ])
        return output_folder, "\n".join(lines).strip() + "\n"

    def _write_memory_snapshot(self) -> None:
        output_folder, content = self._render_memory_snapshot()
        os.makedirs(output_folder, exist_ok=True)
        path = os.path.join(output_folder, MEMORY_SNAPSHOT_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _build_effective_chapter_prompt(
        self,
        chapter_number: int,
        chapter_prompt: str,
        prompt_sections: PromptSections,
        chapter_details_source: Optional[Dict[int, Dict[str, object]]] = None,
    ) -> str:
        prompt = self._apply_chapter_writing_style(chapter_prompt, prompt_sections)
        with self._lock:
            default_target_word_count = int(self._state.chapter_target_word_count or 0)
        details_map = chapter_details_source if chapter_details_source is not None else self._state.chapter_details
        chapter_specific_details = details_map.get(chapter_number, {}) if isinstance(details_map, dict) else {}
        chapter_specific_beats = str(chapter_specific_details.get("beats", "")).strip()
        try:
            chapter_specific_word_count = int(chapter_specific_details.get("target_word_count", 0) or 0) or default_target_word_count
        except (TypeError, ValueError):
            chapter_specific_word_count = default_target_word_count
        if chapter_specific_beats or chapter_specific_word_count > 0:
            detail_lines = []
            if chapter_specific_word_count > 0:
                detail_lines.append(f"Target Word Count: {chapter_specific_word_count}")
            if chapter_specific_beats:
                detail_lines.append("Beats:")
                detail_lines.append(chapter_specific_beats)
            prompt = (
                f"{prompt}\n\nRequired Chapter Details:\n"
                f"{chr(10).join(detail_lines)}\n\n"
                f"These chapter details are mandatory for Chapter {chapter_number}. "
                "Do not skip, defer, or substantially violate them."
            )
        return prompt

    def _make_chapter_prompt_provider(
        self,
        base_chapter_prompts: Dict[int, str],
        prompt_sections: PromptSections,
    ) -> Callable[[int, int, str, str], Dict[str, object]]:
        def provider(
            chapter_number: int,
            attempt_number: int,
            phase: str,
            current_prompt: str,
        ) -> Dict[str, object]:
            with self._lock:
                details = dict(self._state.chapter_details.get(chapter_number, {}))
                version = int(self._chapter_detail_versions.get(chapter_number, 0))
            base_prompt = base_chapter_prompts.get(chapter_number, current_prompt or "")
            effective_prompt = self._build_effective_chapter_prompt(
                chapter_number,
                base_prompt,
                prompt_sections,
                {chapter_number: details} if details else {},
            )
            return {
                "prompt": effective_prompt,
                "version": version,
                "details": details,
                "attempt": attempt_number,
                "phase": phase,
            }

        return provider

    def _handle_validation_event(self, event: Dict) -> None:
        body = str(event.get("body", "")).strip()
        if not body:
            return
        chapter_number = int(event.get("chapter_number", 0) or 0)
        attempt = int(event.get("attempt", 0) or 0)
        phase = str(event.get("phase", "attempt")).strip().lower()
        chapter_label = f"Chapter {chapter_number}" if chapter_number > 0 else "Current chapter"
        if phase == "recovery":
            title = f"{chapter_label} recovery validation failed (attempt {attempt})"
        else:
            title = f"{chapter_label} validation failed (attempt {attempt})"
        with self._condition:
            self._state.current_checkpoint_title = title
            self._state.current_checkpoint_body = body
            self._state.phase_version += 1
            self._append_event(f"{_utc_timestamp()} - {title}")

    def get_snapshot(self) -> RunSnapshot:
        with self._lock:
            return RunSnapshot(
                status=self._state.status,
                phase=self._state.phase,
                mode=self._state.mode,
                waiting_for_input=self._state.waiting_for_input,
                run_active=self._state.run_active,
                busy=self._state.busy,
                stop_requested=self._state.stop_requested,
                prompt=self._state.prompt,
                num_chapters=self._state.num_chapters,
                current_chapter=self._state.current_chapter,
                total_chapters=self._state.total_chapters,
                latest_error=self._state.latest_error,
                latest_advice=self._state.latest_advice,
                outline_text=self._state.outline_text,
                current_checkpoint_title=self._state.current_checkpoint_title,
                current_checkpoint_body=self._state.current_checkpoint_body,
                outline_model=self._state.outline_model,
                writer_model=self._state.writer_model,
                endpoint_url=self._state.endpoint_url,
                token_limit_enabled=self._state.token_limit_enabled,
                max_tokens=self._state.max_tokens,
                reduce_thinking=self._state.reduce_thinking,
                max_iterations=self._state.max_iterations,
                chapter_target_word_count=self._state.chapter_target_word_count,
                output_folder=self._state.output_folder,
                config_name=self._state.config_name,
                chapter_details={number: dict(details) for number, details in self._state.chapter_details.items()},
                phase_version=self._state.phase_version,
                prompt_sections=PromptSections(**self._state.prompt_sections.__dict__),
                chapters=[ChapterStatus(ch.number, ch.title, ch.status) for ch in self._state.chapters],
                chapter_reviews={
                    number: ChapterReviewState(
                        improvement_notes=review.improvement_notes,
                        saved_text=review.saved_text,
                        alternates=list(review.alternates),
                        artifacts=ChapterArtifacts(**review.artifacts.__dict__),
                    )
                    for number, review in self._state.chapter_reviews.items()
                },
                available_models=list(self._state.available_models),
                model_fetch_error=self._state.model_fetch_error,
                outline_approved=self._state.outline_approved,
                awaiting_outline_approval=self._state.awaiting_outline_approval,
                outline_feedback=self._state.outline_feedback,
                outline_regeneration_requested=self._state.outline_regeneration_requested,
                progress=ProgressState(**self._state.progress.__dict__),
                continuity=ContinuityState(
                    chapter_summaries=list(self._state.continuity.chapter_summaries),
                    characters=list(self._state.continuity.characters),
                    world_details=list(self._state.continuity.world_details),
                    alerts=list(self._state.continuity.alerts),
                ),
                recent_events=list(self._state.recent_events),
                progress_events=list(self._state.progress_events),
                saved_configs=list(self._state.saved_configs),
                resume_available=self._state.resume_available,
                resume_chapter_number=self._state.resume_chapter_number,
            )

    def wait_for_update(self, last_phase_version: int, timeout: float = 15.0) -> RunSnapshot:
        with self._condition:
            if self._state.phase_version <= last_phase_version:
                self._condition.wait(timeout=timeout)
            return self.get_snapshot()

    def refresh_models(self, endpoint_url: Optional[str] = None) -> None:
        with self._lock:
            if endpoint_url:
                self._state.endpoint_url = endpoint_url.strip() or self._state.endpoint_url
            base_url = self._state.endpoint_url

        models_url = f"{base_url.rstrip('/')}/models"
        try:
            with urlopen(models_url, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            model_names = sorted(
                item.get("id", "")
                for item in payload.get("data", [])
                if item.get("id")
            )
            with self._lock:
                self._state.available_models = model_names
                self._state.model_fetch_error = ""
                if model_names:
                    if self._state.outline_model not in model_names:
                        self._state.outline_model = model_names[0]
                    if self._state.writer_model not in model_names:
                        self._state.writer_model = model_names[0]
                self._append_event(f"{_utc_timestamp()} - Refreshed model list")
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            with self._lock:
                self._state.available_models = []
                self._state.model_fetch_error = str(exc)
                self._append_event(f"{_utc_timestamp()} - Model refresh failed: {exc}")

    def build_prompt(self, sections: PromptSections) -> str:
        return "\n\n".join(
            block for block in [
                f"Premise:\n{sections.premise.strip()}",
                f"Storylines / Arcs:\n{sections.storylines.strip()}",
                f"Setting / World:\n{sections.setting.strip()}",
                f"Characters:\n{sections.characters.strip()}",
                f"Writing Style:\n{sections.writing_style.strip()}",
                f"Tone:\n{sections.tone.strip()}",
                f"Important Plot Beats:\n{sections.plot_beats.strip()}",
                f"Constraints / Must Include:\n{sections.constraints.strip()}",
            ]
            if block.split(":\n", 1)[1].strip()
        )

    def _build_run_prompt(self, sections: PromptSections, chapter_target_word_count: int) -> str:
        prompt = self.build_prompt(sections)
        if chapter_target_word_count > 0:
            prompt = f"{prompt}\n\nChapter Target Word Count:\n{chapter_target_word_count}"
        return prompt

    def _build_outline_prompt(
        self,
        sections: PromptSections,
        chapter_details: Dict[int, Dict[str, object]],
        chapter_target_word_count: int,
    ) -> str:
        prompt = self._build_run_prompt(sections, chapter_target_word_count)
        chapter_details_text = self._render_chapter_details(chapter_details)
        if chapter_details_text:
            prompt = "\n\n".join([
                prompt,
                "Mandatory Chapter Beat Anchors:",
                "If chapter details or explicit chapter beats are provided below, they are binding requirements.",
                "When shaping the story arc and later outline, do not ignore them, relocate them to different chapters, merge them away, soften them into vague summaries, or contradict them.",
                "Treat each chapter's provided beats as fixed anchors that the story arc must preserve and support.",
                f"Chapter Details:\n{chapter_details_text}",
            ])
        return prompt

    def _apply_chapter_writing_style(self, chapter_prompt: str, sections: PromptSections) -> str:
        prompt = (chapter_prompt or "").strip()
        writing_style = sections.writing_style.strip()
        if not writing_style or "Writing Style Guidance:" in prompt:
            return prompt
        return "\n\n".join([
            prompt,
            "Writing Style Guidance:",
            writing_style,
            "This style guidance is mandatory for the chapter's prose voice and narration unless it conflicts with explicit current-chapter beat anchors.",
        ])

    def _normalize_max_iterations(self, max_iterations: int) -> int:
        try:
            value = int(max_iterations)
        except (TypeError, ValueError):
            value = 5
        return max(1, min(MAX_ITERATIONS_LIMIT, value))

    def _normalize_chapter_details(self, chapter_details: Dict | List | None, num_chapters: int) -> Dict[int, Dict[str, object]]:
        normalized: Dict[int, Dict[str, object]] = {}
        if not chapter_details:
            return normalized
        if isinstance(chapter_details, list):
            iterable = enumerate(chapter_details, start=1)
        elif isinstance(chapter_details, dict):
            iterable = chapter_details.items()
        else:
            return normalized
        for key, value in iterable:
            try:
                chapter_number = int(key)
            except (TypeError, ValueError):
                continue
            if not (1 <= chapter_number <= num_chapters):
                continue
            beats = ""
            target_word_count = 0
            if isinstance(value, dict):
                beats = str(value.get("beats", "")).strip()
                try:
                    target_word_count = int(value.get("target_word_count", 0) or 0)
                except (TypeError, ValueError):
                    target_word_count = 0
            else:
                beats = str(value).strip()
            if beats or target_word_count > 0:
                normalized[chapter_number] = {
                    "beats": beats,
                    "target_word_count": max(0, target_word_count),
                }
        return normalized

    def _render_chapter_details(self, chapter_details: Dict[int, Dict[str, object]]) -> str:
        parts: List[str] = []
        for chapter_number in sorted(chapter_details):
            details = chapter_details[chapter_number]
            beats = str(details.get("beats", "")).strip()
            target_word_count = int(details.get("target_word_count", 0) or 0)
            section_parts = [f"Chapter {chapter_number} Details:"]
            if beats:
                section_parts.append(f"Beats:\n{beats}")
            if target_word_count > 0:
                section_parts.append(f"Target Word Count: {target_word_count}")
            parts.append("\n".join(section_parts))
        return "\n\n".join(parts)

    def _write_outline_file(self, outline_text: str, output_folder: str) -> None:
        if not outline_text.strip():
            return
        os.makedirs(output_folder, exist_ok=True)
        path = os.path.join(output_folder, "outline.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(outline_text)

    def _persist_config_payload(self, safe_name: str, payload: Dict) -> None:
        path = os.path.join(CONFIG_DIR, f"{safe_name}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        with self._lock:
            self._state.config_name = safe_name
            self._refresh_saved_configs()
            self._append_event(f"{_utc_timestamp()} - Saved config {safe_name}")

    def save_config_data(
        self,
        name: str,
        prompt_sections: PromptSections,
        chapter_details: Dict[int, Dict[str, object]],
        num_chapters: int,
        endpoint_url: str,
        outline_model: str,
        writer_model: str,
        token_limit_enabled: bool,
        max_tokens: int,
        reduce_thinking: bool,
        max_iterations: int,
        chapter_target_word_count: int,
    ) -> None:
        safe_name = "".join(ch for ch in name.strip() if ch.isalnum() or ch in (" ", "-", "_")).strip()
        if not safe_name:
            return
        normalized_chapter_details = self._normalize_chapter_details(chapter_details, num_chapters)
        normalized_max_iterations = self._normalize_max_iterations(max_iterations)
        with self._lock:
            output_folder = self._state.output_folder or OUTPUT_FOLDER
        payload = {
            "name": safe_name,
            "created_at": _utc_timestamp(),
            "endpoint_url": endpoint_url.strip() or DEFAULT_BASE_URL,
            "outline_model": outline_model or DEFAULT_MODEL,
            "writer_model": writer_model or DEFAULT_MODEL,
            "num_chapters": max(1, num_chapters),
            "token_limit_enabled": token_limit_enabled,
            "max_tokens": max_tokens,
            "reduce_thinking": reduce_thinking,
            "max_iterations": normalized_max_iterations,
            "chapter_target_word_count": max(0, chapter_target_word_count),
            "output_folder": output_folder,
            "chapter_details": {str(key): value for key, value in normalized_chapter_details.items()},
            "prompt_sections": asdict(prompt_sections),
        }
        self._persist_config_payload(safe_name, payload)

    def start_run(
        self,
        prompt_sections: PromptSections,
        chapter_details: Dict[int, Dict[str, object]],
        num_chapters: int,
        outline_model: str,
        writer_model: str,
        endpoint_url: str,
        token_limit_enabled: bool,
        max_tokens: int,
        reduce_thinking: bool,
        max_iterations: int,
        chapter_target_word_count: int,
    ) -> bool:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return False

            normalized_chapter_details = self._normalize_chapter_details(chapter_details, num_chapters)
            normalized_max_iterations = self._normalize_max_iterations(max_iterations)
            prompt = self._build_run_prompt(prompt_sections, chapter_target_word_count)
            prior_mode = self._state.mode
            available_models = list(self._state.available_models)
            saved_configs = list(self._state.saved_configs)
            config_name = self._state.config_name
            chapters = [ChapterStatus(number=i, title=f"Chapter {i}") for i in range(1, num_chapters + 1)]
            chapter_reviews = {i: ChapterReviewState() for i in range(1, num_chapters + 1)}
            self._state = RunSnapshot(
                status="starting",
                phase="Preparing generation",
                mode=prior_mode,
                waiting_for_input=False,
                run_active=True,
                busy=True,
                stop_requested=False,
                prompt=prompt,
                num_chapters=num_chapters,
                total_chapters=num_chapters,
                outline_model=outline_model or DEFAULT_MODEL,
                writer_model=writer_model or DEFAULT_MODEL,
                endpoint_url=endpoint_url.strip() or DEFAULT_BASE_URL,
                token_limit_enabled=token_limit_enabled,
                max_tokens=max_tokens,
                reduce_thinking=reduce_thinking,
                max_iterations=normalized_max_iterations,
                chapter_target_word_count=max(0, chapter_target_word_count),
                output_folder=self._state.output_folder,
                config_name=config_name,
                chapter_details=normalized_chapter_details,
                prompt_sections=prompt_sections,
                chapters=chapters,
                chapter_reviews=chapter_reviews,
                available_models=available_models,
                saved_configs=saved_configs,
                outline_feedback="",
                outline_regeneration_requested=False,
            )
            self._chapter_memory_records = {}
            self._reset_chapter_detail_versions(normalized_chapter_details, num_chapters)
            self._resume_requested = False
            self._append_event(f"{_utc_timestamp()} - Started a new run")
            self._thread = threading.Thread(
                target=self._run_generation,
                args=(prompt_sections, normalized_chapter_details, num_chapters),
                daemon=True,
            )
            self._thread.start()
            return True

    def save_config(self, name: str) -> None:
        safe_name = "".join(ch for ch in name.strip() if ch.isalnum() or ch in (" ", "-", "_")).strip()
        if not safe_name:
            return
        snapshot = self.get_snapshot()
        payload = {
            "name": safe_name,
            "created_at": _utc_timestamp(),
            "endpoint_url": snapshot.endpoint_url,
            "outline_model": snapshot.outline_model,
            "writer_model": snapshot.writer_model,
            "num_chapters": snapshot.num_chapters,
            "token_limit_enabled": snapshot.token_limit_enabled,
            "max_tokens": snapshot.max_tokens,
            "reduce_thinking": snapshot.reduce_thinking,
            "max_iterations": snapshot.max_iterations,
            "chapter_target_word_count": snapshot.chapter_target_word_count,
            "output_folder": snapshot.output_folder,
            "chapter_details": {str(key): value for key, value in snapshot.chapter_details.items()},
            "prompt_sections": asdict(snapshot.prompt_sections),
        }
        self._persist_config_payload(safe_name, payload)

    def load_config(self, filename: str) -> None:
        path = os.path.join(CONFIG_DIR, filename)
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload.setdefault("name", os.path.splitext(filename)[0])
        self.load_config_payload(payload, f"Loaded config {filename}")

    def load_config_payload(self, payload: Dict, event_label: str = "Loaded external config") -> None:
        sections = PromptSections(**payload.get("prompt_sections", {}))
        with self._lock:
            num_chapters = int(payload.get("num_chapters", self._state.num_chapters))
            chapter_details_source = payload.get("chapter_details")
            if chapter_details_source is None:
                chapter_details_source = payload.get("chapter_beats", {})
            chapter_details = self._normalize_chapter_details(chapter_details_source, num_chapters)
            self._state.prompt_sections = sections
            self._state.endpoint_url = payload.get("endpoint_url", self._state.endpoint_url)
            self._state.outline_model = payload.get("outline_model", self._state.outline_model)
            self._state.writer_model = payload.get("writer_model", self._state.writer_model)
            self._state.num_chapters = num_chapters
            self._state.total_chapters = self._state.num_chapters
            self._state.token_limit_enabled = bool(payload.get("token_limit_enabled", self._state.token_limit_enabled))
            self._state.max_tokens = int(payload.get("max_tokens", self._state.max_tokens))
            self._state.reduce_thinking = bool(payload.get("reduce_thinking", self._state.reduce_thinking))
            self._state.max_iterations = self._normalize_max_iterations(payload.get("max_iterations", self._state.max_iterations))
            self._state.chapter_target_word_count = int(payload.get("chapter_target_word_count", self._state.chapter_target_word_count))
            self._state.output_folder = payload.get("output_folder", self._state.output_folder) or OUTPUT_FOLDER
            self._state.config_name = str(payload.get("name", self._state.config_name or "")).strip()
            self._state.chapter_details = chapter_details
            self._chapter_memory_records = {}
            self._reset_chapter_detail_versions(chapter_details, num_chapters)
            self._state.prompt = self._build_run_prompt(sections, self._state.chapter_target_word_count)
            self._state.chapters = [
                ChapterStatus(number=i, title=f"Chapter {i}")
                for i in range(1, self._state.num_chapters + 1)
            ]
            self._state.chapter_reviews = {i: ChapterReviewState() for i in range(1, self._state.num_chapters + 1)}
            self._state.continuity = ContinuityState()
            self._state.current_chapter = 0
            self._state.latest_advice = ""
            self._state.outline_text = ""
            self._state.current_checkpoint_title = ""
            self._state.current_checkpoint_body = ""
            self._clear_resume_state()
            self._state.phase_version += 1
            self._append_event(f"{_utc_timestamp()} - {event_label}")

    def approve_outline(self) -> None:
        with self._condition:
            self._state.outline_approved = True
            self._state.awaiting_outline_approval = False
            self._state.outline_regeneration_requested = False
            self._resume_requested = True
            self._state.waiting_for_input = False
            self._append_event(f"{_utc_timestamp()} - Outline approved")
            self._condition.notify_all()

    def set_outline_feedback(self, feedback: str) -> None:
        with self._lock:
            self._state.outline_feedback = feedback.strip()
            self._append_event(f"{_utc_timestamp()} - Updated outline feedback")

    def report_error(self, message: str) -> None:
        with self._lock:
            self._state.latest_error = message.strip()
            self._state.phase_version += 1
            self._append_event(f"{_utc_timestamp()} - {message.strip()}")

    def regenerate_outline(self) -> None:
        with self._condition:
            self._state.outline_regeneration_requested = True
            self._state.awaiting_outline_approval = False
            self._state.waiting_for_input = False
            self._state.status = "running"
            self._state.phase = "Regenerating outline"
            self._state.busy = True
            self._state.phase_version += 1
            self._resume_requested = True
            self._append_event(f"{_utc_timestamp()} - Outline regeneration requested")
            self._condition.notify_all()

    def regenerate_chapter(self, chapter_number: int) -> bool:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return False
            if not self._state.outline_approved or not self._state.outline_text:
                return False
            self._state.run_active = True
            self._state.busy = True
            self._state.status = "running"
            self._state.stop_requested = False
            self._state.waiting_for_input = False
            self._state.current_chapter = chapter_number
            self._state.phase = f"Regenerating chapter {chapter_number}"
            self._state.phase_version += 1
            self._append_event(f"{_utc_timestamp()} - Started regeneration for chapter {chapter_number}")
            self._thread = threading.Thread(
                target=self._run_single_chapter_regeneration,
                args=(chapter_number,),
                daemon=True,
            )
            self._thread.start()
            return True

    def set_mode(self, mode: str) -> None:
        if mode not in {"keep_going", "ask_for_advice"}:
            return
        with self._condition:
            self._state.mode = mode
            self._append_event(f"{_utc_timestamp()} - Mode changed to {mode}")
            if mode == "keep_going" and not self._state.awaiting_outline_approval:
                self._resume_requested = True
                self._state.waiting_for_input = False
                self._condition.notify_all()

    def submit_chapter_advice(
        self,
        chapter_number: int,
        beats: str,
        target_word_count: Optional[int] = None,
    ) -> None:
        beats = beats.strip()
        with self._condition:
            max_chapter = max(self._state.total_chapters, self._state.num_chapters, 1)
            if not (1 <= chapter_number <= max_chapter):
                return
            if not beats and (target_word_count is None or target_word_count <= 0):
                return

            existing_details = dict(self._state.chapter_details.get(chapter_number, {}))
            if beats:
                existing_details["beats"] = beats
            if target_word_count is not None and target_word_count > 0:
                existing_details["target_word_count"] = max(0, int(target_word_count))
            self._state.chapter_details[chapter_number] = existing_details

            next_version = int(self._chapter_detail_versions.get(chapter_number, 0)) + 1
            self._chapter_detail_versions[chapter_number] = next_version

            current_attempt = int(self._state.progress.iteration or 0)
            if (
                self._state.run_active
                and self._state.current_chapter == chapter_number
                and current_attempt > 0
            ):
                applies_text = f"Applies on the next attempt for Chapter {chapter_number}."
            elif self._state.run_active and self._state.current_chapter < chapter_number:
                applies_text = f"Will apply automatically when Chapter {chapter_number} starts."
            else:
                applies_text = f"Will apply the next time Chapter {chapter_number} is generated."

            summary_lines = [
                f"Chapter {chapter_number} advice queued as beat override v{next_version}.",
                applies_text,
            ]
            if target_word_count is not None and target_word_count > 0:
                summary_lines.append(f"Target Word Count: {int(target_word_count)}")
            if beats:
                summary_lines.extend(["Beats:", beats])
            summary = "\n".join(summary_lines).strip()

            self._state.latest_advice = summary
            review = self._state.chapter_reviews.setdefault(chapter_number, ChapterReviewState())
            review.improvement_notes = summary
            self._state.phase_version += 1
            self._append_event(
                f"{_utc_timestamp()} - Chapter {chapter_number} beat override queued (v{next_version})"
            )
            if self._state.mode == "keep_going" and not self._state.awaiting_outline_approval:
                self._resume_requested = True
                self._state.waiting_for_input = False
                self._condition.notify_all()

    def continue_run(self) -> None:
        with self._condition:
            if self._state.awaiting_outline_approval:
                return
            if self._thread and self._thread.is_alive():
                if self._state.stop_requested:
                    self._state.stop_requested = False
                    self._state.status = "running"
                    self._state.busy = True
                    if self._state.current_chapter:
                        self._state.phase = f"Generating chapter {self._state.current_chapter}"
                    self._state.phase_version += 1
                    self._append_event(f"{_utc_timestamp()} - Pause request canceled")
                self._resume_requested = True
                self._state.waiting_for_input = False
                self._append_event(f"{_utc_timestamp()} - Continue requested")
                self._condition.notify_all()
                return
            resume_chapter = self._get_resume_chapter_number()
            if resume_chapter <= 0 or not self._state.outline_text:
                return
            self._state.run_active = True
            self._state.busy = True
            self._state.status = "running"
            self._state.stop_requested = False
            self._state.waiting_for_input = False
            self._state.phase = f"Resuming generation from chapter {resume_chapter}"
            self._state.phase_version += 1
            self._append_event(
                f"{_utc_timestamp()} - Resuming generation from chapter {resume_chapter}"
            )
            self._thread = threading.Thread(
                target=self._run_resumed_generation,
                args=(resume_chapter,),
                daemon=True,
            )
            self._thread.start()

    def stop_run(self) -> None:
        with self._condition:
            if self._state.stop_requested:
                return
            self._state.stop_requested = True
            self._state.waiting_for_input = False
            self._state.phase = "Pausing after the current chapter step"
            self._state.phase_version += 1
            self._append_event(f"{_utc_timestamp()} - Pause requested")
            self._condition.notify_all()

    def _append_event(self, event: str) -> None:
        with self._condition:
            self._state.recent_events.append(event)
            self._state.recent_events = self._state.recent_events[-30:]
            self._condition.notify_all()

    def _append_progress_event(self, event: str) -> None:
        with self._condition:
            self._state.progress_events.append(event)
            self._state.progress_events = self._state.progress_events[-40:]
            self._condition.notify_all()

    def _refresh_saved_configs(self) -> None:
        self._state.saved_configs = sorted(
            name for name in os.listdir(CONFIG_DIR) if name.endswith(".json")
        )

    def _set_progress(
        self,
        *,
        chapter_number: int,
        chapter_title: str,
        current_agent: str,
        current_step: str,
        iteration: int,
        max_iterations: int,
        output_stage: str,
        detail: str,
    ) -> None:
        with self._lock:
            self._state.progress = ProgressState(
                chapter_number=chapter_number,
                chapter_title=chapter_title,
                current_agent=current_agent,
                current_step=current_step,
                iteration=iteration,
                max_iterations=max_iterations,
                output_stage=output_stage,
                detail=detail,
            )
            self._state.busy = bool(current_agent and current_agent != "idle")
            self._state.phase_version += 1
            iteration_label = ""
            if iteration > 0:
                iteration_label = f" | iteration {iteration}/{max_iterations or 0}"
            self._append_progress_event(
                f"{_utc_timestamp()} - Chapter {chapter_number or '-'} - "
                f"{current_agent or 'system'}{iteration_label} - {detail or current_step}"
            )

    def _checkpoint(self, title: str, body: str) -> bool:
        with self._condition:
            self._state.current_checkpoint_title = title
            self._state.current_checkpoint_body = body
            self._state.phase_version += 1
            self._append_event(f"{_utc_timestamp()} - Checkpoint: {title}")
            if self._state.stop_requested:
                return False
            if self._state.mode == "keep_going":
                self._state.waiting_for_input = False
                return True
            self._state.status = "waiting"
            self._state.phase = title
            self._state.waiting_for_input = True
            self._state.busy = False
            self._resume_requested = False
            while not self._resume_requested and not self._state.stop_requested:
                self._condition.wait(timeout=0.5)
            self._state.waiting_for_input = False
            self._state.busy = not self._state.stop_requested
            self._resume_requested = False
            return not self._state.stop_requested

    def _outline_approval_gate(self) -> str:
        with self._condition:
            self._state.awaiting_outline_approval = True
            self._state.outline_approved = False
            self._state.status = "waiting"
            self._state.phase = "Awaiting outline approval"
            self._state.waiting_for_input = True
            self._state.busy = False
            self._state.phase_version += 1
            self._append_event(f"{_utc_timestamp()} - Waiting for outline approval")
            while (
                not self._state.outline_approved
                and not self._state.stop_requested
                and not self._state.outline_regeneration_requested
            ):
                self._condition.wait(timeout=0.5)
            self._state.waiting_for_input = False
            self._state.busy = False
            if self._state.stop_requested:
                return "stopped"
            if self._state.outline_regeneration_requested:
                self._state.outline_regeneration_requested = False
                return "regenerate"
            return "approved"

    def _mark_finished(
        self,
        status: str,
        phase: str,
        error: str = "",
        *,
        resume_available: bool = False,
        resume_chapter_number: int = 0,
        ) -> None:
        with self._lock:
            self._state.status = status
            self._state.phase = phase or ("Paused" if status == "stopped" else self._state.phase)
            self._state.latest_error = error
            self._state.run_active = False
            self._state.busy = False
            self._state.stop_requested = False
            self._state.waiting_for_input = False
            self._state.awaiting_outline_approval = False
            total_chapters = int(self._state.total_chapters or self._state.num_chapters or 0)
            if resume_available and 1 <= int(resume_chapter_number or 0) <= total_chapters:
                self._state.resume_available = True
                self._state.resume_chapter_number = int(resume_chapter_number)
            else:
                self._clear_resume_state()
            self._state.phase_version += 1

    def _outline_to_text(self, outline: List[Dict]) -> str:
        parts: List[str] = []
        for chapter in outline:
            prompt = chapter.get("prompt", "")
            if isinstance(prompt, list):
                prompt = "\n".join(str(item).strip() for item in prompt if str(item).strip())
            else:
                prompt = str(prompt).strip()
            parts.append(f"Chapter {chapter['chapter_number']}: {chapter['title']}")
            parts.append("-" * 50)
            parts.append(prompt)
            parts.append("")
        return "\n".join(parts).strip()

    def _parse_outline(self) -> List[Dict]:
        chapters: List[Dict] = []
        current_number: Optional[int] = None
        current_title = ""
        current_prompt: List[str] = []
        for line in self._state.outline_text.splitlines():
            if line.startswith("Chapter ") and ":" in line:
                if current_number is not None:
                    chapters.append({
                        "chapter_number": current_number,
                        "title": current_title,
                        "prompt": "\n".join(current_prompt).strip(),
                    })
                prefix, title = line.split(":", 1)
                try:
                    current_number = int(prefix.replace("Chapter", "").strip())
                except ValueError:
                    current_number = None
                current_title = title.strip() or prefix.strip()
                current_prompt = []
            elif current_number is not None and line.strip() and not set(line.strip()) == {"-"}:
                current_prompt.append(line)
        if current_number is not None:
            chapters.append({
                "chapter_number": current_number,
                "title": current_title,
                "prompt": "\n".join(current_prompt).strip(),
            })
        return chapters

    def _read_chapter_text(self, chapter_number: int) -> str:
        path = os.path.join(self._state.output_folder, f"chapter_{chapter_number:02d}.txt")
        if not os.path.exists(path):
            return f"Chapter file not found at {path}"
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _set_chapter_status(self, chapter_number: int, status: str, title: Optional[str] = None) -> None:
        with self._lock:
            if 1 <= chapter_number <= len(self._state.chapters):
                chapter = self._state.chapters[chapter_number - 1]
                chapter.status = status
                if title:
                    chapter.title = title
                self._state.phase_version += 1

    def _store_chapter_result(self, chapter_number: int, result: Dict, chapter_text: str) -> None:
        with self._lock:
            review = self._state.chapter_reviews.setdefault(chapter_number, ChapterReviewState())
            if review.saved_text:
                review.alternates.append(review.saved_text)
                review.alternates = review.alternates[-5:]
            review.saved_text = chapter_text
            review.artifacts = ChapterArtifacts(
                memory_update=result.get("memory_update", ""),
                draft_scene=result.get("draft_scene", ""),
                editor_feedback=result.get("editor_feedback", ""),
                final_scene=result.get("final_scene", ""),
            )
        self._update_continuity_from_result(chapter_number, result, chapter_text)

    def _update_continuity_from_result(self, chapter_number: int, result: Dict, chapter_text: str) -> None:
        record = self._build_memory_record(chapter_number, result, chapter_text)
        with self._lock:
            self._chapter_memory_records[chapter_number] = record
            self._rebuild_continuity_from_memory_records()
        self._write_memory_snapshot()

    def _progress_callback(self, event: Dict) -> None:
        self._set_progress(
            chapter_number=event.get("chapter_number", 0),
            chapter_title=event.get("chapter_title", ""),
            current_agent=event.get("agent", ""),
            current_step=event.get("step", ""),
            iteration=event.get("iteration", 0),
            max_iterations=event.get("max_iterations", 0),
            output_stage=event.get("output_stage", ""),
            detail=event.get("detail", ""),
        )

    def _set_idle_progress(self, detail: str = "Idle") -> None:
        self._set_progress(
            chapter_number=0,
            chapter_title="",
            current_agent="idle",
            current_step="idle",
            iteration=0,
            max_iterations=0,
            output_stage="idle",
            detail=detail,
        )

    def _create_book_generator(
        self,
        *,
        outline: List[Dict],
        prompt_sections: PromptSections,
        prompt: str,
        output_folder: str,
        endpoint_url: str,
        outline_model: str,
        writer_model: str,
        requested_max_tokens: Optional[int],
        reasoning_effort: Optional[str],
        extra_body: Optional[Dict[str, object]],
        max_iterations: int,
        start_chapter: int,
    ) -> BookGenerator:
        chapter_support_config = get_config(
            local_url=endpoint_url,
            model=outline_model,
            max_tokens=requested_max_tokens,
            reasoning_effort=reasoning_effort,
            extra_body=extra_body,
        )
        writer_config = get_config(
            local_url=endpoint_url,
            model=writer_model,
            max_tokens=requested_max_tokens,
            reasoning_effort=reasoning_effort,
            extra_body=extra_body,
        )
        book_agents = BookAgents(
            chapter_support_config,
            outline,
            writer_agent_config=writer_config,
        )
        chapter_agents = book_agents.create_agents(prompt, len(outline))
        base_chapter_prompts = {
            int(chapter["chapter_number"]): str(chapter.get("prompt", "")).strip()
            for chapter in outline
        }
        chapter_prompt_provider = self._make_chapter_prompt_provider(base_chapter_prompts, prompt_sections)
        return BookGenerator(
            chapter_agents,
            writer_config,
            outline,
            output_dir=output_folder,
            max_iterations=max_iterations,
            progress_callback=self._progress_callback,
            diagnostic_logger=self._append_output_log,
            chapter_prompt_provider=chapter_prompt_provider,
            validation_callback=self._handle_validation_event,
            should_stop_callback=self._should_stop_chapter_generation,
            initial_chapter_memory=self._build_initial_chapter_memory(start_chapter),
        )

    def _mark_generation_paused(self, chapter_number: int, phase: str) -> None:
        resume_chapter = chapter_number if 1 <= chapter_number <= int(self._state.total_chapters or self._state.num_chapters or 0) else 0
        if resume_chapter > 0:
            phase = f"{phase} Ready to resume from chapter {resume_chapter}."
        self._mark_finished(
            "stopped",
            phase,
            resume_available=resume_chapter > 0,
            resume_chapter_number=resume_chapter,
        )

    def _run_chapter_sequence(
        self,
        outline: List[Dict],
        book_generator: BookGenerator,
        prompt_sections: PromptSections,
        start_chapter: int,
    ) -> bool:
        for chapter in outline:
            chapter_number = int(chapter["chapter_number"])
            if chapter_number < start_chapter:
                continue
            if self._state.stop_requested:
                self._append_event(f"{_utc_timestamp()} - Paused before chapter {chapter_number}")
                self._mark_generation_paused(
                    chapter_number,
                    f"Paused before chapter {chapter_number}.",
                )
                return False

            chapter_prompt = self._build_effective_chapter_prompt(
                chapter_number,
                chapter["prompt"],
                prompt_sections,
            )
            with self._lock:
                self._state.current_chapter = chapter_number
                self._state.status = "running"
                self._state.phase = f"Generating chapter {chapter_number}"
                self._state.phase_version += 1
            self._set_chapter_status(chapter_number, "in_progress", chapter["title"])
            try:
                result = book_generator.generate_chapter(chapter_number, chapter_prompt) or {}
            except GenerationPauseRequested:
                self._set_chapter_status(chapter_number, "pending", chapter["title"])
                self._append_event(f"{_utc_timestamp()} - Paused during chapter {chapter_number}")
                self._mark_generation_paused(
                    chapter_number,
                    f"Paused during chapter {chapter_number}.",
                )
                return False

            chapter_text = self._read_chapter_text(chapter_number)
            self._store_chapter_result(chapter_number, result, chapter_text)
            self._set_chapter_status(chapter_number, "completed", chapter["title"])
            if not self._checkpoint(f"Chapter {chapter_number} complete", chapter_text):
                next_resume = self._next_resume_chapter(chapter_number)
                self._append_event(f"{_utc_timestamp()} - Paused after chapter {chapter_number}")
                if next_resume > 0:
                    self._mark_finished(
                        "stopped",
                        f"Paused after chapter {chapter_number}. Ready to resume from chapter {next_resume}.",
                        resume_available=True,
                        resume_chapter_number=next_resume,
                    )
                else:
                    self._mark_finished("completed", "Generation complete")
                return False
        return True

    def _run_resumed_generation(self, start_chapter: int) -> None:
        try:
            with self._lock:
                endpoint_url = self._state.endpoint_url
                outline_model = self._state.outline_model
                writer_model = self._state.writer_model
                prompt = self._state.prompt
                token_limit_enabled = self._state.token_limit_enabled
                max_tokens = self._state.max_tokens
                reduce_thinking = self._state.reduce_thinking
                max_iterations = self._state.max_iterations
                output_folder = self._state.output_folder
                prompt_sections = PromptSections(**self._state.prompt_sections.__dict__)
                self._clear_resume_state()
                self._state.phase = f"Resuming generation from chapter {start_chapter}"
                self._state.phase_version += 1

            self._initialize_output_log(output_folder)
            self._write_memory_snapshot()
            requested_max_tokens = max_tokens if token_limit_enabled else None
            extra_body = {
                "thinking": False,
                "enable_thinking": False,
            } if reduce_thinking else None
            reasoning_effort = "low" if reduce_thinking else None
            outline = self._parse_outline()
            if not outline:
                raise ValueError("No approved outline available to resume generation")

            book_generator = self._create_book_generator(
                outline=outline,
                prompt_sections=prompt_sections,
                prompt=prompt,
                output_folder=output_folder,
                endpoint_url=endpoint_url,
                outline_model=outline_model,
                writer_model=writer_model,
                requested_max_tokens=requested_max_tokens,
                reasoning_effort=reasoning_effort,
                extra_body=extra_body,
                max_iterations=max_iterations,
                start_chapter=start_chapter,
            )
            if not self._run_chapter_sequence(outline, book_generator, prompt_sections, start_chapter):
                return
            self._mark_finished("completed", "Generation complete")
            with self._lock:
                self._append_event(f"{_utc_timestamp()} - Run completed")
        except Exception as exc:  # pragma: no cover
            trace = traceback.format_exc()
            error_message = _format_exception(exc)
            if self._state.reduce_thinking:
                error_message = (
                    f"{error_message} | No Thinking payload="
                    "{'thinking': False, 'enable_thinking': False, 'reasoning_effort': 'low'}"
                )
            self._log_runtime("\n[generation_controller] Resume failed")
            self._log_runtime(trace.rstrip())
            with self._lock:
                if 1 <= self._state.current_chapter <= len(self._state.chapters):
                    self._state.chapters[self._state.current_chapter - 1].status = "failed"
            self._mark_finished(
                "failed",
                "Resume failed",
                error_message,
                resume_available=1 <= start_chapter <= int(self._state.total_chapters or self._state.num_chapters or 0),
                resume_chapter_number=start_chapter,
            )
            with self._lock:
                self._append_event(f"{_utc_timestamp()} - Resume failed: {error_message}")

    def _run_generation(self, prompt_sections: PromptSections, chapter_details: Dict[int, Dict[str, object]], num_chapters: int) -> None:
        try:
            with self._lock:
                self._state.status = "running"
                self._state.phase = "Generating outline"
                self._state.phase_version += 1
                endpoint_url = self._state.endpoint_url
                outline_model = self._state.outline_model
                writer_model = self._state.writer_model
                token_limit_enabled = self._state.token_limit_enabled
                max_tokens = self._state.max_tokens
                reduce_thinking = self._state.reduce_thinking
                max_iterations = self._state.max_iterations
                chapter_target_word_count = self._state.chapter_target_word_count
                output_folder = self._state.output_folder
            self._initialize_output_log(output_folder)
            self._write_memory_snapshot()
            requested_max_tokens = max_tokens if token_limit_enabled else None
            extra_body = {
                "thinking": False,
                "enable_thinking": False,
            } if reduce_thinking else None
            reasoning_effort = "low" if reduce_thinking else None
            if extra_body:
                self._log_runtime(
                    "[generation_controller] Using no-thinking settings: "
                    f"extra_body={extra_body}, reasoning_effort={reasoning_effort}"
                )
            prompt = self._build_run_prompt(prompt_sections, chapter_target_word_count)
            outline_prompt = self._build_outline_prompt(
                prompt_sections,
                chapter_details,
                chapter_target_word_count,
            )
            self._log_runtime_block(
                "RUN SETTINGS",
                "\n".join([
                    f"endpoint_url: {endpoint_url}",
                    f"outline_model: {outline_model}",
                    f"writer_model: {writer_model}",
                    f"token_limit_enabled: {token_limit_enabled}",
                    f"max_tokens: {requested_max_tokens}",
                    f"reduce_thinking: {reduce_thinking}",
                    f"reasoning_effort: {reasoning_effort}",
                    f"max_iterations: {max_iterations}",
                    f"chapter_target_word_count: {chapter_target_word_count}",
                    f"output_folder: {output_folder}",
                    f"num_chapters: {num_chapters}",
                ]),
            )
            self._log_runtime_block("RUN INPUT | BASE PROMPT", prompt)
            self._log_runtime_block("RUN INPUT | OUTLINE PROMPT", outline_prompt)
            approved_outline: List[Dict] = []
            while True:
                with self._lock:
                    outline_feedback = self._state.outline_feedback
                current_outline_prompt = outline_prompt
                if outline_feedback:
                    current_outline_prompt = (
                        f"{outline_prompt}\n\nOutline revision feedback from the human reviewer:\n"
                        f"{outline_feedback}\n\nRegenerate the outline and address this feedback."
                    )
                self._set_progress(
                    chapter_number=0,
                    chapter_title="",
                    current_agent="outline_creator",
                    current_step="generating_outline",
                    iteration=1,
                    max_iterations=1,
                    output_stage="outline",
                    detail="Generating outline draft",
                )
                outline_config = get_config(
                    local_url=endpoint_url,
                    model=outline_model,
                    max_tokens=requested_max_tokens,
                    reasoning_effort=reasoning_effort,
                    extra_body=extra_body,
                )
                outline_agents = BookAgents(outline_config)
                agents = outline_agents.create_agents(current_outline_prompt, num_chapters)
                outline_generator = OutlineGenerator(
                    agents,
                    outline_config,
                    progress_callback=self._progress_callback,
                    diagnostic_logger=self._append_output_log,
                )
                outline = outline_generator.generate_outline(current_outline_prompt, num_chapters)
                outline_text = self._outline_to_text(outline)
                self._write_outline_file(outline_text, output_folder)
                with self._lock:
                    self._state.outline_text = outline_text
                    self._state.phase = "Outline complete"
                    self._state.status = "running"
                    self._state.busy = False
                    self._state.phase_version += 1
                    for chapter in outline:
                        if 1 <= chapter["chapter_number"] <= len(self._state.chapters):
                            self._state.chapters[chapter["chapter_number"] - 1].title = chapter["title"]
                self._set_idle_progress("Outline ready for review")
                if not self._checkpoint("Outline ready for review", outline_text):
                    self._mark_finished("stopped", "Stopped after outline")
                    return
                outline_decision = self._outline_approval_gate()
                if outline_decision == "approved":
                    approved_outline = outline
                    break
                if outline_decision == "stopped":
                    self._mark_finished("stopped", "Outline not approved")
                    return
                self._append_event(f"{_utc_timestamp()} - Regenerating outline with reviewer feedback")
            outline = approved_outline
            book_generator = self._create_book_generator(
                outline=outline,
                prompt_sections=prompt_sections,
                prompt=prompt,
                output_folder=output_folder,
                endpoint_url=endpoint_url,
                outline_model=outline_model,
                writer_model=writer_model,
                requested_max_tokens=requested_max_tokens,
                reasoning_effort=reasoning_effort,
                extra_body=extra_body,
                max_iterations=max_iterations,
                start_chapter=1,
            )
            if not self._run_chapter_sequence(outline, book_generator, prompt_sections, 1):
                return
            self._mark_finished("completed", "Generation complete")
            with self._lock:
                self._append_event(f"{_utc_timestamp()} - Run completed")
        except Exception as exc:  # pragma: no cover
            trace = traceback.format_exc()
            error_message = _format_exception(exc)
            if self._state.reduce_thinking:
                error_message = (
                    f"{error_message} | No Thinking payload="
                    "{'thinking': False, 'enable_thinking': False, 'reasoning_effort': 'low'}"
                )
            self._log_runtime("\n[generation_controller] Run failed")
            self._log_runtime(trace.rstrip())
            with self._lock:
                if 1 <= self._state.current_chapter <= len(self._state.chapters):
                    self._state.chapters[self._state.current_chapter - 1].status = "failed"
            self._mark_finished("failed", "Generation failed", error_message)
            with self._lock:
                self._append_event(f"{_utc_timestamp()} - Run failed: {error_message}")

    def _run_single_chapter_regeneration(self, chapter_number: int) -> None:
        try:
            with self._lock:
                endpoint_url = self._state.endpoint_url
                outline_model = self._state.outline_model
                writer_model = self._state.writer_model
                prompt = self._state.prompt
                token_limit_enabled = self._state.token_limit_enabled
                max_tokens = self._state.max_tokens
                reduce_thinking = self._state.reduce_thinking
                max_iterations = self._state.max_iterations
                chapter_target_word_count = self._state.chapter_target_word_count
                output_folder = self._state.output_folder
                prompt_sections = PromptSections(**self._state.prompt_sections.__dict__)
                self._state.phase = f"Regenerating chapter {chapter_number}"
                self._state.phase_version += 1
            self._initialize_output_log(output_folder)
            self._write_memory_snapshot()
            requested_max_tokens = max_tokens if token_limit_enabled else None
            extra_body = {
                "thinking": False,
                "enable_thinking": False,
            } if reduce_thinking else None
            reasoning_effort = "low" if reduce_thinking else None
            if extra_body:
                self._log_runtime(
                    "[generation_controller] Using no-thinking settings: "
                    f"extra_body={extra_body}, reasoning_effort={reasoning_effort}"
                )
            self._log_runtime_block(
                f"CHAPTER REGEN SETTINGS | CHAPTER {chapter_number}",
                "\n".join([
                    f"endpoint_url: {endpoint_url}",
                    f"outline_model: {outline_model}",
                    f"writer_model: {writer_model}",
                    f"token_limit_enabled: {token_limit_enabled}",
                    f"max_tokens: {requested_max_tokens}",
                    f"reduce_thinking: {reduce_thinking}",
                    f"reasoning_effort: {reasoning_effort}",
                    f"max_iterations: {max_iterations}",
                    f"chapter_target_word_count: {chapter_target_word_count}",
                    f"output_folder: {output_folder}",
                ]),
            )
            self._log_runtime_block(f"CHAPTER REGEN INPUT | BASE PROMPT | CHAPTER {chapter_number}", prompt)
            outline = self._parse_outline()
            if not outline:
                raise ValueError("No approved outline available for regeneration")
            book_generator = self._create_book_generator(
                outline=outline,
                prompt_sections=prompt_sections,
                prompt=prompt,
                output_folder=output_folder,
                endpoint_url=endpoint_url,
                outline_model=outline_model,
                writer_model=writer_model,
                requested_max_tokens=requested_max_tokens,
                reasoning_effort=reasoning_effort,
                extra_body=extra_body,
                max_iterations=max_iterations,
                start_chapter=chapter_number,
            )
            chapter = next((item for item in outline if item["chapter_number"] == chapter_number), None)
            if not chapter:
                raise ValueError(f"Chapter {chapter_number} not found in outline")
            chapter_prompt = self._build_effective_chapter_prompt(
                chapter_number,
                chapter["prompt"],
                prompt_sections,
            )
            self._set_chapter_status(chapter_number, "in_progress", chapter["title"])
            result = book_generator.generate_chapter(chapter_number, chapter_prompt) or {}
            chapter_text = self._read_chapter_text(chapter_number)
            self._store_chapter_result(chapter_number, result, chapter_text)
            self._set_chapter_status(chapter_number, "completed", chapter["title"])
            self._checkpoint(f"Chapter {chapter_number} regenerated", chapter_text)
            resume_chapter = self._get_resume_chapter_number()
            if resume_chapter == chapter_number:
                resume_chapter = self._next_resume_chapter(chapter_number)
            resume_available = resume_chapter > 0
            phase = f"Chapter {chapter_number} regeneration complete"
            if resume_available:
                phase = f"{phase}. Ready to resume from chapter {resume_chapter}."
            self._mark_finished(
                "completed",
                phase,
                resume_available=resume_available,
                resume_chapter_number=resume_chapter,
            )
        except GenerationPauseRequested:
            resume_chapter = self._get_resume_chapter_number()
            resume_available = resume_chapter > 0
            phase = f"Paused during chapter {chapter_number} regeneration."
            if resume_available:
                phase = f"{phase} Ready to resume the book from chapter {resume_chapter}."
            self._set_chapter_status(chapter_number, "pending")
            self._mark_finished(
                "stopped",
                phase,
                resume_available=resume_available,
                resume_chapter_number=resume_chapter,
            )
            with self._lock:
                self._append_event(f"{_utc_timestamp()} - Chapter {chapter_number} regeneration paused")
        except Exception as exc:  # pragma: no cover
            trace = traceback.format_exc()
            error_message = _format_exception(exc)
            if self._state.reduce_thinking:
                error_message = (
                    f"{error_message} | No Thinking payload="
                    "{'thinking': False, 'enable_thinking': False, 'reasoning_effort': 'low'}"
                )
            self._log_runtime(f"\n[generation_controller] Chapter {chapter_number} regeneration failed")
            self._log_runtime(trace.rstrip())
            self._set_chapter_status(chapter_number, "failed")
            resume_chapter = self._get_resume_chapter_number()
            self._mark_finished(
                "failed",
                f"Chapter {chapter_number} regeneration failed",
                error_message,
                resume_available=resume_chapter > 0,
                resume_chapter_number=resume_chapter,
            )
            with self._lock:
                self._append_event(f"{_utc_timestamp()} - Chapter regeneration failed: {error_message}")
