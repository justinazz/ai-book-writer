from typing import Dict, List, Optional

import autogen

from config import OUTPUT_FOLDER

class BookAgents:
    def __init__(self, agent_config: Dict, outline: Optional[List[Dict]] = None):
        """Initialize agents with book outline context"""
        self.agent_config = agent_config
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
            1. Track and summarize each chapter's key events
            2. Monitor character development and relationships
            3. Maintain world-building consistency
            4. Flag any continuity issues
            
            Book Overview:
            {outline_context}
            
            Format your responses as follows:
            - Start updates with 'MEMORY UPDATE:'
            - List key events with 'EVENT:'
            - List character developments with 'CHARACTER:'
            - List world details with 'WORLD:'
            - Flag issues with 'CONTINUITY ALERT:'""",
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
            system_message=f"""You are an expert creative writer who brings scenes to life.
            
            Book Context:
            {outline_context}
            
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
            
            Always reference the outline and previous content.
            Mark drafts with 'SCENE:' and final versions with 'SCENE FINAL:'""",
            llm_config=self.agent_config,
        )

        # Editor: Reviews and improves content
        editor = autogen.AssistantAgent(
            name="editor",
            system_message=f"""You are an expert editor ensuring quality and consistency.
            
            Book Overview:
            {outline_context}
            
            Your focus:
            1. Check alignment with outline
            2. Verify character consistency
            3. Maintain world-building rules
            4. Improve prose quality
            5. Return complete edited chapter
            6. Never ask to start the next chapter, as the next step is finalizing this chapter
            7. If the chapter prompt includes a Chapter Target Word Count, check whether the draft meaningfully aligns with it and require revision if it substantially misses the target
            8. If the chapter prompt includes 'Required Chapter Details', you must verify that every listed beat appears in the chapter in the intended order before approving it
            9. Reject looping or repetitive prose. If paragraphs, sentence patterns, or scene beats are being repeated without meaningful progress, require revision instead of approving the chapter
            
            Format your responses:
            1. Start critiques with 'FEEDBACK:'
            2. Provide suggestions with 'SUGGEST:'
            3. Return full edited chapter with 'EDITED_SCENE:'
            4. If 'Required Chapter Details' are provided, include a 'BEAT CHECK:' section that lists each beat in order and marks it PASS or FAIL
            5. End the beat check with 'BEAT CHECK RESULT: PASS' only if all beats are present in order; otherwise use 'BEAT CHECK RESULT: FAIL'
            6. If repetition or looping is detected, include 'LOOP CHECK RESULT: FAIL'; otherwise include 'LOOP CHECK RESULT: PASS'
            
            Reference specific outline elements in your feedback.""",
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
