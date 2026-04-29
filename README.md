# Book Writer User Manual

This project is a local browser-based control panel for generating a multi-chapter story with AutoGen agents and an OpenAI-compatible local model server such as LM Studio.

The system has two main stages:

1. Generate an outline.
2. Generate chapters from the approved outline.

The UI is designed so you can steer the process at checkpoints instead of running one blind batch job from start to finish.

## Quick Start

1. Activate the virtual environment.
2. Start the UI:

```powershell
venv\Scripts\python.exe web_ui.py
```

3. Open `http://127.0.0.1:8001`

The web UI defaults to port `8001`. To use a different port, update
`WEB_UI_PORT` in `config.py` before starting `web_ui.py`.

On Windows you can also use:

```bat
Launch Web UI.bat
```

## What The System Does

The app coordinates several agent roles:

- `story_planner`: shapes plot structure
- `world_builder`: keeps setting/world details coherent
- `outline_creator`: turns the story setup into a chapter outline
- `memory_keeper`: tracks continuity during chapter writing
- `writer`: writes the prose
- `editor`: critiques and revises the prose

The browser UI is live. It updates phase, active agent, step, progress timeline, chapter status, outline state, and saved chapter text automatically.

## Main Workflow

1. Fill in the run setup fields.
2. Press `Start Run`.
3. The app generates an outline.
4. Review the outline.
5. Either:
   - approve it
   - add outline feedback and regenerate it
6. After approval, chapter generation begins.
7. At checkpoints, the app either continues automatically or waits for you, depending on the current mode.

## Controls

These are the buttons at the top of the left panel.

### `Start Run`

Starts a new run using the current form values.

### `Keep Going`

The run continues automatically through checkpoints.

Use this when you want the system to:

- generate the outline
- pause only if approval is required
- keep writing chapters without waiting after each one

### `Ask for Advice`

The run pauses at the next checkpoint and waits for you.

A checkpoint is normally:

- outline ready for review
- chapter complete
- chapter regeneration complete

Use this when you want to inspect the latest result, add guidance, then continue manually.

### `Continue`

Resumes the run when it is already waiting at a checkpoint.

### `Pause`

Requests a pause at the next checkpoint. It does not kill the current generation mid-response. The phase label changes to `Pausing at the next checkpoint`, then to `Paused` once the run actually reaches a safe pause point.

## Run Setup

### API Endpoint

The OpenAI-compatible local endpoint. Default:

```text
http://127.0.0.1:1234/v1
```

### Refresh Models

Queries the endpoint’s `/models` route and repopulates the model dropdowns.

### Outline Model

The model used for outline generation.

### Writer Model

The model used for chapter generation.

### Temperature

Controls generation randomness. Current default is `0.8`.

### Prompt Sections

These fields build the master story setup:

- `Premise`
- `Storylines / Arcs`
- `Setting / World`
- `Characters`
- `Writing Style`
- `Tone`
- `Important Plot Beats`
- `Constraints / Must Include`

Each textarea has a small `Expand` button that opens a large editor modal for easier editing.

### Chapter Target Word Count

Global target word count for chapters.

- If a chapter does not have its own target word count, this value is used.
- If this is `0`, no global chapter word target is enforced.

### Number of Chapters

Controls how many chapter detail editors appear below it.

### Chapter Details

Each chapter has:

- `Chapter N Details`
- `Chapter N Target Word Count`

`Chapter N Details` is where you put the required beats or events for that specific chapter.

The system uses these chapter details in two places:

- outline generation
- chapter writing and editing

If chapter details exist, the editor performs an extra compliance check and is expected to verify that the chapter follows those details in order.

If a chapter-specific target word count is `0`, the global `Chapter Target Word Count` is used instead.

### Token Limit

If `On`, the system sends `max_tokens` with the request.

If `Off`, the `Max Tokens` field is hidden and no explicit token cap is sent.

Current default max token setting is `8192`.

### Max Iterations

Maximum writer/editor iteration budget for chapter generation. Current default is `5`.

### Thinking Mode

Options:

- `Normal`
- `No Thinking`

`No Thinking` currently tries to send:

```json
{
  "extra_body": {
    "enable_thinking": false
  }
}
```

This is aimed at LM Studio style backends. Whether it works depends on how the local server forwards that request shape to the model backend.

## Configs

There are two kinds of config loading and saving.

### Save Config

Saves the current setup into the project’s `saved_configs` folder.

Loading a saved config also fills the config name field so saving again overwrites the same setup name.

### Load Saved Setup

Loads a config previously saved inside the app.

