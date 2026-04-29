import os
import json
import re
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional

import autogen

from config import (
    MAX_SENTENCE_WORDS,
    MIN_WORD_COUNT_TOLERANCE_WORDS,
    OUTPUT_FOLDER,
    WORD_COUNT_LOWER_TOLERANCE_RATIO,
    WORD_COUNT_UPPER_TOLERANCE_RATIO,
)

META_PROSE_PATTERNS = (
    r"(?im)^\s*the chapter ends here\b.*$",
    r"(?im)^\s*the next chapter should\b.*$",
    r"(?im)^\s*this chapter should\b.*$",
    r"(?im)^\s*the chapter stands at exactly\b.*$",
    r"(?im)^\s*word count\s*:\s*\d+\b.*$",
    r"(?im)^\s*final chapter prose ends properly\b.*$",
    r"(?im)^\s*(?:retry|recovery) context\s*:.*$",
    r"(?im)^\s*to the above,\s*also consider this unresolved prior feedback\s*:.*$",
    r"(?im)^\s*unresolved (?:checklist|loop|feedback) (?:items?|issue)\s*:.*$",
    r"(?im)^\s*final mandatory checklist before (?:writing|revising)\s*:.*$",
    r"(?im)^\s*current-chapter beat anchors\b.*$",
    r"(?im)^\s*this revised draft satisfies\b.*$",
    r"(?im)^\s*this revision successfully satisfies\b.*$",
    r"(?im)^\s*this revised scene adheres closely\b.*$",
    r"(?im)^\s*therefore,\s*this final revision\b.*$",
    r"(?im)^\s*the final mandatory checklist is satisfied\b.*$",
    r"(?im)^\s*all unresolved feedback items have been successfully addressed\b.*$",
    r"(?im)^\s*no numbered checklist items have been merged away\b.*$",
    r"(?im)^\s*the draft maintains consistent character voices\b.*$",
    r"(?im)^\s*transitions are smooth and logical\b.*$",
    r"(?im)^\s*additionally,\s*each beat anchor specified\b.*$",
    r"(?im)^\s*the chapter concludes with\b.*$",
)
MAX_UNBROKEN_TOKEN_CHARS = 180

class GenerationPauseRequested(RuntimeError):
    """Raised when the controller requests a safe pause during chapter generation."""

