"""Generate book outlines using AutoGen agents with improved error handling"""
import autogen
from typing import Callable, Dict, List, Optional
import re
import threading
import time

class OutlineGenerator:
    def __init__(
        self,
        agents: Dict[str, autogen.ConversableAgent],
        agent_config: Dict,
        progress_callback: Optional[Callable[[Dict], None]] = None,
    ):
        self.agents = agents
        self.agent_config = agent_config
        self.progress_callback = progress_callback

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

    def _log_console_message(self, sender: str, content: str) -> None:
        snippet = " ".join(content.strip().split())
        if len(snippet) > 220:
            snippet = snippet[:217] + "..."
        print(f"[outline:{sender}] {snippet}")

    def _get_sender(self, msg: Dict) -> str:
        """Helper to get sender from message regardless of format"""
        return msg.get("sender") or msg.get("name", "")

    def _monitor_groupchat(self, groupchat: autogen.GroupChat, stop_event: threading.Event) -> None:
        seen = 0
        while not stop_event.is_set():
            messages = list(groupchat.messages)
            while seen < len(messages):
                msg = messages[seen]
                sender = self._get_sender(msg)
                if sender and sender != "user_proxy":
                    content = msg.get("content", "")
                    details = {
                        "story_planner": ("story_arc", "Story planner is shaping the story arc"),
                        "world_builder": ("world_building", "World builder is defining settings"),
                        "outline_creator": ("outline_draft", "Outline creator is drafting chapters"),
                    }.get(sender, ("working", f"{sender} is working on the outline"))
                    self._emit_progress(sender, details[0], details[1])
                    self._log_console_message(sender, content)
                seen += 1
            time.sleep(0.4)

    def generate_outline(self, initial_prompt: str, num_chapters: int = 25) -> List[Dict]:
        """Generate a book outline based on initial prompt"""
        print("\nGenerating outline...")

        
        groupchat = autogen.GroupChat(
            agents=[
                self.agents["user_proxy"],
                self.agents["story_planner"],
                self.agents["world_builder"],
                self.agents["outline_creator"]
            ],
            messages=[],
            max_round=4,
            speaker_selection_method="round_robin"
        )
        
        manager = autogen.GroupChatManager(groupchat=groupchat, llm_config=self.agent_config)

        outline_prompt = f"""Let's create a {num_chapters}-chapter outline for a book with the following premise:

{initial_prompt}

Process:
1. Story Planner: Create a high-level story arc and major plot points
2. World Builder: Suggest key settings and world elements needed
3. Outline Creator: Generate a detailed outline with chapter titles and prompts

Start with Chapter 1 and number chapters sequentially.

Make sure each chapter contains at least 3 key events or scene beats.

[Continue with remaining chapters]

Please output all chapters, do not leave out any chapters. Think through every chapter carefully, none should be to be determined later
It is of utmost importance that you detail out every chapter, do not combine chapters, or leave any out
There should be clear content for each chapter. There should be a total of {num_chapters} chapters.

If you cannot follow the exact preferred format, still output clearly labeled chapters with:
- chapter number and title
- key events
- character developments
- setting or world elements
- tone

End the outline with 'END OF OUTLINE'"""

        try:
            stop_event = threading.Event()
            monitor = threading.Thread(
                target=self._monitor_groupchat,
                args=(groupchat, stop_event),
                daemon=True,
            )
            monitor.start()
            # Initiate the chat
            try:
                self.agents["user_proxy"].initiate_chat(
                    manager,
                    message=outline_prompt
                )
            finally:
                stop_event.set()
                monitor.join(timeout=1)

            # Extract the outline from the chat messages
            return self._process_outline_results(groupchat.messages, num_chapters)
            
        except Exception as e:
            print(f"Error generating outline: {str(e)}")
            # Try to salvage any outline content we can find
            return self._emergency_outline_processing(groupchat.messages, num_chapters)

    def _extract_outline_content(self, messages: List[Dict]) -> str:
        """Extract outline content from messages with better error handling"""
        print("Searching for outline content in messages...")
        
        # Look for content between "OUTLINE:" and "END OF OUTLINE"
        for msg in reversed(messages):
            content = msg.get("content", "")
            if "OUTLINE:" in content:
                # Extract content between OUTLINE: and END OF OUTLINE
                start_idx = content.find("OUTLINE:")
                end_idx = content.find("END OF OUTLINE")
                
                if start_idx != -1:
                    if end_idx != -1:
                        return content[start_idx:end_idx].strip()
                    else:
                        # If no END OF OUTLINE marker, take everything after OUTLINE:
                        return content[start_idx:].strip()
                        
        # Fallback: look for content with chapter markers
        for msg in reversed(messages):
            content = msg.get("content", "")
            if (
                re.search(r'(?:CH?A?P?T?E?R)\s*1\s*:', content, re.IGNORECASE)
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
            pattern = rf'^\s*(?:{label_group})\s*:?\s*(.*?)(?=^\s*(?:{stop_group})\s*:|\Z)'
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
        if len(event_lines) < 3 and event_lines:
            while len(event_lines) < 3:
                event_lines.append(event_lines[-1])

        if not event_lines:
            return ""

        return "\n".join([
            f"- Key Events: {self._normalize_bullets(chr(10).join(event_lines))}",
            f"- Character Developments: {character or 'Character progression implied by chapter summary.'}",
            f"- Setting: {setting or 'Setting inferred from chapter summary.'}",
            f"- Tone: {tone or 'Tone inferred from chapter summary.'}",
        ])

    def _extract_numbered_outline(self, outline_content: str, num_chapters: int) -> List[Dict]:
        chapters = []
        pattern = re.compile(
            r'^\s*(\d+)\.\s*Chapter\s+Title\s*:\s*["“]?(.+?)["”]?\s*$',
            re.IGNORECASE | re.MULTILINE,
        )
        matches = list(pattern.finditer(outline_content))
        if not matches:
            return []

        for index, match in enumerate(matches):
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
                ["- Tone", "Tone"],
            )
            tone_text = self._extract_section_block(
                section,
                ["- Tone", "Tone", "Mood"],
                [],
            )

            normalized_events = self._normalize_bullets(events_text) if events_text else ""
            events = re.findall(r'-\s*(.+?)(?=\n|$)', normalized_events)
            if len(events) < 3 and events:
                while len(events) < 3:
                    events.append(events[-1])
                normalized_events = self._normalize_bullets("\n".join(events))

            if normalized_events:
                chapters.append({
                    "chapter_number": chapter_number,
                    "title": title,
                    "prompt": "\n".join([
                        f"- Key Events: {normalized_events}",
                        f"- Character Developments: {character_text or 'Character progression inferred from chapter context.'}",
                        f"- Setting: {setting_text or 'Setting inferred from chapter context.'}",
                        f"- Tone: {tone_text or 'Tone inferred from chapter context.'}",
                    ]),
                })

        return self._verify_chapter_sequence(chapters, num_chapters) if chapters else []

    def _process_outline_results(self, messages: List[Dict], num_chapters: int) -> List[Dict]:
        """Extract and process the outline with strict format requirements"""
        outline_content = self._extract_outline_content(messages)
        
        if not outline_content:
            print("No structured outline found, attempting emergency processing...")
            return self._emergency_outline_processing(messages, num_chapters)

        numbered_outline = self._extract_numbered_outline(outline_content, num_chapters)
        if numbered_outline:
            return numbered_outline

        chapters = []
        chapter_pattern = re.compile(r'((?:CH?A?P?T?E?R)\s*\d+\s*:)', re.IGNORECASE)
        split_sections = chapter_pattern.split(outline_content)
        chapter_sections = []
        for i in range(1, len(split_sections), 2):
            header = split_sections[i]
            body = split_sections[i + 1] if i + 1 < len(split_sections) else ""
            chapter_sections.append((header, body))

        for i, (header, body) in enumerate(chapter_sections, 1):
            try:
                section = f"{header}{body}"
                title = self._extract_chapter_title(section, i)
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
                    ["Tone", "CHAPTER", "CHAPER"],
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
                            "chapter_number": i,
                            "title": title,
                            "prompt": simple_prompt,
                        })
                    else:
                        print(f"Unable to salvage Chapter {i}")
                    continue

                normalized_events = self._normalize_bullets(events_text)
                chapter_info = {
                    "chapter_number": i,
                    "title": title,
                    "prompt": "\n".join([
                        f"- Key Events: {normalized_events}",
                        f"- Character Developments: {character_text or 'Character progression inferred from chapter context.'}",
                        f"- Setting: {setting_text or 'Setting inferred from chapter context.'}",
                        f"- Tone: {tone_text or 'Tone inferred from chapter context.'}"
                    ])
                }
                
                # Verify events (at least 3)
                events = re.findall(r'-\s*(.+?)(?=\n|$)', normalized_events)
                if len(events) < 3:
                    if events:
                        while len(events) < 3:
                            events.append(events[-1])
                        chapter_info["prompt"] = "\n".join([
                            f"- Key Events: {self._normalize_bullets(chr(10).join(events))}",
                            f"- Character Developments: {character_text or 'Character progression inferred from chapter context.'}",
                            f"- Setting: {setting_text or 'Setting inferred from chapter context.'}",
                            f"- Tone: {tone_text or 'Tone inferred from chapter context.'}"
                        ])
                    else:
                        print(f"Chapter {i} has no recoverable events")
                        continue

                chapters.append(chapter_info)

            except Exception as e:
                print(f"Error processing Chapter {i}: {str(e)}")
                continue

        if chapters:
            print(f"Partially processed {len(chapters)} valid chapters out of {num_chapters}")
            return self._verify_chapter_sequence(chapters, num_chapters)

        raise ValueError(f"Only processed {len(chapters)} valid chapters out of {num_chapters} required")

    def _verify_chapter_sequence(self, chapters: List[Dict], num_chapters: int) -> List[Dict]:
        """Verify and fix chapter numbering"""
        chapter_map = {}
        for chapter in sorted(chapters, key=lambda x: x['chapter_number']):
            chapter_number = int(chapter.get('chapter_number', 0) or 0)
            if 1 <= chapter_number <= num_chapters and chapter_number not in chapter_map:
                chapter_map[chapter_number] = chapter

        ordered = []
        for chapter_number in range(1, num_chapters + 1):
            if chapter_number in chapter_map:
                ordered.append(chapter_map[chapter_number])
            else:
                ordered.append({
                    'chapter_number': chapter_number,
                    'title': f'Chapter {chapter_number}',
                    'prompt': '- Key events: [To be determined]\n- Character developments: [To be determined]\n- Setting: [To be determined]\n- Tone: [To be determined]'
                })
        return ordered

    def _emergency_outline_processing(self, messages: List[Dict], num_chapters: int) -> List[Dict]:
        """Emergency processing when normal outline extraction fails"""
        print("Attempting emergency outline processing...")
        
        chapters = []
        current_chapter = None
        
        # Look through all messages for any chapter content
        for msg in messages:
            content = msg.get("content", "")
            lines = content.split('\n')
            
            for line in lines:
                # Look for chapter markers
                chapter_match = re.search(r'(?:CH?A?P?T?E?R)\s*(\d+)', line, re.IGNORECASE)
                if chapter_match and re.search(r"Key\s+Events\s*:", content, re.IGNORECASE):
                    if current_chapter:
                        chapters.append(current_chapter)
                    
                    current_chapter = {
                        'chapter_number': int(chapter_match.group(1)),
                        'title': line.split(':')[-1].strip() if ':' in line else f"Chapter {chapter_match.group(1)}",
                        'prompt': []
                    }
                
                # Collect bullet points
                if current_chapter and line.strip().startswith('-'):
                    current_chapter['prompt'].append(line.strip())
            
            # Add the last chapter if it exists
            if current_chapter and current_chapter.get('prompt'):
                current_chapter['prompt'] = '\n'.join(current_chapter['prompt'])
                chapters.append(current_chapter)
                current_chapter = None
        
        if not chapters:
            print("Emergency processing failed to find any chapters")
            # Create a basic outline structure
            chapters = [
                {
                    'chapter_number': i,
                    'title': f'Chapter {i}',
                    'prompt': '- Key events: [To be determined]\n- Character developments: [To be determined]\n- Setting: [To be determined]\n- Tone: [To be determined]'
                }
                for i in range(1, num_chapters + 1)
            ]
        
        # Ensure proper sequence and number of chapters
        return self._verify_chapter_sequence(chapters, num_chapters)
