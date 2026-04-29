"""Generate book outlines using AutoGen agents with improved error handling."""

import re
from typing import Callable, Dict, List, Optional

import autogen

class OutlineGenerator:
    def __init__(
        self,
        agents: Dict[str, autogen.ConversableAgent],
        agent_config: Dict,
        progress_callback: Optional[Callable[[Dict], None]] = None,
        monitor_callback: Optional[Callable[[Dict], None]] = None,
        diagnostic_logger: Optional[Callable[[str], None]] = None,
    ):
        self.agents = agents
        self.agent_config = agent_config
        self.progress_callback = progress_callback
        self.monitor_callback = monitor_callback
        self.diagnostic_logger = diagnostic_logger
        self.chapter_detail_event_fallbacks: Dict[int, List[str]] = {}

    def _log(self, message: str) -> None:
        print(message)
        if self.diagnostic_logger:
            self.diagnostic_logger(message)

    def _log_block(self, header: str, content: str) -> None:
        body = (content or "").rstrip() or "[empty]"
        divider = "=" * 20
        self._log(f"{divider} {header} {divider}\n{body}\n{divider} END {header} {divider}")

    def _log_agent_setup(self, agent: autogen.ConversableAgent, context_label: str) -> None:
        system_message = getattr(agent, "system_message", "") or "[no system message]"
        self._log_block(f"{context_label} | INPUT | SYSTEM | {agent.name}", system_message)

    def _emit_progress(self, agent: str, step: str, detail: str) -> None:
        if not self.progress_callback:
            return
        self.progress_callback({
            "chapter_number": 0,
            "chapter_title": "",
            "agent": agent,
            "step": step,
            "detail": detail,
            "output_stage": "outline",
            "iteration": 1,
            "max_iterations": 1,
        })

    def _emit_monitor(self, kind: str, label: str, text: str) -> None:
        if not self.monitor_callback:
            return
        self.monitor_callback({
            "kind": kind,
            "label": label,
            "text": text,
        })

    def _extract_last_content(self, chat_history: List[Dict]) -> str:
        for msg in reversed(chat_history):
            content = (msg.get("content") or "").strip()
            if content:
                return content
        return ""

    def _run_agent_step(self, step_name: str, agent: autogen.ConversableAgent, prompt: str, detail: str) -> str:
        self._emit_progress(agent.name, step_name, detail)
        label = f"OUTLINE | STEP {step_name} | {agent.name}"
        self._log_agent_setup(agent, f"OUTLINE | STEP {step_name}")
        self._log_block(f"OUTLINE | STEP {step_name} | INPUT | PROMPT | {agent.name}", prompt)
        self._emit_monitor("input", label, prompt)
        chat_result = self.agents["user_proxy"].initiate_chat(
            agent,
            clear_history=True,
            silent=True,
            max_turns=1,
            message=prompt,
        )
        output = self._extract_last_content(chat_result.chat_history)
        self._emit_monitor("output", label, output)
        self._log_block(f"OUTLINE | STEP {step_name} | OUTPUT | {agent.name}", output)
        return output

    def _build_chapter_beats_priority_note(self, initial_prompt: str) -> str:
        if "Chapter Details:" not in (initial_prompt or ""):
            return ""
        return "\n".join([
            "Mandatory Chapter Detail Guidance:",
            "- The book premise includes explicit chapter details and chapter beats.",
            "- Treat those chapter-specific beats as binding anchors for the story arc and the outline.",
            "- Do not ignore them, contradict them, move them to different chapters, merge them away, or replace them with broader substitutes.",
            "- The story arc must preserve and support the provided chapter beats chapter by chapter.",
            "- When chapter details include purpose, setting, tone, characters, must-include items, avoid items, or chapter guidance, use them to shape that chapter's outline choices.",
            "- For any chapter that includes multiple story beats, convert them into at least 3 distinct, specific key events for that chapter outline.",
            "- Key events may paraphrase the chapter beats naturally, but they must preserve the same concrete actions, reveals, and progression.",
            "- Do not collapse a multi-beat chapter into 1 or 2 generic summary bullets if the provided chapter beats support 3 or more concrete events.",
        ])

    def _extract_chapter_detail_event_fallbacks(self, initial_prompt: str) -> Dict[int, List[str]]:
        fallbacks: Dict[int, List[str]] = {}
        if not initial_prompt:
            return fallbacks

        for match in re.finditer(
            r"Chapter\s+(\d+)\s+Details:\s*(.*?)(?=\n\s*Chapter\s+\d+\s+Details:|\Z)",
            initial_prompt,
            re.IGNORECASE | re.DOTALL,
        ):
            chapter_number = int(match.group(1))
            section = match.group(2).strip()
            beats_match = re.search(
                r"Beats:\s*(.*?)(?=\n\s*(?:Purpose|Characters|Setting|Tone|Chapter Guidance|Must Include|Avoid|Target Word Count):|\Z)",
                section,
                re.IGNORECASE | re.DOTALL,
            )
            beats_text = beats_match.group(1).strip() if beats_match else ""
            event_items = self._derive_event_items_from_beats(beats_text)
            if event_items:
                fallbacks[chapter_number] = event_items
        return fallbacks

    def _derive_event_items_from_beats(self, beats_text: str) -> List[str]:
        if not beats_text:
            return []

        normalized = re.sub(r"\s+", " ", beats_text).strip()
        if not normalized:
            return []

        raw_parts: List[str] = []
        bullet_lines = [
            re.sub(r"^[-*\d\.\)\s]+", "", line.strip()).strip()
            for line in beats_text.splitlines()
            if line.strip()
        ]
        if len(bullet_lines) >= 3:
            raw_parts.extend(bullet_lines)
        else:
            raw_parts.extend(
                part.strip()
                for part in re.split(r"(?<=[.!?])\s+|;\s+|\s+-\s+", normalized)
                if part.strip()
            )

        cleaned_parts: List[str] = []
        seen = set()
        for part in raw_parts:
            item = re.sub(r"^[-*\d\.\)\s]+", "", part).strip(" \"'")
            item = re.sub(r"\s+", " ", item).strip()
            if len(item.split()) < 4:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_parts.append(item)

        return cleaned_parts[:6]

    def _build_story_planner_prompt(self, initial_prompt: str, num_chapters: int) -> str:
        chapter_beats_priority_note = self._build_chapter_beats_priority_note(initial_prompt)
        return f"""Create the high-level story arc for a {num_chapters}-chapter book.

Return only:
STORY_ARC:
[high-level story arc]

Requirements:
- Focus on major plot turns, character arcs, pacing, and transitions.
- Do not write chapter prose.
- Do not write the outline yet.
{chapter_beats_priority_note}

When chapter details are present:
- Mention the chapter-by-chapter progression clearly enough that the later outline can extract at least 3 specific key events for each chapter.
- Preserve the narrative intent of each chapter beat, but you may paraphrase naturally rather than copying beat text verbatim.

Book Premise:
{initial_prompt}"""

    def _build_world_builder_prompt(self, initial_prompt: str, story_arc: str) -> str:
        return f"""Create the world-building context needed for this book.

Return only:
WORLD_ELEMENTS:
[world-building notes]

Requirements:
- Identify the important settings, locations, and recurring world details.
- Support the story arc without writing chapter prose.

Book Premise:
{initial_prompt}

Story Arc:
{story_arc}"""

    def _build_outline_creator_prompt(
        self,
        initial_prompt: str,
        story_arc: str,
        world_elements: str,
        num_chapters: int,
    ) -> str:
        chapter_beats_priority_note = self._build_chapter_beats_priority_note(initial_prompt)
        return f"""Generate a complete {num_chapters}-chapter outline for this book.

Return the outline in this exact format:

OUTLINE:
Chapter 1: [Title]
Chapter Title: [Same title as above]
Key Events:
- [Event 1]
- [Event 2]
- [Event 3]
Character Developments: [specific character moments and changes]
Setting: [specific location and atmosphere]
Tone: [specific emotional and narrative tone]

[Repeat this exact structure for every remaining chapter through Chapter {num_chapters}]

END OF OUTLINE

Requirements:
- Include every chapter from 1 through {num_chapters}.
- Do not skip chapters, combine chapters, or leave placeholders.
- Every chapter must contain at least 3 specific key events.
- When chapter beats are provided for a chapter, derive those key events from the provided beats.
- When chapter details include purpose, setting, tone, characters, must-include items, avoid items, or chapter guidance, reflect them in that chapter's title, key events, character developments, setting, and tone.
- If a chapter beat paragraph contains multiple actions, reveals, or turns, split them into 3-5 concrete key events instead of collapsing them into 1-2 vague bullets.
- Faithful paraphrase is encouraged; exact beat wording is not required.
- Keep numbering sequential and titles clear.
- Output outline content only. Do not include STORY_ARC, WORLD_ELEMENTS, commentary, or notes.
{chapter_beats_priority_note}

Book Premise:
{initial_prompt}

Story Arc:
{story_arc}

World Elements:
{world_elements}"""

    def generate_outline(self, initial_prompt: str, num_chapters: int = 25) -> List[Dict]:
        """Generate a book outline based on initial prompt."""
        self._log("\nGenerating outline...")
        outline_output = ""
        self.chapter_detail_event_fallbacks = self._extract_chapter_detail_event_fallbacks(initial_prompt)
        try:
            story_prompt = self._build_story_planner_prompt(initial_prompt, num_chapters)
            story_arc = self._run_agent_step(
                "story_arc",
                self.agents["story_planner"],
                story_prompt,
                "Story planner is shaping the story arc",
            )

            world_prompt = self._build_world_builder_prompt(initial_prompt, story_arc)
            world_elements = self._run_agent_step(
                "world_building",
                self.agents["world_builder"],
                world_prompt,
                "World builder is defining settings",
            )

            outline_prompt = self._build_outline_creator_prompt(initial_prompt, story_arc, world_elements, num_chapters)
            outline_output = self._run_agent_step(
                "outline_draft",
                self.agents["outline_creator"],
                outline_prompt,
                "Outline creator is drafting chapters",
            )
            return self._process_outline_results([{"content": outline_output}], num_chapters)
        except Exception as e:
            self._log(f"Error generating outline: {str(e)}")
            if outline_output:
                return self._emergency_outline_processing([{"content": outline_output}], num_chapters)
            raise

    def _extract_outline_content(self, messages: List[Dict]) -> str:
        """Extract outline content from messages with better error handling."""
        self._log("Searching for outline content in messages...")

        for msg in reversed(messages):
            content = msg.get("content", "")
            if "OUTLINE:" in content:
                start_idx = content.find("OUTLINE:")
                end_idx = content.find("END OF OUTLINE")
                if start_idx != -1:
                    if end_idx != -1:
                        return content[start_idx:end_idx].strip()
                    return content[start_idx:].strip()
            if "START OF OUTLINE" in content:
                start_idx = content.find("START OF OUTLINE")
                end_idx = content.find("END OF OUTLINE")
                if start_idx != -1:
                    if end_idx != -1:
                        return content[start_idx:end_idx].strip()
                    return content[start_idx:].strip()

        for msg in reversed(messages):
            content = msg.get("content", "")
            if (
                re.search(r'(?:CH?A?P?T?E?R)\s*1\s*:', content, re.IGNORECASE)
                or re.search(r'(?:CH?A?P?T?E?R)\s*1\s*-\s*', content, re.IGNORECASE)
                or "**Chapter 1:**" in content
                or re.search(r'^\s*1\.\s*Chapter\s+Title\s*:', content, re.IGNORECASE | re.MULTILINE)
            ):
                return content

        return ""

    def _extract_chapter_title(self, section: str, chapter_number: int) -> str:
        title_patterns = [
            r'\*?\*?Chapter Title:\*?\*?\s*(.+?)(?=\n|$)',
            r'\*?\*?Title:\*?\*?\s*(.+?)(?=\n|$)',
            rf'(?:CH?A?P?T?E?R)\s*{chapter_number}\s*:\s*(.+?)(?=\n|$)',
            rf'(?:CH?A?P?T?E?R)\s*{chapter_number}\s*-\s*(.+?)(?=\n|$)',
        ]
        for pattern in title_patterns:
            match = re.search(pattern, section, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return f"Chapter {chapter_number}"

    def _extract_section_block(self, section: str, labels: List[str], stop_labels: List[str]) -> str:
        label_group = "|".join(re.escape(label) for label in labels)
        if stop_labels:
            stop_group = "|".join(re.escape(label) for label in stop_labels)
            pattern = rf'^\s*(?:{label_group})\s*:?\s*(.*?)(?=^\s*(?:{stop_group})\s*:?\s*|\Z)'
        else:
            pattern = rf'^\s*(?:{label_group})\s*:?\s*(.*?)(?=\Z)'
        match = re.search(pattern, section, re.IGNORECASE | re.DOTALL | re.MULTILINE)
        if not match:
            return ""
        return match.group(1).strip()

    def _normalize_bullets(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        normalized = []
        for line in lines:
            if line.startswith(("-", "*")):
                normalized.append(f"- {line.lstrip('-* ').strip()}")
            else:
                normalized.append(f"- {line}")
        return "\n".join(normalized)

    def _extract_event_items(self, normalized_events: str) -> List[str]:
        events = re.findall(r'-\s*(.+?)(?=\n|$)', normalized_events or "")
        cleaned_events = [" ".join(event.split()) for event in events if event.strip()]
        return cleaned_events

    def _build_chapter_prompt(
        self,
        chapter_number: int,
        title: str,
        normalized_events: str,
        character_text: str,
        setting_text: str,
        tone_text: str,
    ) -> Dict:
        event_items = self._extract_event_items(normalized_events)
        if len(event_items) < 3:
            fallback_items = self.chapter_detail_event_fallbacks.get(chapter_number, [])
            for fallback_item in fallback_items:
                if len(event_items) >= 3:
                    break
                normalized_key = " ".join(fallback_item.lower().split())
                existing = {" ".join(item.lower().split()) for item in event_items}
                if normalized_key in existing:
                    continue
                event_items.append(fallback_item)
        if not event_items:
            raise ValueError(f"Chapter {chapter_number} ('{title}') has no recoverable key events.")
        if len(event_items) < 3:
            raise ValueError(
                f"Chapter {chapter_number} ('{title}') has only {len(event_items)} recoverable key events."
            )

        return {
            "chapter_number": chapter_number,
            "title": title,
            "prompt": "\n".join([
                f"- Key Events: {self._normalize_bullets(chr(10).join(event_items))}",
                f"- Character Developments: {character_text or 'Character progression inferred from chapter context.'}",
                f"- Setting: {setting_text or 'Setting inferred from chapter context.'}",
                f"- Tone: {tone_text or 'Tone inferred from chapter context.'}",
            ]),
        }

    def _normalize_prompt_value(self, prompt: object) -> str:
        if isinstance(prompt, list):
            return "\n".join(str(item).strip() for item in prompt if str(item).strip())
        if prompt is None:
            return ""
        return str(prompt).strip()

    def _finalize_emergency_chapter(self, chapter: Dict | None) -> Dict | None:
        if not chapter:
            return None
        finalized = dict(chapter)
        finalized["prompt"] = self._normalize_prompt_value(finalized.get("prompt", ""))
        if not finalized["prompt"]:
            return None
        return finalized

    def _build_prompt_from_simple_section(self, section: str) -> str:
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        summary_lines = []
        setting = ""
        tone = ""
        character = ""
        for line in lines:
            lower = line.lower()
            if lower.startswith("setting:"):
                setting = line.split(":", 1)[1].strip()
            elif lower.startswith("world elements:"):
                setting = (setting + " " + line.split(":", 1)[1].strip()).strip()
            elif lower.startswith("tone:"):
                tone = line.split(":", 1)[1].strip()
            elif lower.startswith("character"):
                character = line.split(":", 1)[1].strip()
            elif ":" not in line or lower.startswith(("key events", "event")):
                summary_lines.append(line.replace("Key Events:", "").strip())

        event_lines = [line for line in summary_lines if line]
        if not event_lines:
            return ""

        return "\n".join([
            f"- Key Events: {self._normalize_bullets(chr(10).join(event_lines))}",
            f"- Character Developments: {character or 'Character progression implied by chapter summary.'}",
            f"- Setting: {setting or 'Setting inferred from chapter summary.'}",
            f"- Tone: {tone or 'Tone inferred from chapter summary.'}",
        ])

    def _extract_numbered_outline_partial(self, outline_content: str, log_errors: bool = True) -> List[Dict]:
        chapters = []
        pattern = re.compile(
            r'^\s*(\d+)\.\s*Chapter\s+Title\s*:\s*["â€œ]?(.+?)["â€]?\s*$',
            re.IGNORECASE | re.MULTILINE,
        )
        matches = list(pattern.finditer(outline_content))
        if not matches:
            return []

        for index, match in enumerate(matches):
            try:
                chapter_number = int(match.group(1))
                title = match.group(2).strip()
                start = match.end()
                end = matches[index + 1].start() if index + 1 < len(matches) else len(outline_content)
                section = outline_content[start:end]

                events_text = self._extract_section_block(
                    section,
                    ["- Key Events", "Key Events", "Events", "Scenes", "Scene Beats"],
                    ["- Character Developments", "Character Developments", "- Setting/World Elements", "Setting/World Elements", "- Setting", "Setting", "- Tone", "Tone"],
                )
                character_text = self._extract_section_block(
                    section,
                    ["- Character Developments", "Character Developments", "Characters"],
                    ["- Setting/World Elements", "Setting/World Elements", "- Setting", "Setting", "- Tone", "Tone"],
                )
                setting_text = self._extract_section_block(
                    section,
                    ["- Setting/World Elements", "Setting/World Elements", "- Setting", "Setting", "World Elements", "Location"],
                    ["- Character Developments", "Character Developments", "Characters", "- Tone", "Tone"],
                )
                tone_text = self._extract_section_block(
                    section,
                    ["- Tone", "Tone", "Mood"],
                    [],
                )

                normalized_events = self._normalize_bullets(events_text) if events_text else ""
                if normalized_events:
                    chapters.append(
                        self._build_chapter_prompt(
                            chapter_number,
                            title,
                            normalized_events,
                            character_text,
                            setting_text,
                            tone_text,
                        )
                    )
            except Exception as e:
                if log_errors:
                    self._log(f"Error processing Chapter {match.group(1)}: {str(e)}")
                continue

        return chapters

    def _extract_outline_chapters(self, outline_content: str, num_chapters: int, log_errors: bool = True) -> List[Dict]:
        numbered_outline = self._extract_numbered_outline_partial(outline_content, log_errors=log_errors)
        if numbered_outline:
            return numbered_outline

        chapters = []
        chapter_pattern = re.compile(r'((?:CH?A?P?T?E?R)\s*\d+\s*(?::|-))', re.IGNORECASE)
        split_sections = chapter_pattern.split(outline_content)
        chapter_sections = []
        for i in range(1, len(split_sections), 2):
            header = split_sections[i]
            body = split_sections[i + 1] if i + 1 < len(split_sections) else ""
            chapter_sections.append((header, body))

        for header, body in chapter_sections:
            try:
                chapter_match = re.search(r'(?:CH?A?P?T?E?R)\s*(\d+)', header, re.IGNORECASE)
                if not chapter_match:
                    continue
                chapter_number = int(chapter_match.group(1))
                section = f"{header}{body}"
                title = self._extract_chapter_title(section, chapter_number)
                events_text = self._extract_section_block(
                    section,
                    ["Key Events", "Events", "Scenes", "Scene Beats"],
                    ["Character Developments", "Characters", "Setting", "World Elements", "Tone", "Chapter Title", "Title", "CHAPTER", "CHAPER"],
                )
                character_text = self._extract_section_block(
                    section,
                    ["Character Developments", "Characters", "Character Arcs"],
                    ["Setting", "World Elements", "Tone", "CHAPTER", "CHAPER"],
                )
                setting_text = self._extract_section_block(
                    section,
                    ["Setting", "World Elements", "Location"],
                    ["Character Developments", "Characters", "Tone", "CHAPTER", "CHAPER"],
                )
                tone_text = self._extract_section_block(
                    section,
                    ["Tone", "Mood"],
                    ["CHAPTER", "CHAPER"],
                )

                if not events_text:
                    simple_prompt = self._build_prompt_from_simple_section(section)
                    if simple_prompt:
                        chapters.append({
                            "chapter_number": chapter_number,
                            "title": title,
                            "prompt": simple_prompt,
                        })
                    else:
                        if log_errors:
                            self._log(f"Unable to salvage Chapter {chapter_number}")
                    continue

                normalized_events = self._normalize_bullets(events_text)
                chapters.append(
                    self._build_chapter_prompt(
                        chapter_number,
                        title,
                        normalized_events,
                        character_text,
                        setting_text,
                        tone_text,
                    )
                )

            except Exception as e:
                if log_errors:
                    self._log(f"Error processing Chapter {chapter_number}: {str(e)}")
                continue

        return chapters

    def _process_outline_results(self, messages: List[Dict], num_chapters: int) -> List[Dict]:
        """Extract and process the outline with strict format requirements."""
        outline_content = self._extract_outline_content(messages)

        if not outline_content:
            self._log("No structured outline found, attempting emergency processing...")
            return self._emergency_outline_processing(messages, num_chapters)

        chapters = self._extract_outline_chapters(outline_content, num_chapters, log_errors=True)

        if chapters:
            self._log(f"Partially processed {len(chapters)} valid chapters out of {num_chapters}")
            return self._verify_chapter_sequence(chapters, num_chapters)

        raise ValueError(f"Only processed {len(chapters)} valid chapters out of {num_chapters} required")

    def _verify_chapter_sequence(self, chapters: List[Dict], num_chapters: int) -> List[Dict]:
        """Verify and normalize chapter numbering."""
        chapter_map = {}
        duplicate_numbers = set()
        for chapter in sorted(chapters, key=lambda x: x['chapter_number']):
            chapter_number = int(chapter.get('chapter_number', 0) or 0)
            if not (1 <= chapter_number <= num_chapters):
                continue
            if chapter_number in chapter_map:
                duplicate_numbers.add(chapter_number)
                continue
            normalized_chapter = dict(chapter)
            normalized_chapter["prompt"] = self._normalize_prompt_value(normalized_chapter.get("prompt", ""))
            if normalized_chapter["prompt"]:
                chapter_map[chapter_number] = normalized_chapter

        if duplicate_numbers:
            self._log(f"Duplicate outline chapters detected and rejected: {sorted(duplicate_numbers)}")

        ordered = []
        for chapter_number in range(1, num_chapters + 1):
            if chapter_number in chapter_map:
                ordered.append(chapter_map[chapter_number])
            else:
                ordered.append({
                    "chapter_number": chapter_number,
                    "title": f"Chapter {chapter_number}",
                    "prompt": "- Key events: [To be determined]\n- Character developments: [To be determined]\n- Setting: [To be determined]\n- Tone: [To be determined]",
                })

        return ordered

    def _emergency_outline_processing(self, messages: List[Dict], num_chapters: int) -> List[Dict]:
        """Emergency processing when normal outline extraction fails."""
        self._log("Attempting emergency outline processing...")

        chapters = []
        current_chapter = None

        for msg in messages:
            content = msg.get("content", "")
            lines = content.split('\n')

            for line in lines:
                chapter_match = re.search(r'(?:CH?A?P?T?E?R)\s*(\d+)', line, re.IGNORECASE)
                has_events_block = bool(re.search(r"Key\s+Events\s*:?", content, re.IGNORECASE))
                if chapter_match and has_events_block:
                    finalized_chapter = self._finalize_emergency_chapter(current_chapter)
                    if finalized_chapter:
                        chapters.append(finalized_chapter)

                    current_chapter = {
                        'chapter_number': int(chapter_match.group(1)),
                        'title': self._extract_chapter_title(line, int(chapter_match.group(1))),
                        'prompt': []
                    }

                if current_chapter and line.strip():
                    stripped_line = line.strip()
                    if stripped_line.startswith(("-", "*")):
                        current_chapter['prompt'].append(stripped_line)
                    elif re.match(r"^(Key Events|Character Developments|Setting|World Elements|Tone)\s*:?", stripped_line, re.IGNORECASE):
                        current_chapter['prompt'].append(stripped_line)
                    elif current_chapter['prompt']:
                        current_chapter['prompt'].append(f"- {stripped_line}")

            finalized_chapter = self._finalize_emergency_chapter(current_chapter)
            if finalized_chapter:
                chapters.append(finalized_chapter)
            current_chapter = None

        if not chapters:
            self._log("Emergency processing failed to find any chapters")
            chapters = [
                {
                    "chapter_number": i,
                    "title": f"Chapter {i}",
                    "prompt": "- Key events: [To be determined]\n- Character developments: [To be determined]\n- Setting: [To be determined]\n- Tone: [To be determined]",
                }
                for i in range(1, num_chapters + 1)
            ]

        return self._verify_chapter_sequence(chapters, num_chapters)
