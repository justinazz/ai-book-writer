"""Background generation controller for the browser UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
import threading
import traceback
from typing import Dict, List, Optional
from urllib.error import URLError
from urllib.request import urlopen

from agents import BookAgents
from book_generator import BookGenerator
from config import DEFAULT_BASE_URL, DEFAULT_MODEL, MAX_ITERATIONS_LIMIT, OUTPUT_FOLDER, get_config
from outline_generator import OutlineGenerator


CONFIG_DIR = "saved_configs"


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
    temperature: float = 0.8
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


class GenerationController:
    """Runs outline and chapter generation in the background with pause points."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._diagnostic_log_lock = threading.RLock()
        self._diagnostic_log_path: Optional[str] = None
        self._state = RunSnapshot()
        self._pending_advice: List[str] = []
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
                temperature=self._state.temperature,
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
        temperature: float,
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
            "temperature": temperature,
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
        temperature: float,
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
                temperature=temperature,
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
            self._pending_advice = []
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
            "temperature": snapshot.temperature,
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
            self._state.temperature = float(payload.get("temperature", self._state.temperature))
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
            self._state.prompt = self._build_run_prompt(sections, self._state.chapter_target_word_count)
            self._state.chapters = [
                ChapterStatus(number=i, title=f"Chapter {i}")
                for i in range(1, self._state.num_chapters + 1)
            ]
            self._state.chapter_reviews = {i: ChapterReviewState() for i in range(1, self._state.num_chapters + 1)}
            self._state.current_chapter = 0
            self._state.outline_text = ""
            self._state.current_checkpoint_title = ""
            self._state.current_checkpoint_body = ""
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

    def submit_advice(self, advice: str) -> None:
        advice = advice.strip()
        if not advice:
            return
        with self._condition:
            self._pending_advice.append(advice)
            self._state.latest_advice = advice
            self._append_event(f"{_utc_timestamp()} - Advice queued for the next step")
            if self._state.mode == "keep_going" and not self._state.awaiting_outline_approval:
                self._resume_requested = True
                self._state.waiting_for_input = False
                self._condition.notify_all()

    def continue_run(self) -> None:
        with self._condition:
            if self._state.awaiting_outline_approval:
                return
            self._resume_requested = True
            self._state.waiting_for_input = False
            self._append_event(f"{_utc_timestamp()} - Continue requested")
            self._condition.notify_all()

    def stop_run(self) -> None:
        with self._condition:
            self._state.stop_requested = True
            self._state.waiting_for_input = False
            self._state.phase = "Pausing at the next checkpoint"
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

    def _consume_advice(self) -> str:
        with self._lock:
            if not self._pending_advice:
                return ""
            advice = "\n\n".join(self._pending_advice)
            self._pending_advice.clear()
            return advice

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
            self._append_progress_event(
                f"{_utc_timestamp()} - Chapter {chapter_number or '-'} - "
                f"{current_agent or 'system'} - {detail or current_step}"
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

    def _mark_finished(self, status: str, phase: str, error: str = "") -> None:
        with self._lock:
            self._state.status = status
            self._state.phase = "Paused" if status == "stopped" else phase
            self._state.latest_error = error
            self._state.run_active = False
            self._state.busy = False
            self._state.waiting_for_input = False
            self._state.awaiting_outline_approval = False
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
        self._update_continuity_from_result(chapter_number, result)

    def _update_continuity_from_result(self, chapter_number: int, result: Dict) -> None:
        memory_text = result.get("memory_update", "")
        if not memory_text:
            return
        summary = f"Chapter {chapter_number}: {memory_text[:300].strip()}"
        characters: List[str] = []
        world: List[str] = []
        alerts: List[str] = []
        for line in memory_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("CHARACTER:"):
                characters.append(stripped.replace("CHARACTER:", "", 1).strip())
            elif stripped.startswith("WORLD:"):
                world.append(stripped.replace("WORLD:", "", 1).strip())
            elif stripped.startswith("CONTINUITY ALERT:"):
                alerts.append(stripped.replace("CONTINUITY ALERT:", "", 1).strip())
        with self._lock:
            self._state.continuity.chapter_summaries.append(summary)
            self._state.continuity.chapter_summaries = self._state.continuity.chapter_summaries[-20:]
            self._state.continuity.characters.extend(item for item in characters if item)
            self._state.continuity.world_details.extend(item for item in world if item)
            self._state.continuity.alerts.extend(item for item in alerts if item)
            self._state.continuity.characters = self._state.continuity.characters[-40:]
            self._state.continuity.world_details = self._state.continuity.world_details[-40:]
            self._state.continuity.alerts = self._state.continuity.alerts[-20:]

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

    def _run_generation(self, prompt_sections: PromptSections, chapter_details: Dict[int, Dict[str, object]], num_chapters: int) -> None:
        try:
            with self._lock:
                self._state.status = "running"
                self._state.phase = "Generating outline"
                self._state.phase_version += 1
                endpoint_url = self._state.endpoint_url
                outline_model = self._state.outline_model
                writer_model = self._state.writer_model
                temperature = self._state.temperature
                token_limit_enabled = self._state.token_limit_enabled
                max_tokens = self._state.max_tokens
                reduce_thinking = self._state.reduce_thinking
                max_iterations = self._state.max_iterations
                chapter_target_word_count = self._state.chapter_target_word_count
                output_folder = self._state.output_folder
            self._initialize_output_log(output_folder)
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
                    f"temperature: {temperature}",
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
                    temperature=temperature,
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
            advice = self._consume_advice()
            if advice:
                prompt = f"{prompt}\n\nHuman guidance to respect going forward:\n{advice}"
            writer_config = get_config(
                local_url=endpoint_url,
                model=writer_model,
                temperature=temperature,
                max_tokens=requested_max_tokens,
                reasoning_effort=reasoning_effort,
                extra_body=extra_body,
            )
            book_agents = BookAgents(writer_config, outline)
            chapter_agents = book_agents.create_agents(prompt, num_chapters)
            book_generator = BookGenerator(
                chapter_agents,
                writer_config,
                outline,
                output_dir=output_folder,
                max_iterations=max_iterations,
                progress_callback=self._progress_callback,
                diagnostic_logger=self._append_output_log,
            )
            for chapter in outline:
                if self._state.stop_requested:
                    self._mark_finished("stopped", f"Stopped before chapter {chapter['chapter_number']}")
                    return
                chapter_number = chapter["chapter_number"]
                review = self._state.chapter_reviews.get(chapter_number, ChapterReviewState())
                advice = self._consume_advice()
                extras = [text for text in [advice] if text]
                chapter_prompt = chapter["prompt"]
                chapter_specific_details = chapter_details.get(chapter_number, {})
                chapter_specific_beats = str(chapter_specific_details.get("beats", "")).strip()
                chapter_specific_word_count = int(chapter_specific_details.get("target_word_count", 0) or 0) or chapter_target_word_count
                if chapter_specific_beats or chapter_specific_word_count > 0:
                    detail_lines = []
                    if chapter_specific_word_count > 0:
                        detail_lines.append(f"Target Word Count: {chapter_specific_word_count}")
                    if chapter_specific_beats:
                        detail_lines.append("Beats:")
                        detail_lines.append(chapter_specific_beats)
                    chapter_prompt = (
                        f"{chapter_prompt}\n\nRequired Chapter Details:\n"
                        f"{chr(10).join(detail_lines)}\n\n"
                        f"These chapter details are mandatory for Chapter {chapter_number}. "
                        "Do not skip, defer, or substantially violate them."
                    )
                if extras:
                    chapter_prompt = f"{chapter_prompt}\n\nAdditional guidance for this chapter:\n" + "\n\n".join(extras)
                with self._lock:
                    self._state.current_chapter = chapter_number
                    self._state.status = "running"
                    self._state.phase = f"Generating chapter {chapter_number}"
                    self._state.phase_version += 1
                self._set_chapter_status(chapter_number, "in_progress", chapter["title"])
                result = book_generator.generate_chapter(chapter_number, chapter_prompt) or {}
                chapter_text = self._read_chapter_text(chapter_number)
                self._store_chapter_result(chapter_number, result, chapter_text)
                self._set_chapter_status(chapter_number, "completed", chapter["title"])
                if not self._checkpoint(f"Chapter {chapter_number} complete", chapter_text):
                    self._mark_finished("stopped", f"Stopped after chapter {chapter_number}")
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
                writer_model = self._state.writer_model
                prompt = self._state.prompt
                temperature = self._state.temperature
                token_limit_enabled = self._state.token_limit_enabled
                max_tokens = self._state.max_tokens
                reduce_thinking = self._state.reduce_thinking
                max_iterations = self._state.max_iterations
                chapter_target_word_count = self._state.chapter_target_word_count
                output_folder = self._state.output_folder
                self._state.phase = f"Regenerating chapter {chapter_number}"
                self._state.phase_version += 1
            self._initialize_output_log(output_folder)
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
                    f"writer_model: {writer_model}",
                    f"temperature: {temperature}",
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
            writer_config = get_config(
                local_url=endpoint_url,
                model=writer_model,
                temperature=temperature,
                max_tokens=requested_max_tokens,
                reasoning_effort=reasoning_effort,
                extra_body=extra_body,
            )
            book_agents = BookAgents(writer_config, outline)
            chapter_agents = book_agents.create_agents(prompt, len(outline))
            book_generator = BookGenerator(
                chapter_agents,
                writer_config,
                outline,
                output_dir=output_folder,
                max_iterations=max_iterations,
                progress_callback=self._progress_callback,
                diagnostic_logger=self._append_output_log,
            )
            chapter = next((item for item in outline if item["chapter_number"] == chapter_number), None)
            if not chapter:
                raise ValueError(f"Chapter {chapter_number} not found in outline")
            review = self._state.chapter_reviews.get(chapter_number, ChapterReviewState())
            chapter_prompt = chapter["prompt"]
            chapter_specific_details = self._state.chapter_details.get(chapter_number, {})
            chapter_specific_beats = str(chapter_specific_details.get("beats", "")).strip()
            chapter_specific_word_count = int(chapter_specific_details.get("target_word_count", 0) or 0) or chapter_target_word_count
            if chapter_specific_beats or chapter_specific_word_count > 0:
                detail_lines = []
                if chapter_specific_word_count > 0:
                    detail_lines.append(f"Target Word Count: {chapter_specific_word_count}")
                if chapter_specific_beats:
                    detail_lines.append("Beats:")
                    detail_lines.append(chapter_specific_beats)
                chapter_prompt = (
                    f"{chapter_prompt}\n\nRequired Chapter Details:\n"
                    f"{chr(10).join(detail_lines)}\n\n"
                    f"These chapter details are mandatory for Chapter {chapter_number}. "
                    "Do not skip, defer, or substantially violate them."
                )
            self._set_chapter_status(chapter_number, "in_progress", chapter["title"])
            result = book_generator.generate_chapter(chapter_number, chapter_prompt) or {}
            chapter_text = self._read_chapter_text(chapter_number)
            self._store_chapter_result(chapter_number, result, chapter_text)
            self._set_chapter_status(chapter_number, "completed", chapter["title"])
            self._checkpoint(f"Chapter {chapter_number} regenerated", chapter_text)
            self._mark_finished("completed", f"Chapter {chapter_number} regeneration complete")
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
            self._mark_finished("failed", f"Chapter {chapter_number} regeneration failed", error_message)
            with self._lock:
                self._append_event(f"{_utc_timestamp()} - Chapter regeneration failed: {error_message}")
