"""Simple browser UI for controlling book generation."""
from __future__ import annotations

import json
import os
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import socket
from typing import Dict
from urllib.parse import parse_qs

from config import MAX_ITERATIONS_LIMIT
from generation_controller import GenerationController, PromptSections


controller = GenerationController()
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STYLESHEET_PATH = os.path.join(STATIC_DIR, "ui.less")

DEFAULT_PROMPT = """
Create a story in my established writing style with these key elements:
Its important that it has several key storylines that intersect and influence each other. The story should be set in a modern corporate environment, with a focus on technology and finance. The protagonist is a software engineer named Dane who has just completed a groundbreaking stock prediction algorithm. The algorithm predicts a catastrophic market crash, but Dane oversleeps and must rush to an important presentation to share his findings with executives. The tension arises from the questioning of whether his "error" might actually be correct.

The piece is written in third-person limited perspective, following Dane's thoughts and experiences. The prose is direct and technical when describing the protagonist's work, but becomes more introspective during personal moments. The author employs a mix of dialogue and internal monologue, with particular attention to time progression and technical details around the algorithm and stock predictions.
Story Arch:

Setup: Dane completes a groundbreaking stock prediction algorithm late at night
Initial Conflict: The algorithm predicts a catastrophic market crash
Rising Action: Dane oversleeps and must rush to an important presentation
Climax: The presentation to executives where he must explain his findings
Tension Point: The questioning of whether his "error" might actually be correct
""".strip()

DEFAULT_SECTIONS = PromptSections(
    premise="Create a story in my established writing style about Dane, a software engineer who discovers his stock prediction algorithm points to a catastrophic market crash.",
    storylines="Corporate pressure around Dane's presentation.\nThe uncertainty over whether the model is wrong or prophetic.\nIntersecting finance, office politics, and personal strain.",
    setting="A contemporary financial technology company in a modern city, with offices, transit, and corporate presentation rooms.",
    characters="Dane: brilliant, overworked software engineer.\nGary: nervous boss trying to balance support and optics.\nJonathan Morego: skeptical executive asking hard questions.\nSilence: brief Uber driver mention.\nC-level executives: tense audience.",
    writing_style="Third-person limited focused on Dane. Direct and technical around code and markets, more introspective in personal moments.",
    tone="Technical thriller blended with workplace drama, grounded and tense.",
    plot_beats="Dane finishes the model late at night.\nThe model predicts a crash.\nHe oversleeps and races to the presentation.\nExecutives challenge the validity of his findings.",
    constraints="Use intersecting storylines. Keep the corporate and financial details grounded in reality. Maintain tension around whether the prediction is an error.\r\nAround 1000 words per chapter.",
)


def _load_external_config_payload(body: str) -> Dict:
    candidates = [body, body.lstrip("\ufeff")]
    sanitized = re.sub(r",(\s*[}\]])", r"\1", body.lstrip("\ufeff"))
    if sanitized not in candidates:
        candidates.append(sanitized)

    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _project_name(snapshot) -> str:
    return snapshot.config_name or "Current Book Project"


def _mode_display(snapshot) -> str:
    return "Auto" if snapshot.mode == "keep_going" else "Guided"


def _status_display(status: str) -> str:
    return (status or "idle").replace("_", " ").strip().title() or "Idle"


def _phase_area(snapshot) -> str:
    phase = (snapshot.phase or "").lower()
    if snapshot.awaiting_outline_approval:
        return "Planning"
    if snapshot.current_chapter > 0 or snapshot.resume_available or snapshot.outline_approved:
        return "Writing"
    if "chapter" in phase or phase == "generation complete":
        return "Writing"
    return "Planning"


def _status_tone(snapshot) -> str:
    if snapshot.latest_error or snapshot.status == "failed":
        return "error"
    if snapshot.status == "completed":
        return "success"
    if snapshot.waiting_for_input or snapshot.status == "stopped":
        return "warning"
    if snapshot.run_active:
        return "running"
    return "accent"


def _context_banner(snapshot) -> Dict[str, str]:
    phase_area = _phase_area(snapshot)
    if snapshot.latest_error:
        return {
            "level": "error",
            "title": "Generation needs attention",
            "body": snapshot.latest_error,
        }
    if snapshot.awaiting_outline_approval:
        return {
            "level": "warning",
            "title": "Outline ready for review",
            "body": "Stay in Planning to review the outline, add feedback, and approve or regenerate it.",
        }
    if snapshot.resume_available and not snapshot.run_active:
        chapter_label = f"Chapter {snapshot.resume_chapter_number}" if snapshot.resume_chapter_number else "the latest checkpoint"
        return {
            "level": "accent",
            "title": "A writing session is ready to resume",
            "body": f"Open Writing to inspect the saved work, then press Continue to resume from {chapter_label}.",
        }
    if snapshot.waiting_for_input:
        if phase_area == "Planning":
            return {
                "level": "warning",
                "title": snapshot.current_checkpoint_title or "Waiting for outline review",
                "body": "Use Planning to inspect the outline, add feedback if needed, then approve or regenerate it.",
            }
        return {
            "level": "warning",
            "title": snapshot.current_checkpoint_title or "Waiting for guidance",
            "body": "Use Writing to inspect the checkpoint, queue advice if needed, then continue.",
        }
    if snapshot.run_active:
        return {
            "level": "running",
            "title": snapshot.phase or "Generation running",
            "body": snapshot.progress.detail or "The system is actively moving through the current generation phase.",
        }
    if snapshot.outline_approved:
        return {
            "level": "success",
            "title": "Planning is approved",
            "body": "Switch to Writing to monitor chapters, inspect output, or regenerate a specific chapter.",
        }
    if snapshot.outline_text:
        return {
            "level": "accent",
            "title": "Outline drafted",
            "body": "Review it in Planning and decide whether to approve it or ask for another pass.",
        }
    return {
        "level": "accent",
        "title": "Start in Planning",
        "body": "Shape the story brief, chapter structure, and runtime settings before starting a run.",
    }


def _selected_chapter_number(snapshot) -> int:
    if snapshot.current_chapter:
        return snapshot.current_chapter
    for chapter in snapshot.chapters:
        review = snapshot.chapter_reviews.get(chapter.number)
        if review and review.saved_text:
            return chapter.number
    return snapshot.chapters[0].number if snapshot.chapters else 0