class BookGenerator:
    def __init__(
        self,
        agents: Dict[str, autogen.ConversableAgent],
        agent_config: Dict,
        outline: List[Dict],
        output_dir: Optional[str] = None,
        max_iterations: int = 5,
        progress_callback: Optional[Callable[[Dict], None]] = None,
        monitor_callback: Optional[Callable[[Dict], None]] = None,
        diagnostic_logger: Optional[Callable[[str], None]] = None,
        chapter_prompt_provider: Optional[Callable[[int, int, str, str], Dict]] = None,
        validation_callback: Optional[Callable[[Dict], None]] = None,
        should_stop_callback: Optional[Callable[[], bool]] = None,
        initial_chapter_memory: Optional[Dict[int, str]] = None,
        runtime_settings_provider: Optional[Callable[[], Dict[str, object]]] = None,
        writer_model_name: str = "",
    ):
        """Initialize with outline to maintain chapter count context"""
        self.agents = agents
        self.agent_config = agent_config
        self.output_dir = output_dir or OUTPUT_FOLDER
        self.chapter_memory: Dict[int, str] = {
            int(chapter_number): str(entry).strip()
            for chapter_number, entry in (initial_chapter_memory or {}).items()
            if str(entry).strip()
        }
        self.max_iterations = max(1, max_iterations)
        self.outline = outline  # Store the outline
        self.progress_callback = progress_callback
        self.monitor_callback = monitor_callback
        self.diagnostic_logger = diagnostic_logger
        self.chapter_prompt_provider = chapter_prompt_provider
        self.validation_callback = validation_callback
        self.should_stop_callback = should_stop_callback
        self.runtime_settings_provider = runtime_settings_provider
        self.writer_model_name = str(writer_model_name or "").strip()
        self.writer_system_message = getattr(self.agents.get("writer"), "system_message", "") or ""
        self._writer_config_signature = self._config_signature(self.agent_config)
        os.makedirs(self.output_dir, exist_ok=True)

    def _log(self, message: str) -> None:
        print(message)
        if self.diagnostic_logger:
            self.diagnostic_logger(message)

    def _log_block(self, header: str, content: str) -> None:
        body = (content or "").rstrip() or "[empty]"
        divider = "=" * 20
        self._log(f"{divider} {header} {divider}\n{body}\n{divider} END {header} {divider}")

    @staticmethod
    def _config_signature(config: Dict) -> str:
        try:
            return json.dumps(config or {}, sort_keys=True, ensure_ascii=False, default=str)
        except TypeError:
            return repr(config or {})

    def _build_writer_agent(self, name: str = "writer") -> autogen.AssistantAgent:
        system_message = self.writer_system_message or getattr(self.agents.get("writer"), "system_message", "") or ""
        return autogen.AssistantAgent(
            name=name,
            system_message=system_message,
            llm_config=self.agent_config,
        )

    def _refresh_writer_runtime(self, chapter_number: int, attempt: int, step_name: str) -> bool:
        if not self.runtime_settings_provider:
            return False
        try:
            payload = self.runtime_settings_provider() or {}
        except Exception as exc:
            self._log(
                "[book_generator] Runtime settings provider failed "
                f"for chapter {chapter_number}, attempt {attempt}, step {step_name}: {exc}"
            )
            return False

        changed_bits: List[str] = []
        next_config = payload.get("writer_config")
        if isinstance(next_config, dict):
            next_signature = self._config_signature(next_config)
            if next_signature != self._writer_config_signature:
                self.agent_config = next_config
                self._writer_config_signature = next_signature
                self.agents["writer"] = self._build_writer_agent("writer")
                changed_bits.append("writer config refreshed")

        next_writer_model = str(payload.get("writer_model") or "").strip()
        if next_writer_model and next_writer_model != self.writer_model_name:
            previous_writer_model = self.writer_model_name or "[unset]"
            self.writer_model_name = next_writer_model
            changed_bits.append(f"writer model {previous_writer_model} -> {next_writer_model}")

        next_max_iterations = payload.get("max_iterations")
        if next_max_iterations is not None:
            normalized_max_iterations = max(1, int(next_max_iterations))
            if normalized_max_iterations != self.max_iterations:
                previous_max_iterations = self.max_iterations
                self.max_iterations = normalized_max_iterations
                changed_bits.append(
                    f"max iterations {previous_max_iterations} -> {normalized_max_iterations}"
                )

        if changed_bits:
            self._log(
                "[book_generator] Applied live runtime update "
                f"for chapter {chapter_number}, attempt {attempt}, step {step_name}: "
                + "; ".join(changed_bits)
            )
            return True
        return False

    def _log_groupchat_setup(self, groupchat: autogen.GroupChat, context_label: str) -> None:
        for agent in groupchat.agents:
            system_message = getattr(agent, "system_message", "") or "[no system message]"
            self._log_block(f"{context_label} | INPUT | SYSTEM | {agent.name}", system_message)

    def _emit_progress(self, chapter_number: int, chapter_title: str, agent: str, step: str, detail: str, output_stage: str = "", iteration: int = 0) -> None:
        if not self.progress_callback:
            return
        self.progress_callback({
            "chapter_number": chapter_number,
            "chapter_title": chapter_title,
            "agent": agent,
            "step": step,
            "detail": detail,
            "output_stage": output_stage,
            "iteration": iteration,
            "max_iterations": self.max_iterations,
        })

    def _emit_monitor(self, kind: str, label: str, text: str) -> None:
        if not self.monitor_callback:
            return
        self.monitor_callback({
            "kind": kind,
            "label": label,
            "text": text,
        })

    def _resolve_chapter_prompt_for_attempt(
        self,
        chapter_number: int,
        attempt: int,
        phase: str,
        fallback_prompt: str,
    ) -> Dict[str, object]:
        if not self.chapter_prompt_provider:
            return {
                "prompt": fallback_prompt,
                "version": 0,
                "details": {},
                "attempt": attempt,
                "phase": phase,
            }
        try:
            payload = self.chapter_prompt_provider(chapter_number, attempt, phase, fallback_prompt) or {}
        except Exception as exc:
            self._log(f"Chapter prompt provider failed for Chapter {chapter_number}, attempt {attempt}: {exc}")
            return {
                "prompt": fallback_prompt,
                "version": 0,
                "details": {},
                "attempt": attempt,
                "phase": phase,
            }
        prompt = str(payload.get("prompt") or fallback_prompt or "").strip()
        try:
            version = int(payload.get("version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        return {
            "prompt": prompt or fallback_prompt,
            "version": version,
            "details": details,
            "attempt": attempt,
            "phase": phase,
        }

    def _summarize_chapter_details(self, details: Dict[str, object]) -> str:
        if not isinstance(details, dict):
            return ""
        beats = str(details.get("beats", "")).strip()
        purpose = str(details.get("purpose", "")).strip()
        tone = str(details.get("tone", "")).strip()
        characters = str(details.get("characters", "")).strip()
        setting = str(details.get("setting", "")).strip()
        must_include = details.get("must_include")
        avoid = details.get("avoid")
        chapter_guidance = details.get("chapter_guidance") if isinstance(details.get("chapter_guidance"), dict) else {}
        try:
            target_word_count = int(details.get("target_word_count", 0) or 0)
        except (TypeError, ValueError):
            target_word_count = 0
        lines: List[str] = []
        if purpose:
            lines.append(f"Purpose: {purpose}")
        if characters:
            lines.append(f"Characters: {characters}")
        if setting:
            lines.append(f"Setting: {setting}")
        if tone:
            lines.append(f"Tone: {tone}")
        if target_word_count > 0:
            lines.append(f"Target Word Count: {target_word_count}")
        if beats:
            lines.append("Beats:")
            lines.append(beats)
        if isinstance(chapter_guidance, dict) and chapter_guidance:
            lines.append("Chapter Guidance:")
            distribution = chapter_guidance.get("word_count_distribution", {})
            if isinstance(distribution, dict):
                for label in ("opening", "middle", "ending"):
                    value = str(distribution.get(label, "")).strip()
                    if value:
                        lines.append(f"- {label.title()}: {value}")
            emphasis = str(chapter_guidance.get("emphasis", "")).strip()
            compression = str(chapter_guidance.get("compression", "")).strip()
            if emphasis:
                lines.append(f"Emphasis: {emphasis}")
            if compression:
                lines.append(f"Compression: {compression}")
        if isinstance(must_include, list) and must_include:
            lines.append("Must Include:")
            lines.extend(f"- {str(item).strip()}" for item in must_include if str(item).strip())
        if isinstance(avoid, list) and avoid:
            lines.append("Avoid:")
            lines.extend(f"- {str(item).strip()}" for item in avoid if str(item).strip())
        return "\n".join(lines).strip()

    def _raise_if_pause_requested(
        self,
        chapter_number: int,
        chapter_title: str,
        attempt: int,
        detail: str,
        output_stage: str = "",
    ) -> None:
        if not self.should_stop_callback or not self.should_stop_callback():
            return
        self._emit_progress(
            chapter_number,
            chapter_title,
            "system",
            "pause_requested",
            detail,
            output_stage,
            attempt,
        )
        raise GenerationPauseRequested(f"Pause requested during Chapter {chapter_number}")

    def _log_console_message(self, chapter_number: int, sender: str, content: str) -> None:
        self._log_block(f"CHAPTER {chapter_number} | OUTPUT | {sender}", content)

    def _infer_step_from_message(self, sender: str, content: str) -> Dict[str, str]:
        sender = sender or "system"
        lowered = content.lower()
        if sender == "memory_keeper":
            return {"step": "memory_update", "detail": "Memory keeper updated continuity", "output_stage": "planning"}
        if sender == "writer_final":
            return {"step": "revision", "detail": "Writer final is revising the chapter", "output_stage": "revision"}
        if sender == "writer":
            if "scene final:" in lowered:
                return {"step": "finalizing", "detail": "Writer produced final scene text", "output_stage": "final"}
            return {"step": "drafting", "detail": "Writer is drafting the chapter", "output_stage": "draft"}
        if sender == "editor":
            return {"step": "editor_review", "detail": "Editor is reviewing the draft", "output_stage": "feedback"}
        if sender == "story_planner":
            return {"step": "retry_outline", "detail": "Story planner is outlining a retry", "output_stage": "planning"}
        return {"step": "working", "detail": f"{sender} is working", "output_stage": ""}

    def _monitor_groupchat(self, groupchat: autogen.GroupChat, chapter_number: int, chapter_title: str, stop_event: threading.Event) -> None:
        seen = 0
        while not stop_event.is_set():
            messages = list(groupchat.messages)
            while seen < len(messages):
                msg = messages[seen]
                sender = self._get_sender(msg)
                content = msg.get("content", "")
                if sender and sender != "user_proxy":
                    step = self._infer_step_from_message(sender, content)
                    self._emit_progress(
                        chapter_number,
                        chapter_title,
                        sender,
                        step["step"],
                        step["detail"],
                        step["output_stage"],
                        1,
                    )
                    self._log_console_message(chapter_number, sender, content)
                seen += 1
            time.sleep(0.4)

    def _clean_chapter_content(self, content: str) -> str:
        """Clean up chapter content by removing artifacts and chapter numbers"""
        content = re.sub(
            r"(?im)^\s*(?:SCENE FINAL|SCENE|CHAPTER FINAL|CHAPTER|EDITED_SCENE(?: FINAL)?)\s*:\s*\n?",
            "",
            content,
        )
        content = re.sub(
            r"(?is)\n\s*(?:"
            r"FEEDBACK|WORD COUNT ADVICE|SUGGEST|BEAT CHECK|BEAT CHECK RESULT|LOOP CHECK RESULT|"
            r"SENTENCE LENGTH CHECK|SENTENCE LENGTH CHECK RESULT|"
            r"MEMORY UPDATE|PLAN|SETTING|SCENE FINAL PROSE ONLY OUTPUT ACCOMPLISHED|"
            r"RETRY CONTEXT|RECOVERY CONTEXT|UNRESOLVED CHECKLIST ITEMS|UNRESOLVED LOOP ISSUE|"
            r"UNRESOLVED FEEDBACK ITEMS|SYSTEM WORD COUNT RECOVERY ADVICE|SYSTEM LOOP RECOVERY ADVICE|"
            r"SYSTEM OUTPUT INTEGRITY ADVICE|FINAL MANDATORY CHECKLIST BEFORE WRITING|"
            r"FINAL MANDATORY CHECKLIST BEFORE REVISING|CURRENT-CHAPTER BEAT ANCHORS"
            r")\s*:?.*\Z",
            "",
            content,
        )
        content = re.sub(
            r"(?is)(?:^|\n)\s*(?:"
            r"to the above,\s*also consider this unresolved prior feedback|"
            r"this revised draft satisfies|"
            r"this revision successfully satisfies|"
            r"this revised scene adheres closely|"
            r"therefore,\s*this final revision|"
            r"the final mandatory checklist is satisfied|"
            r"all unresolved feedback items have been successfully addressed|"
            r"no numbered checklist items have been merged away|"
            r"the draft maintains consistent character voices|"
            r"transitions are smooth and logical|"
            r"additionally,\s*each beat anchor specified|"
            r"the chapter concludes with"
            r")\b.*\Z",
            "",
            content,
        )
        content = re.sub(
            r"(?im)^\s*SCENE FINAL PROSE ONLY OUTPUT ACCOMPLISHED\s*:?\s*$",
            "",
            content,
        )
        # Remove standalone chapter heading references without clobbering normal prose
        content = re.sub(r"(?im)^\s*\*?\s*\(Chapter \d+[^\n)]*\)\s*$", "", content)
        content = re.sub(r"(?im)^\s*\*?\s*Chapter \d+\b[^\n]*\n?", "", content, count=1)
        content = re.sub(r"(?im)^\s*(beat check result|loop check result|sentence length check result|beat check summary|word count)\s*:.*$", "", content)
        content = re.sub(r"(?im)^\s*(?:fragment\s+)?word count(?: approx\.)?\s*:.*$", "", content)
        content = re.sub(r"(?im)^\s*sentence length check\s*:.*$", "", content)
        
        # Clean up any remaining markdown artifacts
        content = content.replace('*', '')
        content = content.strip()
        
        return content

    def _find_meta_prose_artifacts(self, content: str) -> List[str]:
        cleaned = self._clean_chapter_content(content or "")
        findings: List[str] = []
        for pattern in META_PROSE_PATTERNS:
            match = re.search(pattern, cleaned)
            if match:
                findings.append(match.group(0).strip())
        return findings

    def _find_overlong_unbroken_tokens(self, content: str, limit: int = MAX_UNBROKEN_TOKEN_CHARS) -> List[str]:
        if not content or limit <= 0:
            return []
        return re.findall(rf"\S{{{limit + 1},}}", content)

    def _validate_prose_integrity(self, content: str) -> tuple[bool, str]:
        cleaned = self._clean_chapter_content(content or "")
        overlong_tokens = self._find_overlong_unbroken_tokens(cleaned)
        if overlong_tokens:
            example = overlong_tokens[0]
            excerpt = example[:80] + ("..." if len(example) > 80 else "")
            return (
                False,
                f"Detected degenerate unbroken token ({len(example)} characters) in chapter content. Example: {excerpt}",
            )
        findings = self._find_meta_prose_artifacts(content)
        if not findings:
            return True, "No prohibited meta prose detected."
        example = findings[0]
        return False, f"Detected prohibited meta prose in chapter content. Example: {example}"

    def _build_basic_chapter_summary(self, chapter_number: int, chapter_content: str) -> str:
        cleaned = self._clean_chapter_content(chapter_content or "")
        excerpt = self._slice_words(cleaned, 0, 50) or cleaned[:250].strip()
        excerpt = re.sub(r"\s+", " ", excerpt).strip()
        if not excerpt:
            excerpt = "[no usable summary]"
        return f"Chapter {chapter_number} Summary: {excerpt}..."

    def _store_chapter_memory(self, chapter_number: int, chapter_content: str, memory_update: str) -> None:
        if memory_update:
            self.chapter_memory[chapter_number] = f"Chapter {chapter_number} Memory:\n{memory_update.strip()}"
            return
        self.chapter_memory[chapter_number] = self._build_basic_chapter_summary(chapter_number, chapter_content)

    def _contains_any_tag_marker(self, content: str, tags: List[str]) -> bool:
        upper_content = (content or "").upper()
        return any(f"{tag.upper()}:" in upper_content for tag in tags)

    def _looks_like_meta_response(self, content: str) -> bool:
        cleaned = " ".join((content or "").strip().split())
        if not cleaned:
            return False

        lowered = cleaned.lower()
        meta_starts = (
            "understood",
            "thank you for the feedback",
            "thanks for the feedback",
            "i understand",
            "i will revise",
            "i'll revise",
            "here is the revised",
            "here's the revised",
            "revision:",
            "acknowledged",
            "noted",
            "this revised draft satisfies",
            "this revision successfully satisfies",
            "this revised scene adheres closely",
            "therefore, this final revision",
            "the final mandatory checklist is satisfied",
            "recovery context:",
            "retry context:",
        )
        if lowered.startswith(meta_starts):
            return True
        if "feedback" in lowered and "draft" in lowered and self._count_words(cleaned) < 40:
            return True
        return False

    def _extract_story_candidate(self, content: str, tags: List[str], allow_raw_fallback: bool = True) -> str:
        raw_content = (content or "").strip()
        if not raw_content:
            return ""

        tagged = self._extract_tagged_content(raw_content, tags)
        if tagged:
            candidate = self._clean_chapter_content(tagged)
            if candidate and not self._looks_like_plan_output(candidate) and not self._looks_like_meta_response(candidate):
                return candidate
            return ""

        if self._contains_any_tag_marker(raw_content, tags):
            return ""

        if not allow_raw_fallback:
            return ""

        candidate = self._clean_chapter_content(raw_content)
        if self._looks_like_meta_response(candidate):
            return ""
        if candidate and not self._looks_like_plan_output(candidate):
            return candidate
        return ""

    def _word_spans(self, content: str) -> List[re.Match[str]]:
        return list(re.finditer(r"\b\w+(?:['’-]\w+)*\b", content, re.UNICODE))

    def _slice_words(self, content: str, start_word: int, end_word: int) -> str:
        spans = self._word_spans(content)
        if not spans:
            return ""
        start_word = max(0, start_word)
        end_word = min(len(spans), end_word)
        if start_word >= end_word:
            return ""
        start_index = spans[start_word].start()
        end_index = spans[end_word - 1].end()
        return content[start_index:end_index].strip()

    def _trim_to_sentence_boundary(self, content: str) -> str:
        trimmed = (content or "").rstrip()
        match = list(re.finditer(r"[.!?][\"')\]]?\s", trimmed))
        if match:
            cutoff = match[-1].end()
            return trimmed[:cutoff].rstrip()
        return trimmed

    def _find_repetition_cutoff(self, content: str) -> int:
        if not content:
            return 0

        paragraph_matches = list(re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", content))
        seen_paragraphs: Dict[str, int] = {}
        for match in paragraph_matches:
            paragraph = match.group(0).strip()
            normalized = " ".join(paragraph.lower().split())
            if len(normalized) < 80:
                continue
            if normalized in seen_paragraphs:
                return match.start()
            seen_paragraphs[normalized] = match.start()

        sentence_matches = list(re.finditer(r"[\s\S]*?(?:[.!?](?:\s+|$)|\Z)", content))
        sentences = []
        for match in sentence_matches:
            sentence = match.group(0).strip()
            if sentence:
                sentences.append((match.start(), match.end(), " ".join(sentence.lower().split())))

        window_size = 3
        seen_windows: Dict[str, int] = {}
        for index in range(max(0, len(sentences) - window_size + 1)):
            window_sentences = [item[2] for item in sentences[index:index + window_size]]
            window_text = " ".join(window_sentences)
            if len(window_text) < 120:
                continue
            if window_text in seen_windows:
                return sentences[index][0]
            seen_windows[window_text] = index

        return 0

    def _apply_loop_guard(self, content: str, target_word_count: int = 0) -> tuple[str, str]:
        cleaned = self._clean_chapter_content(content or "")
        if not cleaned:
            return "", ""

        repetition_cutoff = self._find_repetition_cutoff(cleaned)
        if repetition_cutoff > 0:
            truncated = self._trim_to_sentence_boundary(cleaned[:repetition_cutoff])
            if self._looks_like_story_text(truncated):
                return truncated, "Removed repetitive tail from model output before downstream review."

        actual_word_count = self._count_words(cleaned)
        minimum_word_count, maximum_word_count = self._word_count_bounds(target_word_count)
        if target_word_count > 0 and actual_word_count > max(maximum_word_count * 2, target_word_count + 600):
            safe_limit = maximum_word_count + max(120, target_word_count // 5)
            truncated = self._trim_to_sentence_boundary(self._slice_words(cleaned, 0, safe_limit))
            if self._looks_like_story_text(truncated):
                return truncated, (
                    f"Truncated oversized model output from {actual_word_count} words to protect prompt budget."
                )

        return cleaned, ""

    def _compact_text_for_prompt(self, text: str, max_words: int, label: str) -> str:
        cleaned = self._clean_chapter_content(text or "")
        if cleaned:
            def replace_overlong_token(match: re.Match[str]) -> str:
                token = match.group(0)
                head = token[:60]
                tail = token[-40:] if len(token) > 100 else ""
                marker = f"[long unbroken token truncated from {len(token)} chars in {label}]"
                return " ".join(part for part in (head, marker, tail) if part)

            cleaned = re.sub(
                rf"\S{{{MAX_UNBROKEN_TOKEN_CHARS + 1},}}",
                replace_overlong_token,
                cleaned,
            )
        actual_word_count = self._count_words(cleaned)
        if actual_word_count <= max_words:
            return cleaned

        head_words = max(1, int(max_words * 0.75))
        tail_words = max(1, max_words - head_words)
        head = self._slice_words(cleaned, 0, head_words)
        tail = self._slice_words(cleaned, max(0, actual_word_count - tail_words), actual_word_count)
        omitted_words = max(0, actual_word_count - self._count_words(head) - self._count_words(tail))
        parts = [head]
        if tail:
            parts.append(f"[... {omitted_words} words omitted from {label} to stay within prompt budget ...]")
            parts.append(tail)
        return "\n\n".join(part for part in parts if part).strip()

    def _normalize_editor_status(self, value: object) -> str:
        normalized = str(value or "").strip().upper()
        return normalized if normalized in {"PASS", "FAIL"} else ""

    def _structured_value_to_lines(self, value: object, prefix: str = "") -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            lines = [line.strip() for line in value.splitlines() if line.strip()]
            if not lines and value.strip():
                lines = [value.strip()]
            if prefix:
                return [line if line.startswith(prefix) else f"{prefix}{line}" for line in lines]
            return lines
        if isinstance(value, list):
            lines: List[str] = []
            for item in value:
                if isinstance(item, dict):
                    parts = [
                        f"{key}: {str(val).strip()}"
                        for key, val in item.items()
                        if str(val).strip()
                    ]
                    if parts:
                        lines.append((prefix if prefix else "- ") + "; ".join(parts))
                else:
                    lines.extend(self._structured_value_to_lines(item, prefix or "- "))
            return lines
        if isinstance(value, dict):
            lines = []
            for key, val in value.items():
                text = str(val).strip()
                if text:
                    lines.append((prefix if prefix else "- ") + f"{key}: {text}")
            return lines
        text = str(value).strip()
        if not text:
            return []
        return [text if not prefix else f"{prefix}{text}"]

    def _build_editor_feedback_from_payload(self, payload: Dict) -> str:
        feedback = payload.get("feedback")
        if isinstance(feedback, dict):
            payload = feedback

        beat_items_value = payload.get("beat_check", payload.get("beat_checks", []))
        if isinstance(beat_items_value, dict):
            beat_items_value = beat_items_value.get("items", [])
        beat_lines: List[str] = []
        if isinstance(beat_items_value, list):
            for index, item in enumerate(beat_items_value, start=1):
                if isinstance(item, dict):
                    item_index = item.get("index", index)
                    beat = str(
                        item.get("beat")
                        or item.get("item")
                        or item.get("requirement")
                        or f"Checklist item {item_index}"
                    ).strip()
                    status = self._normalize_editor_status(item.get("status") or item.get("result"))
                    evidence = str(
                        item.get("evidence")
                        or item.get("note")
                        or item.get("notes")
                        or item.get("details")
                        or item.get("reason")
                        or ""
                    ).strip()
                    line = f"{item_index}. {beat}"
                    if status:
                        line += f" - {status}"
                    if evidence:
                        line += f" ({evidence})"
                    beat_lines.append(line)
                else:
                    text = str(item).strip()
                    if text:
                        beat_lines.append(text)
        elif isinstance(beat_items_value, str):
            beat_lines = [line.strip() for line in beat_items_value.splitlines() if line.strip()]

        beat_result = self._normalize_editor_status(
            payload.get("beat_check_result") or payload.get("beat_result")
        )
        if not beat_result and beat_lines:
            upper_lines = [line.upper() for line in beat_lines]
            if any("FAIL" in line for line in upper_lines):
                beat_result = "FAIL"
            elif all("PASS" in line for line in upper_lines):
                beat_result = "PASS"

        loop_block = payload.get("loop_check")
        loop_result = self._normalize_editor_status(payload.get("loop_check_result") or payload.get("loop_result"))
        loop_lines = self._structured_value_to_lines(payload.get("loop_check_notes"))
        if isinstance(loop_block, dict):
            loop_result = loop_result or self._normalize_editor_status(loop_block.get("status"))
            loop_lines = loop_lines or self._structured_value_to_lines(
                loop_block.get("notes") or loop_block.get("evidence") or loop_block.get("details")
            )
        elif loop_block and not loop_lines:
            loop_lines = self._structured_value_to_lines(loop_block)

        sentence_block = payload.get("sentence_length_check", payload.get("sentence_check"))
        sentence_result = self._normalize_editor_status(
            payload.get("sentence_length_check_result")
            or payload.get("sentence_check_result")
        )
        sentence_lines = self._structured_value_to_lines(sentence_block)
        if isinstance(sentence_block, dict):
            sentence_result = sentence_result or self._normalize_editor_status(sentence_block.get("status"))
            sentence_lines = sentence_lines or self._structured_value_to_lines(
                sentence_block.get("notes") or sentence_block.get("findings") or sentence_block.get("details")
            )

        word_count_advice = payload.get("word_count_advice", payload.get("word_count"))
        if isinstance(word_count_advice, dict):
            word_count_lines = self._structured_value_to_lines(word_count_advice)
        else:
            word_count_lines = self._structured_value_to_lines(word_count_advice)

        suggest_lines = self._structured_value_to_lines(payload.get("suggest") or payload.get("suggestions"))

        lines: List[str] = ["FEEDBACK:"]
        if beat_lines or beat_result:
            lines.append("BEAT CHECK:")
            lines.extend(beat_lines or ["- No beat-check items returned."])
            if beat_result:
                lines.append(f"BEAT CHECK RESULT: {beat_result}")
        if loop_result:
            lines.append(f"LOOP CHECK RESULT: {loop_result}")
        if loop_lines:
            lines.extend(loop_lines)
        if sentence_lines or sentence_result:
            lines.append("SENTENCE LENGTH CHECK:")
            lines.extend(sentence_lines or ["- No sentence-length findings returned."])
            if sentence_result:
                lines.append(f"SENTENCE LENGTH CHECK RESULT: {sentence_result}")
        if word_count_lines:
            lines.append("WORD COUNT ADVICE:")
            lines.extend(word_count_lines)
        if suggest_lines:
            lines.append("SUGGEST:")
            lines.extend(suggest_lines)
        return "\n".join(line for line in lines if line.strip()).strip()

    def _extract_editor_json_payload(self, editor_output: str) -> Optional[Dict]:
        text = (editor_output or "").strip()
        if not text:
            return None

        candidates: List[str] = [text]
        fence_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        candidates.extend(match.strip() for match in fence_matches if match.strip())
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            candidates.append(text[first_brace:last_brace + 1].strip())

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _normalize_editor_markdown_labels(self, editor_output: str) -> str:
        text = (editor_output or "").strip()
        if not text:
            return ""

        labels = [
            "FEEDBACK",
            "BEAT CHECK",
            "BEAT CHECK RESULT",
            "LOOP CHECK RESULT",
            "SENTENCE LENGTH CHECK",
            "SENTENCE LENGTH CHECK RESULT",
            "WORD COUNT ADVICE",
            "SUGGEST",
        ]
        normalized = text
        for label in labels:
            normalized = re.sub(
                rf"(?im)^\s*[-*]?\s*\*+\s*{re.escape(label)}\s*:\s*\*+\s*(PASS|FAIL)?\s*$",
                lambda match: f"{label}: {match.group(1).upper()}" if match.group(1) else f"{label}:",
                normalized,
            )
        return normalized

    def _normalize_editor_output(self, editor_output: str) -> str:
        payload = self._extract_editor_json_payload(editor_output)
        if payload is not None:
            normalized = self._build_editor_feedback_from_payload(payload)
            if normalized:
                return normalized
        return self._normalize_editor_markdown_labels(editor_output)

    def _extract_labeled_block(self, text: str, label: str, stop_labels: List[str]) -> str:
        if not text:
            return ""
        stop_pattern = "|".join(re.escape(stop_label) for stop_label in stop_labels)
        pattern = rf"({re.escape(label)}\s*:.*?)(?=\n\s*(?:{stop_pattern})\s*:|\Z)"
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else ""

    def _extract_sentences(self, content: str) -> List[str]:
        cleaned = self._clean_chapter_content(content or "")
        sentence_matches = list(re.finditer(r"[\s\S]*?(?:[.!?](?:[\"')\]]*)?(?=\s|$)|\Z)", cleaned))
        sentences: List[str] = []
        for match in sentence_matches:
            sentence = match.group(0).strip()
            if sentence and re.search(r"\w", sentence):
                sentences.append(sentence)
        return sentences

    def _find_overlong_sentences(self, content: str) -> List[tuple[str, int]]:
        overlong: List[tuple[str, int]] = []
        for sentence in self._extract_sentences(content):
            word_count = self._count_words(sentence)
            if word_count > MAX_SENTENCE_WORDS:
                overlong.append((sentence, word_count))
        return overlong

    def _validate_sentence_length(self, content: str) -> tuple[bool, str]:
        overlong_sentences = self._find_overlong_sentences(content)
        if not overlong_sentences:
            return True, f"No sentence exceeds {MAX_SENTENCE_WORDS} words."

        longest_sentence, longest_word_count = max(overlong_sentences, key=lambda item: item[1])
        excerpt = self._slice_words(longest_sentence, 0, min(18, longest_word_count))
        if longest_word_count > 18:
            excerpt = f"{excerpt}..."
        return (
            False,
            f"Detected {len(overlong_sentences)} sentence(s) over {MAX_SENTENCE_WORDS} words; "
            f"longest is {longest_word_count} words. Example: {excerpt}",
        )

    def _compact_editor_feedback_for_retry(self, editor_output: str) -> str:
        editor_output = self._normalize_editor_output(editor_output)
        if not editor_output:
            return ""

        stripped_editor_output = re.sub(
            r"(WORD COUNT ADVICE:.*?)(?=\n[A-Z][A-Z\s]+:|\Z)",
            "",
            editor_output,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        stripped_editor_output = re.sub(
            r"(?im)^\s*(?:fragment\s+)?word count(?: approx\.)?\s*:.*$",
            "",
            stripped_editor_output,
        ).strip()

        sections: List[str] = []
        beat_check_block = self._extract_labeled_block(
            stripped_editor_output,
            "BEAT CHECK",
            ["BEAT CHECK RESULT", "LOOP CHECK RESULT", "SENTENCE LENGTH CHECK", "SENTENCE LENGTH CHECK RESULT", "WORD COUNT ADVICE", "SUGGEST", "FEEDBACK"],
        )
        if beat_check_block:
            sections.append(beat_check_block)
        beat_status = self._extract_pass_fail_status(stripped_editor_output, "BEAT CHECK RESULT")
        if beat_status:
            sections.append(f"BEAT CHECK RESULT: {beat_status}")
        loop_status = self._extract_pass_fail_status(stripped_editor_output, "LOOP CHECK RESULT")
        if loop_status:
            sections.append(f"LOOP CHECK RESULT: {loop_status}")
        sentence_length_block = self._extract_labeled_block(
            stripped_editor_output,
            "SENTENCE LENGTH CHECK",
            ["SENTENCE LENGTH CHECK RESULT", "WORD COUNT ADVICE", "SUGGEST", "FEEDBACK"],
        )
        if sentence_length_block:
            sections.append(sentence_length_block)
        sentence_status = self._extract_pass_fail_status(stripped_editor_output, "SENTENCE LENGTH CHECK RESULT")
        if sentence_status:
            sections.append(f"SENTENCE LENGTH CHECK RESULT: {sentence_status}")
        suggest_block = self._extract_labeled_block(
            stripped_editor_output,
            "SUGGEST",
            ["BEAT CHECK", "BEAT CHECK RESULT", "LOOP CHECK RESULT", "SENTENCE LENGTH CHECK", "SENTENCE LENGTH CHECK RESULT", "WORD COUNT ADVICE", "FEEDBACK"],
        )
        if suggest_block:
            sections.append(suggest_block)
        compact = "\n\n".join(section for section in sections if section).strip()
        if compact:
            return self._compact_text_for_prompt(compact, 450, "editor feedback")
        if stripped_editor_output:
            return self._compact_text_for_prompt(stripped_editor_output, 450, "editor feedback")
        return ""

    def _extract_required_beat_items(self, beats_block: str) -> List[str]:
        cleaned_block = re.sub(
            r"\n?\s*These chapter details are mandatory.*\Z",
            "",
            beats_block or "",
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        if not cleaned_block:
            return []

        line_items = [
            re.sub(r"^[-*\d\.\)\s]+", "", line.strip()).strip()
            for line in cleaned_block.splitlines()
            if line.strip()
        ]
        line_items = [
            item for item in line_items
            if item and not item.lower().startswith((
                "target word count:",
                "these chapter details are mandatory",
                "purpose:",
                "tone:",
                "characters:",
                "setting:",
                "chapter guidance:",
                "word count distribution:",
                "emphasis:",
                "compression:",
                "must include:",
                "avoid:",
            ))
        ]
        if len(line_items) > 1:
            items = line_items
        else:
            single_block = line_items[0] if line_items else re.sub(r"\s+", " ", cleaned_block).strip()
            sentence_parts = re.split(r"(?<=[.!?])\s+(?=(?:['\"“”A-Z]))", single_block)
            items = []
            for part in sentence_parts:
                normalized = part.strip(" -")
                if not normalized:
                    continue
                word_total = len(re.findall(r"\b\w+\b", normalized))
                if word_total < 4 and items:
                    items[-1] = f"{items[-1]} {normalized}".strip()
                else:
                    items.append(normalized)

        deduped_items: List[str] = []
        seen: set[str] = set()
        for item in items:
            normalized_key = " ".join(item.lower().split())
            if not normalized_key or normalized_key in seen:
                continue
            seen.add(normalized_key)
            deduped_items.append(item)
        return deduped_items

    def _format_required_beat_checklist(self, required_beats: List[str]) -> str:
        if not required_beats:
            return ""
        checklist = "\n".join(f"{index}. {beat}" for index, beat in enumerate(required_beats, start=1))
        return "\n".join([
            "Current-Chapter Beat Anchors (faithful paraphrase allowed):",
            checklist,
            "",
            "Follow these beat anchors for the current chapter only.",
            "Preserve the narrative intent, order, and specificity of these beats, but natural rewording in prose is acceptable.",
            "If these beat anchors conflict with broader outline summary bullets, follow these beat anchors.",
            "Do not import explicit beats from other chapters.",
            "If the chapter needs more length, deepen these beats before inventing any new ending beat or postscript scene.",
            "Prefer on-page expansion through sensory detail, body language, interiority, dialogue subtext, and immediate consequences.",
        ]).strip()

    def _extract_beat_check_items(self, editor_output: str) -> List[str]:
        editor_output = self._normalize_editor_output(editor_output)
        beat_check_block = self._extract_labeled_block(
            editor_output,
            "BEAT CHECK",
            ["BEAT CHECK RESULT", "LOOP CHECK RESULT", "SENTENCE LENGTH CHECK", "SENTENCE LENGTH CHECK RESULT", "WORD COUNT ADVICE", "SUGGEST", "FEEDBACK"],
        )
        if not beat_check_block:
            return []
        items: List[str] = []
        for line in beat_check_block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.upper().startswith("BEAT CHECK:"):
                continue
            if "PASS" in stripped.upper() or "FAIL" in stripped.upper():
                items.append(stripped)
        return items

    def _extract_failed_beat_check_items(self, editor_output: str) -> List[str]:
        return [
            item
            for item in self._extract_beat_check_items(editor_output)
            if "FAIL" in item.upper()
        ]

    def _extract_editor_guidance_block(self, editor_output: str, label: str) -> str:
        editor_output = self._normalize_editor_output(editor_output)
        return self._extract_labeled_block(
            editor_output,
            label,
            [
                "BEAT CHECK",
                "BEAT CHECK RESULT",
                "LOOP CHECK RESULT",
                "SENTENCE LENGTH CHECK",
                "SENTENCE LENGTH CHECK RESULT",
                "WORD COUNT ADVICE",
                "SUGGEST",
                "FEEDBACK",
            ],
        )

    def _build_retry_feedback_focus(self, editor_output: str, chapter_content: str = "", target_word_count: int = 0) -> str:
        if not editor_output:
            return ""

        sections: List[str] = []
        failed_beats = self._extract_failed_beat_check_items(editor_output)
        if failed_beats:
            normalized_failed_beats = [
                re.sub(r"^[-*\s]+", "", item).strip()
                for item in failed_beats
            ]
            sections.append(
                "\n".join([
                    "Unresolved Checklist Items:",
                    *[f"- {item}" for item in normalized_failed_beats],
                ])
            )

        if self._extract_pass_fail_status(editor_output, "LOOP CHECK RESULT") == "FAIL":
            sections.append("Unresolved Loop Issue:\n- The prior draft repeated material and must advance instead of restating the same beat.")

        sentence_length_passed = True
        if chapter_content:
            sentence_length_passed, _ = self._validate_sentence_length(chapter_content)
        if self._extract_pass_fail_status(editor_output, "SENTENCE LENGTH CHECK RESULT") == "FAIL" and not sentence_length_passed:
            sentence_block = self._extract_labeled_block(
                editor_output,
                "SENTENCE LENGTH CHECK",
                ["SENTENCE LENGTH CHECK RESULT", "WORD COUNT ADVICE", "SUGGEST", "FEEDBACK"],
            )
            if sentence_block:
                sections.append(sentence_block)
            sections.append("SENTENCE LENGTH CHECK RESULT: FAIL")

        if target_word_count > 0:
            _, word_count_passed, _ = self._validate_word_count(chapter_content, target_word_count)
            if not word_count_passed:
                word_count_block = self._extract_editor_guidance_block(editor_output, "WORD COUNT ADVICE")
                if word_count_block:
                    sections.append(word_count_block)

        suggest_block = self._extract_editor_guidance_block(editor_output, "SUGGEST")
        if suggest_block:
            sections.append(suggest_block)

        if sections:
            return self._compact_text_for_prompt("\n\n".join(sections), 300, "unresolved prior feedback")

        return ""

    def _extract_pass_fail_status(self, content: str, label: str) -> str:
        content = self._normalize_editor_output(content)
        matches = re.findall(
            rf"(?im)^\s*[-*]?\s*{re.escape(label)}\s*:\s*(PASS|FAIL)\s*$",
            content or "",
        )
        return matches[-1].upper() if matches else ""

    def _summarize_chapter_requirements(self, prompt: str, include_additional_guidance: bool = True) -> str:
        summary = re.sub(
            r"\n\s*Required Chapter Details:\s*.*\Z",
            "",
            prompt or "",
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        if not include_additional_guidance:
            summary = re.sub(
                r"\n\s*Additional Chapter Guidance:\s*.*\Z",
                "",
                summary,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
        if not summary:
            summary = (prompt or "").strip()
        return re.sub(r"\n{3,}", "\n\n", summary).strip()

    def _build_actionable_revision_feedback(
        self,
        editor_output: str,
        chapter_content: str,
        target_word_count: int,
    ) -> str:
        if not editor_output:
            return ""

        sections: List[str] = []
        failed_beats = self._extract_failed_beat_check_items(editor_output)
        if failed_beats:
            normalized_failed_beats = [
                re.sub(r"^[-*\s]+", "", item).strip()
                for item in failed_beats
            ]
            sections.append(
                "\n".join([
                    "Specific Issues To Repair:",
                    *[f"- {item}" for item in normalized_failed_beats],
                ])
            )

        if self._extract_pass_fail_status(editor_output, "LOOP CHECK RESULT") == "FAIL" or self._is_repetitive_output(chapter_content):
            sections.append("Loop Issue:\n- Remove repeated phrasing or recycled beat coverage and make each paragraph add new story movement.")

        sentence_length_passed, sentence_length_message = self._validate_sentence_length(chapter_content)
        if not sentence_length_passed:
            sections.append("Sentence Length Issue:\n- " + sentence_length_message)

        prose_integrity_passed, prose_integrity_message = self._validate_prose_integrity(chapter_content)
        if not prose_integrity_passed:
            sections.append("Output Integrity Issue:\n- " + prose_integrity_message)

        if target_word_count > 0:
            _, word_count_passed, _ = self._validate_word_count(chapter_content, target_word_count)
            if not word_count_passed:
                word_count_block = self._extract_editor_guidance_block(editor_output, "WORD COUNT ADVICE")
                if word_count_block:
                    sections.append(word_count_block)
            word_count_guidance = self._build_word_count_retry_guidance(chapter_content, target_word_count)
            if word_count_guidance:
                sections.append(word_count_guidance)

        suggest_block = self._extract_editor_guidance_block(editor_output, "SUGGEST")
        if suggest_block:
            sections.append(suggest_block)

        if not sections:
            return ""

        return self._compact_text_for_prompt(
            "\n\n".join(sections),
            360,
            "actionable revision feedback",
        )

    def _count_actionable_revision_issues(
        self,
        editor_output: str,
        chapter_content: str,
        target_word_count: int,
    ) -> int:
        issue_count = len(self._extract_failed_beat_check_items(editor_output))
        if self._extract_pass_fail_status(editor_output, "LOOP CHECK RESULT") == "FAIL" or self._is_repetitive_output(chapter_content):
            issue_count += 1
        sentence_length_passed, _ = self._validate_sentence_length(chapter_content)
        if not sentence_length_passed:
            issue_count += 1
        prose_integrity_passed, _ = self._validate_prose_integrity(chapter_content)
        if not prose_integrity_passed:
            issue_count += 1
        if target_word_count > 0:
            _, word_count_passed, _ = self._validate_word_count(chapter_content, target_word_count)
            if not word_count_passed:
                issue_count += 1
        return issue_count

    def _count_words(self, content: str) -> int:
        return len(re.findall(r"\b\w+(?:['’-]\w+)*\b", content, re.UNICODE))

    def _word_count_bounds(self, target_word_count: int) -> tuple[int, int]:
        if target_word_count <= 0:
            return 0, 0
        lower_tolerance = max(
            MIN_WORD_COUNT_TOLERANCE_WORDS,
            int(round(target_word_count * WORD_COUNT_LOWER_TOLERANCE_RATIO)),
        )
        upper_tolerance = max(
            MIN_WORD_COUNT_TOLERANCE_WORDS,
            int(round(target_word_count * WORD_COUNT_UPPER_TOLERANCE_RATIO)),
        )
        return max(1, target_word_count - lower_tolerance), target_word_count + upper_tolerance

    def _validate_word_count(self, chapter_content: str, target_word_count: int) -> tuple[int, bool, str]:
        actual_word_count = self._count_words(chapter_content)
        if target_word_count <= 0:
            return actual_word_count, True, f"Programmatic word count: {actual_word_count}"
        minimum_word_count, maximum_word_count = self._word_count_bounds(target_word_count)
        within_range = minimum_word_count <= actual_word_count <= maximum_word_count
        if within_range:
            message = (
                f"Programmatic word count: {actual_word_count} "
                f"(target {target_word_count}, allowed range {minimum_word_count}-{maximum_word_count})"
            )
        else:
            direction = "below" if actual_word_count < minimum_word_count else "above"
            message = (
                f"Programmatic word count {actual_word_count} is {direction} "
                f"the allowed range {minimum_word_count}-{maximum_word_count} for target {target_word_count}"
            )
        return actual_word_count, within_range, message

    def _build_word_count_retry_guidance(self, chapter_content: str, target_word_count: int) -> str:
        if not chapter_content or target_word_count <= 0:
            return ""
        cleaned_content = self._clean_chapter_content(chapter_content)
        if (
            not cleaned_content
            or self._looks_like_plan_output(cleaned_content)
            or self._looks_like_meta_response(cleaned_content)
        ):
            return ""
        if not self._looks_like_story_text(cleaned_content) and self._count_words(cleaned_content) < 40:
            return ""

        actual_word_count, within_range, _ = self._validate_word_count(cleaned_content, target_word_count)
        if within_range:
            return ""
        minimum_word_count, maximum_word_count = self._word_count_bounds(target_word_count)
        if actual_word_count < minimum_word_count:
            return "\n".join([
                "System Word Count Recovery Advice:",
                f"- The previous usable prose was only {actual_word_count} words long for a target of {target_word_count}.",
                f"- Add roughly {max(1, target_word_count - actual_word_count)} more words of real scene material.",
                "- Expand underdeveloped existing beats before creating any new beat, coda, or aftermath scene.",
                "- Prefer concrete additions such as extra dialogue turns, physical action, sensory detail, interiority, or immediate consequences inside an existing beat.",
                "- Add content where the scene feels summarized or rushed instead of repeating the same emotional beat.",
                "- Do not tack on a generic reflection, travel beat, recap, or late epilogue just to hit the number.",
                "- The editor should point to specific places that need expansion before the final rewrite.",
            ])
        return "\n".join([
            "System Word Count Recovery Advice:",
            f"- The previous usable prose was {actual_word_count} words long for a target of {target_word_count}.",
            f"- Cut roughly {max(1, actual_word_count - target_word_count)} words while keeping all required beats intact.",
            "- Compress redundant description, repeated emotional reactions, or dialogue exchanges that restate the same point.",
            "- Combine or trim weaker paragraphs rather than deleting important plot movement.",
            "- The editor should point to specific places that can be cut or tightened before the final rewrite.",
        ])

    def _select_retry_guidance_source(self, *candidates: str) -> str:
        for candidate in candidates:
            cleaned = self._clean_chapter_content(candidate or "").strip()
            if not cleaned or self._looks_like_plan_output(cleaned) or self._looks_like_meta_response(cleaned):
                continue
            if self._looks_like_story_text(cleaned) or self._count_words(cleaned) >= 40:
                return cleaned
        return ""

    def _strip_word_count_recovery_advice(self, text: str) -> str:
        if not text:
            return ""
        stripped = re.sub(
            r"(?:\n\s*)?System Word Count Recovery Advice:\s*(?:\n-.*?)*(?=(?:\n\s*[A-Z][A-Za-z\s]+:|\Z))",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
        return stripped

    def _build_loop_retry_guidance(self) -> str:
        return "\n".join([
            "System Loop Recovery Advice:",
            "- The previous draft entered a repetition loop and started restating the same material.",
            "- Do not repeat earlier paragraphs, dialogue, or emotional beats once they have already been written.",
            "- If you need more length, add genuinely new action, dialogue, sensory detail, or consequences instead of rephrasing prior text.",
            "- Stop the scene once the intended chapter beat sequence is complete.",
        ])

    def _is_repetitive_output(self, content: str) -> bool:
        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", content) if paragraph.strip()]
        if len(paragraphs) >= 3:
            normalized_paragraphs = [" ".join(paragraph.lower().split()) for paragraph in paragraphs]
            duplicate_ratio = 1 - (len(set(normalized_paragraphs)) / max(len(normalized_paragraphs), 1))
            if duplicate_ratio >= 0.34:
                return True

        sentences = [
            " ".join(sentence.lower().split())
            for sentence in re.split(r"(?<=[.!?])\s+", content)
            if sentence.strip()
        ]
        if len(sentences) >= 6:
            repeated_sentences = len(sentences) - len(set(sentences))
            if repeated_sentences >= max(2, len(sentences) // 5):
                return True

        window_counts: Dict[str, int] = {}
        for index in range(len(sentences) - 2):
            window = " ".join(sentences[index:index + 3])
            window_counts[window] = window_counts.get(window, 0) + 1
            if len(window) > 80 and window_counts[window] >= 2:
                return True

        return False

    def _looks_like_plan_output(self, content: str) -> bool:
        cleaned = (content or "").strip()
        if not cleaned:
            return False
        upper_cleaned = cleaned.upper()
        if upper_cleaned.startswith((
            "PLAN:",
            "OUTLINE:",
            "KEY EVENTS:",
            "BEAT CHECK:",
            "SETTING:",
            "STORY ARC:",
            "WORLD ELEMENTS:",
        )):
            return True

        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if not lines:
            return False
        bullet_lines = [line for line in lines if line.startswith(("-", "*"))]
        sentence_count = len(re.findall(r"[.!?]", cleaned))
        if len(lines) >= 4 and len(bullet_lines) >= max(3, len(lines) // 2) and sentence_count <= 2:
            return True

        return False
    

    def initiate_group_chat(self) -> autogen.GroupChat:
        """Create a new group chat for the agents with improved speaking order"""
        writer_final = self._build_writer_agent("writer_final")
        
        return autogen.GroupChat(
            agents=[
                self.agents["user_proxy"],
                self.agents["memory_keeper"],
                self.agents["writer"],
                self.agents["editor"],
                writer_final
            ],
            messages=[],
            max_round=max(5, 2 + (self.max_iterations * 2)),
            speaker_selection_method="round_robin"
        )

    def _get_sender(self, msg: Dict) -> str:
        """Helper to get sender from message regardless of format"""
        return msg.get("sender") or msg.get("name", "")

    def _extract_tagged_content(self, content: str, tags: List[str]) -> str:
        for tag in tags:
            marker = f"{tag}:"
            if marker in content:
                extracted = content.split(marker, 1)[1].strip()
                if extracted:
                    return extracted
        return ""

    def _extract_required_chapter_details(self, prompt: str) -> tuple[List[str], int]:
        match = re.search(
            r"Required Chapter Details:\s*(.*?)(?:\n\s*Additional (?:Chapter )?Guidance:|\Z)",
            prompt,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return [], 0
        details_block = match.group(1).strip()
        details_block = re.sub(
            r"\n?\s*These chapter details are mandatory.*\Z",
            "",
            details_block,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        word_count_match = re.search(r"Target Word Count:\s*(\d+)", details_block, re.IGNORECASE)
        target_word_count = int(word_count_match.group(1)) if word_count_match else 0
        beats_block_match = re.search(r"Beats:\s*(.*)", details_block, re.IGNORECASE | re.DOTALL)
        beats_block = beats_block_match.group(1).strip() if beats_block_match else details_block
        beats = self._extract_required_beat_items(beats_block)
        return beats, target_word_count

    def _verify_chapter_complete(
        self,
        messages: List[Dict],
        chapter_number: int,
        required_beats: Optional[List[str]] = None,
        target_word_count: int = 0,
    ) -> bool:
        """Verify chapter completion by analyzing entire conversation context"""
        self._log("******************** VERIFYING CHAPTER COMPLETION ****************")
        current_chapter = chapter_number
        chapter_content = None
        beat_check_seen = not required_beats
        beat_check_passed = not required_beats
        loop_check_passed = True
        word_count_passed = target_word_count <= 0
        sequence_complete = {
            'memory_update': False,
            'plan': False,
            'setting': False,
            'scene': False,
            'feedback': False,
            'scene_final': False,
            'confirmation': False
        }
        
        # Analyze full conversation
        for msg in messages:
            content = msg.get("content", "")
            sender = self._get_sender(msg)
            if sender == "editor":
                content = self._normalize_editor_output(content)
            
            # Track chapter number
            if not current_chapter:
                num_match = re.search(r"Chapter (\d+):", content)
                if num_match:
                    current_chapter = int(num_match.group(1))
            
            # Track completion sequence
            if "MEMORY UPDATE:" in content: sequence_complete['memory_update'] = True
            if "PLAN:" in content: sequence_complete['plan'] = True
            if "SETTING:" in content: sequence_complete['setting'] = True
            if "SCENE:" in content or "CHAPTER:" in content:
                sequence_complete['scene'] = True
            if "FEEDBACK:" in content: sequence_complete['feedback'] = True
            if "BEAT CHECK:" in content or "BEAT CHECK RESULT:" in content.upper():
                beat_check_seen = True
            if "BEAT CHECK RESULT: PASS" in content.upper():
                beat_check_passed = True
            if "BEAT CHECK RESULT: FAIL" in content.upper():
                beat_check_passed = False
            if "LOOP CHECK RESULT: FAIL" in content.upper():
                loop_check_passed = False
            final_text = ""
            if sender in {"writer", "writer_final", "editor"}:
                final_text = self._extract_tagged_content(content, ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE"])
            if final_text and not self._looks_like_plan_output(final_text):
                sequence_complete['scene_final'] = True
                chapter_content = self._clean_chapter_content(final_text)
            if "**Confirmation:**" in content and "successfully" in content:
                sequence_complete['confirmation'] = True

            #print all sequence_complete flags
            self._log(f"******************** SEQUENCE COMPLETE ************** {sequence_complete}")
            self._log(f"******************** CURRENT_CHAPTER **************** {current_chapter}")
            self._log(f"******************** CHAPTER_CONTENT **************** {chapter_content}")
            self._log(f"******************** BEAT CHECK SEEN *************** {beat_check_seen}")
            self._log(f"******************** BEAT CHECK PASSED ************* {beat_check_passed}")
            self._log(f"******************** LOOP CHECK PASSED ************* {loop_check_passed}")

        # Verify all steps completed and content exists
        if not chapter_content:
            chapter_content = self._extract_best_chapter_candidate(messages)
        if chapter_content and self._is_repetitive_output(chapter_content):
            loop_check_passed = False
        if chapter_content:
            _, word_count_passed, word_count_message = self._validate_word_count(chapter_content, target_word_count)
            self._log(f"******************** WORD COUNT CHECK ************** {word_count_message}")
        return bool(current_chapter and chapter_content and beat_check_seen and beat_check_passed and loop_check_passed and word_count_passed)
    
    def _prepare_chapter_context(self, chapter_number: int, prompt: str) -> str:
        """Prepare context for chapter generation"""
        prior_chapters = [
            self.chapter_memory[number]
            for number in sorted(self.chapter_memory)
            if number < chapter_number and self.chapter_memory[number]
        ]
        if chapter_number == 1 or not prior_chapters:
            return "No previous chapter summaries."

        recent_memory = prior_chapters[-3:]
        context_parts = ["Previous Chapter Summaries:", *recent_memory]
        return "\n".join(context_parts)

    def _is_recoverable_chapter_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in [
                "generation incomplete",
                "no content found",
                "outline-like plan output",
                "file not created",
                "detected repetitive looping output",
                "repetitive looping output",
                "programmatic word count",
                "usable prose",
                "prohibited meta prose",
                "context length",
                "n_keep",
                "n_ctx",
            ]
        )

    def _is_retryable_model_load_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in [
                "failed to load model",
                "error loading model",
                "internal server error",
                "server had an error",
                "temporarily unavailable",
                "connection reset",
                "connection aborted",
                "connection refused",
                "timed out",
            ]
        )

    def _extract_last_content(self, chat_history: List[Dict]) -> str:
        for msg in reversed(chat_history):
            content = (msg.get("content") or "").strip()
            if content:
                return content
        return ""

    def _run_agent_step(
        self,
        chapter_number: int,
        chapter_title: str,
        attempt: int,
        step_name: str,
        agent: autogen.ConversableAgent,
        prompt: str,
        output_stage: str,
        detail: str,
    ) -> str:
        self._emit_progress(
            chapter_number,
            chapter_title,
            agent.name,
            step_name,
            detail,
            output_stage,
            attempt,
        )
        system_message = getattr(agent, "system_message", "") or "[no system message]"
        self._log_block(
            f"CHAPTER {chapter_number} | ATTEMPT {attempt} | STEP {step_name} | INPUT | SYSTEM | {agent.name}",
            system_message,
        )
        self._log_block(
            f"CHAPTER {chapter_number} | ATTEMPT {attempt} | STEP {step_name} | INPUT | PROMPT | {agent.name}",
            prompt,
        )
        label = f"CHAPTER {chapter_number} | ATTEMPT {attempt} | STEP {step_name} | {agent.name}"
        self._emit_monitor("input", label, prompt)
        retry_delays = (1.0, 3.0)
        for retry_index in range(len(retry_delays) + 1):
            try:
                chat_result = self.agents["user_proxy"].initiate_chat(
                    agent,
                    clear_history=True,
                    silent=True,
                    max_turns=1,
                    message=prompt,
                )
                break
            except Exception as exc:
                if retry_index >= len(retry_delays) or not self._is_retryable_model_load_error(exc):
                    raise
                delay = retry_delays[retry_index]
                self._log(
                    "[book_generator] Retryable model/provider failure during "
                    f"{step_name} for chapter {chapter_number} via {agent.name}: {exc}. "
                    f"Retrying in {delay:.1f}s."
                )
                time.sleep(delay)
        output = self._extract_last_content(chat_result.chat_history)
        self._emit_monitor("output", label, output)
        self._log_block(
            f"CHAPTER {chapter_number} | ATTEMPT {attempt} | STEP {step_name} | OUTPUT | {agent.name}",
            output,
        )
        return output

    def _extract_story_block(self, content: str, tags: List[str]) -> str:
        return self._extract_story_candidate(content, tags, allow_raw_fallback=True)

    def _review_candidate_for_save(
        self,
        chapter_number: int,
        chapter_title: str,
        prompt: str,
        context: str,
        candidate_scene: str,
        required_beats: List[str],
        target_word_count: int,
        attempt: int,
        step_name: str = "editor_final_check",
        detail: str = "Editor is validating the final candidate",
    ) -> str:
        editor_prompt = self._build_editor_prompt(
            chapter_number,
            chapter_title,
            prompt,
            context,
            candidate_scene,
            required_beats,
            target_word_count,
        )
        editor_output = self._run_agent_step(
            chapter_number,
            chapter_title,
            attempt,
            step_name,
            self.agents["editor"],
            editor_prompt,
            "feedback",
            detail,
        )
        return self._normalize_editor_output(editor_output)

    def _finalize_chapter_result(
        self,
        chapter_number: int,
        chapter_title: str,
        final_scene: str,
        target_word_count: int,
        attempt: int,
    ) -> str:
        self._save_chapter(chapter_number, final_scene, target_word_count)

        memory_update = ""
        try:
            memory_prompt = self._build_memory_keeper_prompt(chapter_number, chapter_title, final_scene)
            memory_output = self._run_agent_step(
                chapter_number,
                chapter_title,
                attempt,
                "memory_update",
                self.agents["memory_keeper"],
                memory_prompt,
                "memory",
                "Memory keeper is updating continuity",
            )
            if "MEMORY UPDATE:" in memory_output:
                memory_update = memory_output.split("MEMORY UPDATE:", 1)[1].strip()
            else:
                memory_update = memory_output.strip()
        except Exception as memory_exc:
            self._log(f"Memory keeper failed after Chapter {chapter_number} save: {memory_exc}")
            self._log(traceback.format_exc().rstrip())

        self._store_chapter_memory(chapter_number, final_scene, memory_update)
        return memory_update

    def _verify_pipeline_result(
        self,
        chapter_number: int,
        draft_scene: str,
        editor_output: str,
        final_scene: str,
        required_beats: Optional[List[str]] = None,
        target_word_count: int = 0,
        attempt: int = 0,
        phase: str = "attempt",
    ) -> bool:
        editor_output = self._normalize_editor_output(editor_output)
        beat_check_items = self._extract_beat_check_items(editor_output) if required_beats else []
        beat_check_complete = not required_beats or len(beat_check_items) >= len(required_beats)
        beat_check_status = self._extract_pass_fail_status(editor_output, "BEAT CHECK RESULT")
        loop_check_status = self._extract_pass_fail_status(editor_output, "LOOP CHECK RESULT")
        sentence_check_status = self._extract_pass_fail_status(editor_output, "SENTENCE LENGTH CHECK RESULT")
        beat_check_seen = (
            not required_beats
            or (bool(beat_check_status) and "BEAT CHECK:" in editor_output and beat_check_complete)
        )
        beat_check_passed = not required_beats or (
            beat_check_status == "PASS" and beat_check_complete
        )
        loop_check_passed = loop_check_status == "PASS"
        sentence_check_seen = bool(sentence_check_status)
        sentence_check_passed = sentence_check_status == "PASS"
        if final_scene and self._is_repetitive_output(final_scene):
            loop_check_passed = False
        word_count_passed = True
        word_count_message = "No programmatic word count target"
        sentence_length_passed = True
        sentence_length_message = f"No sentence exceeds {MAX_SENTENCE_WORDS} words."
        prose_integrity_passed = True
        prose_integrity_message = "No prohibited meta prose detected."
        if final_scene:
            _, word_count_passed, word_count_message = self._validate_word_count(final_scene, target_word_count)
            sentence_length_passed, sentence_length_message = self._validate_sentence_length(final_scene)
            prose_integrity_passed, prose_integrity_message = self._validate_prose_integrity(final_scene)
        validation_lines = [
            f"chapter: {chapter_number}",
            f"draft_present: {bool(draft_scene)}",
            f"final_present: {bool(final_scene)}",
            f"required_beats: {len(required_beats or [])}",
            f"beat_check_items: {len(beat_check_items) if required_beats else 0}",
            f"beat_check_complete: {beat_check_complete}",
            f"beat_check_status: {beat_check_status or '[missing/invalid]'}",
            f"beat_check_seen: {beat_check_seen}",
            f"beat_check_passed: {beat_check_passed}",
            f"loop_check_status: {loop_check_status or '[missing/invalid]'}",
            f"loop_check_passed: {loop_check_passed}",
            f"sentence_check_status: {sentence_check_status or '[missing/invalid]'}",
            f"sentence_check_seen: {sentence_check_seen}",
            f"sentence_check_passed: {sentence_check_passed}",
            f"sentence_length_passed: {sentence_length_passed}",
            f"prose_integrity_passed: {prose_integrity_passed}",
            f"word_count_passed: {word_count_passed}",
            sentence_length_message,
            prose_integrity_message,
            word_count_message,
        ]
        validation_text = "\n".join(validation_lines)
        self._log_block(
            f"CHAPTER {chapter_number} | VALIDATION",
            validation_text,
        )
        passed = bool(
            final_scene
            and beat_check_seen
            and beat_check_passed
            and loop_check_passed
            and sentence_length_passed
            and prose_integrity_passed
            and word_count_passed
        )
        if not passed and self.validation_callback:
            try:
                self.validation_callback({
                    "chapter_number": chapter_number,
                    "attempt": attempt,
                    "phase": phase,
                    "body": validation_text,
                })
            except Exception as exc:
                self._log(f"Validation callback failed for Chapter {chapter_number}: {exc}")
        return passed

    def _draft_ready_for_final_check(
        self,
        draft_scene: str,
        editor_output: str,
        required_beats: Optional[List[str]],
        target_word_count: int,
    ) -> bool:
        editor_output = self._normalize_editor_output(editor_output)
        if not draft_scene:
            return False
        if self._is_repetitive_output(draft_scene):
            return False
        if self._extract_pass_fail_status(editor_output, "BEAT CHECK RESULT") != "PASS":
            return False
        beat_check_items = self._extract_beat_check_items(editor_output) if required_beats else []
        if required_beats and len(beat_check_items) < len(required_beats):
            return False
        if self._extract_pass_fail_status(editor_output, "LOOP CHECK RESULT") != "PASS":
            return False
        sentence_length_passed, _ = self._validate_sentence_length(draft_scene)
        if not sentence_length_passed:
            return False
        prose_integrity_passed, _ = self._validate_prose_integrity(draft_scene)
        if not prose_integrity_passed:
            return False
        _, word_count_passed, _ = self._validate_word_count(draft_scene, target_word_count)
        return word_count_passed

    def _build_writer_prompt(
        self,
        chapter_number: int,
        chapter_title: str,
        prompt: str,
        context: str,
        required_beats: List[str],
        target_word_count: int,
        retry_context: str,
        attempt: int = 1,
        prior_scene: str = "",
        prior_editor_output: str = "",
    ) -> str:
        range_instruction = ""
        if target_word_count > 0:
            minimum_word_count, maximum_word_count = self._word_count_bounds(target_word_count)
            range_instruction = (
                f"\nTarget word count: {target_word_count} words.\n"
                f"Required programmatic range: {minimum_word_count}-{maximum_word_count} words."
            )
        beat_focus_block = self._format_required_beat_checklist(required_beats)
        prompt_summary = self._summarize_chapter_requirements(prompt)
        use_repair_mode = attempt >= 3 and bool(prior_scene and self._extract_failed_beat_check_items(prior_editor_output))
        if use_repair_mode:
            prior_scene_for_prompt = self._compact_text_for_prompt(
                prior_scene,
                max(1200, (maximum_word_count + 200) if target_word_count > 0 else 1600),
                "previous draft scene",
            )
            return f"""Repair Chapter {chapter_number}: {chapter_title} by improving the existing draft below.

Return only:
SCENE:
[full revised chapter prose]

Requirements:
- Output prose only.
- Do not include FEEDBACK, MEMORY UPDATE, PLAN, notes, bullets, or placeholders.
- Keep the working material from the existing draft where it already satisfies the checklist.
- Explicitly repair every unresolved checklist item in the order shown.
- If the chapter summary includes Writing Style Guidance, follow it closely and preserve that prose direction in the revision.
- Do not satisfy multiple numbered checklist items with one vague summary line if the individual beats need to appear distinctly on page.
- Do not rewrite from scratch unless required to satisfy the checklist cleanly.
- Missing, merging away, or reordering any numbered checklist item causes automatic rejection and another retry.
- Treat the current-chapter beat anchors as the highest-priority narrative requirements for this scene.
- Faithful paraphrase is encouraged; exact wording is not required.
- Use the broader outline only for continuity after the checklist is satisfied.
- Do not import explicit beats from other chapters.
- Do not copy checklist labels, retry labels, or compliance commentary into the prose.
- Make real forward progress in every paragraph.
- If the chapter needs more length, deepen an existing beat before inventing any new event, coda, or aftermath.
- Preferred expansion order: sensory detail, physical business, dialogue subtext, interior thought, and immediate consequences inside the active beat.
- Do not tack on a low-stakes reflection or wrap-up after the scene has already landed.
- Give the scene a proper ending, not an abrupt cutoff.{range_instruction}

Previous Context:
{context}
{retry_context}

Chapter Summary:
{prompt_summary}

Existing Draft To Repair:
SCENE:
{prior_scene_for_prompt}

Final Mandatory Checklist Before Writing:
{beat_focus_block}"""
        return f"""Write Chapter {chapter_number}: {chapter_title}.

Return only:
SCENE:
[full chapter prose]

Requirements:
- Write full prose, not notes or bullets.
- Do not include FEEDBACK, MEMORY UPDATE, PLAN, or placeholders.
- Preserve all required beats in the chapter prompt.
- If the chapter summary includes Writing Style Guidance, follow it closely and let it shape the prose throughout the scene.
- Treat the current-chapter beat anchors as the highest-priority narrative requirements for this scene.
- Faithful paraphrase is encouraged; exact wording is not required.
- Do not satisfy multiple numbered checklist items with one vague summary line if the individual beats need to appear distinctly on page.
- Missing, merging away, or reordering any numbered checklist item causes automatic rejection and another retry.
- Use the broader outline only for continuity after the checklist is satisfied.
- Do not import explicit beats from other chapters.
- Do not copy checklist labels, retry labels, or compliance commentary into the prose.
- Make real forward progress in every paragraph.
- If the chapter needs more length, deepen an existing beat before inventing any new event, coda, or aftermath.
- Preferred expansion order: sensory detail, physical business, dialogue subtext, interior thought, and immediate consequences inside the active beat.
- Do not tack on a low-stakes reflection or wrap-up after the scene has already landed.
- Give the scene a proper ending, not an abrupt cutoff.{range_instruction}

Previous Context:
{context}
{retry_context}

Chapter Summary:
{prompt_summary}

Final Mandatory Checklist Before Writing:
{beat_focus_block}"""

    def _build_editor_prompt(
        self,
        chapter_number: int,
        chapter_title: str,
        prompt: str,
        context: str,
        draft_scene: str,
        required_beats: List[str],
        target_word_count: int,
    ) -> str:
        range_instruction = ""
        if target_word_count > 0:
            minimum_word_count, maximum_word_count = self._word_count_bounds(target_word_count)
            range_instruction = (
                f"\nThe target is {target_word_count} words and the acceptable range is "
                f"{minimum_word_count}-{maximum_word_count} words."
            )
        draft_for_prompt = self._compact_text_for_prompt(
            draft_scene,
            max(1200, (maximum_word_count + 200) if target_word_count > 0 else 1600),
            "draft scene",
        )
        beat_focus_block = self._format_required_beat_checklist(required_beats)
        prompt_summary = self._summarize_chapter_requirements(prompt, include_additional_guidance=False)
        return f"""Review the draft for Chapter {chapter_number}: {chapter_title}.

Return ONLY valid JSON. Do not write the final chapter.
Do not wrap the JSON in markdown or code fences.

Use this exact JSON shape:
{{
  "beat_check": [
    {{"index": 1, "beat": "required beat text", "status": "PASS", "evidence": "short evidence note"}}
  ],
  "beat_check_result": "PASS",
  "loop_check_result": "PASS",
  "loop_check_notes": ["short note"],
  "sentence_length_check": ["short note"],
  "sentence_length_check_result": "PASS",
  "word_count_advice": "short paragraph",
  "suggest": "optional short paragraph"
}}

In WORD COUNT ADVICE:
- If the draft is too short, identify specific moments, exchanges, or paragraphs that should be expanded and explain what to add.
- If the draft is too long, identify specific moments, exchanges, or paragraphs that should be cut or tightened and explain what to remove or compress.
- If the length is acceptable, say that directly and explicitly state that no word-count changes are required.
- Do not recommend trimming or padding only to hit the exact target if the draft is already inside the acceptable range.
- Prefer depth-first expansion inside underdeveloped existing beats before recommending any brand-new event.
- Name the specific beat item, exchange, or paragraph that should be expanded when the chapter is short.
- Recommend additions such as sensory detail, physical action, interior reasoning, dialogue subtext, or immediate consequences tied to an existing beat.
- Do not recommend a new coda, epilogue, travel beat, recap, or generic reflection solely to raise the word count.
- Report whole-draft word count information only in WORD COUNT ADVICE, not in any other section.{range_instruction}
- If the chapter summary includes Writing Style Guidance, check whether the draft materially follows it and call out meaningful style drift in the structured feedback.

In BEAT CHECK:
- Check the current-chapter beat anchors below item by item and in order.
- Do not merge multiple checklist items into one line.
- Mark each checklist item PASS or FAIL and include a short evidence note.
- Judge by narrative intent and concrete on-page evidence, not exact wording.
- Faithful paraphrase or natural rewording counts as PASS if the same beat clearly happens in the right place.
- Mark an item FAIL only if the underlying beat is absent, materially out of order, contradicted, or reduced to a vague implication without a clear on-page moment.
- Put any explanation in the BEAT CHECK lines themselves.
- The line 'BEAT CHECK RESULT:' must be a standalone exact line ending in only PASS or FAIL.

In SENTENCE LENGTH CHECK:
- Review the draft for overly long sentences.
- If any sentence exceeds {MAX_SENTENCE_WORDS} words, mark SENTENCE LENGTH CHECK RESULT: FAIL.
- Quote or paraphrase the first offending sentence fragment and include its approximate sentence-fragment word count.
- Do not describe a sentence fragment count as the draft or chapter word count.
- If no sentence exceeds {MAX_SENTENCE_WORDS} words, mark SENTENCE LENGTH CHECK RESULT: PASS.
- Every `status` value and every `*_result` value must be exactly `PASS` or `FAIL`.

{beat_focus_block}

Chapter Summary:
{prompt_summary}

Previous Context:
{context}

Draft:
SCENE:
{draft_for_prompt}"""

    def _build_writer_final_prompt(
        self,
        chapter_number: int,
        chapter_title: str,
        prompt: str,
        context: str,
        draft_scene: str,
        editor_output: str,
        required_beats: List[str],
        target_word_count: int,
        retry_context: str,
    ) -> str:
        range_instruction = ""
        if target_word_count > 0:
            minimum_word_count, maximum_word_count = self._word_count_bounds(target_word_count)
            range_instruction = (
                f"\nThe final chapter must land inside the programmatic range "
                f"{minimum_word_count}-{maximum_word_count} words for the target of {target_word_count}."
            )
        draft_for_prompt = self._compact_text_for_prompt(
            draft_scene,
            max(1200, (maximum_word_count + 200) if target_word_count > 0 else 1600),
            "draft scene",
        )
        editor_feedback_for_prompt = self._compact_text_for_prompt(
            self._build_actionable_revision_feedback(editor_output, draft_scene, target_word_count),
            450,
            "actionable revision feedback",
        )
        beat_focus_block = self._format_required_beat_checklist(required_beats)
        prompt_summary = self._summarize_chapter_requirements(prompt)
        issue_count = self._count_actionable_revision_issues(editor_output, draft_scene, target_word_count)
        _, draft_in_range, _ = self._validate_word_count(draft_scene, target_word_count)
        use_local_repair_mode = bool(
            draft_scene
            and draft_in_range
            and issue_count > 0
            and issue_count <= 2
            and self._extract_pass_fail_status(editor_output, "LOOP CHECK RESULT") != "FAIL"
        )

        if use_local_repair_mode:
            return f"""Perform a surgical revision of Chapter {chapter_number}: {chapter_title} using the draft below.

Return only:
SCENE FINAL:
[full revised chapter prose]

Requirements:
- Output prose only.
- Preserve the existing draft's wording and structure wherever it already works.
- Change only the smallest number of sentences or paragraphs needed to fix the specific unresolved issues below.
- Do not rewrite from scratch.
- Do not include FEEDBACK, MEMORY UPDATE, PLAN, notes, bullets, or placeholders.
- If the chapter summary includes Writing Style Guidance, preserve that prose direction while revising.
- Do not copy prompt labels, checklist text, or retry context into the prose.
- Treat the current-chapter beat anchors below as higher priority than generic outline bullets or editor paraphrases.
- Faithful paraphrase is encouraged; exact wording is not required.
- Missing, merging away, or reordering any numbered checklist item causes automatic rejection and another retry.
- If extra length is needed, deepen earlier or existing beats before inventing any new event or aftermath.
- Prefer expansion through sensory detail, physical business, dialogue subtext, interiority, and immediate consequences within the current beat.
- Do not tack on a low-stakes coda, recap, travel beat, or generic reflection after the intended ending.
- Keep the chapter coherent and fully written from start to finish.{range_instruction}

Previous Context:
{context}

Chapter Summary:
{prompt_summary}

Specific Unresolved Issues:
{editor_feedback_for_prompt or "No actionable issues were extracted; preserve the draft and only polish as needed."}

Draft To Revise:
SCENE:
{draft_for_prompt}

{retry_context}

Final Mandatory Checklist Before Revising:
{beat_focus_block}"""

        return f"""Revise Chapter {chapter_number}: {chapter_title} using the draft and focused revision notes below.

Return only:
SCENE FINAL:
[full revised chapter prose]

Requirements:
- Output prose only.
- Do not include FEEDBACK, MEMORY UPDATE, PLAN, notes, bullets, or placeholders.
- Fix every issue raised by the editor while preserving required beats and scene momentum.
- If the chapter summary includes Writing Style Guidance, preserve that prose direction while revising.
- Treat the current-chapter beat anchors below as higher priority than generic outline bullets or editor paraphrases.
- Faithful paraphrase is encouraged; exact wording is not required.
- Do not satisfy multiple numbered checklist items with one vague summary line if the individual beats need to appear distinctly on page.
- Missing, merging away, or reordering any numbered checklist item causes automatic rejection and another retry.
- Use the broader outline only for continuity after the checklist is satisfied.
- Do not import explicit beats from other chapters.
- Do not copy prompt labels, checklist text, or retry context into the prose.
- If extra length is needed, deepen earlier or existing beats before inventing any new event or aftermath.
- Prefer expansion through sensory detail, physical business, dialogue subtext, interiority, and immediate consequences within the current beat.
- Do not tack on a low-stakes coda, recap, travel beat, or generic reflection after the intended ending.
- Keep the chapter coherent and fully written from start to finish.{range_instruction}

Previous Context:
{context}

Chapter Summary:
{prompt_summary}

Draft:
SCENE:
{draft_for_prompt}

{retry_context}

Final Mandatory Checklist Before Revising:
{beat_focus_block}

Actionable Revision Focus:
{editor_feedback_for_prompt or "No actionable revision notes were extracted. Preserve the draft and keep the checklist intact."}"""

    def _build_memory_keeper_prompt(self, chapter_number: int, chapter_title: str, chapter_content: str) -> str:
        return f"""Update story continuity for the accepted Chapter {chapter_number}: {chapter_title}.

Return only a MEMORY UPDATE in the required memory format.
Do not write story prose, feedback, or a plan.

Accepted Chapter Text:
{chapter_content}"""

    def generate_chapter(self, chapter_number: int, prompt: str) -> Dict:
        """Generate a single chapter with a deterministic writer -> editor -> writer_final pipeline."""
        self._log(f"\nGenerating Chapter {chapter_number}...")
        chapter_title = self.outline[chapter_number - 1]["title"]
        context = self._prepare_chapter_context(chapter_number, prompt)
        active_prompt = prompt
        active_prompt_version: Optional[int] = None
        last_error = ""
        retry_guidance = ""
        prior_editor_output = ""
        prior_scene = ""
        self._refresh_writer_runtime(chapter_number, 0, "chapter_start")
        base_attempt_budget = max(3, self.max_iterations)
        max_attempts = base_attempt_budget
        attempt = 1

        while attempt <= max_attempts:
            self._refresh_writer_runtime(chapter_number, attempt, "attempt_start")
            base_attempt_budget = max(3, self.max_iterations)
            max_attempts = max(max_attempts, base_attempt_budget)
            draft_scene = ""
            editor_output = ""
            final_scene = ""
            final_review_output = ""
            try:
                self._raise_if_pause_requested(
                    chapter_number,
                    chapter_title,
                    attempt,
                    "Pause requested; stopping before the next chapter attempt.",
                    "draft",
                )
                prompt_state = self._resolve_chapter_prompt_for_attempt(
                    chapter_number,
                    attempt,
                    "attempt",
                    active_prompt,
                )
                active_prompt = str(prompt_state.get("prompt") or active_prompt or "").strip()
                prompt_version = int(prompt_state.get("version", 0) or 0)
                details_summary = self._summarize_chapter_details(prompt_state.get("details") or {})
                if active_prompt_version is None:
                    active_prompt_version = prompt_version
                elif prompt_version != active_prompt_version:
                    active_prompt_version = prompt_version
                    last_error = ""
                    retry_guidance = ""
                    prior_editor_output = ""
                    prior_scene = ""
                    max_attempts = max(max_attempts, attempt - 1 + base_attempt_budget)
                    self._log_block(
                        f"CHAPTER {chapter_number} | ATTEMPT {attempt} | CHAPTER ADVICE APPLIED | V{prompt_version}",
                        details_summary or "The chapter prompt changed for this attempt.",
                    )
                required_beats, target_word_count = self._extract_required_chapter_details(active_prompt)
                retry_context = ""
                if last_error:
                    retry_context = (
                        f"\n\nRetry Context:\nPrevious failure: {last_error}"
                    )
                if retry_guidance:
                    retry_context += f"\n\n{retry_guidance}"
                if prior_editor_output:
                    retry_feedback_focus = self._build_retry_feedback_focus(
                        prior_editor_output,
                        prior_scene,
                        target_word_count,
                    )
                    if retry_feedback_focus:
                        retry_context += (
                            "\n\nTo the above, also consider this unresolved prior feedback:\n"
                            f"{retry_feedback_focus}"
                        )

                writer_prompt = self._build_writer_prompt(
                    chapter_number,
                    chapter_title,
                    active_prompt,
                    context,
                    required_beats,
                    target_word_count,
                    retry_context,
                    attempt,
                    prior_scene,
                    prior_editor_output,
                )
                self._raise_if_pause_requested(
                    chapter_number,
                    chapter_title,
                    attempt,
                    "Pause requested; stopping before writer draft.",
                    "draft",
                )
                self._refresh_writer_runtime(chapter_number, attempt, "writer_draft")
                writer_output = self._run_agent_step(
                    chapter_number,
                    chapter_title,
                    attempt,
                    "writer_draft",
                    self.agents["writer"],
                    writer_prompt,
                    "draft",
                    "Writer is drafting the chapter",
                )
                draft_scene = self._extract_story_block(writer_output, ["SCENE", "CHAPTER", "SCENE FINAL", "CHAPTER FINAL"])
                draft_scene, draft_guard_note = self._apply_loop_guard(draft_scene, target_word_count)
                if draft_guard_note:
                    self._log_block(
                        f"CHAPTER {chapter_number} | ATTEMPT {attempt} | STEP writer_draft | GUARDRAIL",
                        draft_guard_note,
                    )
                if not self._looks_like_story_text(draft_scene):
                    raise ValueError("Writer draft did not return usable prose")
                if self._is_repetitive_output(draft_scene):
                    raise ValueError("Writer draft contains repetitive looping output")
                draft_integrity_passed, draft_integrity_message = self._validate_prose_integrity(draft_scene)
                if not draft_integrity_passed:
                    raise ValueError(draft_integrity_message)

                editor_prompt = self._build_editor_prompt(
                    chapter_number,
                    chapter_title,
                    active_prompt,
                    context,
                    draft_scene,
                    required_beats,
                    target_word_count,
                )
                self._raise_if_pause_requested(
                    chapter_number,
                    chapter_title,
                    attempt,
                    "Pause requested; stopping before editor review.",
                    "feedback",
                )
                editor_output = self._run_agent_step(
                    chapter_number,
                    chapter_title,
                    attempt,
                    "editor_review",
                    self.agents["editor"],
                    editor_prompt,
                    "feedback",
                    "Editor is reviewing the draft",
                )
                editor_output = self._normalize_editor_output(editor_output)
                prior_editor_output = editor_output

                if self._draft_ready_for_final_check(
                    draft_scene,
                    editor_output,
                    required_beats,
                    target_word_count,
                ):
                    final_scene = draft_scene
                    self._log_block(
                        f"CHAPTER {chapter_number} | ATTEMPT {attempt} | STEP writer_final | SKIPPED",
                        "Draft already satisfies the actionable beat, loop, sentence-length, prose-integrity, and word-count checks. Promoting draft directly to final review.",
                    )
                else:
                    self._refresh_writer_runtime(chapter_number, attempt, "writer_final")
                    writer_final = self._build_writer_agent("writer_final")
                    current_word_count_guidance = self._build_word_count_retry_guidance(draft_scene, target_word_count)
                    final_retry_context = self._strip_word_count_recovery_advice(retry_context)
                    if current_word_count_guidance:
                        final_retry_context = (
                            f"{final_retry_context}\n\n{current_word_count_guidance}".strip()
                            if final_retry_context
                            else current_word_count_guidance
                        )
                    final_prompt = self._build_writer_final_prompt(
                        chapter_number,
                        chapter_title,
                        active_prompt,
                        context,
                        draft_scene,
                        editor_output,
                        required_beats,
                        target_word_count,
                        final_retry_context,
                    )
                    self._raise_if_pause_requested(
                        chapter_number,
                        chapter_title,
                        attempt,
                        "Pause requested; stopping before writer final revision.",
                        "revision",
                    )
                    final_output = self._run_agent_step(
                        chapter_number,
                        chapter_title,
                        attempt,
                        "writer_final",
                        writer_final,
                        final_prompt,
                        "revision",
                        "Writer final is revising the chapter",
                    )
                    final_scene = self._extract_story_block(final_output, ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE", "SCENE", "CHAPTER"])
                    final_scene, final_guard_note = self._apply_loop_guard(final_scene, target_word_count)
                    if final_guard_note:
                        self._log_block(
                            f"CHAPTER {chapter_number} | ATTEMPT {attempt} | STEP writer_final | GUARDRAIL",
                            final_guard_note,
                        )
                    if not self._looks_like_story_text(final_scene):
                        raise ValueError("Writer final did not return usable prose")
                    if self._is_repetitive_output(final_scene):
                        raise ValueError("Writer final contains repetitive looping output")
                    final_integrity_passed, final_integrity_message = self._validate_prose_integrity(final_scene)
                    if not final_integrity_passed:
                        raise ValueError(final_integrity_message)

                self._raise_if_pause_requested(
                    chapter_number,
                    chapter_title,
                    attempt,
                    "Pause requested; stopping before final validation review.",
                    "feedback",
                )
                final_review_output = self._review_candidate_for_save(
                    chapter_number,
                    chapter_title,
                    active_prompt,
                    context,
                    final_scene,
                    required_beats,
                    target_word_count,
                    attempt,
                )
                prior_editor_output = final_review_output or editor_output

                if not self._verify_pipeline_result(
                    chapter_number,
                    draft_scene,
                    final_review_output,
                    final_scene,
                    required_beats,
                    target_word_count,
                    attempt,
                    "attempt",
                ):
                    raise ValueError(f"Chapter {chapter_number} generation incomplete")

                memory_update = self._finalize_chapter_result(
                    chapter_number,
                    chapter_title,
                    final_scene,
                    target_word_count,
                    attempt,
                )

                self._emit_progress(
                    chapter_number,
                    chapter_title,
                    "writer_final",
                    "completed",
                    "Final chapter saved",
                    "final",
                    attempt,
                )
                return {
                    "memory_update": memory_update,
                    "draft_scene": draft_scene,
                    "editor_feedback": final_review_output or editor_output,
                    "final_scene": final_scene,
                }

            except GenerationPauseRequested:
                raise
            except Exception as exc:
                last_error = str(exc)
                retry_source = self._select_best_chapter_candidate(
                    [draft_scene, final_scene, prior_scene],
                    target_word_count,
                ) or self._select_retry_guidance_source(draft_scene, final_scene, prior_scene)
                if retry_source:
                    prior_scene = retry_source
                retry_guidance = self._build_word_count_retry_guidance(retry_source, target_word_count)
                if "repetitive looping output" in last_error.lower() or "context length" in last_error.lower():
                    loop_guidance = self._build_loop_retry_guidance()
                    if loop_guidance not in retry_guidance:
                        retry_guidance = f"{retry_guidance}\n\n{loop_guidance}".strip() if retry_guidance else loop_guidance
                if "prohibited meta prose" in last_error.lower():
                    integrity_guidance = "\n".join([
                        "System Output Integrity Advice:",
                        "- Return prose only after the required tag.",
                        "- Do not include word-count claims, sequel notes, summaries, or commentary about what the chapter does.",
                        "- Do not mention the next chapter, the draft process, or the revision process inside the prose.",
                    ])
                    retry_guidance = f"{retry_guidance}\n\n{integrity_guidance}".strip() if retry_guidance else integrity_guidance
                self._log(f"Error in chapter {chapter_number}: {last_error}")
                if attempt < max_attempts and self._is_recoverable_chapter_error(exc):
                    self._log(
                        f"Recoverable chapter failure detected. "
                        f"Retrying Chapter {chapter_number} with stronger guidance ({attempt}/{max_attempts})..."
                    )
                    attempt += 1
                    continue
                self._log(traceback.format_exc().rstrip())
                if self._is_recoverable_chapter_error(exc):
                    return self._handle_chapter_generation_failure(
                        chapter_number,
                        active_prompt,
                        last_error,
                        retry_guidance,
                        prior_scene,
                        prior_editor_output,
                    )
                raise

    def _extract_final_scene(self, messages: List[Dict]) -> Optional[str]:
        """Extract chapter content with improved content detection"""
        for msg in reversed(messages):
            content = msg.get("content", "")
            sender = self._get_sender(msg)
            
            if sender in ["writer", "writer_final"]:
                allow_raw_fallback = sender in {"writer", "writer_final"}
                candidate = self._extract_story_candidate(
                    content,
                    ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE", "SCENE", "CHAPTER"],
                    allow_raw_fallback=allow_raw_fallback,
                )
                if candidate:
                    return candidate
                    
        return None

    def _looks_like_story_text(self, content: str) -> bool:
        cleaned = self._clean_chapter_content(content or "").strip()
        if len(cleaned) < 120:
            return False
        if self._looks_like_plan_output(cleaned):
            return False
        prose_integrity_passed, _ = self._validate_prose_integrity(cleaned)
        if not prose_integrity_passed:
            return False
        if cleaned.upper().startswith(("FEEDBACK:", "PLAN:", "SETTING:", "BEAT CHECK:", "LOOP CHECK RESULT:")):
            return False
        sentence_count = len(re.findall(r"[.!?]", cleaned))
        return sentence_count >= 2 or len(cleaned.splitlines()) >= 3

    def _extract_best_chapter_candidate(self, messages: List[Dict]) -> str:
        candidates: List[str] = []
        for msg in reversed(messages):
            content = (msg.get("content") or "").strip()
            sender = self._get_sender(msg)
            if sender in {"user_proxy", "memory_keeper", "editor"}:
                continue
            candidate = self._extract_story_candidate(
                content,
                ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE", "SCENE", "CHAPTER"],
                allow_raw_fallback=sender in {"writer", "writer_final"},
            )
            if self._looks_like_story_text(candidate):
                candidates.append(candidate)
        return max(candidates, key=len) if candidates else ""

    def _select_best_chapter_candidate(self, candidates: List[str], target_word_count: int = 0) -> str:
        ranked_candidates = []
        for candidate in candidates:
            cleaned = self._clean_chapter_content(candidate or "")
            if not cleaned:
                continue
            if self._looks_like_plan_output(cleaned):
                continue
            if self._is_repetitive_output(cleaned):
                continue
            prose_integrity_passed, _ = self._validate_prose_integrity(cleaned)
            if not prose_integrity_passed:
                continue
            sentence_length_passed, _ = self._validate_sentence_length(cleaned)
            if not sentence_length_passed:
                continue
            if not self._looks_like_story_text(cleaned):
                continue

            actual_word_count, within_range, _ = self._validate_word_count(cleaned, target_word_count)
            distance_from_target = abs(actual_word_count - target_word_count) if target_word_count > 0 else 0
            ranked_candidates.append((
                1 if within_range else 0,
                -distance_from_target,
                actual_word_count,
                len(cleaned),
                cleaned,
            ))

        if not ranked_candidates:
            return ""

        ranked_candidates.sort(reverse=True)
        return ranked_candidates[0][4]

    def _handle_chapter_generation_failure(
        self,
        chapter_number: int,
        prompt: str,
        last_error: str = "",
        retry_guidance: str = "",
        prior_scene: str = "",
        prior_editor_output: str = "",
    ) -> Dict:
        """Handle failed chapter generation with a deterministic retry pipeline."""
        self._log(f"Attempting deterministic recovery for Chapter {chapter_number}...")
        chapter_title = self.outline[chapter_number - 1]["title"]
        context = self._prepare_chapter_context(chapter_number, prompt)
        active_prompt = prompt
        active_prompt_version: Optional[int] = None
        self._refresh_writer_runtime(chapter_number, 0, "recovery_start")
        base_attempt_budget = max(3, self.max_iterations)
        max_retry_attempts = 2
        retry_attempt = 1

        while retry_attempt <= max_retry_attempts:
            self._refresh_writer_runtime(chapter_number, self.max_iterations + retry_attempt, "recovery_attempt_start")
            base_attempt_budget = max(3, self.max_iterations)
            draft_scene = ""
            editor_output = ""
            final_scene = ""
            final_review_output = ""
            try:
                attempt_number = self.max_iterations + retry_attempt
                self._raise_if_pause_requested(
                    chapter_number,
                    chapter_title,
                    attempt_number,
                    "Pause requested; stopping before the next recovery attempt.",
                    "draft",
                )
                prompt_state = self._resolve_chapter_prompt_for_attempt(
                    chapter_number,
                    attempt_number,
                    "recovery",
                    active_prompt,
                )
                active_prompt = str(prompt_state.get("prompt") or active_prompt or "").strip()
                prompt_version = int(prompt_state.get("version", 0) or 0)
                details_summary = self._summarize_chapter_details(prompt_state.get("details") or {})
                if active_prompt_version is None:
                    active_prompt_version = prompt_version
                elif prompt_version != active_prompt_version:
                    active_prompt_version = prompt_version
                    last_error = ""
                    retry_guidance = ""
                    prior_editor_output = ""
                    prior_scene = ""
                    max_retry_attempts = max(max_retry_attempts, retry_attempt - 1 + base_attempt_budget)
                    self._log_block(
                        f"CHAPTER {chapter_number} | RECOVERY ATTEMPT {retry_attempt} | CHAPTER ADVICE APPLIED | V{prompt_version}",
                        details_summary or "The chapter prompt changed for this recovery attempt.",
                    )
                required_beats, target_word_count = self._extract_required_chapter_details(active_prompt)
                retry_context = ""
                if last_error:
                    retry_context = (
                        "\n\nRecovery Context:\n"
                        f"The previous attempt failed with: {last_error}"
                    )
                if retry_guidance:
                    retry_context += f"\n\n{retry_guidance}"
                if prior_editor_output:
                    retry_feedback_focus = self._build_retry_feedback_focus(
                        prior_editor_output,
                        prior_scene,
                        target_word_count,
                    )
                    if retry_feedback_focus:
                        retry_context += (
                            "\n\nTo the above, also consider this unresolved prior feedback:\n"
                            f"{retry_feedback_focus}"
                        )

                writer_prompt = self._build_writer_prompt(
                    chapter_number,
                    chapter_title,
                    active_prompt,
                    context,
                    required_beats,
                    target_word_count,
                    retry_context,
                    attempt_number,
                    prior_scene,
                    prior_editor_output,
                )
                self._raise_if_pause_requested(
                    chapter_number,
                    chapter_title,
                    attempt_number,
                    "Pause requested; stopping before recovery writer draft.",
                    "draft",
                )
                self._refresh_writer_runtime(chapter_number, attempt_number, "recovery_writer_draft")
                writer_output = self._run_agent_step(
                    chapter_number,
                    chapter_title,
                    attempt_number,
                    "recovery_writer_draft",
                    self.agents["writer"],
                    writer_prompt,
                    "draft",
                    "Writer is drafting the recovery chapter",
                )
                draft_scene = self._extract_story_block(
                    writer_output,
                    ["SCENE", "CHAPTER", "SCENE FINAL", "CHAPTER FINAL"],
                )
                draft_scene, draft_guard_note = self._apply_loop_guard(draft_scene, target_word_count)
                if draft_guard_note:
                    self._log_block(
                        f"CHAPTER {chapter_number} | RECOVERY ATTEMPT {retry_attempt} | STEP recovery_writer_draft | GUARDRAIL",
                        draft_guard_note,
                    )
                if not self._looks_like_story_text(draft_scene):
                    raise ValueError("Writer draft did not return usable prose")
                if self._is_repetitive_output(draft_scene):
                    raise ValueError("Writer draft contains repetitive looping output")
                draft_integrity_passed, draft_integrity_message = self._validate_prose_integrity(draft_scene)
                if not draft_integrity_passed:
                    raise ValueError(draft_integrity_message)

                editor_prompt = self._build_editor_prompt(
                    chapter_number,
                    chapter_title,
                    active_prompt,
                    context,
                    draft_scene,
                    required_beats,
                    target_word_count,
                )
                self._raise_if_pause_requested(
                    chapter_number,
                    chapter_title,
                    attempt_number,
                    "Pause requested; stopping before recovery editor review.",
                    "feedback",
                )
                editor_output = self._run_agent_step(
                    chapter_number,
                    chapter_title,
                    attempt_number,
                    "recovery_editor_review",
                    self.agents["editor"],
                    editor_prompt,
                    "feedback",
                    "Editor is reviewing the recovery draft",
                )
                editor_output = self._normalize_editor_output(editor_output)

                if self._draft_ready_for_final_check(
                    draft_scene,
                    editor_output,
                    required_beats,
                    target_word_count,
                ):
                    final_scene = draft_scene
                    self._log_block(
                        f"CHAPTER {chapter_number} | ATTEMPT {attempt_number} | STEP recovery_writer_final | SKIPPED",
                        "Recovery draft already satisfies the actionable beat, loop, sentence-length, prose-integrity, and word-count checks. Promoting draft directly to final review.",
                    )
                else:
                    self._refresh_writer_runtime(chapter_number, attempt_number, "recovery_writer_final")
                    writer_final = self._build_writer_agent("writer_final")
                    current_word_count_guidance = self._build_word_count_retry_guidance(draft_scene, target_word_count)
                    final_retry_context = self._strip_word_count_recovery_advice(retry_context)
                    if current_word_count_guidance:
                        final_retry_context = (
                            f"{final_retry_context}\n\n{current_word_count_guidance}".strip()
                            if final_retry_context
                            else current_word_count_guidance
                        )
                    final_prompt = self._build_writer_final_prompt(
                        chapter_number,
                        chapter_title,
                        active_prompt,
                        context,
                        draft_scene,
                        editor_output,
                        required_beats,
                        target_word_count,
                        final_retry_context,
                    )
                    self._raise_if_pause_requested(
                        chapter_number,
                        chapter_title,
                        attempt_number,
                        "Pause requested; stopping before recovery writer final revision.",
                        "revision",
                    )
                    final_output = self._run_agent_step(
                        chapter_number,
                        chapter_title,
                        attempt_number,
                        "recovery_writer_final",
                        writer_final,
                        final_prompt,
                        "revision",
                        "Writer final is revising the recovery chapter",
                    )
                    final_scene = self._extract_story_block(
                        final_output,
                        ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE", "SCENE", "CHAPTER"],
                    )
                    final_scene, final_guard_note = self._apply_loop_guard(final_scene, target_word_count)
                    if final_guard_note:
                        self._log_block(
                            f"CHAPTER {chapter_number} | RECOVERY ATTEMPT {retry_attempt} | STEP recovery_writer_final | GUARDRAIL",
                            final_guard_note,
                        )
                    if not self._looks_like_story_text(final_scene):
                        raise ValueError("Writer final did not return usable prose")
                    if self._is_repetitive_output(final_scene):
                        raise ValueError("Writer final contains repetitive looping output")
                    final_integrity_passed, final_integrity_message = self._validate_prose_integrity(final_scene)
                    if not final_integrity_passed:
                        raise ValueError(final_integrity_message)

                self._raise_if_pause_requested(
                    chapter_number,
                    chapter_title,
                    attempt_number,
                    "Pause requested; stopping before recovery final validation review.",
                    "feedback",
                )
                final_review_output = self._review_candidate_for_save(
                    chapter_number,
                    chapter_title,
                    active_prompt,
                    context,
                    final_scene,
                    required_beats,
                    target_word_count,
                    attempt_number,
                    "recovery_editor_final_check",
                    "Editor is validating the recovered final candidate",
                )
                prior_editor_output = final_review_output or editor_output

                if not self._verify_pipeline_result(
                    chapter_number,
                    draft_scene,
                    final_review_output,
                    final_scene,
                    required_beats,
                    target_word_count,
                    attempt_number,
                    "recovery",
                ):
                    raise ValueError(f"Chapter {chapter_number} generation incomplete")

                memory_update = self._finalize_chapter_result(
                    chapter_number,
                    chapter_title,
                    final_scene,
                    target_word_count,
                    attempt_number,
                )
                self._log_block(
                    f"CHAPTER {chapter_number} | RECOVERY",
                    "Deterministic recovery pipeline produced an accepted final chapter.",
                )
                return {
                    "memory_update": memory_update,
                    "draft_scene": draft_scene,
                    "editor_feedback": final_review_output or editor_output,
                    "final_scene": final_scene,
                }

            except GenerationPauseRequested:
                raise
            except Exception as e:
                last_error = str(e)
                prior_scene = self._select_best_chapter_candidate(
                    [draft_scene, final_scene, prior_scene],
                    target_word_count,
                ) or self._select_retry_guidance_source(draft_scene, final_scene, prior_scene)
                retry_guidance = self._build_word_count_retry_guidance(prior_scene, target_word_count)
                if "repetitive looping output" in last_error.lower() or "context length" in last_error.lower():
                    loop_guidance = self._build_loop_retry_guidance()
                    if loop_guidance not in retry_guidance:
                        retry_guidance = f"{retry_guidance}\n\n{loop_guidance}".strip() if retry_guidance else loop_guidance
                if "prohibited meta prose" in last_error.lower():
                    integrity_guidance = "\n".join([
                        "System Output Integrity Advice:",
                        "- Return prose only after the required tag.",
                        "- Do not include word-count claims, sequel notes, summaries, or commentary about what the chapter does.",
                        "- Do not mention the next chapter, the draft process, or the revision process inside the prose.",
                    ])
                    retry_guidance = f"{retry_guidance}\n\n{integrity_guidance}".strip() if retry_guidance else integrity_guidance
                self._log(f"Error in retry attempt for Chapter {chapter_number}: {last_error}")
                if retry_attempt < max_retry_attempts and self._is_recoverable_chapter_error(e):
                    self._log(
                        f"Deterministic recovery attempt failed for Chapter {chapter_number}; "
                        f"trying again with stronger guidance ({retry_attempt}/{max_retry_attempts})..."
                    )
                    retry_attempt += 1
                    continue
                self._log(traceback.format_exc().rstrip())
                self._log("Unable to generate chapter content after retry")
                raise

    def _extract_artifacts(self, messages: List[Dict]) -> Dict:
        artifacts = {
            "memory_update": "",
            "draft_scene": "",
            "editor_feedback": "",
            "final_scene": "",
        }
        for msg in messages:
            sender = self._get_sender(msg)
            content = msg.get("content", "")
            if sender == "memory_keeper" and "MEMORY UPDATE:" in content and not artifacts["memory_update"]:
                artifacts["memory_update"] = content.split("MEMORY UPDATE:", 1)[1].strip()
            elif sender == "writer" and not artifacts["draft_scene"]:
                draft_text = self._extract_story_candidate(content, ["SCENE", "CHAPTER"], allow_raw_fallback=True)
                if draft_text and self._looks_like_story_text(draft_text):
                    artifacts["draft_scene"] = draft_text
            elif sender == "editor" and not artifacts["editor_feedback"]:
                normalized_editor_feedback = self._normalize_editor_output(content)
                if normalized_editor_feedback:
                    artifacts["editor_feedback"] = normalized_editor_feedback
            elif sender in ["writer", "writer_final"]:
                final_text = self._extract_story_candidate(
                    content,
                    ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE"],
                    allow_raw_fallback=sender in {"writer", "writer_final"},
                )
                if final_text and self._looks_like_story_text(final_text):
                    artifacts["final_scene"] = final_text
        if not artifacts["final_scene"]:
            artifacts["final_scene"] = self._extract_final_scene(messages) or ""
        return artifacts

    def _attempt_recovery_expansion(
        self,
        chapter_number: int,
        prompt: str,
        artifacts: Dict[str, str],
        target_word_count: int,
    ) -> str:
        chapter_title = self.outline[chapter_number - 1]["title"]
        required_beats, _ = self._extract_required_chapter_details(prompt)
        base_scene = artifacts.get("final_scene") or artifacts.get("draft_scene") or ""
        if not base_scene:
            return ""

        actual_word_count, within_range, _ = self._validate_word_count(base_scene, target_word_count)
        if within_range or target_word_count <= 0:
            return ""

        minimum_word_count, maximum_word_count = self._word_count_bounds(target_word_count)
        if actual_word_count >= minimum_word_count:
            return ""

        expansion_guidance = "\n".join([
            "Recovery Expansion Context:",
            f"- The recovered chapter candidate is still too short at {actual_word_count} words.",
            f"- The required range is {minimum_word_count}-{maximum_word_count} words for the target of {target_word_count}.",
            "- Expand the existing chapter prose instead of restarting from scratch.",
            "- Preserve all current scene facts, ordering, and required beats while adding concrete material in thin moments.",
            "- Do not output notes, an apology, or meta commentary.",
        ])
        self._refresh_writer_runtime(chapter_number, self.max_iterations + 1, "retry_length_recovery")
        writer_final = self._build_writer_agent("writer_final")
        recovery_prompt = self._build_writer_final_prompt(
            chapter_number,
            chapter_title,
            prompt,
            self._prepare_chapter_context(chapter_number, prompt),
            base_scene,
            artifacts.get("editor_feedback", ""),
            required_beats,
            target_word_count,
            f"\n\n{expansion_guidance}",
        )
        recovery_output = self._run_agent_step(
            chapter_number,
            chapter_title,
            self.max_iterations + 1,
            "retry_length_recovery",
            writer_final,
            recovery_prompt,
            "revision",
            "Writer final is expanding the recovered chapter",
        )
        recovered_scene = self._extract_story_block(
            recovery_output,
            ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE", "SCENE", "CHAPTER"],
        )
        recovered_scene, guard_note = self._apply_loop_guard(recovered_scene, target_word_count)
        if guard_note:
            self._log_block(
                f"CHAPTER {chapter_number} | RECOVERY | LENGTH GUARDRAIL",
                guard_note,
            )
        if not recovered_scene or self._looks_like_plan_output(recovered_scene):
            return ""
        if self._is_repetitive_output(recovered_scene):
            return ""
        return recovered_scene

    def _process_chapter_results(self, chapter_number: int, prompt: str, messages: List[Dict], target_word_count: int = 0) -> Dict:
        """Process and save chapter results, updating memory"""
        try:
            chapter_title = self.outline[chapter_number - 1]["title"]
            context = self._prepare_chapter_context(chapter_number, prompt)
            required_beats, _ = self._extract_required_chapter_details(prompt)
            artifacts = self._extract_artifacts(messages)
            if artifacts["final_scene"] and self._looks_like_plan_output(artifacts["final_scene"]):
                artifacts["final_scene"] = ""
            if artifacts["draft_scene"] and self._looks_like_plan_output(artifacts["draft_scene"]):
                artifacts["draft_scene"] = ""
            best_candidate = self._select_best_chapter_candidate(
                [
                    artifacts.get("final_scene", ""),
                    artifacts.get("draft_scene", ""),
                    self._extract_best_chapter_candidate(messages),
                ],
                target_word_count,
            )
            if best_candidate and best_candidate != artifacts.get("final_scene", ""):
                self._log_block(
                    f"CHAPTER {chapter_number} | RECOVERY | BEST CANDIDATE",
                    "Selected the strongest available chapter candidate from retry artifacts instead of the raw SCENE FINAL tag.",
                )
            if best_candidate:
                artifacts["final_scene"] = best_candidate
            elif not artifacts["final_scene"]:
                artifacts["final_scene"] = artifacts["draft_scene"] or self._extract_best_chapter_candidate(messages)
            if artifacts["final_scene"] and target_word_count > 0:
                _, word_count_passed, _ = self._validate_word_count(artifacts["final_scene"], target_word_count)
                if not word_count_passed:
                    expanded_candidate = self._attempt_recovery_expansion(
                        chapter_number,
                        prompt,
                        artifacts,
                        target_word_count,
                    )
                    if expanded_candidate:
                        self._log_block(
                            f"CHAPTER {chapter_number} | RECOVERY | LENGTH RECOVERY",
                            "Expanded the recovered chapter candidate before saving because the best retry artifact was still outside the word-count range.",
                        )
                        artifacts["final_scene"] = expanded_candidate
            if not artifacts["final_scene"]:
                raise ValueError(f"No content found for Chapter {chapter_number}")

            final_review_output = self._review_candidate_for_save(
                chapter_number,
                chapter_title,
                prompt,
                context,
                artifacts["final_scene"],
                required_beats,
                target_word_count,
                self.max_iterations + 2,
                "recovery_editor_final_check",
                "Editor is validating the recovery candidate before save",
            )
            if not self._verify_pipeline_result(
                chapter_number,
                artifacts.get("draft_scene", ""),
                final_review_output,
                artifacts["final_scene"],
                required_beats,
                target_word_count,
                self.max_iterations + 2,
                "recovery",
            ):
                raise ValueError(f"Chapter {chapter_number} generation incomplete")

            memory_update = self._finalize_chapter_result(
                chapter_number,
                chapter_title,
                artifacts["final_scene"],
                target_word_count,
                self.max_iterations + 2,
            )
            artifacts["memory_update"] = memory_update
            artifacts["editor_feedback"] = final_review_output or artifacts.get("editor_feedback", "")
            return artifacts
            
        except Exception as e:
            self._log(f"Error processing chapter results: {str(e)}")
            self._log(traceback.format_exc().rstrip())
            raise

    def _save_chapter(self, chapter_number: int, chapter_content: str, target_word_count: int = 0) -> None:
        self._log(f"\nSaving Chapter {chapter_number}")
        try:
            if not chapter_content:
                raise ValueError(f"No content found for Chapter {chapter_number}")
                
            chapter_content = self._clean_chapter_content(chapter_content)
            if self._looks_like_plan_output(chapter_content):
                raise ValueError(f"Outline-like PLAN output returned instead of prose for Chapter {chapter_number}")
            if self._is_repetitive_output(chapter_content):
                raise ValueError(f"Detected repetitive looping output in Chapter {chapter_number}")
            prose_integrity_passed, prose_integrity_message = self._validate_prose_integrity(chapter_content)
            if not prose_integrity_passed:
                raise ValueError(prose_integrity_message)
            sentence_length_passed, sentence_length_message = self._validate_sentence_length(chapter_content)
            if not sentence_length_passed:
                raise ValueError(sentence_length_message)
            _, word_count_passed, word_count_message = self._validate_word_count(chapter_content, target_word_count)
            if not word_count_passed:
                raise ValueError(word_count_message)
            
            filename = os.path.join(self.output_dir, f"chapter_{chapter_number:02d}.txt")
            
            # Create backup if file exists
            if os.path.exists(filename):
                backup_filename = f"{filename}.backup"
                import shutil
                shutil.copy2(filename, backup_filename)
                
            with open(filename, "w", encoding='utf-8') as f:
                f.write(f"Chapter {chapter_number}\n\n{chapter_content}")
                
            # Verify file
            with open(filename, "r", encoding='utf-8') as f:
                saved_content = f.read()
                if len(saved_content.strip()) == 0:
                    raise IOError(f"File {filename} is empty")
                    
            self._log(f"Saved to: {filename}")
            self._log(prose_integrity_message)
            self._log(sentence_length_message)
            self._log(word_count_message)
            
        except Exception as e:
            self._log(f"Error saving chapter: {str(e)}")
            self._log(traceback.format_exc().rstrip())
            raise

    def generate_book(self, outline: List[Dict]) -> None:
        """Generate the book with strict chapter sequencing"""
        self._log("\nStarting Book Generation...")
        self._log(f"Total chapters: {len(outline)}")
        
        # Sort outline by chapter number
        sorted_outline = sorted(outline, key=lambda x: x["chapter_number"])
        
        for chapter in sorted_outline:
            chapter_number = chapter["chapter_number"]
            
            # Verify previous chapter exists and is valid
            if chapter_number > 1:
                prev_file = os.path.join(self.output_dir, f"chapter_{chapter_number-1:02d}.txt")
                if not os.path.exists(prev_file):
                    self._log(f"Previous chapter {chapter_number-1} not found. Stopping.")
                    break
                    
                # Verify previous chapter content
                with open(prev_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if not self._verify_chapter_content(content, chapter_number-1):
                        self._log(f"Previous chapter {chapter_number-1} content invalid. Stopping.")
                        break
            
            # Generate current chapter
            self._log(f"\n{'='*20} Chapter {chapter_number} {'='*20}")
            self.generate_chapter(chapter_number, chapter["prompt"])
            
            # Verify current chapter
            chapter_file = os.path.join(self.output_dir, f"chapter_{chapter_number:02d}.txt")
            if not os.path.exists(chapter_file):
                self._log(f"Failed to generate chapter {chapter_number}")
                break
                
            with open(chapter_file, 'r', encoding='utf-8') as f:
                content = f.read()
                if not self._verify_chapter_content(content, chapter_number):
                    self._log(f"Chapter {chapter_number} content invalid")
                    break
                    
            self._log(f"Chapter {chapter_number} complete")
            time.sleep(5)

    def _verify_chapter_content(self, content: str, chapter_number: int) -> bool:
        """Verify chapter content is valid"""
        if not content:
            return False
            
        # Check for chapter header
        if f"Chapter {chapter_number}" not in content:
            return False
            
        # Ensure content isn't just metadata
        lines = content.split('\n')
        content_lines = [line for line in lines if line.strip() and 'MEMORY UPDATE:' not in line]
        
        return len(content_lines) >= 3  # At least chapter header + 2 content lines
