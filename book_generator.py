import os
import re
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional

import autogen

from config import OUTPUT_FOLDER

class BookGenerator:
    def __init__(
        self,
        agents: Dict[str, autogen.ConversableAgent],
        agent_config: Dict,
        outline: List[Dict],
        max_iterations: int = 5,
        progress_callback: Optional[Callable[[Dict], None]] = None,
    ):
        """Initialize with outline to maintain chapter count context"""
        self.agents = agents
        self.agent_config = agent_config
        self.output_dir = OUTPUT_FOLDER
        self.chapters_memory = []  # Store chapter summaries
        self.max_iterations = max(1, max_iterations)
        self.outline = outline  # Store the outline
        self.progress_callback = progress_callback
        os.makedirs(self.output_dir, exist_ok=True)

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

    def _log_console_message(self, chapter_number: int, sender: str, content: str) -> None:
        snippet = " ".join(content.strip().split())
        if len(snippet) > 260:
            snippet = snippet[:257] + "..."
        print(f"[chapter {chapter_number}:{sender}] {snippet}")

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
        # Remove chapter number references
        content = re.sub(r'\*?\s*\(Chapter \d+.*?\)', '', content)
        content = re.sub(r'\*?\s*Chapter \d+.*?\n', '', content, count=1)
        
        # Clean up any remaining markdown artifacts
        content = content.replace('*', '')
        content = content.strip()
        
        return content

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
    

    def initiate_group_chat(self) -> autogen.GroupChat:
        """Create a new group chat for the agents with improved speaking order"""
        outline_context = "\n".join([
            f"\nChapter {ch['chapter_number']}: {ch['title']}\n{ch['prompt']}"
            for ch in sorted(self.outline, key=lambda x: x['chapter_number'])
        ])

        messages = [{
            "role": "system",
            "content": f"Complete Book Outline:\n{outline_context}"
        }]

        writer_final = autogen.AssistantAgent(
            name="writer_final",
            system_message=self.agents["writer"].system_message,
            llm_config=self.agent_config
        )
        
        return autogen.GroupChat(
            agents=[
                self.agents["user_proxy"],
                self.agents["memory_keeper"],
                self.agents["writer"],
                self.agents["editor"],
                writer_final
            ],
            messages=messages,
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
            r"Required Chapter Details:\s*(.*?)(?:\n\s*Additional guidance for this chapter:|\Z)",
            prompt,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return [], 0
        details_block = match.group(1).strip()
        word_count_match = re.search(r"Target Word Count:\s*(\d+)", details_block, re.IGNORECASE)
        target_word_count = int(word_count_match.group(1)) if word_count_match else 0
        beats_block_match = re.search(r"Beats:\s*(.*)", details_block, re.IGNORECASE | re.DOTALL)
        beats_block = beats_block_match.group(1).strip() if beats_block_match else details_block
        beats: List[str] = []
        for line in beats_block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            stripped = re.sub(r"^[-*\d\.\)\s]+", "", stripped).strip()
            if stripped and not stripped.lower().startswith("target word count:"):
                beats.append(stripped)
        return beats, target_word_count

    def _verify_chapter_complete(self, messages: List[Dict], required_beats: Optional[List[str]] = None) -> bool:
        """Verify chapter completion by analyzing entire conversation context"""
        print("******************** VERIFYING CHAPTER COMPLETION ****************")
        current_chapter = None
        chapter_content = None
        beat_check_seen = not required_beats
        beat_check_passed = not required_beats
        loop_check_passed = True
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
            if "BEAT CHECK:" in content:
                beat_check_seen = True
            if "BEAT CHECK RESULT: PASS" in content.upper():
                beat_check_passed = True
            if "BEAT CHECK RESULT: FAIL" in content.upper():
                beat_check_passed = False
            if "LOOP CHECK RESULT: FAIL" in content.upper():
                loop_check_passed = False
            final_text = self._extract_tagged_content(content, ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE"])
            if final_text:
                sequence_complete['scene_final'] = True
                chapter_content = final_text
            if "**Confirmation:**" in content and "successfully" in content:
                sequence_complete['confirmation'] = True

            #print all sequence_complete flags
            print("******************** SEQUENCE COMPLETE **************", sequence_complete)
            print("******************** CURRENT_CHAPTER ****************", current_chapter)
            print("******************** CHAPTER_CONTENT ****************", chapter_content)
            print("******************** BEAT CHECK SEEN ***************", beat_check_seen)
            print("******************** BEAT CHECK PASSED *************", beat_check_passed)
            print("******************** LOOP CHECK PASSED *************", loop_check_passed)

        # Verify all steps completed and content exists
        if chapter_content and self._is_repetitive_output(chapter_content):
            loop_check_passed = False
        return bool(current_chapter and chapter_content and beat_check_seen and beat_check_passed and loop_check_passed)
    
    def _prepare_chapter_context(self, chapter_number: int, prompt: str) -> str:
        """Prepare context for chapter generation"""
        if chapter_number == 1:
            return f"Initial Chapter\nRequirements:\n{prompt}"
            
        context_parts = [
            "Previous Chapter Summaries:",
            *[f"Chapter {i+1}: {summary}" for i, summary in enumerate(self.chapters_memory)],
            "\nCurrent Chapter Requirements:",
            prompt
        ]
        return "\n".join(context_parts)

    def generate_chapter(self, chapter_number: int, prompt: str) -> Dict:
        """Generate a single chapter with completion verification"""
        print(f"\nGenerating Chapter {chapter_number}...")
        chapter_title = self.outline[chapter_number - 1]["title"]
        
        try:
            # Create group chat with reduced rounds
            groupchat = self.initiate_group_chat()
            manager = autogen.GroupChatManager(
                groupchat=groupchat,
                llm_config=self.agent_config
            )

            # Prepare context
            context = self._prepare_chapter_context(chapter_number, prompt)
            required_beats, target_word_count = self._extract_required_chapter_details(prompt)
            editor_beat_instruction = ""
            if required_beats:
                ordered_beats = "\n".join(f"- {beat}" for beat in required_beats)
                editor_beat_instruction = f"""

            Required Chapter Details For Editor Validation:
            {ordered_beats}

            The editor must include a BEAT CHECK section and verify that these beats are present in this order.
            If any beat is missing or out of order, the editor must return BEAT CHECK RESULT: FAIL and require revision."""
            word_count_instruction = ""
            if target_word_count > 0:
                word_count_instruction = f"""

            Target Word Count Requirement:
            Aim for approximately {target_word_count} words. The writer should target this length, and the editor should flag meaningful under-shooting or over-shooting."""
            anti_loop_instruction = """

            Anti-Repetition Requirement:
            The editor must check for looping or repetitive prose.
            If the draft repeats the same ideas, paragraphs, or sentence patterns without advancing the scene, the editor must return LOOP CHECK RESULT: FAIL and require revision.
            Only use LOOP CHECK RESULT: PASS when the chapter continues to make forward progress."""
            self._emit_progress(chapter_number, chapter_title, "system", "starting", "Starting chapter generation", "planning", 1)
            chapter_prompt = f"""
            IMPORTANT: Wait for confirmation before proceeding.
            IMPORTANT: This is Chapter {chapter_number}. Do not proceed to next chapter until explicitly instructed.
            DO NOT END THE STORY HERE unless this is actually the final chapter ({self.outline[-1]['chapter_number']}).

            Current Task: Generate Chapter {chapter_number} content only.

            Chapter Outline:
            Title: {self.outline[chapter_number - 1]['title']}

            Chapter Requirements:
            {prompt}

            Previous Context for Reference:
            {context}

            Follow this exact sequence for Chapter {chapter_number} only:

            1. Memory Keeper: Context (MEMORY UPDATE)
            2. Writer: Draft (SCENE)
            3. Editor: Review (FEEDBACK)
            4. Writer Final: Revision (SCENE FINAL)

            Wait for each step to complete before proceeding.{editor_beat_instruction}{word_count_instruction}{anti_loop_instruction}"""

            # Start generation
            stop_event = threading.Event()
            monitor = threading.Thread(
                target=self._monitor_groupchat,
                args=(groupchat, chapter_number, chapter_title, stop_event),
                daemon=True,
            )
            monitor.start()
            try:
                self.agents["user_proxy"].initiate_chat(
                    manager,
                    message=chapter_prompt
                )
            finally:
                stop_event.set()
                monitor.join(timeout=1)

            if not self._verify_chapter_complete(groupchat.messages, required_beats):
                raise ValueError(f"Chapter {chapter_number} generation incomplete")
        
            result = self._process_chapter_results(chapter_number, groupchat.messages)
            chapter_file = os.path.join(self.output_dir, f"chapter_{chapter_number:02d}.txt")
            if not os.path.exists(chapter_file):
                raise FileNotFoundError(f"Chapter {chapter_number} file not created")
            self._emit_progress(chapter_number, chapter_title, "writer_final", "completed", "Final chapter saved", "final", 1)
        
            completion_msg = f"Chapter {chapter_number} is complete. Proceed with next chapter."
            self.agents["user_proxy"].send(completion_msg, manager)
            return result
            
        except Exception as e:
            print(f"Error in chapter {chapter_number}: {str(e)}")
            traceback.print_exc()
            return self._handle_chapter_generation_failure(chapter_number, prompt)

    def _extract_final_scene(self, messages: List[Dict]) -> Optional[str]:
        """Extract chapter content with improved content detection"""
        for msg in reversed(messages):
            content = msg.get("content", "")
            sender = self._get_sender(msg)
            
            if sender in ["writer", "writer_final", "editor"]:
                tagged = self._extract_tagged_content(
                    content,
                    ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE", "SCENE", "CHAPTER"],
                )
                if tagged:
                    return tagged

                # Handle raw content
                if len(content.strip()) > 100:  # Minimum content threshold
                    return content.strip()
                    
        return None

    def _handle_chapter_generation_failure(self, chapter_number: int, prompt: str) -> Dict:
        """Handle failed chapter generation with simplified retry"""
        print(f"Attempting simplified retry for Chapter {chapter_number}...")
        chapter_title = self.outline[chapter_number - 1]["title"]
        
        try:
            # Create a new group chat with just essential agents
            required_beats, target_word_count = self._extract_required_chapter_details(prompt)
            retry_groupchat = autogen.GroupChat(
                agents=[
                    self.agents["user_proxy"],
                    self.agents["story_planner"],
                    self.agents["writer"]
                ],
                messages=[],
                max_round=3
            )
            
            manager = autogen.GroupChatManager(
                groupchat=retry_groupchat,
                llm_config=self.agent_config
            )

            beat_instruction = ""
            if required_beats or target_word_count > 0:
                beat_lines = []
                if target_word_count > 0:
                    beat_lines.append(f"Target Word Count: {target_word_count}")
                if required_beats:
                    beat_lines.append("Beats:")
                    beat_lines.extend(f"- {beat}" for beat in required_beats)
                beat_instruction = (
                    "\n\nRequired Chapter Details:\n"
                    + "\n".join(beat_lines)
                    + "\n\nThese chapter details are mandatory and any listed beats must appear in this order."
                )

            retry_prompt = f"""Emergency chapter generation for Chapter {chapter_number}.
            
{prompt}
{beat_instruction}

Please generate this chapter in two steps:
1. Story Planner: Create a basic outline (tag: PLAN)
2. Writer: Write the complete chapter (tag: SCENE FINAL)

Keep it simple and direct.
Do not repeat paragraphs, recycle the same sentences, or loop over the same beat without advancing the chapter."""

            self.agents["user_proxy"].initiate_chat(
                manager,
                message=retry_prompt
            )
            
            # Save the retry results
            self._emit_progress(chapter_number, chapter_title, "story_planner", "retry_outline", "Emergency outline pass", "planning", 2)
            self._emit_progress(chapter_number, chapter_title, "writer", "retry_draft", "Emergency final draft pass", "revision", 2)
            return self._process_chapter_results(chapter_number, retry_groupchat.messages)
            
        except Exception as e:
            print(f"Error in retry attempt for Chapter {chapter_number}: {str(e)}")
            traceback.print_exc()
            print("Unable to generate chapter content after retry")
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
                draft_text = self._extract_tagged_content(content, ["SCENE", "CHAPTER"])
                if draft_text:
                    artifacts["draft_scene"] = draft_text
            elif sender == "editor" and "FEEDBACK:" in content and not artifacts["editor_feedback"]:
                artifacts["editor_feedback"] = content.split("FEEDBACK:", 1)[1].strip()
            elif sender in ["writer", "writer_final", "editor"]:
                final_text = self._extract_tagged_content(content, ["SCENE FINAL", "CHAPTER FINAL", "EDITED_SCENE"])
                if final_text:
                    artifacts["final_scene"] = final_text
        if not artifacts["final_scene"]:
            artifacts["final_scene"] = self._extract_final_scene(messages) or ""
        return artifacts

    def _process_chapter_results(self, chapter_number: int, messages: List[Dict]) -> Dict:
        """Process and save chapter results, updating memory"""
        try:
            artifacts = self._extract_artifacts(messages)
            # Extract the Memory Keeper's final summary
            memory_updates = []
            for msg in reversed(messages):
                sender = self._get_sender(msg)
                content = msg.get("content", "")
                
                if sender == "memory_keeper" and "MEMORY UPDATE:" in content:
                    update_start = content.find("MEMORY UPDATE:") + 14
                    memory_updates.append(content[update_start:].strip())
                    break
            
            # Add to memory even if no explicit update (use basic content summary)
            if memory_updates:
                self.chapters_memory.append(memory_updates[0])
            else:
                # Create basic memory from chapter content
                chapter_content = artifacts["final_scene"]
                if chapter_content:
                    basic_summary = f"Chapter {chapter_number} Summary: {chapter_content[:200]}..."
                    self.chapters_memory.append(basic_summary)
            
            # Extract and save the chapter content
            self._save_chapter(chapter_number, artifacts["final_scene"])
            return artifacts
            
        except Exception as e:
            print(f"Error processing chapter results: {str(e)}")
            traceback.print_exc()
            raise

    def _save_chapter(self, chapter_number: int, chapter_content: str) -> None:
        print(f"\nSaving Chapter {chapter_number}")
        try:
            if not chapter_content:
                raise ValueError(f"No content found for Chapter {chapter_number}")
                
            chapter_content = self._clean_chapter_content(chapter_content)
            if self._is_repetitive_output(chapter_content):
                raise ValueError(f"Detected repetitive looping output in Chapter {chapter_number}")
            
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
                    
            print(f"✓ Saved to: {filename}")
            
        except Exception as e:
            print(f"Error saving chapter: {str(e)}")
            traceback.print_exc()
            raise

    def generate_book(self, outline: List[Dict]) -> None:
        """Generate the book with strict chapter sequencing"""
        print("\nStarting Book Generation...")
        print(f"Total chapters: {len(outline)}")
        
        # Sort outline by chapter number
        sorted_outline = sorted(outline, key=lambda x: x["chapter_number"])
        
        for chapter in sorted_outline:
            chapter_number = chapter["chapter_number"]
            
            # Verify previous chapter exists and is valid
            if chapter_number > 1:
                prev_file = os.path.join(self.output_dir, f"chapter_{chapter_number-1:02d}.txt")
                if not os.path.exists(prev_file):
                    print(f"Previous chapter {chapter_number-1} not found. Stopping.")
                    break
                    
                # Verify previous chapter content
                with open(prev_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if not self._verify_chapter_content(content, chapter_number-1):
                        print(f"Previous chapter {chapter_number-1} content invalid. Stopping.")
                        break
            
            # Generate current chapter
            print(f"\n{'='*20} Chapter {chapter_number} {'='*20}")
            self.generate_chapter(chapter_number, chapter["prompt"])
            
            # Verify current chapter
            chapter_file = os.path.join(self.output_dir, f"chapter_{chapter_number:02d}.txt")
            if not os.path.exists(chapter_file):
                print(f"Failed to generate chapter {chapter_number}")
                break
                
            with open(chapter_file, 'r', encoding='utf-8') as f:
                content = f.read()
                if not self._verify_chapter_content(content, chapter_number):
                    print(f"Chapter {chapter_number} content invalid")
                    break
                    
            print(f"✓ Chapter {chapter_number} complete")
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
