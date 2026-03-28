# UI Restructure Plan

## Goal

Restructure the app around the actual user workflow instead of the current prototype-era control panel.

Target mental model:

1. `Planning`: define the book, load settings, generate and approve the outline.
2. `Writing`: run generation, monitor progress, review chapters, and steer the process.
3. `Editing` (future): reopen a saved book project later to refine chapters, add detail, and extend the book.

The UI shell should always keep global process state visible and keep the main workspace focused on the active phase.

## What The Current UI Does

The current page in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L360) is a single two-column control panel that mixes:

- global controls
- setup fields
- config management
- outline review
- chapter advice
- live progress
- checkpoint review
- chapter reading
- artifacts and continuity
- errors and event logs

This is functional, but the information architecture is doing too much at once.

Current section split:

- Left side: `Controls`, `Run Setup`, `Configs`, `Models`, `Outline Approval`, `Chapter Advice`, `Latest Error`, `Recent Events`
- Right side: `Live Progress`, `Checkpoint Review`, `Outline`, `Last Chapter Advice`, `Chapter Artifacts`, `Continuity Panel`, `Chapters`

## Main UX Problems

- Setup and live operations share the same visual priority, so the page feels noisy before a run even starts.
- Outline work is separated across multiple places instead of feeling like one contained planning step.
- Chapter observation and chapter intervention are split across unrelated panels.
- Errors, waiting states, and approval moments do not dominate the layout when they matter most.
- Configs are treated like a primary activity even though they are support tooling.
- The page is optimized for feature exposure, not for the story workflow.

## Proposed Product Model

The UI should revolve around a single concept: a `Book Project`.

A project should eventually contain:

- planning inputs
- runtime settings
- approved outline
- chapter texts
- chapter artifacts
- continuity memory
- revision history / alternates
- project status

Today the app already persists pieces of this separately:

- config payloads are saved in [generation_controller.py](/E:/AI/BookWriter/ai-book-writer/generation_controller.py#L573)
- the outline is written to [generation_controller.py](/E:/AI/BookWriter/ai-book-writer/generation_controller.py#L556)
- chapter text is stored and read from chapter files in [generation_controller.py](/E:/AI/BookWriter/ai-book-writer/generation_controller.py#L1041)
- continuity memory is written in [generation_controller.py](/E:/AI/BookWriter/ai-book-writer/generation_controller.py#L265)

What is missing for a true Editing phase is a single resumable project state.

## Proposed Shell

### 1. Sticky Top Bar

This becomes the always-visible process shell.

Contents:

- project title / active setup name
- overall status badge: `Idle`, `Running`, `Waiting`, `Paused`, `Error`, `Complete`
- current phase badge: `Planning` or `Writing`
- current chapter indicator
- mode toggle: `Auto` vs `Guided`
- primary process controls

Recommended controls:

- `Start`
- `Continue`
- `Pause`
- `Auto` / `Guided` mode toggle

This is cleaner than distributing process control across the body of the page.

### 2. Context Banner Under The Top Bar

This appears only when something needs attention.

Examples:

- `Outline ready for review`
- `Waiting for chapter advice`
- `Validation failed on Chapter 4`
- `Generation failed`

This banner should contain the immediate next action, so the user does not have to hunt for the right panel.

### 3. Primary Tabs

- `Planning`
- `Writing`
- `Editing`

`Editing` should be visually present only if we want to signal the roadmap. If implemented early, it should be disabled or labeled as future work until project persistence exists.

## Proposed Tab Structure

## Planning

Planning is the initial setup and outline approval phase.

This tab should answer:

- What book are we making?
- How is it structured?
- Which models and runtime settings are we using?
- Is the outline ready to approve?

Recommended layout:

- Left rail: planning steps / section navigation
- Main workspace: currently selected planning step
- Right rail: compact run summary and setup utilities

Recommended planning steps:

1. `Story Brief`
2. `Structure`
3. `Models & Runtime`
4. `Outline Review`

### Story Brief

Place these current fields here:

- premise
- storylines
- setting
- characters
- writing style
- tone
- plot beats
- constraints

This should feel like authoring a creative brief, not filling out a diagnostic form.

### Structure

Place these current fields here:

- number of chapters
- global chapter target word count
- per-chapter beats
- per-chapter target word counts

This section should probably use a chapter table or chapter cards instead of a long repeated form block.

### Models & Runtime

Place these current fields here:

- API endpoint
- refresh models
- outline model
- writer model
- token limit settings
- max tokens
- max iterations
- thinking mode

Place support utilities here as secondary actions:

- save config
- load config
- import config
- export config

This keeps technical setup available without letting it dominate the creative workflow.

### Outline Review

This is still part of Planning, not Writing.

Place these current elements here:

- generated outline
- outline approval status
- outline feedback
- approve outline
- regenerate outline

Recommended interaction:

- the outline occupies the main reading area
- feedback sits beside or below it
- approval actions stay sticky near the outline

## Writing

Writing is the live execution and monitoring phase.

This tab should answer:

- What is the system doing right now?
- Which chapter are we on?
- What has already been written?
- What intervention can I make if the process needs guidance?

Recommended layout:

- Left rail: chapter navigator
- Main workspace: selected chapter text or current checkpoint
- Right rail: live activity and intervention tools

### Left Rail: Chapter Navigator

Place here:

- chapter list
- chapter status
- current chapter highlight

Selecting a chapter should swap the main reading pane to that chapter.

### Main Workspace

Primary content:

- selected chapter text
- if waiting at a checkpoint, show the checkpoint card above the text

Secondary reference:

- approved outline excerpt for the selected chapter

This should become the reading-first area of the app.

### Right Rail: Activity And Tools

Place here:

- current agent
- current step
- iteration
- output stage
- progress detail
- progress timeline
- recent events
- latest error

Also place chapter intervention tools here:

- chapter advice
- regenerate chapter
- last advice

`Chapter Artifacts` and `Continuity` can live here as secondary inspector panels or inner tabs.

Recommended inspector tabs:

- `Activity`
- `Artifacts`
- `Continuity`

That keeps the live writing view focused while still exposing the useful diagnostics.

## Editing (Future)

Editing should be treated as a different mode from Writing.

Writing is about the live generation run.
Editing is about reopening an existing book project and refining it deliberately.

This tab should eventually support:

- selecting an existing saved book project
- reopening chapters and outline state
- refining a chapter with a targeted instruction
- amplifying scenes or adding detail
- generating alternates
- updating continuity after edits
- adding new chapters or inserting chapters later

Recommended editing layout:

- Left rail: project and chapter picker
- Main workspace: editable chapter document plus revision goal
- Right rail: outline, continuity, change history, alternates

## Mapping From Current UI To Proposed UI

Current `Controls` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L370)
Move to: top bar

Current `Run Setup` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L392)
Move to: `Planning > Story Brief`, `Planning > Structure`, and `Planning > Models & Runtime`

Current `Configs` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L447)
Move to: `Planning > Models & Runtime` as utilities

Current `Outline Approval` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L471)
Move to: `Planning > Outline Review`