### Load External Config

Lets you choose a JSON file from anywhere on disk and load it once. It does not automatically copy that file into the project’s internal saved-config folder.

### Save External Config

Downloads the current setup as a JSON file directly from the browser.

## Config Format

The current config format is JSON and centers around structured setup data. Example:

```json
{
  "name": "Corporate Thriller Draft",
  "created_at": "2026-03-22 12:00:00 UTC",
  "endpoint_url": "http://127.0.0.1:1234/v1",
  "outline_model": "nemomix-unleashed-12b",
  "writer_model": "nemomix-unleashed-12b",
  "num_chapters": 10,
  "token_limit_enabled": true,
  "max_tokens": 8192,
  "reduce_thinking": false,
  "max_iterations": 5,
  "chapter_target_word_count": 1800,
  "output_folder": "E:\\AI\\BookWriter\\book_output",
  "chapter_details": {
    "1": {
      "beats": "Dane finishes the model and discovers the crash signal.",
      "target_word_count": 2000
    },
    "2": {
      "beats": "Dane oversleeps and rushes to the office.",
      "target_word_count": 0
    }
  },
  "prompt_sections": {
    "premise": "...",
    "storylines": "...",
    "setting": "...",
    "characters": "...",
    "writing_style": "...",
    "tone": "...",
    "plot_beats": "...",
    "constraints": "..."
  }
}
```

Note:

- `mode` is not stored in configs
- `version` is not stored in configs
- `characters` may be either one string or an array of strings in `prompt_sections` and in each `chapter_details` entry; arrays are read as newline-separated text

## Outline Approval

After the outline is generated, the system enters an approval gate.

You can then:

- read the outline
- type into `Outline Feedback`
- press `Save Feedback`
- press `Regenerate Outline`
- or press `Approve Outline`

The run does not continue to chapter writing until the outline is approved.

## Advice

The `Advice` box queues guidance for the next checkpoint-driven step.

Examples:

- make Dane less confident here
- slow the pacing in chapter 3
- foreshadow the executive conflict earlier

This advice is not the same as the saved story setup. It is short-term steering for the next part of the run.

## Regenerate Chapter

`Regenerate Chapter` reruns a specific chapter using:

- the approved outline
- the current global setup
- the current chapter’s details
- the current generation settings

Use this after the outline has been approved.

## Live Panels

### Live Progress

Shows:

- current agent
- current step
- iteration counter
- output stage
- progress detail
- progress timeline

### Checkpoint Review

Shows the current checkpoint title and its content.

### Outline

Shows the latest parsed outline text.

### Last Advice

Shows the last queued advice string.

### Chapter Artifacts

Shows draft, editor feedback, and final scene artifacts for the currently active chapter when available.

### Continuity Panel

Shows story-memory style summaries such as:

- chapter summaries
- characters
- world details
- continuity alerts

### Chapters

The chapter list at the bottom updates live.

- pending chapters are italic
- completed chapters are bold
- written chapters can be expanded so you can read them in the UI

## Sounds And Visual Feedback

The UI uses different cues:

- soft cue for agent handoffs
- stronger completion cue for chapter completion
- warning cue when the run enters a waiting-for-user-input state

The phase line also includes a spinner while the system is actively generating.

## Output Files

Generated outline and chapters are written to:

```text
E:\AI\BookWriter\book_output
```

Typical outputs:

- `outline.txt`
- `chapter_01.txt`
- `chapter_02.txt`

## Command Window Output

The command window prints live agent activity and tracebacks.

This is useful for:

- seeing which agent is currently speaking
- debugging generation failures
- confirming whether a local backend actually received a request

## Common Issues

### Start Run fails immediately

Likely causes:

- invalid model config shape
- unsupported request fields on the local backend
- the UI server is still running old code and needs a restart

### Outline becomes `[To be determined]`

This usually means the model returned something the outline parser could only partially salvage, or too many chapters were missing/malformed.

The current parser tries to preserve valid chapters and fill only the missing ones, but smaller local models can still struggle with strict chapter formatting.

### `No Thinking` does nothing

That depends on LM Studio or the backend behind it. The app can send the flag, but the server must support it and forward it correctly.

### The UI is live but a text field should not be overwritten

The UI is designed to avoid overwriting focused or dirty inputs. Explicit config loads still resync the form on purpose.

## Development Notes

Useful commands:

```powershell
venv\Scripts\python.exe -m py_compile config.py generation_controller.py web_ui.py book_generator.py agents.py outline_generator.py
```

That is the fastest basic syntax check after changes.