def _render_page() -> bytes:
    return _render_page_v2()
    snapshot = controller.get_snapshot()
    if snapshot.resume_available and not snapshot.run_active:
        mode_label = f"Ready to resume from Chapter {snapshot.resume_chapter_number}"
    elif snapshot.status == "stopped":
        mode_label = "Paused"
    elif snapshot.stop_requested and snapshot.run_active:
        mode_label = "Pausing after the current chapter step"
    else:
        mode_label = "Keep Going" if snapshot.mode == "keep_going" else "Ask for Advice"
    waiting = "Yes" if snapshot.waiting_for_input else "No"
    can_start = not snapshot.run_active
    can_control = snapshot.run_active
    can_queue_chapter_advice = snapshot.run_active or snapshot.resume_available
    can_continue = (
        (snapshot.run_active and snapshot.waiting_for_input and not snapshot.awaiting_outline_approval)
        or (snapshot.resume_available and not snapshot.awaiting_outline_approval)
    )

    checkpoint_body = snapshot.current_checkpoint_body or "Nothing to review yet."
    outline_text = snapshot.outline_text or "Outline not generated yet."
    models = snapshot.available_models or [snapshot.outline_model, snapshot.writer_model]
    models = [model for index, model in enumerate(models) if model and model not in models[:index]]
    sections = snapshot.prompt_sections if snapshot.prompt_sections.premise else DEFAULT_SECTIONS
    chapter_details = snapshot.chapter_details or {}
    chapter_items = "".join(_render_chapter_item(chapter.number, chapter.title, chapter.status) for chapter in snapshot.chapters) or "<li class='chapter pending'><span class='chapter-title'><em>No chapters yet.</em></span></li>"
    model_error = snapshot.model_fetch_error or "No model errors."
    saved_configs = _render_saved_config_options(snapshot.saved_configs)
    progress_events = "\n".join(reversed(snapshot.progress_events)) or "No progress events yet."
    continuity_text = _render_continuity(snapshot)
    current_review = snapshot.chapter_reviews.get(snapshot.current_chapter or 1)
    current_artifacts = _render_artifacts(current_review) if current_review else "No chapter artifacts yet."
    outline_feedback = snapshot.outline_feedback or ""
    recent_events_text = "\n".join(snapshot.recent_events) or "No events yet."
    config_name = snapshot.config_name or ""

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Book Writer Control Panel</title>
  <style>
    :root {{
      --bg: #f3efe5;
      --panel: #fffaf1;
      --ink: #1d2a2f;
      --muted: #5d6a6f;
      --line: #d8cdb7;
      --accent: #0d6b63;
      --accent-2: #d2872c;
      --accent-3: #2a4f85;
      --danger: #9f3a2d;
      --done: #1f5131;
      --pending: #7a6b58;
      --progress: #9b5d18;
      --flash: #fff4bf;
    }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background:
        radial-gradient(circle at top left, #fff6df 0, transparent 32%),
        linear-gradient(135deg, #efe5cf, var(--bg));
      color: var(--ink);
    }}
    .wrap {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
      font-weight: 600;
    }}
    .hero {{
      margin-bottom: 20px;
      padding: 24px;
      border: 1px solid var(--line);
      background: linear-gradient(135deg, rgba(255,250,241,0.96), rgba(248,238,220,0.92));
      box-shadow: 0 14px 40px rgba(29, 42, 47, 0.08);
    }}
    .grid {{
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 18px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 16px;
      box-shadow: 0 8px 24px rgba(29, 42, 47, 0.06);
    }}
    textarea, input, select {{
      width: 100%;
      box-sizing: border-box;
      padding: 10px;
      border: 1px solid var(--line);
      background: #fffdf8;
      color: var(--ink);
      font: inherit;
    }}
    textarea {{
      min-height: 160px;
      resize: vertical;
    }}
    .advice-box {{
      min-height: 100px;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    button {{
      border: 0;
      padding: 10px 14px;
      color: white;
      background: var(--accent);
      cursor: pointer;
      font: inherit;
    }}
    button.secondary {{
      background: var(--accent-2);
    }}
    button.danger {{
      background: var(--danger);
    }}
    button:disabled {{
      cursor: not-allowed;
      opacity: 0.5;
    }}
    .helper-note {{
      margin-top: 10px;
      font-size: 0.94rem;
      color: var(--muted);
    }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin: 14px 0;
    }}
    .card {{
      border: 1px solid var(--line);
      padding: 10px;
      background: #fffdf8;
    }}
    .label {{
      display: block;
      color: var(--muted);
      margin-bottom: 4px;
      font-size: 0.92rem;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      font-family: Consolas, "Courier New", monospace;
      font-size: 0.92rem;
      line-height: 1.4;
    }}
    .status {{
      font-style: italic;
      color: var(--muted);
    }}
    .spinner-shell {{
      display: inline-block;
      width: 0.95rem;
      height: 0.95rem;
      border-radius: 50%;
      border: 2px solid #c9d7de;
      vertical-align: -0.12rem;
      margin-right: 0.35rem;
      position: relative;
      box-sizing: border-box;
    }}
    .spinner-shell.active {{
      border-color: #b9ccd6;
      animation: spin 0.9s linear infinite;
    }}
    .spinner-shell.active .spinner-dot {{
      position: absolute;
      width: 0.22rem;
      height: 0.22rem;
      border-radius: 50%;
      background: var(--accent-3);
      top: -0.05rem;
      left: 0.31rem;
    }}
    .chapter-list {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 8px;
    }}
    .chapter {{
      border: 1px solid var(--line);
      background: #fffdf8;
      padding: 9px 10px;
    }}
    .chapter.pending .chapter-title {{
      font-style: italic;
      color: var(--pending);
    }}
    .chapter.in_progress {{
      border-left: 5px solid var(--progress);
    }}
    .chapter.completed .chapter-title {{
      font-weight: 700;
      color: var(--done);
    }}
    .chapter.failed {{
      border-left: 5px solid var(--danger);
    }}
    .chapter button {{
      width: 100%;
      text-align: left;
      background: transparent;
      color: var(--ink);
      padding: 0;
      border: 0;
    }}
    .chapter-reader {{
      display: none;
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid var(--line);
    }}
    .chapter.open .chapter-reader {{
      display: block;
    }}
    .phase-box.flash {{
      animation: phaseflash 1.4s ease-out;
    }}
    .subgrid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .textarea-toolbar {{
      display: flex;
      justify-content: flex-end;
      margin: 6px 0 12px;
    }}
    .expand-textarea-button {{
      padding: 6px 10px;
      font-size: 0.88rem;
      background: var(--accent-3);
    }}
    .modal-backdrop {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(18, 26, 30, 0.62);
      z-index: 999;
      padding: 24px;
      box-sizing: border-box;
    }}
    .modal-backdrop.open {{
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .modal-dialog {{
      width: min(980px, 100%);
      max-height: 92vh;
      overflow: auto;
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.24);
      padding: 16px;
    }}
    .modal-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .modal-header h2 {{
      margin: 0;
    }}
    #textarea-modal-input {{
      min-height: 65vh;
    }}
    @keyframes phaseflash {{
      0% {{ background: var(--flash); }}
      100% {{ background: #fffdf8; }}
    }}
    @keyframes spin {{
      from {{ transform: rotate(0deg); }}
      to {{ transform: rotate(360deg); }}
    }}
    @media (max-width: 900px) {{
      .grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Book Writer Control Panel</h1>
      <div class="status" id="phase-status"><span id="live-spinner" class="spinner-shell{' active' if snapshot.busy else ''}"><span class="spinner-dot"></span></span> Mode: {escape(mode_label)} | Status: {escape(snapshot.status)} | Phase: {escape(snapshot.phase)}</div>
      <div class="meta">
        <div class="card"><span class="label">Waiting For Input</span>{escape(waiting)}</div>
        <div class="card"><span class="label">Current Chapter</span><span id="current-chapter-display">{snapshot.current_chapter} / {snapshot.total_chapters}</span></div>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Controls</h2>
        <div class="controls">
          <button id="start-button" type="submit" form="run-setup-form" {"disabled" if not can_start else ""}>&#9889; Start Run</button>
          <form method="post" action="/mode" class="async-form">
            <input type="hidden" name="mode" value="keep_going">
            <button id="keep-going-button" class="secondary" {"disabled" if not can_control else ""}>&#9654; Keep Going</button>
          </form>
          <form method="post" action="/mode" class="async-form">
            <input type="hidden" name="mode" value="ask_for_advice">
            <button id="ask-advice-button" class="secondary" {"disabled" if not can_control else ""}>&#128172; Ask for Advice</button>
          </form>
          <form method="post" action="/continue" class="async-form">
            <button id="continue-button" {"disabled" if not can_continue else ""}>&#9658; Continue</button>
          </form>
          <form method="post" action="/stop" class="async-form">
            <button id="stop-button" class="danger" {"disabled" if not can_control else ""}>&#9208; Pause</button>
          </form>
        </div>
        <div class="card helper-note">
          <strong>Ask for Advice</strong> pauses at the next checkpoint so you can review the outline or latest chapter, add guidance, and then continue.
        </div>

        <h2 style="margin-top: 18px;">Run Setup</h2>
        <form id="run-setup-form" method="post" action="/start" class="async-form">
          <label class="label" for="endpoint_url">API Endpoint</label>
          <input id="endpoint_url" type="text" name="endpoint_url" value="{escape(snapshot.endpoint_url)}">
          <div class="controls">
            <button formaction="/refresh-models" class="secondary">&#10227; Refresh Models</button>
          </div>

          <label class="label" for="outline_model">Outline Model</label>
          <select id="outline_model" name="outline_model">{_render_options(models, snapshot.outline_model)}</select>

          <label class="label" for="writer_model">Writer Model</label>
          <select id="writer_model" name="writer_model">{_render_options(models, snapshot.writer_model)}</select>

          <label class="label" for="premise">Premise</label>
          <textarea id="premise" name="premise">{escape(sections.premise)}</textarea>
          <label class="label" for="storylines">Storylines / Arcs</label>
          <textarea id="storylines" name="storylines">{escape(sections.storylines)}</textarea>
          <label class="label" for="setting">Setting / World</label>
          <textarea id="setting" name="setting">{escape(sections.setting)}</textarea>
          <label class="label" for="characters">Characters</label>
          <textarea id="characters" name="characters">{escape(sections.characters)}</textarea>
          <label class="label" for="writing_style">Writing Style</label>
          <textarea id="writing_style" name="writing_style">{escape(sections.writing_style)}</textarea>
          <label class="label" for="tone">Tone</label>
          <textarea id="tone" name="tone">{escape(sections.tone)}</textarea>
          <label class="label" for="plot_beats">Important Plot Beats</label>
          <textarea id="plot_beats" name="plot_beats">{escape(sections.plot_beats)}</textarea>
          <label class="label" for="constraints">Constraints / Must Include</label>
          <textarea id="constraints" name="constraints">{escape(sections.constraints)}</textarea>
          <label class="label" for="chapter_target_word_count">Chapter Target Word Count</label>
          <input id="chapter_target_word_count" type="number" min="0" max="50000" name="chapter_target_word_count" value="{snapshot.chapter_target_word_count}">
          <label class="label" for="num_chapters">Number of Chapters</label>
          <input id="num_chapters" type="number" min="1" max="100" name="num_chapters" value="{snapshot.num_chapters}">
          <div id="chapter-details-editor">
            {_render_chapter_details_inputs(snapshot.num_chapters, chapter_details)}
          </div>
          <label class="label" for="token_limit_enabled">Token Limit</label>
          <select id="token_limit_enabled" name="token_limit_enabled">
            <option value="on"{" selected" if snapshot.token_limit_enabled else ""}>On</option>
            <option value="off"{" selected" if not snapshot.token_limit_enabled else ""}>Off</option>
          </select>
          <div id="max-tokens-group" style="display: {"block" if snapshot.token_limit_enabled else "none"};">
            <label class="label" for="max_tokens">Max Tokens</label>
            <input id="max_tokens" type="number" min="128" max="64000" name="max_tokens" value="{snapshot.max_tokens}">
          </div>
          <label class="label" for="max_iterations">Max Iterations</label>
          <input id="max_iterations" type="number" min="1" max="{MAX_ITERATIONS_LIMIT}" name="max_iterations" value="{snapshot.max_iterations}">
          <label class="label" for="reduce_thinking">Thinking Mode</label>
          <select id="reduce_thinking" name="reduce_thinking">
            <option value="off"{" selected" if not snapshot.reduce_thinking else ""}>Normal</option>
            <option value="on"{" selected" if snapshot.reduce_thinking else ""}>No Thinking</option>
          </select>
        </form>

        <h2 style="margin-top: 18px;">Configs</h2>
        <form method="post" action="/save-config" class="async-form">
          <label class="label" for="config_name">Save Current Setup</label>
          <input id="config_name" type="text" name="config_name" placeholder="Example: Dane thriller v1" value="{escape(config_name)}">
          <div class="controls">
            <button class="secondary">&#128190; Save Config</button>
          </div>
        </form>
        <form method="post" action="/load-config" class="async-form">
          <label class="label" for="config_file">Load Saved Setup</label>
          <select id="config_file" name="config_file">{saved_configs}</select>
          <div class="controls">
            <button class="secondary">&#128194; Load Config</button>
          </div>
        </form>
        <div class="controls">
          <button id="external-config-button" class="secondary" type="button">&#128194; Load External Config</button>
          <button id="save-external-config-button" class="secondary" type="button">&#128190; Save External Config</button>
          <input id="external-config-input" type="file" accept=".json,application/json" style="display:none;">
        </div>

        <h2 style="margin-top: 18px;">Models</h2>
        <div class="card"><pre id="model-error">{escape(model_error)}</pre></div>

        <h2 style="margin-top: 18px;">Outline Approval</h2>
        <div class="card"><pre id="outline-approval-status">Approved: {"Yes" if snapshot.outline_approved else "No"} | Awaiting approval: {"Yes" if snapshot.awaiting_outline_approval else "No"}</pre></div>
        <form method="post" action="/outline-feedback" class="async-form">
          <label class="label" for="outline_feedback">Outline Feedback</label>
          <textarea id="outline_feedback" name="outline_feedback" placeholder="Ask for pacing changes, stronger arcs, better chapter titles, more detail, or structural fixes.">{escape(outline_feedback)}</textarea>
          <div class="controls">
            <button class="secondary">&#9998; Save Feedback</button>
          </div>
        </form>
        <form method="post" action="/approve-outline" class="async-form">
          <div class="controls">
            <button id="approve-outline-button" {"disabled" if not snapshot.awaiting_outline_approval else ""}>&#10003; Approve Outline</button>
            <button id="regen-outline-button" formaction="/regenerate-outline" class="secondary" {"disabled" if not snapshot.awaiting_outline_approval else ""}>&#8635; Regenerate Outline</button>
          </div>
        </form>

        <h2 style="margin-top: 18px;">Chapter Advice</h2>
        <form method="post" action="/chapter-advice" class="async-form">
          <label class="label" for="chapter_advice_number">Chapter Number</label>
          <input id="chapter_advice_number" type="number" min="1" max="{snapshot.total_chapters or snapshot.num_chapters or 1}" name="chapter_number" value="{snapshot.current_chapter or 1}">
          <label class="label" for="advice">Replacement beats for the next attempt of that chapter</label>
          <textarea class="advice-box" id="advice" name="advice" placeholder="Example: 1. He stalls at the threshold. 2. She notices the blood on his cuff. 3. Their argument expands into the hidden ledger reveal."></textarea>
          <label class="label" for="chapter_advice_target_word_count">Optional replacement target word count</label>
          <input id="chapter_advice_target_word_count" type="number" min="0" max="50000" name="target_word_count" value="">
          <div class="controls">
            <button id="chapter-advice-button" {"disabled" if not can_queue_chapter_advice else ""}>&#10148; Queue Chapter Advice</button>
          </div>
        </form>
        <form method="post" action="/regenerate-chapter" class="async-form">
          <label class="label" for="regen_chapter_number">Regenerate Chapter</label>
          <input id="regen_chapter_number" type="number" min="1" max="{snapshot.total_chapters or snapshot.num_chapters}" name="chapter_number" value="{snapshot.current_chapter or 1}">
          <div class="controls">
            <button id="regen-chapter-button" {"disabled" if snapshot.run_active else ""}>&#8635; Regenerate Chapter</button>
          </div>
        </form>

        <h2 style="margin-top: 18px;">Latest Error</h2>
        <div class="card"><pre id="latest-error">{escape(snapshot.latest_error or "No errors recorded.")}</pre></div>

        <h2 style="margin-top: 18px;">Recent Events</h2>
        <div class="card"><pre id="recent-events">{escape(recent_events_text)}</pre></div>
      </div>

      <div class="panel">
        <h2>Live Progress</h2>
        <div class="subgrid" style="margin-bottom: 16px;">
          <div class="card"><span class="label">Current Agent</span><pre id="progress-agent">{escape(snapshot.progress.current_agent or "Idle")}</pre></div>
          <div class="card"><span class="label">Current Step</span><pre id="progress-step">{escape(snapshot.progress.current_step or "Idle")}</pre></div>
          <div class="card"><span class="label">Iteration</span><pre id="progress-iteration">{snapshot.progress.iteration or 0}/{snapshot.progress.max_iterations or 0}</pre></div>
          <div class="card"><span class="label">Output Stage</span><pre id="progress-stage">{escape(snapshot.progress.output_stage or "n/a")}</pre></div>
        </div>
        <div class="card" style="margin-bottom: 16px;">
          <span class="label">Progress Detail</span>
          <pre id="progress-detail">{escape(snapshot.progress.detail or "No active detail yet.")}</pre>
        </div>
        <div class="card" style="margin-bottom: 16px;">
          <span class="label">Progress Timeline</span>
          <pre id="progress-events">{escape(progress_events)}</pre>
        </div>

        <h2>Checkpoint Review</h2>
        <div class="card phase-box" id="checkpoint-box" style="margin-bottom: 16px;">
          <span class="label">Current Checkpoint</span>
          <pre id="checkpoint-title">{escape(snapshot.current_checkpoint_title or "Waiting for the first checkpoint.")}</pre>
        </div>
        <div class="card" style="margin-bottom: 16px;">
          <span class="label">Checkpoint Content</span>
          <pre id="checkpoint-body">{escape(checkpoint_body)}</pre>
        </div>

        <h2>Outline</h2>
        <div class="card" style="margin-bottom: 16px;">
          <pre id="outline-text">{escape(outline_text)}</pre>
        </div>

        <h2>Last Chapter Advice</h2>
        <div class="card">
          <pre id="last-advice">{escape(snapshot.latest_advice or "No advice submitted yet.")}</pre>
        </div>

        <h2 style="margin-top: 18px;">Chapter Artifacts</h2>
        <div class="card" style="margin-bottom: 16px;">
          <pre id="chapter-artifacts">{escape(current_artifacts)}</pre>
        </div>

        <h2>Continuity Panel</h2>
        <div class="card" style="margin-bottom: 16px;">
          <pre id="continuity-panel">{escape(continuity_text)}</pre>
        </div>

        <h2 style="margin-top: 18px;">Chapters</h2>
        <ul class="chapter-list" id="chapter-list">
          {chapter_items}
        </ul>
      </div>
    </section>
  </div>
  <div id="textarea-modal" class="modal-backdrop" aria-hidden="true">
    <div class="modal-dialog">
      <div class="modal-header">
        <h2 id="textarea-modal-title">Expanded Editor</h2>
        <button type="button" id="textarea-modal-close" class="secondary">&#10005; Close</button>
      </div>
      <textarea id="textarea-modal-input" aria-label="Expanded editor"></textarea>
    </div>
  </div>
  <script>
    let lastPhaseVersion = {snapshot.phase_version};
    function escapeHtml(value) {{
      return value
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }}
    function chapterHtml(chapter) {{
      const status = chapter.status || "pending";
      let title = `Chapter ${{chapter.number}}: ${{chapter.title}}`;
      if (status === "pending") {{
        title = `<em>${{escapeHtml(title)}}</em>`;
      }} else if (status === "completed") {{
        title = `<strong>${{escapeHtml(title)}}</strong>`;
      }} else {{
        title = escapeHtml(title);
      }}
      const reader = chapter.saved_text
        ? `<div class="chapter-reader"><pre>${{escapeHtml(chapter.saved_text)}}</pre></div>`
        : ``;
      const clickable = chapter.saved_text
        ? `<button type="button" class="chapter-toggle" data-chapter="${{chapter.number}}"><span class="chapter-title">${{title}}</span></button>`
        : `<span class="chapter-title">${{title}}</span>`;
      return `<li class="chapter ${{status}}" data-chapter="${{chapter.number}}">${{clickable}}${{reader}}</li>`;
    }}
    function playSoftCue() {{
      try {{
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const oscillator = ctx.createOscillator();
        const gain = ctx.createGain();
        oscillator.type = "sine";
        oscillator.frequency.setValueAtTime(720, ctx.currentTime);
        gain.gain.setValueAtTime(0.0001, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.035, ctx.currentTime + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.18);
        oscillator.connect(gain);
        gain.connect(ctx.destination);
        oscillator.start();
        oscillator.stop(ctx.currentTime + 0.18);
      }} catch (error) {{
      }}
    }}
    function playCompleteCue() {{
      try {{
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const now = ctx.currentTime;
        [660, 880].forEach((freq, index) => {{
          const oscillator = ctx.createOscillator();
          const gain = ctx.createGain();
          oscillator.type = "sine";
          oscillator.frequency.setValueAtTime(freq, now + index * 0.11);
          gain.gain.setValueAtTime(0.0001, now + index * 0.11);
          gain.gain.exponentialRampToValueAtTime(0.09, now + index * 0.11 + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.11 + 0.24);
          oscillator.connect(gain);
          gain.connect(ctx.destination);
          oscillator.start(now + index * 0.11);
          oscillator.stop(now + index * 0.11 + 0.24);
        }});
      }} catch (error) {{
      }}
    }}
    function playWaitingCue() {{
      try {{
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const now = ctx.currentTime;
        [780, 620].forEach((freq, index) => {{
          const oscillator = ctx.createOscillator();
          const gain = ctx.createGain();
          oscillator.type = "triangle";
          oscillator.frequency.setValueAtTime(freq, now + index * 0.16);
          gain.gain.setValueAtTime(0.0001, now + index * 0.16);
          gain.gain.exponentialRampToValueAtTime(0.14, now + index * 0.16 + 0.03);
          gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.16 + 0.28);
          oscillator.connect(gain);
          gain.connect(ctx.destination);
          oscillator.start(now + index * 0.16);
          oscillator.stop(now + index * 0.16 + 0.28);
        }});
      }} catch (error) {{
      }}
    }}
    function flashPhase() {{
      const box = document.getElementById("checkpoint-box");
      box.classList.remove("flash");
      void box.offsetWidth;
      box.classList.add("flash");
    }}
    let lastAgent = {json.dumps(snapshot.progress.current_agent or "")};
    let lastKnownState = {json.dumps(_snapshot_payload(snapshot))};
    let lastWaitingForInput = {json.dumps(snapshot.waiting_for_input)};
    let lastCompletionSignal = {json.dumps((f"phase:{snapshot.phase}" if snapshot.phase == "Generation complete" else f"checkpoint:{snapshot.current_checkpoint_title}") if (snapshot.current_checkpoint_title or snapshot.phase == "Generation complete") else "")};
    let activeModalSourceId = "";
    function modalSource() {{
      return activeModalSourceId ? document.getElementById(activeModalSourceId) : null;
    }}
    function getTextareaHeading(textarea) {{
      const label = textarea.id ? document.querySelector(`label[for="${{textarea.id}}"]`) : null;
      return (label?.textContent || textarea.placeholder || "Expanded Editor").trim();
    }}
    function openTextareaModal(textarea) {{
      const modal = document.getElementById("textarea-modal");
      const modalInput = document.getElementById("textarea-modal-input");
      const modalTitle = document.getElementById("textarea-modal-title");
      activeModalSourceId = textarea.id || "";
      modalTitle.textContent = getTextareaHeading(textarea);
      modalInput.value = textarea.value || "";
      modal.classList.add("open");
      modal.setAttribute("aria-hidden", "false");
      modalInput.focus();
      modalInput.selectionStart = modalInput.value.length;
    }}
    function closeTextareaModal() {{
      const modal = document.getElementById("textarea-modal");
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
      activeModalSourceId = "";
    }}
    function syncModalToSource() {{
      const source = modalSource();
      const modalInput = document.getElementById("textarea-modal-input");
      if (!source || !modalInput) return;
      source.value = modalInput.value;
      if (source.id) {{
        dirtyFields.add(source.id);
      }}
    }}
    function ensureTextareaExpandButtons(root = document) {{
      root.querySelectorAll("textarea").forEach((textarea) => {{
        if (textarea.id === "textarea-modal-input" || textarea.dataset.expandBound === "true") return;
        textarea.dataset.expandBound = "true";
        const toolbar = document.createElement("div");
        toolbar.className = "textarea-toolbar";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary expand-textarea-button";
        button.innerHTML = "&#8599; Expand";
        button.addEventListener("click", () => openTextareaModal(textarea));
        toolbar.appendChild(button);
        textarea.insertAdjacentElement("afterend", toolbar);
      }});
    }}
    function syncModels(models, selectedOutline, selectedWriter) {{
      const outlineSelect = document.getElementById("outline_model");
      const writerSelect = document.getElementById("writer_model");
      const list = (models && models.length ? models : [selectedOutline, selectedWriter]).filter(Boolean);
      const unique = [...new Set(list)];
      const outlineOptions = unique.map((model) => `<option value="${{escapeHtml(model)}}"${{model === selectedOutline ? " selected" : ""}}>${{escapeHtml(model)}}</option>`).join("");
      const writerOptions = unique.map((model) => `<option value="${{escapeHtml(model)}}"${{model === selectedWriter ? " selected" : ""}}>${{escapeHtml(model)}}</option>`).join("");
      outlineSelect.innerHTML = outlineOptions;
      writerSelect.innerHTML = writerOptions;
      outlineSelect.value = selectedOutline || unique[0] || "";
      writerSelect.value = selectedWriter || unique[0] || "";
    }}
    const dirtyFields = new Set();
    let forceNextFormSync = false;
    let lastRenderedChapterDetailSignature = "";
    function isFieldLocked(fieldId) {{
      const element = document.getElementById(fieldId);
      if (!element) return false;
      return document.activeElement === element || dirtyFields.has(fieldId);
    }}
    function setFieldValue(fieldId, value) {{
      const element = document.getElementById(fieldId);
      if (!element) return;
      if (!forceNextFormSync && isFieldLocked(fieldId)) return;
      element.value = value ?? "";
      dirtyFields.delete(fieldId);
    }}
    function setSelectValue(fieldId, value) {{
      const element = document.getElementById(fieldId);
      if (!element) return;
      if (!forceNextFormSync && isFieldLocked(fieldId)) return;
      element.value = value ?? "";
      dirtyFields.delete(fieldId);
    }}
    function collectChapterDetailsFields() {{
      const details = {{}};
      document.querySelectorAll(".chapter-detail-beats-input, .chapter-detail-wordcount-input").forEach((input) => {{
        const chapter = Number(input.dataset.chapter || "0");
        if (!chapter) return;
        details[chapter] = details[chapter] || {{ beats: "", target_word_count: 0 }};
        if (input.classList.contains("chapter-detail-beats-input")) {{
          details[chapter].beats = input.value;
        }} else if (input.classList.contains("chapter-detail-wordcount-input")) {{
          details[chapter].target_word_count = Number(input.value || 0);
        }}
      }});
      Object.keys(details).forEach((chapter) => {{
        const item = details[chapter];
        if (!(item.beats || item.target_word_count > 0)) {{
          delete details[chapter];
        }}
      }});
      return details;
    }}
    function renderChapterDetailEditors(numChapters, values) {{
      const container = document.getElementById("chapter-details-editor");
      const details = values || {{}};
      const existingDetails = collectChapterDetailsFields();
      const mergedDetails = forceNextFormSync ? details : {{ ...details, ...existingDetails }};
      const total = Math.max(1, Number(numChapters || 1));
      const normalizedDetails = {{}};
      for (let chapter = 1; chapter <= total; chapter += 1) {{
        const item = mergedDetails[chapter] ?? mergedDetails[String(chapter)] ?? {{}};
        normalizedDetails[chapter] = {{
          beats: item.beats ?? "",
          target_word_count: Number(item.target_word_count ?? 0),
        }};
      }}
      const signature = JSON.stringify({{ total, details: normalizedDetails }});
      if (!forceNextFormSync && signature === lastRenderedChapterDetailSignature) {{
        return;
      }}
      let html = "";
      for (let chapter = 1; chapter <= total; chapter += 1) {{
        const detail = normalizedDetails[chapter] ?? {{ beats: "", target_word_count: 0 }};
        html += `<label class="label" for="chapter_detail_beats_${{chapter}}">Chapter ${{chapter}} Details</label>`;
        html += `<textarea id="chapter_detail_beats_${{chapter}}" name="chapter_detail_beats_${{chapter}}" class="chapter-detail-beats-input" data-chapter="${{chapter}}" placeholder="Required beats for Chapter ${{chapter}}.">${{escapeHtml(detail.beats || "")}}</textarea>`;
        html += `<label class="label" for="chapter_detail_wordcount_${{chapter}}">Chapter ${{chapter}} Target Word Count</label>`;
        html += `<input id="chapter_detail_wordcount_${{chapter}}" name="chapter_detail_wordcount_${{chapter}}" type="number" min="0" max="50000" class="chapter-detail-wordcount-input" data-chapter="${{chapter}}" value="${{Number(detail.target_word_count || 0)}}">`;
      }}
      container.innerHTML = html;
      lastRenderedChapterDetailSignature = signature;
      bindEditableFields(container);
      ensureTextareaExpandButtons(container);
    }}
    function buildExternalConfigFromState(state) {{
      const liveChapterDetails = collectChapterDetailsFields();
      return {{
        name: document.getElementById("config_name").value.trim() || "external-config",
        created_at: new Date().toISOString(),
        endpoint_url: document.getElementById("endpoint_url").value || state.endpoint_url || "",
        outline_model: document.getElementById("outline_model").value || state.outline_model || "",
        writer_model: document.getElementById("writer_model").value || state.writer_model || "",
        num_chapters: Number(document.getElementById("num_chapters").value ?? state.num_chapters ?? 10),
        token_limit_enabled: document.getElementById("token_limit_enabled").value === "on",
        max_tokens: Number(document.getElementById("max_tokens").value ?? state.max_tokens ?? 4096),
        reduce_thinking: document.getElementById("reduce_thinking").value === "on",
        max_iterations: Number(document.getElementById("max_iterations").value ?? state.max_iterations ?? 5),
        chapter_target_word_count: Number(document.getElementById("chapter_target_word_count").value ?? state.chapter_target_word_count ?? 0),
        output_folder: state.output_folder || "",
        chapter_details: Object.keys(liveChapterDetails).length ? liveChapterDetails : (state.chapter_details || {{}}),
        prompt_sections: {{
          premise: document.getElementById("premise").value,
          storylines: document.getElementById("storylines").value,
          setting: document.getElementById("setting").value,
          characters: document.getElementById("characters").value,
          writing_style: document.getElementById("writing_style").value,
          tone: document.getElementById("tone").value,
          plot_beats: document.getElementById("plot_beats").value,
          constraints: document.getElementById("constraints").value,
        }},
      }};
    }}
    function applyState(state) {{
      lastKnownState = state;
      document.getElementById("phase-status").innerHTML = `<span id="live-spinner" class="spinner-shell ${{state.busy ? "active" : ""}}"><span class="spinner-dot"></span></span> Mode: ${{escapeHtml(state.mode_label)}} | Status: ${{escapeHtml(state.status)}} | Phase: ${{escapeHtml(state.phase)}}`;
      document.getElementById("current-chapter-display").textContent = `${{state.current_chapter}} / ${{state.total_chapters}}`;
      setFieldValue("endpoint_url", state.endpoint_url || "");
      setFieldValue("config_name", state.config_name || "");
      if (forceNextFormSync || (!isFieldLocked("outline_model") && !isFieldLocked("writer_model"))) {{
        syncModels(state.available_models || [], state.outline_model || "", state.writer_model || "");
        dirtyFields.delete("outline_model");
        dirtyFields.delete("writer_model");
      }}
      setFieldValue("premise", state.prompt_sections?.premise || "");
      setFieldValue("storylines", state.prompt_sections?.storylines || "");
      setFieldValue("setting", state.prompt_sections?.setting || "");
      setFieldValue("characters", state.prompt_sections?.characters || "");
      setFieldValue("writing_style", state.prompt_sections?.writing_style || "");
      setFieldValue("tone", state.prompt_sections?.tone || "");
      setFieldValue("plot_beats", state.prompt_sections?.plot_beats || "");
      setFieldValue("constraints", state.prompt_sections?.constraints || "");
      setFieldValue("chapter_target_word_count", state.chapter_target_word_count ?? 0);
      setFieldValue("num_chapters", state.num_chapters ?? 10);
      renderChapterDetailEditors(state.num_chapters ?? 10, state.chapter_details || {{}});
      const chapterAdviceNumber = document.getElementById("chapter_advice_number");
      if (chapterAdviceNumber && document.activeElement !== chapterAdviceNumber) {{
        chapterAdviceNumber.value = state.current_chapter || chapterAdviceNumber.value || 1;
      }}
      document.getElementById("checkpoint-title").textContent = state.current_checkpoint_title || "Waiting for the first checkpoint.";
      document.getElementById("checkpoint-body").textContent = state.current_checkpoint_body || "Nothing to review yet.";
      document.getElementById("outline-text").textContent = state.outline_text || "Outline not generated yet.";
      document.getElementById("last-advice").textContent = state.latest_advice || "No advice submitted yet.";
      document.getElementById("model-error").textContent = state.model_fetch_error || "No model errors.";
      document.getElementById("latest-error").textContent = state.latest_error || "No errors recorded.";
      document.getElementById("recent-events").textContent = state.recent_events || "No events yet.";
      document.getElementById("outline-approval-status").textContent = `Approved: ${{state.outline_approved ? "Yes" : "No"}} | Awaiting approval: ${{state.awaiting_outline_approval ? "Yes" : "No"}}`;
      setFieldValue("outline_feedback", state.outline_feedback || "");
      setSelectValue("token_limit_enabled", state.token_limit_enabled ? "on" : "off");
      setFieldValue("max_tokens", state.max_tokens ?? 4096);
      setFieldValue("max_iterations", state.max_iterations ?? 5);
      setSelectValue("reduce_thinking", state.reduce_thinking ? "on" : "off");
      document.getElementById("progress-agent").textContent = state.progress.current_agent || "Idle";
      document.getElementById("progress-step").textContent = state.progress.current_step || "Idle";
      document.getElementById("progress-iteration").textContent = `${{state.progress.iteration || 0}}/${{state.progress.max_iterations || 0}}`;
      document.getElementById("progress-stage").textContent = state.progress.output_stage || "n/a";
      document.getElementById("progress-detail").textContent = state.progress.detail || "No active detail yet.";
      document.getElementById("progress-events").textContent = state.progress_events || "No progress events yet.";
      document.getElementById("continuity-panel").textContent = state.continuity || "No continuity data yet.";
      document.getElementById("chapter-artifacts").textContent = state.current_artifacts || "No chapter artifacts yet.";
      document.getElementById("chapter-list").innerHTML = state.chapters.map(chapterHtml).join("");
      bindChapterToggles();
      document.getElementById("max-tokens-group").style.display = state.token_limit_enabled ? "block" : "none";
      document.getElementById("start-button").disabled = state.run_active;
      document.getElementById("keep-going-button").disabled = !state.run_active || state.mode === "keep_going";
      document.getElementById("ask-advice-button").disabled = !state.run_active || state.mode === "ask_for_advice";
      document.getElementById("continue-button").disabled = ((!state.run_active || !state.waiting_for_input) && !state.resume_available) || state.awaiting_outline_approval;
      document.getElementById("stop-button").disabled = !state.run_active || state.status === "completed" || state.status === "failed";
      document.getElementById("approve-outline-button").disabled = !state.awaiting_outline_approval;
      document.getElementById("regen-outline-button").disabled = !state.awaiting_outline_approval;
      document.getElementById("chapter-advice-button").disabled = !state.run_active && !state.resume_available;
      document.getElementById("regen-chapter-button").disabled = state.run_active || !state.outline_approved;
      if (state.busy && state.progress.current_agent && state.progress.current_agent !== "idle" && state.progress.current_agent !== lastAgent) {{
        playSoftCue();
      }}
      if (state.waiting_for_input && !lastWaitingForInput) {{
        playWaitingCue();
      }}
      lastAgent = state.progress.current_agent || "";
      lastWaitingForInput = Boolean(state.waiting_for_input);
      if (state.phase_version !== lastPhaseVersion) {{
        lastPhaseVersion = state.phase_version;
        flashPhase();
        const checkpointTitle = state.current_checkpoint_title || "";
        const isCompletionCheckpoint =
          /^Chapter \d+ complete$/i.test(checkpointTitle) ||
          /^Chapter \d+ regenerated$/i.test(checkpointTitle) ||
          /^Outline ready for review$/i.test(checkpointTitle) ||
          state.phase === "Generation complete";
        const completionSignal =
          state.phase === "Generation complete"
            ? `phase:${{state.phase}}`
            : (checkpointTitle ? `checkpoint:${{checkpointTitle}}` : "");
        if (isCompletionCheckpoint && completionSignal && completionSignal !== lastCompletionSignal) {{
          playWaitingCue();
          playCompleteCue();
        }}
        if (isCompletionCheckpoint && completionSignal) {{
          lastCompletionSignal = completionSignal;
        }}
      }}
      forceNextFormSync = false;
    }}
    function bindEditableFields(root = document) {{
      root.querySelectorAll("input, textarea, select").forEach((field) => {{
        if (field.dataset.dirtyBound === "true") return;
        if (field.id === "textarea-modal-input") return;
        field.dataset.dirtyBound = "true";
        field.addEventListener("input", () => {{
          if (field.id) dirtyFields.add(field.id);
        }});
        field.addEventListener("change", () => {{
          if (field.id) dirtyFields.add(field.id);
        }});
      }});
    }}
    function bindAsyncForms() {{
      document.querySelectorAll("form.async-form").forEach((form) => {{
        form.querySelectorAll("button, input[type='submit']").forEach((button) => {{
          button.addEventListener("click", () => {{
            form.dataset.submitAction = button.getAttribute("formaction") || form.getAttribute("action") || window.location.pathname;
          }});
        }});
        form.addEventListener("submit", async (event) => {{
          event.preventDefault();
          const action = form.dataset.submitAction || form.getAttribute("action") || window.location.pathname;
          const formData = new FormData(form);
          if (action === "/save-config") {{
            const setupForm = document.getElementById("run-setup-form");
            if (setupForm) {{
              new FormData(setupForm).forEach((value, key) => {{
                if (!formData.has(key)) {{
                  formData.append(key, value);
                }}
              }});
            }}
          }}
          try {{
            await fetch(action, {{
              method: "POST",
              body: new URLSearchParams(formData).toString(),
              headers: {{
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "X-Requested-With": "fetch",
              }},
            }});
            forceNextFormSync = true;
            Array.from(form.elements || []).forEach((field) => {{
              if (field.id) dirtyFields.delete(field.id);
            }});
          }} catch (error) {{
          }} finally {{
            delete form.dataset.submitAction;
          }}
        }});
      }});
    }}
    function bindChapterToggles() {{
      document.querySelectorAll(".chapter-toggle").forEach((button) => {{
        button.onclick = () => {{
          const chapter = button.closest(".chapter");
          chapter.classList.toggle("open");
        }};
      }});
    }}
    document.getElementById("token_limit_enabled").addEventListener("change", (event) => {{
      document.getElementById("max-tokens-group").style.display = event.target.value === "on" ? "block" : "none";
    }});
    document.getElementById("num_chapters").addEventListener("input", (event) => {{
      renderChapterDetailEditors(event.target.value, collectChapterDetailsFields());
    }});
    document.getElementById("external-config-button").addEventListener("click", () => {{
      document.getElementById("external-config-input").click();
    }});
    document.getElementById("external-config-input").addEventListener("change", async (event) => {{
      const file = event.target.files?.[0];
      if (!file) return;
      const text = await file.text();
      try {{
        forceNextFormSync = true;
        dirtyFields.clear();
        await fetch("/load-external-config", {{
          method: "POST",
          body: text,
          headers: {{
            "Content-Type": "application/json",
            "X-Requested-With": "fetch",
          }},
        }});
      }} catch (error) {{
      }} finally {{
        event.target.value = "";
      }}
    }});
    document.getElementById("save-external-config-button").addEventListener("click", () => {{
      const payload = buildExternalConfigFromState(lastKnownState || {{}});
      const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: "application/json" }});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const safeName = (payload.name || "external-config").replace(/[^a-z0-9_-]+/gi, "_");
      link.href = url;
      link.download = `${{safeName}}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }});
    document.getElementById("textarea-modal-close").addEventListener("click", closeTextareaModal);
    document.getElementById("textarea-modal").addEventListener("click", (event) => {{
      if (event.target.id === "textarea-modal") {{
        closeTextareaModal();
      }}
    }});
    document.getElementById("textarea-modal-input").addEventListener("input", syncModalToSource);
    document.addEventListener("keydown", (event) => {{
      if (event.key === "Escape" && document.getElementById("textarea-modal").classList.contains("open")) {{
        closeTextareaModal();
      }}
    }});
    bindEditableFields();
    ensureTextareaExpandButtons();
    bindAsyncForms();
    bindChapterToggles();
    const source = new EventSource("/events");
    source.onmessage = (event) => {{
      applyState(JSON.parse(event.data));
    }};
    source.onerror = () => {{
    }};
  </script>
</body>
</html>
"""
    return html.encode("utf-8")


def _render_page_v2() -> bytes:
    snapshot = controller.get_snapshot()
    prepared = _prepare_page_context(snapshot)
    initial_state = json.dumps(_snapshot_payload(snapshot))
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Book Writer Workspace</title>
  <link rel="stylesheet" href="/static/ui.less">
</head>
<body>
  <div class="app-shell">
    {_render_topbar(prepared)}
    {_render_context_banner(prepared)}
    {_render_primary_tabs(prepared)}
    <main class="tab-panels">
      {_render_planning_tab(prepared)}
      {_render_writing_tab(prepared)}
      {_render_editing_tab()}
    </main>
  </div>
  <div id="textarea-modal" class="modal-backdrop" aria-hidden="true">
    <div class="modal-dialog">
      <div class="modal-header">
        <h2 id="textarea-modal-title">Expanded Editor</h2>
        <button type="button" id="textarea-modal-close" class="secondary">&#10005; Close</button>
      </div>
      <textarea id="textarea-modal-input" aria-label="Expanded editor"></textarea>
    </div>
  </div>
  <script>
    window.__BOOK_UI_INITIAL_STATE__ = {initial_state};
  </script>
  <script src="/static/app.js"></script>
</body>
</html>
"""
    return html.encode("utf-8")


def _prepare_page_context(snapshot) -> Dict[str, object]:
    if snapshot.resume_available and not snapshot.run_active:
        mode_label = f"Ready to resume from Chapter {snapshot.resume_chapter_number}"
    elif snapshot.status == "stopped":
        mode_label = "Paused"
    elif snapshot.stop_requested and snapshot.run_active:
        mode_label = "Pausing after the current chapter step"
    else:
        mode_label = "Keep Going" if snapshot.mode == "keep_going" else "Ask for Advice"

    waiting = "Yes" if snapshot.waiting_for_input else "No"
    can_start = not snapshot.run_active
    can_control = snapshot.run_active
    can_queue_chapter_advice = snapshot.run_active or snapshot.resume_available
    can_continue = (
        (snapshot.run_active and snapshot.waiting_for_input and not snapshot.awaiting_outline_approval)
        or (snapshot.resume_available and not snapshot.awaiting_outline_approval)
    )
    phase_area = _phase_area(snapshot)
    status_display = _status_display(snapshot.status)
    mode_display = _mode_display(snapshot)
    banner = _context_banner(snapshot)
    outline_status_text = (
        "Approved"
        if snapshot.outline_approved
        else ("Awaiting review" if snapshot.awaiting_outline_approval else "Not approved yet")
    )
    selected_number = _selected_chapter_number(snapshot)
    selected_chapter = next((chapter for chapter in snapshot.chapters if chapter.number == selected_number), None)
    selected_review = snapshot.chapter_reviews.get(selected_number)
    chapter_items = (
        "".join(
            _render_chapter_item(chapter.number, chapter.title, chapter.status, chapter.number == selected_number)
            for chapter in snapshot.chapters
        )
        or "<li class='chapter chapter--placeholder'><span class='chapter-title'>No chapters yet.</span></li>"
    )
    model_list = snapshot.available_models or [snapshot.outline_model, snapshot.writer_model]
    unique_models = [model for index, model in enumerate(model_list) if model and model not in model_list[:index]]
    return {
        "snapshot": snapshot,
        "mode_label": mode_label,
        "waiting": waiting,
        "can_start": can_start,
        "can_control": can_control,
        "can_queue_chapter_advice": can_queue_chapter_advice,
        "can_continue": can_continue,
        "phase_area": phase_area,
        "status_display": status_display,
        "status_tone": _status_tone(snapshot),
        "mode_display": mode_display,
        "project_name": _project_name(snapshot),
        "banner": banner,
        "outline_status_text": outline_status_text,
        "selected_chapter_number": selected_number,
        "selected_chapter_title": (
            f"Chapter {selected_chapter.number}: {selected_chapter.title}"
            if selected_chapter
            else "Chapter workspace"
        ),
        "selected_chapter_status": _status_display(selected_chapter.status) if selected_chapter else "Not started",
        "selected_chapter_text": (
            selected_review.saved_text
            if selected_review and selected_review.saved_text
            else "Select a chapter from the left rail or start a run to read generated text here."
        ),
        "selected_artifacts": _render_artifacts(selected_review),
        "chapter_items": chapter_items,
        "outline_text": snapshot.outline_text or "Outline not generated yet.",
        "checkpoint_body": snapshot.current_checkpoint_body or "Nothing to review yet.",
        "models": _render_options(unique_models, snapshot.outline_model),
        "writer_models": _render_options(unique_models, snapshot.writer_model),
        "sections": snapshot.prompt_sections if snapshot.prompt_sections.premise else DEFAULT_SECTIONS,
        "chapter_details": snapshot.chapter_details or {},
        "model_error": snapshot.model_fetch_error or "No model errors.",
        "saved_configs": _render_saved_config_options(snapshot.saved_configs),
        "progress_events": "\n".join(reversed(snapshot.progress_events)) or "No progress events yet.",
        "continuity_text": _render_continuity(snapshot),
        "outline_feedback": snapshot.outline_feedback or "",
        "recent_events_text": "\n".join(snapshot.recent_events) or "No events yet.",
        "config_name": snapshot.config_name or "",
    }


def _render_topbar(context: Dict[str, object]) -> str:
    snapshot = context["snapshot"]
    start_label = "Start New Run" if snapshot.resume_available and not snapshot.run_active else "Start Run"
    continue_label = "Resume Run" if snapshot.resume_available and not snapshot.run_active else "Continue"
    return f"""
    <header class="app-topbar">
      <div class="app-topbar__identity">
        <div>
          <div class="eyebrow">Book Writer Workspace</div>
          <h1 data-bind="project-name">{escape(str(context["project_name"]))}</h1>
        </div>
        <div class="status-cluster">
          <span class="status-chip status-chip--{escape(str(context["status_tone"]))}" id="overall-status-chip" data-bind="status-display">{escape(str(context["status_display"]))}</span>
          <span class="status-chip status-chip--muted" data-bind="phase-area">{escape(str(context["phase_area"]))}</span>
          <span class="status-chip status-chip--muted" data-bind="mode-display">{escape(str(context["mode_display"]))}</span>
        </div>
      </div>
      <div class="app-topbar__metrics">
        <div class="metric-card">
          <span class="metric-label">Live phase</span>
          <div class="metric-value status-line" id="phase-status"><span id="live-spinner" class="spinner-shell{' active' if snapshot.busy else ''}"><span class="spinner-dot"></span></span>{escape(snapshot.phase or "Idle")}</div>
        </div>
        <div class="metric-card">
          <span class="metric-label">Current chapter</span>
          <div class="metric-value" data-bind="current-chapter">{snapshot.current_chapter} / {snapshot.total_chapters}</div>
        </div>
        <div class="metric-card">
          <span class="metric-label">Waiting for input</span>
          <div class="metric-value" data-bind="waiting-input">{escape(str(context["waiting"]))}</div>
        </div>
      </div>
      <div class="app-topbar__controls">
        <button id="start-button" type="submit" form="run-setup-form" {"disabled" if not context["can_start"] else ""}>&#9889; {escape(start_label)}</button>
        <form method="post" action="/mode" class="async-form inline-form">
          <input type="hidden" name="mode" value="keep_going">
          <button id="keep-going-button" class="secondary" {"disabled" if not context["can_control"] else ""}>Auto</button>
        </form>
        <form method="post" action="/mode" class="async-form inline-form">
          <input type="hidden" name="mode" value="ask_for_advice">
          <button id="ask-advice-button" class="secondary" {"disabled" if not context["can_control"] else ""}>Guided</button>
        </form>
        <form method="post" action="/continue" class="async-form inline-form">
          <button id="continue-button" {"disabled" if not context["can_continue"] else ""}>&#9658; {escape(continue_label)}</button>
        </form>
        <form method="post" action="/stop" class="async-form inline-form">
          <button id="stop-button" class="danger" {"disabled" if not context["can_control"] else ""}>&#9208; Pause</button>
        </form>
      </div>
    </header>
    """


def _render_context_banner(context: Dict[str, object]) -> str:
    banner = context["banner"]
    return f"""
    <section class="context-banner context-banner--{escape(str(banner["level"]))}" id="context-banner">
      <div class="context-banner__copy">
        <div class="eyebrow">Next Step</div>
        <h2 id="context-banner-title">{escape(str(banner["title"]))}</h2>
        <p id="context-banner-body">{escape(str(banner["body"]))}</p>
      </div>
      <div class="context-banner__meta">
        <div class="mini-metric">
          <span>Mode</span>
          <strong id="context-mode-label">{escape(str(context["mode_label"]))}</strong>
        </div>
        <div class="mini-metric">
          <span>Outline</span>
          <strong data-bind="outline-status-summary">{escape(str(context["outline_status_text"]))}</strong>
        </div>
      </div>
    </section>
    """


def _render_primary_tabs(context: Dict[str, object]) -> str:
    planning_active = context["phase_area"] == "Planning"
    writing_active = context["phase_area"] == "Writing"
    return f"""
    <nav class="primary-tabs" aria-label="Primary">
      <button type="button" class="tab-button{' active' if planning_active else ''}" id="tab-planning" data-tab-target="planning" aria-selected="{'true' if planning_active else 'false'}">Planning</button>
      <button type="button" class="tab-button{' active' if writing_active else ''}" id="tab-writing" data-tab-target="writing" aria-selected="{'true' if writing_active else 'false'}">Writing</button>
      <button type="button" class="tab-button" id="tab-editing" data-tab-target="editing" aria-selected="false">Editing</button>
    </nav>
    """


def _render_planning_tab(context: Dict[str, object]) -> str:
    snapshot = context["snapshot"]
    sections = context["sections"]
    return f"""
      <section class="tab-panel{' active' if context["phase_area"] == "Planning" else ''}" data-tab="planning">
        <div class="planning-layout">
          <aside class="workflow-sidebar">
            <section class="workspace-card">
              <div class="eyebrow">Workflow</div>
              <h2>Planning Steps</h2>
              <div class="step-list">
                <button type="button" class="step-link" data-scroll-target="planning-brief">Story Brief</button>
                <button type="button" class="step-link" data-scroll-target="planning-structure">Structure</button>
                <button type="button" class="step-link" data-scroll-target="planning-runtime">Models &amp; Runtime</button>
                <button type="button" class="step-link" data-scroll-target="planning-outline-review">Outline Review</button>
              </div>
            </section>
            <section class="workspace-card">
              <div class="eyebrow">Run Snapshot</div>
              <h2>Current State</h2>
              <div class="info-list">
                <div class="info-row"><span>Status</span><strong data-bind="status-display">{escape(str(context["status_display"]))}</strong></div>
                <div class="info-row"><span>Phase area</span><strong data-bind="phase-area">{escape(str(context["phase_area"]))}</strong></div>
                <div class="info-row"><span>Mode</span><strong data-bind="mode-display">{escape(str(context["mode_display"]))}</strong></div>
                <div class="info-row"><span>Waiting</span><strong data-bind="waiting-input">{escape(str(context["waiting"]))}</strong></div>
                <div class="info-row"><span>Current chapter</span><strong data-bind="current-chapter">{snapshot.current_chapter} / {snapshot.total_chapters}</strong></div>
                <div class="info-row"><span>Outline</span><strong data-bind="outline-status-summary">{escape(str(context["outline_status_text"]))}</strong></div>
              </div>
            </section>
            <section class="workspace-card">
              <div class="eyebrow">Guided Review</div>
              <p class="support-copy">Guided mode pauses at checkpoints so you can inspect the outline or latest chapter, add direction, and continue when you are ready.</p>
            </section>
          </aside>

          <form id="run-setup-form" method="post" action="/start" class="async-form planning-form">
            <section class="workspace-card" id="planning-brief">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">Planning</div>
                  <h2>Story Brief</h2>
                </div>
                <p class="support-copy">This is the creative foundation for the whole book. Treat it like the source of truth for the project.</p>
              </div>
              <div class="field-stack">
                <label class="label" for="premise">Premise</label>
                <textarea id="premise" name="premise">{escape(sections.premise)}</textarea>
                <label class="label" for="storylines">Storylines / Arcs</label>
                <textarea id="storylines" name="storylines">{escape(sections.storylines)}</textarea>
                <label class="label" for="setting">Setting / World</label>
                <textarea id="setting" name="setting">{escape(sections.setting)}</textarea>
                <label class="label" for="characters">Characters</label>
                <textarea id="characters" name="characters">{escape(sections.characters)}</textarea>
                <label class="label" for="writing_style">Writing Style</label>
                <textarea id="writing_style" name="writing_style">{escape(sections.writing_style)}</textarea>
                <label class="label" for="tone">Tone</label>
                <textarea id="tone" name="tone">{escape(sections.tone)}</textarea>
                <label class="label" for="plot_beats">Important Plot Beats</label>
                <textarea id="plot_beats" name="plot_beats">{escape(sections.plot_beats)}</textarea>
                <label class="label" for="constraints">Constraints / Must Include</label>
                <textarea id="constraints" name="constraints">{escape(sections.constraints)}</textarea>
              </div>
            </section>

            <section class="workspace-card" id="planning-structure">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">Planning</div>
                  <h2>Structure</h2>
                </div>
                <p class="support-copy">Set the chapter scaffold and chapter-specific anchors here so the outline and writing phases stay aligned.</p>
              </div>
              <div class="field-grid-two">
                <div>
                  <label class="label" for="num_chapters">Number of Chapters</label>
                  <input id="num_chapters" type="number" min="1" max="100" name="num_chapters" value="{snapshot.num_chapters}">
                </div>
                <div>
                  <label class="label" for="chapter_target_word_count">Chapter Target Word Count</label>
                  <input id="chapter_target_word_count" type="number" min="0" max="50000" name="chapter_target_word_count" value="{snapshot.chapter_target_word_count}">
                </div>
              </div>
              <div class="chapter-editor-shell">
                <div class="subheading">Chapter details</div>
                <div id="chapter-details-editor">
                  {_render_chapter_details_inputs(snapshot.num_chapters, context["chapter_details"])}
                </div>
              </div>
            </section>

            <section class="workspace-card" id="planning-runtime">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">Planning</div>
                  <h2>Models &amp; Runtime</h2>
                </div>
                <p class="support-copy">Keep the technical setup together here so the creative setup is not mixed with transport and model tuning.</p>
              </div>
              <div class="field-stack">
                <label class="label" for="endpoint_url">API Endpoint</label>
                <input id="endpoint_url" type="text" name="endpoint_url" value="{escape(snapshot.endpoint_url)}">
                <div class="controls">
                  <button formaction="/refresh-models" class="secondary">&#10227; Refresh Models</button>
                </div>
                <div class="field-grid-two">
                  <div>
                    <label class="label" for="outline_model">Outline Model</label>
                    <select id="outline_model" name="outline_model">{context["models"]}</select>
                  </div>
                  <div>
                    <label class="label" for="writer_model">Writer Model</label>
                    <select id="writer_model" name="writer_model">{context["writer_models"]}</select>
                  </div>
                </div>
                <div class="field-grid-three">
                  <div>
                    <label class="label" for="token_limit_enabled">Token Limit</label>
                    <select id="token_limit_enabled" name="token_limit_enabled">
                      <option value="on"{" selected" if snapshot.token_limit_enabled else ""}>On</option>
                      <option value="off"{" selected" if not snapshot.token_limit_enabled else ""}>Off</option>
                    </select>
                  </div>
                  <div id="max-tokens-group" style="display: {"block" if snapshot.token_limit_enabled else "none"};">
                    <label class="label" for="max_tokens">Max Tokens</label>
                    <input id="max_tokens" type="number" min="128" max="64000" name="max_tokens" value="{snapshot.max_tokens}">
                  </div>
                  <div>
                    <label class="label" for="max_iterations">Max Iterations</label>
                    <input id="max_iterations" type="number" min="1" max="{MAX_ITERATIONS_LIMIT}" name="max_iterations" value="{snapshot.max_iterations}">
                  </div>
                </div>
                <label class="label" for="reduce_thinking">Thinking Mode</label>
                <select id="reduce_thinking" name="reduce_thinking">
                  <option value="off"{" selected" if not snapshot.reduce_thinking else ""}>Normal</option>
                  <option value="on"{" selected" if snapshot.reduce_thinking else ""}>No Thinking</option>
                </select>
              </div>
            </section>
          </form>

          <aside class="planning-rail">
            <section class="workspace-card" id="planning-outline-review">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">Planning</div>
                  <h2>Outline Review</h2>
                </div>
                <span class="status-chip status-chip--muted" data-bind="outline-status-summary">{escape(str(context["outline_status_text"]))}</span>
              </div>
              <div class="inline-note" id="outline-approval-status">Approved: {"Yes" if snapshot.outline_approved else "No"} | Awaiting approval: {"Yes" if snapshot.awaiting_outline_approval else "No"}</div>
              <form method="post" action="/outline-feedback" class="async-form">
                <label class="label" for="outline_feedback">Outline Feedback</label>
                <textarea id="outline_feedback" name="outline_feedback" placeholder="Ask for pacing changes, stronger arcs, better chapter titles, more detail, or structural fixes.">{escape(str(context["outline_feedback"]))}</textarea>
                <div class="controls">
                  <button class="secondary">&#9998; Save Feedback</button>
                </div>
              </form>
              <form method="post" action="/approve-outline" class="async-form">
                <div class="controls">
                  <button id="approve-outline-button" {"disabled" if not snapshot.awaiting_outline_approval else ""}>&#10003; Approve Outline</button>
                  <button id="regen-outline-button" formaction="/regenerate-outline" class="secondary" {"disabled" if not snapshot.awaiting_outline_approval else ""}>&#8635; Regenerate Outline</button>
                </div>
              </form>
              <div class="subheading">Generated outline</div>
              <pre id="outline-text" class="longform-view">{escape(str(context["outline_text"]))}</pre>
            </section>

            <section class="workspace-card">
              <div class="eyebrow">Utilities</div>
              <h2>Configs</h2>
              <form method="post" action="/save-config" class="async-form">
                <label class="label" for="config_name">Save Current Setup</label>
                <input id="config_name" type="text" name="config_name" placeholder="Example: Dane thriller v1" value="{escape(str(context["config_name"]))}">
                <div class="controls">
                  <button class="secondary">&#128190; Save Config</button>
                </div>
              </form>
              <form method="post" action="/load-config" class="async-form">
                <label class="label" for="config_file">Load Saved Setup</label>
                <select id="config_file" name="config_file">{context["saved_configs"]}</select>
                <div class="controls">
                  <button class="secondary">&#128194; Load Config</button>
                </div>
              </form>
              <div class="controls">
                <button id="external-config-button" class="secondary" type="button">&#128194; Load External Config</button>
                <button id="save-external-config-button" class="secondary" type="button">&#128190; Save External Config</button>
                <input id="external-config-input" type="file" accept=".json,application/json" style="display:none;">
              </div>
            </section>

            <section class="workspace-card">
              <div class="eyebrow">Diagnostics</div>
              <h2>Model Status</h2>
              <pre id="model-error">{escape(str(context["model_error"]))}</pre>
            </section>
          </aside>
        </div>
      </section>
    """


def _render_writing_tab(context: Dict[str, object]) -> str:
    snapshot = context["snapshot"]
    return f"""
      <section class="tab-panel{' active' if context["phase_area"] == "Writing" else ''}" data-tab="writing">
        <div class="writing-layout">
          <aside class="chapter-sidebar">
            <section class="workspace-card sticky-card">
              <div class="eyebrow">Writing</div>
              <h2>Chapters</h2>
              <ul class="chapter-list" id="chapter-list">
                {context["chapter_items"]}
              </ul>
            </section>
          </aside>

          <div class="writing-main">
            <section class="workspace-card phase-box" id="checkpoint-box">
              <div class="eyebrow">Checkpoint</div>
              <h2 id="checkpoint-title">{escape(snapshot.current_checkpoint_title or "Waiting for the first checkpoint.")}</h2>
              <pre id="checkpoint-body">{escape(str(context["checkpoint_body"]))}</pre>
            </section>

            <section class="workspace-card chapter-reader-card">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">Writing Workspace</div>
                  <h2 id="selected-chapter-title">{escape(str(context["selected_chapter_title"]))}</h2>
                </div>
                <span class="status-chip status-chip--muted" id="selected-chapter-status">{escape(str(context["selected_chapter_status"]))}</span>
              </div>
              <pre id="selected-chapter-content" class="longform-view chapter-view">{escape(str(context["selected_chapter_text"]))}</pre>
            </section>
          </div>

          <aside class="writing-rail">
            <section class="workspace-card">
              <div class="eyebrow">Intervention</div>
              <h2>Chapter Tools</h2>
              <form method="post" action="/chapter-advice" class="async-form">
                <label class="label" for="chapter_advice_number">Chapter Number</label>
                <input id="chapter_advice_number" type="number" min="1" max="{snapshot.total_chapters or snapshot.num_chapters or 1}" name="chapter_number" value="{context["selected_chapter_number"] or snapshot.current_chapter or 1}">
                <label class="label" for="advice">Replacement beats for the next attempt</label>
                <textarea class="advice-box" id="advice" name="advice" placeholder="Example: 1. He stalls at the threshold. 2. She notices the blood on his cuff. 3. Their argument expands into the hidden ledger reveal."></textarea>
                <label class="label" for="chapter_advice_target_word_count">Optional replacement target word count</label>
                <input id="chapter_advice_target_word_count" type="number" min="0" max="50000" name="target_word_count" value="">
                <div class="controls">
                  <button id="chapter-advice-button" {"disabled" if not context["can_queue_chapter_advice"] else ""}>&#10148; Queue Chapter Advice</button>
                </div>
              </form>
              <form method="post" action="/regenerate-chapter" class="async-form">
                <label class="label" for="regen_chapter_number">Regenerate Chapter</label>
                <input id="regen_chapter_number" type="number" min="1" max="{snapshot.total_chapters or snapshot.num_chapters or 1}" name="chapter_number" value="{context["selected_chapter_number"] or snapshot.current_chapter or 1}">
                <div class="controls">
                  <button id="regen-chapter-button" {"disabled" if snapshot.run_active else ""}>&#8635; Regenerate Chapter</button>
                </div>
              </form>
              <div class="subheading">Last queued advice</div>
              <pre id="last-advice">{escape(snapshot.latest_advice or "No advice submitted yet.")}</pre>
            </section>

            <section class="workspace-card">
              <div class="eyebrow">Activity</div>
              <h2>Live Progress</h2>
              <div class="subgrid">
                <div class="metric-box"><span class="label">Current Agent</span><pre id="progress-agent">{escape(snapshot.progress.current_agent or "Idle")}</pre></div>
                <div class="metric-box"><span class="label">Current Step</span><pre id="progress-step">{escape(snapshot.progress.current_step or "Idle")}</pre></div>
                <div class="metric-box"><span class="label">Iteration</span><pre id="progress-iteration">{snapshot.progress.iteration or 0}/{snapshot.progress.max_iterations or 0}</pre></div>
                <div class="metric-box"><span class="label">Output Stage</span><pre id="progress-stage">{escape(snapshot.progress.output_stage or "n/a")}</pre></div>
              </div>
              <div class="subheading">Progress detail</div>
              <pre id="progress-detail">{escape(snapshot.progress.detail or "No active detail yet.")}</pre>
              <div class="subheading">Progress timeline</div>
              <pre id="progress-events">{escape(str(context["progress_events"]))}</pre>
            </section>

            <section class="workspace-card">
              <div class="eyebrow">Inspector</div>
              <h2>Chapter Artifacts</h2>
              <pre id="chapter-artifacts">{escape(str(context["selected_artifacts"]))}</pre>
            </section>

            <section class="workspace-card">
              <div class="eyebrow">Inspector</div>
              <h2>Continuity</h2>
              <pre id="continuity-panel">{escape(str(context["continuity_text"]))}</pre>
            </section>

            <section class="workspace-card">
              <div class="eyebrow">Reference</div>
              <h2>Approved Outline</h2>
              <pre id="outline-reference" class="longform-view">{escape(str(context["outline_text"]))}</pre>
            </section>

            <section class="workspace-card">
              <div class="eyebrow">Diagnostics</div>
              <h2>Errors &amp; Events</h2>
              <div class="subheading">Latest error</div>
              <pre id="latest-error">{escape(snapshot.latest_error or "No errors recorded.")}</pre>
              <div class="subheading">Recent events</div>
              <pre id="recent-events">{escape(str(context["recent_events_text"]))}</pre>
            </section>
          </aside>
        </div>
      </section>
    """


def _render_editing_tab() -> str:
    return """
      <section class="tab-panel" data-tab="editing">
        <div class="editing-layout">
          <section class="workspace-card">
            <div class="eyebrow">Future Phase</div>
            <h2>Editing Workspace</h2>
            <p class="support-copy">This area is reserved for the later companion workflow: reopen a saved book project, revise chapters, amplify scenes, and grow the book over time.</p>
            <div class="feature-list">
              <div class="feature-pill">Reload an existing book state</div>
              <div class="feature-pill">Targeted chapter refinement</div>
              <div class="feature-pill">Alternates and revision history</div>
              <div class="feature-pill">Continuity-aware updates</div>
              <div class="feature-pill">Future chapter insertion</div>
            </div>
          </section>
          <section class="workspace-card">
            <div class="eyebrow">Missing Piece</div>
            <h2>What Editing Needs First</h2>
            <p class="support-copy">The UI shell is ready for Editing, but the product still needs a persistent Book Project state that combines config, outline, chapter text, artifacts, continuity, and revision history into one resumable unit.</p>
            <pre>Book Project = setup + outline + chapters + artifacts + continuity + revision history</pre>
          </section>
        </div>
      </section>
    """


class BookUIHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/events":
            self._send_event_stream()
            return
        if path == "/state":
            self._send_state()
            return
        if path == "/static/ui.less":
            self._send_static(STYLESHEET_PATH, "text/css; charset=utf-8")
            return
        if path == "/static/app.js":
            self._send_static(os.path.join(STATIC_DIR, "app.js"), "application/javascript; charset=utf-8")
            return
        if path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        page = _render_page()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        form = parse_qs(body)
        is_async = self.headers.get("X-Requested-With", "") == "fetch"

        if path == "/start":
            num_chapters = int(_first(form, "num_chapters", "10") or "10")
            chapter_details = _extract_chapter_details(form, num_chapters)
            sections = PromptSections(
                premise=_first(form, "premise", DEFAULT_SECTIONS.premise),
                storylines=_first(form, "storylines", DEFAULT_SECTIONS.storylines),
                setting=_first(form, "setting", DEFAULT_SECTIONS.setting),
                characters=_first(form, "characters", DEFAULT_SECTIONS.characters),
                writing_style=_first(form, "writing_style", DEFAULT_SECTIONS.writing_style),
                tone=_first(form, "tone", DEFAULT_SECTIONS.tone),
                plot_beats=_first(form, "plot_beats", DEFAULT_SECTIONS.plot_beats),
                constraints=_first(form, "constraints", DEFAULT_SECTIONS.constraints),
            )
            controller.start_run(
                sections,
                chapter_details,
                num_chapters,
                _first(form, "outline_model", ""),
                _first(form, "writer_model", ""),
                _first(form, "endpoint_url", ""),
                _first(form, "token_limit_enabled", "on") == "on",
                int(_first(form, "max_tokens", "4096") or "4096"),
                _first(form, "reduce_thinking", "off") == "on",
                int(_first(form, "max_iterations", "5") or "5"),
                int(_first(form, "chapter_target_word_count", "0") or "0"),
            )
        elif path == "/refresh-models":
            controller.refresh_models(_first(form, "endpoint_url", ""))
        elif path == "/save-config":
            if "premise" in form or "num_chapters" in form:
                num_chapters = int(_first(form, "num_chapters", "10") or "10")
                chapter_details = _extract_chapter_details(form, num_chapters)
                sections = PromptSections(
                    premise=_first(form, "premise", DEFAULT_SECTIONS.premise),
                    storylines=_first(form, "storylines", DEFAULT_SECTIONS.storylines),
                    setting=_first(form, "setting", DEFAULT_SECTIONS.setting),
                    characters=_first(form, "characters", DEFAULT_SECTIONS.characters),
                    writing_style=_first(form, "writing_style", DEFAULT_SECTIONS.writing_style),
                    tone=_first(form, "tone", DEFAULT_SECTIONS.tone),
                    plot_beats=_first(form, "plot_beats", DEFAULT_SECTIONS.plot_beats),
                    constraints=_first(form, "constraints", DEFAULT_SECTIONS.constraints),
                )
                controller.save_config_data(
                    _first(form, "config_name", ""),
                    sections,
                    chapter_details,
                    num_chapters,
                    _first(form, "endpoint_url", ""),
                    _first(form, "outline_model", ""),
                    _first(form, "writer_model", ""),
                    _first(form, "token_limit_enabled", "on") == "on",
                    int(_first(form, "max_tokens", "4096") or "4096"),
                    _first(form, "reduce_thinking", "off") == "on",
                    int(_first(form, "max_iterations", "5") or "5"),
                    int(_first(form, "chapter_target_word_count", "0") or "0"),
                )
            else:
                controller.save_config(_first(form, "config_name", ""))
        elif path == "/load-config":
            controller.load_config(_first(form, "config_file", ""))
        elif path == "/load-external-config":
            try:
                payload = _load_external_config_payload(body)
            except json.JSONDecodeError as exc:
                message = f"Invalid external config JSON near line {exc.lineno}, column {exc.colno}: {exc.msg}"
                controller._log_runtime(f"[web_ui] {message}")
                controller.report_error(message)
                if is_async:
                    encoded = json.dumps({"error": message}).encode("utf-8")
                    self.send_response(HTTPStatus.BAD_REQUEST)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                self.send_error(HTTPStatus.BAD_REQUEST, message)
                return
            controller.load_config_payload(payload, "Loaded external config")
        elif path == "/outline-feedback":
            controller.set_outline_feedback(_first(form, "outline_feedback", ""))
        elif path == "/approve-outline":
            controller.approve_outline()
        elif path == "/regenerate-outline":
            controller.set_outline_feedback(_first(form, "outline_feedback", ""))
            controller.regenerate_outline()
        elif path == "/mode":
            controller.set_mode(_first(form, "mode", "keep_going"))
        elif path == "/continue":
            controller.continue_run()
        elif path in {"/advice", "/chapter-advice"}:
            snapshot = controller.get_snapshot()
            chapter_number_raw = _first(form, "chapter_number", str(snapshot.current_chapter or 1))
            try:
                chapter_number = int(chapter_number_raw or str(snapshot.current_chapter or 1))
            except ValueError:
                chapter_number = snapshot.current_chapter or 1
            target_word_count_raw = _first(form, "target_word_count", "").strip()
            try:
                target_word_count = int(target_word_count_raw) if target_word_count_raw else None
            except ValueError:
                target_word_count = None
            controller.submit_chapter_advice(
                chapter_number,
                _first(form, "advice", ""),
                target_word_count,
            )
        elif path == "/regenerate-chapter":
            controller.regenerate_chapter(int(_first(form, "chapter_number", "1") or "1"))
        elif path == "/stop":
            controller.stop_run()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if is_async:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        else:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_static(self, file_path: str, content_type: str) -> None:
        if not os.path.exists(file_path):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with open(file_path, "rb") as handle:
            payload = handle.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_state(self) -> None:
        snapshot = controller.get_snapshot()
        body = _snapshot_payload(snapshot)
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_event_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True

        last_phase_version = -1
        try:
            while True:
                snapshot = controller.wait_for_update(last_phase_version, timeout=15.0)
                payload = _snapshot_payload(snapshot)
                data = json.dumps(payload)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                last_phase_version = snapshot.phase_version
        except (BrokenPipeError, ConnectionResetError, socket.error):
            return


def _snapshot_payload(snapshot):
    if snapshot.resume_available and not snapshot.run_active:
        mode_label = f"Ready to resume from Chapter {snapshot.resume_chapter_number}"
    elif snapshot.status == "stopped":
        mode_label = "Paused"
    elif snapshot.stop_requested and snapshot.run_active:
        mode_label = "Pausing after the current chapter step"
    else:
        mode_label = "Keep Going" if snapshot.mode == "keep_going" else "Ask for Advice"
    banner = _context_banner(snapshot)
    return {
            "status": snapshot.status,
            "status_display": _status_display(snapshot.status),
            "status_tone": _status_tone(snapshot),
            "phase": snapshot.phase,
            "phase_area": _phase_area(snapshot),
            "mode": snapshot.mode,
            "mode_label": mode_label,
            "mode_display": _mode_display(snapshot),
            "run_active": snapshot.run_active,
            "busy": snapshot.busy,
            "stop_requested": snapshot.stop_requested,
            "waiting_for_input": snapshot.waiting_for_input,
            "project_name": _project_name(snapshot),
            "banner": banner,
            "resume_available": snapshot.resume_available,
            "resume_chapter_number": snapshot.resume_chapter_number,
            "current_chapter": snapshot.current_chapter,
            "total_chapters": snapshot.total_chapters,
            "num_chapters": snapshot.num_chapters,
            "endpoint_url": snapshot.endpoint_url,
            "outline_model": snapshot.outline_model,
            "writer_model": snapshot.writer_model,
            "available_models": snapshot.available_models,
            "phase_version": snapshot.phase_version,
            "current_checkpoint_title": snapshot.current_checkpoint_title,
            "current_checkpoint_body": snapshot.current_checkpoint_body,
            "outline_text": snapshot.outline_text,
            "latest_advice": snapshot.latest_advice,
            "latest_error": snapshot.latest_error,
            "model_fetch_error": snapshot.model_fetch_error,
            "outline_approved": snapshot.outline_approved,
            "awaiting_outline_approval": snapshot.awaiting_outline_approval,
            "outline_feedback": snapshot.outline_feedback,
            "recent_events": "\n".join(snapshot.recent_events) or "No events yet.",
            "token_limit_enabled": snapshot.token_limit_enabled,
            "max_tokens": snapshot.max_tokens,
            "reduce_thinking": snapshot.reduce_thinking,
            "max_iterations": snapshot.max_iterations,
            "chapter_target_word_count": snapshot.chapter_target_word_count,
            "output_folder": snapshot.output_folder,
            "config_name": snapshot.config_name,
            "chapter_details": {str(key): value for key, value in snapshot.chapter_details.items()},
            "prompt_sections": snapshot.prompt_sections.__dict__,
            "progress": snapshot.progress.__dict__,
            "progress_events": "\n".join(reversed(snapshot.progress_events)) or "No progress events yet.",
            "continuity": _render_continuity(snapshot),
            "current_artifacts": _render_artifacts(snapshot.chapter_reviews.get(snapshot.current_chapter or 1)),
            "chapters": [
                {
                    "number": chapter.number,
                    "title": chapter.title,
                    "status": chapter.status,
                    "saved_text": snapshot.chapter_reviews.get(chapter.number).saved_text if snapshot.chapter_reviews.get(chapter.number) else "",
                    "artifacts_text": _render_artifacts(snapshot.chapter_reviews.get(chapter.number)),
                }
                for chapter in snapshot.chapters
            ],
        }


def _first(form: Dict[str, list], key: str, default: str) -> str:
    values = form.get(key)
    if not values:
        return default
    return values[0]


def _extract_chapter_details(form: Dict[str, list], num_chapters: int) -> Dict[int, Dict[str, object]]:
    chapter_details: Dict[int, Dict[str, object]] = {}
    for chapter_number in range(1, num_chapters + 1):
        beats = _first(form, f"chapter_detail_beats_{chapter_number}", "").strip()
        try:
            target_word_count = int(_first(form, f"chapter_detail_wordcount_{chapter_number}", "0") or "0")
        except ValueError:
            target_word_count = 0
        if beats or target_word_count > 0:
            chapter_details[chapter_number] = {
                "beats": beats,
                "target_word_count": max(0, target_word_count),
            }
    return chapter_details


def _render_options(models: list[str], selected: str) -> str:
    if not models:
        models = [selected] if selected else [""]

    options = []
    for model in models:
        label = model or "No models found"
        is_selected = " selected" if model == selected else ""
        options.append(f'<option value="{escape(model)}"{is_selected}>{escape(label)}</option>')
    return "".join(options)


def _render_saved_config_options(saved_configs: list[str]) -> str:
    if not saved_configs:
        return '<option value="">No saved configs yet</option>'
    return "".join(
        f'<option value="{escape(name)}">{escape(name)}</option>'
        for name in saved_configs
    )


def _render_chapter_details_inputs(num_chapters: int, chapter_details: Dict[int, Dict[str, object]] | Dict[str, Dict[str, object]]) -> str:
    items = []
    for chapter_number in range(1, max(num_chapters, 1) + 1):
        details = chapter_details.get(chapter_number, {}) or chapter_details.get(str(chapter_number), {}) or {}
        beats = details.get("beats", "") if isinstance(details, dict) else ""
        target_word_count = details.get("target_word_count", 0) if isinstance(details, dict) else 0
        items.append(
            f"""
            <label class="label" for="chapter_detail_beats_{chapter_number}">Chapter {chapter_number} Details</label>
            <textarea id="chapter_detail_beats_{chapter_number}" name="chapter_detail_beats_{chapter_number}" class="chapter-detail-beats-input" data-chapter="{chapter_number}" placeholder="Required beats for Chapter {chapter_number}.">{escape(str(beats or ""))}</textarea>
            <label class="label" for="chapter_detail_wordcount_{chapter_number}">Chapter {chapter_number} Target Word Count</label>
            <input id="chapter_detail_wordcount_{chapter_number}" name="chapter_detail_wordcount_{chapter_number}" type="number" min="0" max="50000" class="chapter-detail-wordcount-input" data-chapter="{chapter_number}" value="{int(target_word_count or 0)}">
            """
        )
    return "".join(items)


def _render_chapter_item(number: int, title: str, status: str, selected: bool = False) -> str:
    status_label = _status_display(status)
    selected_class = " selected" if selected else ""
    return (
        f'<li class="chapter {escape(status)}{selected_class}" data-chapter="{number}">'
        f'<button type="button" class="chapter-toggle" data-chapter="{number}">'
        f'<span class="chapter-kicker">Chapter {number}</span>'
        f'<span class="chapter-title">{escape(title)}</span>'
        f'<span class="chapter-meta">{escape(status_label)}</span>'
        f"</button></li>"
    )


def _render_continuity(snapshot) -> str:
    parts = ["Chapter Summaries:"]
    parts.extend(snapshot.continuity.chapter_summaries[-5:] or ["None yet."])
    parts.append("")
    parts.append("Characters:")
    parts.extend(snapshot.continuity.characters[-8:] or ["None yet."])
    parts.append("")
    parts.append("World Details:")
    parts.extend(snapshot.continuity.world_details[-8:] or ["None yet."])
    parts.append("")
    parts.append("Continuity Alerts:")
    parts.extend(snapshot.continuity.alerts[-8:] or ["None yet."])
    return "\n".join(parts)


def _render_artifacts(review) -> str:
    if not review:
        return "No chapter artifacts yet."
    return "\n\n".join([
        "Draft Scene:",
        review.artifacts.draft_scene or "No draft captured yet.",
        "Editor Feedback:",
        review.artifacts.editor_feedback or "No feedback captured yet.",
        "Final Scene:",
        review.artifacts.final_scene or "No final scene captured yet.",
    ])


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), BookUIHandler)
    controller._log_runtime(f"Book Writer UI running at http://{host}:{port}")
    controller._log_runtime("Open the URL in your browser and control generation from there.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        controller._log_runtime("\nShutting down UI server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