Current `Chapter Advice` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L487)
Move to: `Writing > Activity / Tools`

Current `Live Progress` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L515)
Move to: `Writing > Activity`

Current `Checkpoint Review` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L531)
Move to: contextual banner plus `Writing` main workspace, or `Planning` if it is outline review

Current `Outline` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L541)
Move to: `Planning > Outline Review`, with read-only reference access from `Writing`

Current `Chapter Artifacts` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L551)
Move to: `Writing > Inspector > Artifacts`

Current `Continuity Panel` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L556)
Move to: `Writing > Inspector > Continuity`

Current `Chapters` in [web_ui.py](/E:/AI/BookWriter/ai-book-writer/web_ui.py#L561)
Move to: `Writing > Chapter Navigator`

## Suggested Low-Fidelity Wireframe

```text
+--------------------------------------------------------------------------------------------------+
| Book Project / Config Name | Running | Planning | Ch 0/12 | Auto | Start | Continue | Pause    |
+--------------------------------------------------------------------------------------------------+
| Context banner: Outline ready for review. Add feedback, approve, or regenerate.                |
+--------------------------------------------------------------------------------------------------+
| [Planning] [Writing] [Editing]                                                                  |
+--------------------------------------------------------------------------------------------------+
| Left rail                    | Main workspace                              | Right rail          |
| Planning steps               | Selected planning step                      | Summary             |
| - Story Brief                |                                             | Models              |
| - Structure                  | Story fields / structure / outline review   | Config utilities    |
| - Models & Runtime           |                                             | Run summary         |
| - Outline Review             |                                             |                    |
+--------------------------------------------------------------------------------------------------+
```

```text
+--------------------------------------------------------------------------------------------------+
| Book Project / Config Name | Running | Writing | Ch 4/12 | Guided | Continue | Pause            |
+--------------------------------------------------------------------------------------------------+
| Context banner: Waiting for chapter guidance on Chapter 4.                                      |
+--------------------------------------------------------------------------------------------------+
| [Planning] [Writing] [Editing]                                                                  |
+--------------------------------------------------------------------------------------------------+
| Left rail          | Main workspace                                  | Right rail             |
| Chapters           | Checkpoint card                                 | Activity               |
| 1 Complete         | Selected chapter reader                         | Artifacts              |
| 2 Complete         |                                                 | Continuity             |
| 3 Complete         |                                                 | Advice / regenerate    |
| 4 In progress      |                                                 | Errors / events        |
| ...                |                                                 |                        |
+--------------------------------------------------------------------------------------------------+
```

## Implementation Guidance

For the first implementation pass, keep the backend behavior mostly intact and only reorganize the UI shell.

Suggested order:

1. Introduce a persistent top bar with status and process controls.
2. Convert the page body into tabs: `Planning` and `Writing`.
3. Move current sections into those tabs without changing backend endpoints yet.
4. Add contextual waiting/error banners.
5. Split `Planning` into sub-sections and `Writing` into chapter navigator plus activity inspector.
6. After that, design project persistence for a real `Editing` tab.

## Recommendation On Editing

Do not force Editing into the first restructure as a fully working third workflow.

Best next-step UX stance:

- design the shell so `Editing` fits naturally later
- keep the first implementation focused on `Planning` and `Writing`
- introduce a persistent `Book Project` state before building editing interactions

That will keep the restructure coherent instead of turning Editing into another prototype-era catch-all area.
