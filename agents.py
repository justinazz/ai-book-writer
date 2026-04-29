from typing import Dict, List, Optional

import autogen

from config import OUTPUT_FOLDER

class BookAgents:
    def __init__(
        self,
        agent_config: Dict,
        outline: Optional[List[Dict]] = None,
        writer_agent_config: Optional[Dict] = None,
    ):
        """Initialize agents with book outline context"""
        self.agent_config = agent_config
        self.writer_agent_config = writer_agent_config or agent_config
        self.outline = outline
        self.world_elements = {}  # Track described locations/elements
        self.character_developments = {}  # Track character arcs
        
    def _format_outline_context(self) -> str:
        """Format the book outline into a readable context"""
        if not self.outline:
            return ""
            
        context_parts = ["Complete Book Outline:"]
        for chapter in self.outline:
            context_parts.extend([
                f"\nChapter {chapter['chapter_number']}: {chapter['title']}",
                chapter['prompt']
            ])
        return "\n".join(context_parts)

    def create_agents(self, initial_prompt, num_chapters) -> Dict:
        """Create and return all agents needed for book generation"""
        outline_context = self._format_outline_context()
        
        # Memory Keeper: Maintains story continuity and context
        memory_keeper = autogen.AssistantAgent(
            name="memory_keeper",
            system_message=f"""You are the keeper of the story's continuity and context.
            Your responsibilities:
            1. Track and summarize each accepted chapter's key events
            2. Monitor character development and relationships
            3. Maintain world-building consistency
            4. Flag any continuity issues
            5. Operate only on accepted final chapter prose, never on drafts
            
            Book Overview:
            {outline_context}
            
            Format your responses as follows:
            - Start updates with 'MEMORY UPDATE:'
            - List key events with 'EVENT:'
            - List character developments with 'CHARACTER:'
            - List world details with 'WORLD:'
            - Flag issues with 'CONTINUITY ALERT:'

            Never output SCENE, SCENE FINAL, CHAPTER, FEEDBACK, PLAN, or EDITED_SCENE.""",
            llm_config=self.agent_config,
        )
        
        # Story Planner - Focuses on high-level story structure
        story_planner = autogen.AssistantAgent(
            name="story_planner",
            system_message=f"""You are an expert story arc planner focused on overall narrative structure.

            Your sole responsibility is creating the high-level story arc.
            When given an initial story premise:
            1. Identify major plot points and story beats
            2. Map character arcs and development
            3. Note major story transitions
            4. Plan narrative pacing
            5. When explicit chapter beats are provided in the prompt, preserve them chapter by chapter and leave enough granularity for at least 3 chapter-level key events later

            Format your output EXACTLY as:
            STORY_ARC:
            - Major Plot Points:
            [List each major event that drives the story]
            
            - Character Arcs:
            [For each main character, describe their development path]
            
            - Story Beats:
            [List key emotional and narrative moments in sequence]
            
            - Key Transitions:
            [Describe major shifts in story direction or tone]
            
            Always provide specific, detailed content - never use placeholders.""",
            llm_config=self.agent_config,
        )

        # Outline Creator - Creates detailed chapter outlines
        outline_creator = autogen.AssistantAgent(
            name="outline_creator",
            system_message=f"""Generate a detailed {num_chapters}-chapter outline.

            YOU MUST USE EXACTLY THIS FORMAT FOR EACH CHAPTER - NO DEVIATIONS:

            Chapter 1: [Title]
            Chapter Title: [Same title as above]
            Key Events:
            - [Event 1]
            - [Event 2]
            - [Event 3]
            Character Developments: [Specific character moments and changes]
            Setting: [Specific location and atmosphere]
            Tone: [Specific emotional and narrative tone]

            [REPEAT THIS EXACT FORMAT FOR ALL {num_chapters} CHAPTERS]

            Requirements:
            1. EVERY field must be present for EVERY chapter
            2. EVERY chapter must have AT LEAST 3 specific Key Events
            3. ALL chapters must be detailed - no placeholders
            4. Format must match EXACTLY - including all headings and bullet points
            5. When chapter beats are provided, summarize them into 3-5 distinct Key Events instead of collapsing them into 1-2 vague bullets
            6. Preserve the narrative intent of the chapter beats, but natural paraphrase is allowed
            7. When chapter details include purpose, setting, tone, characters, must-include items, avoid items, or chapter guidance, reflect them in the outline instead of dropping them

            Initial Premise:
            {initial_prompt}

            START WITH 'OUTLINE:' AND END WITH 'END OF OUTLINE'
            """,
            llm_config=self.agent_config,
        )

        # World Builder: Creates and maintains the story setting
        world_builder = autogen.AssistantAgent(
            name="world_builder",
            system_message=f"""You are an expert in world-building who creates rich, consistent settings.
            
            Your role is to establish ALL settings and locations needed for the entire story based on a provided story arc.

            Book Overview:
            {outline_context}
            
            Your responsibilities:
            1. Review the story arc to identify every location and setting needed
            2. Create detailed descriptions for each setting, including:
            - Physical layout and appearance
            - Atmosphere and environmental details
            - Important objects or features
            - Sensory details (sights, sounds, smells)
            3. Identify recurring locations that appear multiple times
            4. Note how settings might change over time
            5. Create a cohesive world that supports the story's themes
            
            Format your response as:
            WORLD_ELEMENTS:
            
            [LOCATION NAME]:
            - Physical Description: [detailed description]
            - Atmosphere: [mood, time of day, lighting, etc.]
            - Key Features: [important objects, layout elements]
            - Sensory Details: [what characters would experience]
            
            [RECURRING ELEMENTS]:
            - List any settings that appear multiple times
            - Note any changes to settings over time
            
            [TRANSITIONS]:
            - How settings connect to each other
            - How characters move between locations""",
            llm_config=self.agent_config,
        )

        # Writer: Generates the actual prose
        writer = autogen.AssistantAgent(
            name="writer",
            system_message="""You are an expert creative writer who brings scenes to life.
            
            Your focus:
            1. Write according to the outlined plot points
            2. Maintain consistent character voices
            3. Incorporate world-building details
            4. Create engaging prose
            5. Please make sure that you write the complete scene, do not leave it incomplete
            6. Follow any Chapter Target Word Count provided in the chapter prompt. If a target is given, aim closely for it
            7. Ensure transitions are smooth and logical
            8. Do not cut off the scene, make sure it has a proper ending
            9. Add a lot of details, and describe the environment and characters where it makes sense
            10. If the chapter prompt includes 'Required Chapter Details', treat those details as the highest-priority beat anchors for the current chapter
            11. If any numbered checklist item is missing, merged away, or out of order, the draft will be rejected and retried
            12. Use the broader outline only for continuity after the current chapter checklist is satisfied
            13. Faithful paraphrase is encouraged; exact wording from the chapter beats is not required
            14. If the current chapter beat anchors conflict with broader outline bullets, follow the current chapter beat anchors
            15. Do not import explicit beats from other chapters into the current chapter
            16. When prior feedback identifies failed checklist items, repair those failed items before adding extra flourish
            17. Never copy prompt scaffolding such as Retry Context, Recovery Context, beat-check labels, checklist headings, or compliance summaries into the prose
            18. If the chapter prompt includes Additional Chapter Guidance, use it to shape emphasis, pacing, tone, setting, and character focus after the required beats are satisfied
            19. If the chapter needs more length, deepen the existing beats before inventing any new event, coda, or aftermath
            20. Preferred expansion order: sharper sensory detail, clearer physical action, richer dialogue subtext, deeper interiority, and more immediate consequences inside the active beat
            21. Do not tack on a low-stakes postscript, recap, travel beat, or generic reflection after the intended ending just to reach the word count
            22. Every added paragraph should either advance a required beat, intensify it, reveal character, or sharpen the atmosphere
            23. When an important beat arrives, slow down and dramatize it on page instead of summarizing past it
            24. If the natural scene ending has landed cleanly, stop rather than padding beyond it
            
            Use the outline and previous content for continuity, but never let them override the current chapter beat anchors in the active prompt.
            Mark drafts with 'SCENE:' and final versions with 'SCENE FINAL:'""",
            llm_config=self.writer_agent_config,
        )

        # Editor: Reviews and improves content
        editor = autogen.AssistantAgent(
            name="editor",
            system_message="""You are an expert editor ensuring quality and consistency.

            Your focus:
            1. Check alignment with the current chapter requirements and supplied continuity notes
            2. Verify character consistency
            3. Maintain world-building rules
            4. Improve prose quality
            5. Return review feedback unless the prompt explicitly asks for an edited scene
            6. Never ask to start the next chapter, as the next step is finalizing this chapter
            7. If the chapter prompt includes a Chapter Target Word Count, check whether the draft meaningfully aligns with it and require revision if it substantially misses the target
            8. If the chapter prompt includes 'Required Chapter Details', you must treat them as the primary checklist for the current chapter and verify that every listed beat appears in the intended order before approving it
            9. Reject looping or repetitive prose. If paragraphs, sentence patterns, or scene beats are being repeated without meaningful progress, require revision instead of approving the chapter
            10. Reject chapters with overly long sentences; if any sentence runs beyond the allowed limit in the chapter prompt or editor instructions, require revision instead of approving the chapter
            11. Do not substitute a broad summary for the required beat checklist
            12. Do not approve a chapter by relying on beats from other chapters or later scenes
            13. Judge beat coverage by narrative intent and concrete on-page evidence, not by literal wording
            14. Faithful paraphrase of a beat should pass when the same action, reveal, or interaction clearly occurs in the right order
            15. Result lines must be exact standalone lines ending in only PASS or FAIL with no extra explanation on that line
            16. Do not fail sentence length based on a fragment below the allowed limit or on vague style concerns; only fail it when an actual sentence exceeds the limit
            17. Reject chapters that appear to hit length by stapling on low-value filler, recap, or a fresh aftermath beat after the intended ending
            18. When a chapter is short, direct expansion toward underdeveloped existing beats through dialogue subtext, sensory detail, interiority, physical business, or immediate consequences
            19. Do not suggest adding a brand-new coda, epilogue, travel beat, or generic reflection solely to raise the word count
            
            Format your responses:
            1. If the prompt asks for structured JSON feedback, return only valid JSON with the exact keys requested
            2. Otherwise start critiques with 'FEEDBACK:'
            3. Provide suggestions with 'SUGGEST:'
            4. Only return 'EDITED_SCENE:' when the prompt explicitly asks for a rewritten scene
            5. If 'Required Chapter Details' are provided, include a beat-by-beat verdict in the requested structure and preserve item order
            6. End every requested result field with exactly PASS or FAIL and no extra wording inside that result value
            7. Include repetition/looping verdicts whenever the prompt requests them
            8. Include sentence-length verdicts whenever the prompt requests them
            9. Include dedicated word-count guidance whenever a target word count is provided
            10. If the draft is already inside the allowed word-count range, state that no word-count changes are required and do not suggest trimming or padding merely to hit the exact target
            11. Keep sentence-fragment word counts inside the sentence-length section only; do not present a fragment count as the draft or chapter word count
            12. If the draft is short, point the writer toward earlier or existing beats that need more depth instead of recommending a new late scene solely for length

            Base your review on the current chapter requirements and continuity notes provided in the active prompt.
            Do not rely on hidden assumptions about later chapters or missing full-book context.
            Never output SCENE, SCENE FINAL, CHAPTER, CHAPTER FINAL, or EDITED_SCENE unless the prompt explicitly requests an edited scene.""",
            llm_config=self.agent_config,
        )

        # User Proxy: Manages the interaction
        user_proxy = autogen.UserProxyAgent(
            name="user_proxy",
            human_input_mode="TERMINATE",
            code_execution_config={
                "work_dir": OUTPUT_FOLDER,
                "use_docker": False
            }
        )

        return {
            "story_planner": story_planner,
            "world_builder": world_builder,
            "memory_keeper": memory_keeper,
            "writer": writer,
            "editor": editor,
            "user_proxy": user_proxy,
            "outline_creator": outline_creator
        }

    def update_world_element(self, element_name: str, description: str) -> None:
        """Track a new or updated world element"""
        self.world_elements[element_name] = description

    def update_character_development(self, character_name: str, development: str) -> None:
        """Track character development"""
        if character_name not in self.character_developments:
            self.character_developments[character_name] = []
        self.character_developments[character_name].append(development)

    def get_world_context(self) -> str:
        """Get formatted world-building context"""
        if not self.world_elements:
            return "No established world elements yet."
        
        return "\n".join([
            "Established World Elements:",
            *[f"- {name}: {desc}" for name, desc in self.world_elements.items()]
        ])

    def get_character_context(self) -> str:
        """Get formatted character development context"""
        if not self.character_developments:
            return "No character developments tracked yet."
        
        return "\n".join([
            "Character Development History:",
            *[f"- {name}:\n  " + "\n  ".join(devs) 
              for name, devs in self.character_developments.items()]
        ])
