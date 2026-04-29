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

from config import MAX_ITERATIONS_LIMIT, WEB_UI_PORT
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

DEFAULT_OVERALL_WORD_COUNT_ADVICE = ""


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


def _outline_status_text(snapshot) -> str:
    if snapshot.outline_approved:
        return "Approved"
    if snapshot.awaiting_outline_approval:
        return "Awaiting review"
    if snapshot.outline_text:
        return "Draft ready"
    return "Not generated"


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
    if snapshot.waiting_for_input or snapshot.awaiting_outline_approval or snapshot.status == "waiting":
        return "warning"
    if snapshot.run_active and not snapshot.stop_requested:
        return "success"
    return "idle"


def _live_phase_indicator(snapshot) -> Dict[str, str]:
    if snapshot.latest_error or snapshot.status == "failed":
        return {"tone": "error", "label": "Error"}
    if snapshot.waiting_for_input or snapshot.awaiting_outline_approval or snapshot.status == "waiting":
        return {"tone": "waiting", "label": "Waiting"}
    if snapshot.stop_requested and snapshot.run_active:
        return {"tone": "idle", "label": "Pause pending"}
    if snapshot.run_active:
        return {"tone": "generating", "label": "Generating"}
    if snapshot.resume_available or snapshot.status == "stopped":
        return {"tone": "idle", "label": "Paused"}
    return {"tone": "idle", "label": "Idle"}


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
            "body": f"Open Writing to inspect the saved work, regenerate another chapter if needed, then press Continue to resume from {chapter_label}.",
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
            "body": "Use Writing to inspect the checkpoint, queue advice or regenerate a chapter if needed, then continue.",
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


def _default_reader_view(snapshot) -> str:
    if snapshot.current_chapter:
        return "chapter"
    for chapter in snapshot.chapters:
        review = snapshot.chapter_reviews.get(chapter.number)
        if review and review.saved_text:
            return "chapter"
    if snapshot.outline_text:
        return "outline"
    return "chapter" if snapshot.chapters else "outline"


def _render_page() -> bytes:
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
      {_render_monitor_tab(prepared)}
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
        mode_label = "Auto" if snapshot.mode == "keep_going" else "Guided"

    waiting = "Yes" if snapshot.waiting_for_input else "No"
    can_start = not snapshot.run_active
    can_control = snapshot.run_active
    can_queue_chapter_advice = snapshot.run_active or snapshot.resume_available
    can_regenerate_chapter = (
        snapshot.outline_approved
        and not snapshot.awaiting_outline_approval
        and (not snapshot.run_active or snapshot.waiting_for_input)
    )
    can_continue = (
        (snapshot.run_active and (snapshot.waiting_for_input or snapshot.stop_requested) and not snapshot.awaiting_outline_approval)
        or (snapshot.resume_available and not snapshot.awaiting_outline_approval)
    )
    phase_area = _phase_area(snapshot)
    status_display = _status_display(snapshot.status)
    mode_display = _mode_display(snapshot)
    banner = _context_banner(snapshot)
    outline_status_text = _outline_status_text(snapshot)
    live_phase = _live_phase_indicator(snapshot)
    selected_number = _selected_chapter_number(snapshot)
    selected_reader_view = _default_reader_view(snapshot)
    selected_chapter = (
        next((chapter for chapter in snapshot.chapters if chapter.number == selected_number), None)
        if selected_reader_view == "chapter"
        else None
    )
    selected_review = snapshot.chapter_reviews.get(selected_number) if selected_reader_view == "chapter" else None
    tool_chapter_number = selected_number or snapshot.current_chapter or 1
    selected_detail = (
        snapshot.chapter_details.get(tool_chapter_number)
        or snapshot.chapter_details.get(str(tool_chapter_number), {})
        or {}
    )
    selected_tool_review = snapshot.chapter_reviews.get(tool_chapter_number)
    selected_improvement_notes = (
        selected_tool_review.improvement_notes
        if selected_tool_review and selected_tool_review.improvement_notes
        else "No advice submitted yet for this chapter."
    )
    chapter_items = _render_outline_item(snapshot, selected_reader_view == "outline")
    chapter_items += (
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
        "can_regenerate_chapter": can_regenerate_chapter,
        "can_continue": can_continue,
        "phase_area": phase_area,
        "status_display": status_display,
        "status_tone": _status_tone(snapshot),
        "mode_display": mode_display,
        "live_phase_tone": live_phase["tone"],
        "live_phase_label": live_phase["label"],
        "project_name": _project_name(snapshot),
        "banner": banner,
        "outline_status_text": outline_status_text,
        "selected_chapter_number": selected_number,
        "tool_chapter_number": tool_chapter_number,
        "selected_reader_view": selected_reader_view,
        "selected_chapter_title": (
            "Outline"
            if selected_reader_view == "outline"
            else (
                f"Chapter {selected_chapter.number}: {selected_chapter.title}"
                if selected_chapter
                else "Chapter workspace"
            )
        ),
        "selected_chapter_status": (
            outline_status_text
            if selected_reader_view == "outline"
            else (_status_display(selected_chapter.status) if selected_chapter else "Not started")
        ),
        "selected_chapter_text": (
            (snapshot.outline_text or "Outline not generated yet.")
            if selected_reader_view == "outline"
            else (
                selected_review.saved_text
                if selected_review and selected_review.saved_text
                else "Select a chapter from the left rail or start a run to read generated text here."
            )
        ),
        "selected_artifacts": (
            "Outline view does not have chapter artifacts."
            if selected_reader_view == "outline"
            else _render_artifacts(selected_review)
        ),
        "selected_chapter_detail": selected_detail,
        "selected_chapter_improvement_notes": selected_improvement_notes,
        "chapter_items": chapter_items,
        "outline_text": snapshot.outline_text or "Outline not generated yet.",
        "checkpoint_body": snapshot.current_checkpoint_body or "Nothing to review yet.",
        "models": _render_options(unique_models, snapshot.outline_model),
        "writer_models": _render_options(unique_models, snapshot.writer_model),
        "sections": snapshot.prompt_sections if snapshot.prompt_sections.premise else DEFAULT_SECTIONS,
        "overall_word_count_advice": snapshot.overall_word_count_advice or DEFAULT_OVERALL_WORD_COUNT_ADVICE,
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
          <span class="status-chip status-chip--{escape(str(context["status_tone"]))}" id="overall-status-chip">
            <span class="phase-indicator__dot status-chip__dot"></span>
            <span id="overall-status-text" data-bind="status-display">{escape(str(context["status_display"]))}</span>
          </span>
        </div>
      </div>
      <div class="app-topbar__metrics">
        <div class="metric-card">
          <span class="metric-label">Live phase</span>
          <div class="metric-value phase-status__text" id="phase-status">{escape(snapshot.phase or "Idle")}</div>
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
        <div class="control-row">
          <button id="start-button" type="submit" form="run-setup-form" {"disabled" if not context["can_start"] else ""}>&#9889; {escape(start_label)}</button>
          <form method="post" action="/mode" class="async-form inline-form">
            <input type="hidden" name="mode" value="keep_going">
            <button id="keep-going-button" class="secondary" {"disabled" if not context["can_control"] else ""}>Auto</button>
          </form>
          <form method="post" action="/mode" class="async-form inline-form">
            <input type="hidden" name="mode" value="ask_for_advice">
            <button id="ask-advice-button" class="secondary" {"disabled" if not context["can_control"] else ""}>Guided</button>
          </form>
        </div>
        <div class="control-row control-row--secondary">
          <form method="post" action="/continue" class="async-form inline-form">
            <button id="continue-button" {"disabled" if not context["can_continue"] else ""}>&#9658; {escape(continue_label)}</button>
          </form>
          <form method="post" action="/stop" class="async-form inline-form">
            <button id="stop-button" class="danger" {"disabled" if not context["can_control"] else ""}>&#9208; Pause</button>
          </form>
        </div>
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
      <button type="button" class="tab-button" id="tab-monitor" data-tab-target="monitor" aria-selected="false">Monitor</button>
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
              <div class="field-stack" style="margin-top: 16px;">
                <label class="label" for="overall_word_count_advice">Overall Word Count Advice</label>
                <textarea id="overall_word_count_advice" name="overall_word_count_advice">{escape(str(context["overall_word_count_advice"]))}</textarea>
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
              <form method="post" action="/approve-outline" class="async-form">
                <label class="label" for="outline_feedback">Outline Feedback</label>
                <textarea id="outline_feedback" name="outline_feedback" placeholder="Ask for pacing changes, stronger arcs, better chapter titles, more detail, or structural fixes.">{escape(str(context["outline_feedback"]))}</textarea>
                <div class="controls">
                  <button id="approve-outline-button" {"disabled" if not snapshot.awaiting_outline_approval else ""}>&#10003; Approve Outline</button>
                  <button id="regen-outline-button" formaction="/regenerate-outline" class="secondary" {"disabled" if not snapshot.awaiting_outline_approval else ""}>&#8635; Regenerate Outline</button>
                </div>
                  <br />
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
                <div class="controls controls--stack">
                  <button class="secondary">&#128190; Save Config</button>
                </div>
              </form>
              <form method="post" action="/load-config" class="async-form">
                <label class="label" for="config_file">Load Saved Setup</label>
                <select id="config_file" name="config_file">{context["saved_configs"]}</select>
                <div class="controls controls--stack">
                  <button class="secondary">&#128194; Load Config</button>
                </div>
              </form>
              <div class="controls controls--stack">
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
              <h2>Outline &amp; Chapters</h2>
              <ul class="chapter-list" id="chapter-list">
                {context["chapter_items"]}
              </ul>
            </section>
          </aside>

          <div class="writing-main">
            <section class="workspace-card chapter-reader-card">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">Writing Workspace</div>
                  <h2 id="selected-chapter-title">{escape(str(context["selected_chapter_title"]))}</h2>
                </div>
                <span class="status-chip status-chip--muted" id="selected-chapter-status">{escape(str(context["selected_chapter_status"]))}</span>
              </div>
              <pre id="selected-chapter-content" class="chapter-reader-content">{escape(str(context["selected_chapter_text"]))}</pre>
            </section>
          </div>

          <section class="workspace-card phase-box" id="checkpoint-box">
            <div class="eyebrow">Checkpoint</div>
            <h2 id="checkpoint-title">{escape(snapshot.current_checkpoint_title or "Waiting for the first checkpoint.")}</h2>
            <pre id="checkpoint-body">{escape(str(context["checkpoint_body"]))}</pre>
          </section>

          <aside class="writing-rail">
            <section class="workspace-card">
              <div class="eyebrow">Intervention</div>
              <h2>Chapter Tools</h2>
              {_render_chapter_tool_inputs(
                  context["tool_chapter_number"],
                  context["selected_chapter_detail"],
                  snapshot.total_chapters or snapshot.num_chapters or 1,
                  context["can_queue_chapter_advice"],
                  context["can_regenerate_chapter"],
              )}
              <div class="subheading">Last queued advice</div>
              <pre id="last-advice">{escape(str(context["selected_chapter_improvement_notes"]))}</pre>
            </section>

            <section class="workspace-card">
              <div class="eyebrow">Inspector</div>
              <h2>Continuity</h2>
              <pre id="continuity-panel">{escape(str(context["continuity_text"]))}</pre>
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


def _render_monitor_tab(context: Dict[str, object]) -> str:
    snapshot = context["snapshot"]
    latest_input = snapshot.latest_llm_input or "No LLM input captured yet."
    latest_output = snapshot.latest_llm_output or "No LLM output captured yet."
    input_label = snapshot.latest_llm_input_label or "Waiting for first LLM call"
    output_label = snapshot.latest_llm_output_label or "Waiting for first LLM response"
    updated_at = snapshot.latest_llm_updated_at or "Not updated yet"
    return f"""
      <section class="tab-panel" data-tab="monitor">
        <div class="monitor-layout">
          <section class="workspace-card monitor-card">
            <div class="monitor-panel-heading">
              <div>
                <div class="eyebrow">Monitor</div>
                <h2>Latest LLM Input</h2>
                <p class="support-copy" id="latest-llm-input-label">{escape(input_label)}</p>
              </div>
              <button type="button" class="secondary monitor-copy-button" data-copy-target="latest-llm-input">&#128203; Copy</button>
            </div>
            <div class="inline-note">Updated: <span id="latest-llm-input-updated">{escape(updated_at)}</span></div>
            <pre id="latest-llm-input" class="monitor-text-panel">{escape(latest_input)}</pre>
          </section>
          <section class="workspace-card monitor-card">
            <div class="monitor-panel-heading">
              <div>
                <div class="eyebrow">Monitor</div>
                <h2>Latest LLM Output</h2>
                <p class="support-copy" id="latest-llm-output-label">{escape(output_label)}</p>
              </div>
              <button type="button" class="secondary monitor-copy-button" data-copy-target="latest-llm-output">&#128203; Copy</button>
            </div>
            <div class="inline-note">Updated: <span id="latest-llm-output-updated">{escape(updated_at)}</span></div>
            <pre id="latest-llm-output" class="monitor-text-panel">{escape(latest_output)}</pre>
          </section>
        </div>
      </section>
    """


class BookUIHandler(BaseHTTPRequestHandler):
    def _send_async_error(self, status: HTTPStatus, message: str) -> None:
        encoded = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

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
            sections = _prompt_sections_from_form(form)
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
                _first(form, "overall_word_count_advice", DEFAULT_OVERALL_WORD_COUNT_ADVICE),
            )
        elif path == "/update-runtime-settings":
            _apply_runtime_settings_from_form(form)
        elif path == "/refresh-models":
            controller.refresh_models(_first(form, "endpoint_url", ""))
        elif path == "/save-config":
            _apply_runtime_settings_from_form(form)
            if "premise" in form or "num_chapters" in form:
                num_chapters = int(_first(form, "num_chapters", "10") or "10")
                chapter_details = _extract_chapter_details(form, num_chapters)
                sections = _prompt_sections_from_form(form)
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
                    _first(form, "overall_word_count_advice", DEFAULT_OVERALL_WORD_COUNT_ADVICE),
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
            _apply_runtime_settings_from_form(form)
            controller.approve_outline()
        elif path == "/regenerate-outline":
            _apply_runtime_settings_from_form(form)
            controller.set_outline_feedback(_first(form, "outline_feedback", ""))
            controller.regenerate_outline()
        elif path == "/mode":
            _apply_runtime_settings_from_form(form)
            controller.set_mode(_first(form, "mode", "keep_going"))
        elif path == "/continue":
            _apply_runtime_settings_from_form(form)
            controller.continue_run()
        elif path in {"/advice", "/chapter-advice"}:
            _apply_runtime_settings_from_form(form)
            snapshot = controller.get_snapshot()
            chapter_number_raw = _first(form, "chapter_number", str(snapshot.current_chapter or 1))
            try:
                chapter_number = int(chapter_number_raw or str(snapshot.current_chapter or 1))
            except ValueError:
                chapter_number = snapshot.current_chapter or 1
            chapter_detail, has_structured_fields = _extract_chapter_tool_detail(form)
            controller.submit_chapter_advice(
                chapter_number,
                chapter_detail,
                replace_existing=has_structured_fields,
            )
        elif path == "/regenerate-chapter":
            _apply_runtime_settings_from_form(form)
            if "premise" in form or "chapter_detail_beats_1" in form or "overall_word_count_advice" in form:
                snapshot = controller.get_snapshot()
                detail_count = int(snapshot.total_chapters or snapshot.num_chapters or 1)
                controller.update_runtime_planning(
                    _prompt_sections_from_form(form),
                    _extract_chapter_details(form, detail_count),
                    int(_first(form, "chapter_target_word_count", str(snapshot.chapter_target_word_count or 0)) or "0"),
                    _first(form, "overall_word_count_advice", snapshot.overall_word_count_advice or DEFAULT_OVERALL_WORD_COUNT_ADVICE),
                )
            chapter_detail, has_structured_fields = _extract_chapter_tool_detail(form)
            chapter_number_raw = _first(form, "chapter_number", "1")
            try:
                chapter_number = int(chapter_number_raw or "1")
            except ValueError:
                chapter_number = 1
            if has_structured_fields:
                controller.submit_chapter_advice(
                    chapter_number,
                    chapter_detail,
                    replace_existing=True,
                    resume_if_keep_going=False,
                )
            started = controller.regenerate_chapter(chapter_number)
            if not started:
                message = controller.get_snapshot().latest_error or "Unable to regenerate that chapter right now."
                if is_async:
                    self._send_async_error(HTTPStatus.CONFLICT, message)
                    return
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
        mode_label = "Auto" if snapshot.mode == "keep_going" else "Guided"
    banner = _context_banner(snapshot)
    live_phase = _live_phase_indicator(snapshot)
    return {
            "status": snapshot.status,
            "status_display": _status_display(snapshot.status),
            "status_tone": _status_tone(snapshot),
            "phase": snapshot.phase,
            "phase_area": _phase_area(snapshot),
            "live_phase_tone": live_phase["tone"],
            "live_phase_label": live_phase["label"],
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
            "latest_llm_input": snapshot.latest_llm_input,
            "latest_llm_input_label": snapshot.latest_llm_input_label,
            "latest_llm_output": snapshot.latest_llm_output,
            "latest_llm_output_label": snapshot.latest_llm_output_label,
            "latest_llm_updated_at": snapshot.latest_llm_updated_at,
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
            "overall_word_count_advice": snapshot.overall_word_count_advice,
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
                    "improvement_notes": snapshot.chapter_reviews.get(chapter.number).improvement_notes if snapshot.chapter_reviews.get(chapter.number) else "",
                }
                for chapter in snapshot.chapters
            ],
        }


def _first(form: Dict[str, list], key: str, default: str) -> str:
    values = form.get(key)
    if not values:
        return default
    return values[0]


def _optional_int(form: Dict[str, list], key: str) -> int | None:
    if key not in form:
        return None
    try:
        return int(_first(form, key, "0") or "0")
    except ValueError:
        return None


def _apply_runtime_settings_from_form(form: Dict[str, list]) -> None:
    runtime_field_names = {
        "endpoint_url",
        "outline_model",
        "writer_model",
        "token_limit_enabled",
        "max_tokens",
        "reduce_thinking",
        "max_iterations",
    }
    if not any(name in form for name in runtime_field_names):
        return
    controller.update_runtime_settings(
        endpoint_url=_first(form, "endpoint_url", "") if "endpoint_url" in form else None,
        outline_model=_first(form, "outline_model", "") if "outline_model" in form else None,
        writer_model=_first(form, "writer_model", "") if "writer_model" in form else None,
        token_limit_enabled=(_first(form, "token_limit_enabled", "off") == "on")
        if "token_limit_enabled" in form else None,
        max_tokens=_optional_int(form, "max_tokens"),
        reduce_thinking=(_first(form, "reduce_thinking", "off") == "on")
        if "reduce_thinking" in form else None,
        max_iterations=_optional_int(form, "max_iterations"),
    )


def _prompt_sections_from_form(form: Dict[str, list]) -> PromptSections:
    return PromptSections(
        premise=_first(form, "premise", DEFAULT_SECTIONS.premise),
        storylines=_first(form, "storylines", DEFAULT_SECTIONS.storylines),
        setting=_first(form, "setting", DEFAULT_SECTIONS.setting),
        characters=_first(form, "characters", DEFAULT_SECTIONS.characters),
        writing_style=_first(form, "writing_style", DEFAULT_SECTIONS.writing_style),
        tone=_first(form, "tone", DEFAULT_SECTIONS.tone),
        plot_beats=_first(form, "plot_beats", DEFAULT_SECTIONS.plot_beats),
        constraints=_first(form, "constraints", DEFAULT_SECTIONS.constraints),
    )


def _parse_text_list(value: str) -> list[str]:
    return [
        re.sub(r"^[-*\d\.\)\s]+", "", line.strip()).strip()
        for line in (value or "").splitlines()
        if line.strip()
    ]


def _render_text_list(value: object) -> str:
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ""
    return str(value).strip()


def _extract_chapter_details(form: Dict[str, list], num_chapters: int) -> Dict[int, Dict[str, object]]:
    chapter_details: Dict[int, Dict[str, object]] = {}
    for chapter_number in range(1, num_chapters + 1):
        purpose = _first(form, f"chapter_detail_purpose_{chapter_number}", "").strip()
        beats = _first(form, f"chapter_detail_beats_{chapter_number}", "").strip()
        characters = _first(form, f"chapter_detail_characters_{chapter_number}", "").strip()
        setting = _first(form, f"chapter_detail_setting_{chapter_number}", "").strip()
        tone = _first(form, f"chapter_detail_tone_{chapter_number}", "").strip()
        must_include = _parse_text_list(_first(form, f"chapter_detail_must_include_{chapter_number}", ""))
        avoid = _parse_text_list(_first(form, f"chapter_detail_avoid_{chapter_number}", ""))
        guidance_emphasis = _first(form, f"chapter_detail_guidance_emphasis_{chapter_number}", "").strip()
        guidance_compression = _first(form, f"chapter_detail_guidance_compression_{chapter_number}", "").strip()
        guidance_opening = _first(form, f"chapter_detail_guidance_opening_{chapter_number}", "").strip()
        guidance_middle = _first(form, f"chapter_detail_guidance_middle_{chapter_number}", "").strip()
        guidance_ending = _first(form, f"chapter_detail_guidance_ending_{chapter_number}", "").strip()
        try:
            target_word_count = int(_first(form, f"chapter_detail_wordcount_{chapter_number}", "0") or "0")
        except ValueError:
            target_word_count = 0
        chapter_guidance: Dict[str, object] = {}
        distribution = {
            "opening": guidance_opening,
            "middle": guidance_middle,
            "ending": guidance_ending,
        }
        distribution = {key: value for key, value in distribution.items() if value}
        if distribution:
            chapter_guidance["word_count_distribution"] = distribution
        if guidance_emphasis:
            chapter_guidance["emphasis"] = guidance_emphasis
        if guidance_compression:
            chapter_guidance["compression"] = guidance_compression

        detail: Dict[str, object] = {}
        if purpose:
            detail["purpose"] = purpose
        if beats:
            detail["beats"] = beats
        if target_word_count > 0:
            detail["target_word_count"] = max(0, target_word_count)
        if characters:
            detail["characters"] = characters
        if setting:
            detail["setting"] = setting
        if tone:
            detail["tone"] = tone
        if must_include:
            detail["must_include"] = must_include
        if avoid:
            detail["avoid"] = avoid
        if chapter_guidance:
            detail["chapter_guidance"] = chapter_guidance
        if detail:
            chapter_details[chapter_number] = detail
    return chapter_details


def _extract_chapter_tool_detail(form: Dict[str, list]) -> tuple[Dict[str, object], bool]:
    structured_field_names = (
        "chapter_tool_purpose",
        "chapter_tool_beats",
        "chapter_tool_target_word_count",
        "chapter_tool_tone",
        "chapter_tool_characters",
        "chapter_tool_setting",
        "chapter_tool_must_include",
        "chapter_tool_avoid",
        "chapter_tool_guidance_emphasis",
        "chapter_tool_guidance_compression",
        "chapter_tool_guidance_opening",
        "chapter_tool_guidance_middle",
        "chapter_tool_guidance_ending",
    )
    has_structured_fields = any(name in form for name in structured_field_names)
    if has_structured_fields:
        purpose = _first(form, "chapter_tool_purpose", "").strip()
        beats = _first(form, "chapter_tool_beats", "").strip()
        characters = _first(form, "chapter_tool_characters", "").strip()
        setting = _first(form, "chapter_tool_setting", "").strip()
        tone = _first(form, "chapter_tool_tone", "").strip()
        must_include = _parse_text_list(_first(form, "chapter_tool_must_include", ""))
        avoid = _parse_text_list(_first(form, "chapter_tool_avoid", ""))
        guidance_emphasis = _first(form, "chapter_tool_guidance_emphasis", "").strip()
        guidance_compression = _first(form, "chapter_tool_guidance_compression", "").strip()
        guidance_opening = _first(form, "chapter_tool_guidance_opening", "").strip()
        guidance_middle = _first(form, "chapter_tool_guidance_middle", "").strip()
        guidance_ending = _first(form, "chapter_tool_guidance_ending", "").strip()
        try:
            target_word_count = int(_first(form, "chapter_tool_target_word_count", "0") or "0")
        except ValueError:
            target_word_count = 0

        chapter_guidance: Dict[str, object] = {}
        distribution = {
            "opening": guidance_opening,
            "middle": guidance_middle,
            "ending": guidance_ending,
        }
        distribution = {key: value for key, value in distribution.items() if value}
        if distribution:
            chapter_guidance["word_count_distribution"] = distribution
        if guidance_emphasis:
            chapter_guidance["emphasis"] = guidance_emphasis
        if guidance_compression:
            chapter_guidance["compression"] = guidance_compression

        detail: Dict[str, object] = {
            "purpose": purpose,
            "beats": beats,
            "target_word_count": max(0, target_word_count),
            "tone": tone,
            "characters": characters,
            "setting": setting,
            "must_include": must_include,
            "avoid": avoid,
            "chapter_guidance": chapter_guidance,
        }
        return detail, True

    beats = _first(form, "advice", "").strip()
    target_word_count_raw = _first(form, "target_word_count", "").strip()
    try:
        target_word_count = int(target_word_count_raw) if target_word_count_raw else 0
    except ValueError:
        target_word_count = 0
    detail = {}
    if beats:
        detail["beats"] = beats
    if target_word_count > 0:
        detail["target_word_count"] = target_word_count
    return detail, False


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
        purpose = details.get("purpose", "") if isinstance(details, dict) else ""
        beats = details.get("beats", "") if isinstance(details, dict) else ""
        target_word_count = details.get("target_word_count", 0) if isinstance(details, dict) else 0
        characters = details.get("characters", "") if isinstance(details, dict) else ""
        setting = details.get("setting", "") if isinstance(details, dict) else ""
        tone = details.get("tone", "") if isinstance(details, dict) else ""
        must_include = details.get("must_include", []) if isinstance(details, dict) else []
        avoid = details.get("avoid", []) if isinstance(details, dict) else []
        chapter_guidance = details.get("chapter_guidance", {}) if isinstance(details, dict) and isinstance(details.get("chapter_guidance"), dict) else {}
        distribution = chapter_guidance.get("word_count_distribution", {}) if isinstance(chapter_guidance.get("word_count_distribution"), dict) else {}
        items.append(
            f"""
            <div class="chapter-detail-group" data-chapter-group="{chapter_number}">
              <div class="chapter-detail-group__heading">Chapter {chapter_number}</div>
              <div class="chapter-detail-group__fields chapter-detail-group__fields--rich">
                <div class="chapter-detail-group__field chapter-detail-group__field--full">
                  <label class="label" for="chapter_detail_purpose_{chapter_number}">Purpose</label>
                  <textarea id="chapter_detail_purpose_{chapter_number}" name="chapter_detail_purpose_{chapter_number}" data-chapter="{chapter_number}" data-detail-path="purpose" placeholder="What this chapter needs to accomplish.">{escape(str(purpose or ""))}</textarea>
                </div>
                <div class="chapter-detail-group__field chapter-detail-group__field--full">
                  <label class="label" for="chapter_detail_beats_{chapter_number}">Beats</label>
                  <textarea id="chapter_detail_beats_{chapter_number}" name="chapter_detail_beats_{chapter_number}" class="chapter-detail-beats-input" data-chapter="{chapter_number}" data-detail-path="beats" placeholder="Required beats for Chapter {chapter_number}.">{escape(str(beats or ""))}</textarea>
                </div>
                <div class="field-grid-two chapter-detail-group__field chapter-detail-group__field--full">
                  <div>
                    <label class="label" for="chapter_detail_wordcount_{chapter_number}">Target Word Count</label>
                    <input id="chapter_detail_wordcount_{chapter_number}" name="chapter_detail_wordcount_{chapter_number}" type="number" min="0" max="50000" class="chapter-detail-wordcount-input" data-chapter="{chapter_number}" data-detail-path="target_word_count" data-value-type="number" value="{int(target_word_count or 0)}">
                  </div>
                  <div>
                    <label class="label" for="chapter_detail_tone_{chapter_number}">Tone</label>
                    <textarea id="chapter_detail_tone_{chapter_number}" name="chapter_detail_tone_{chapter_number}" data-chapter="{chapter_number}" data-detail-path="tone" placeholder="Chapter-specific emotional and narrative tone.">{escape(str(tone or ""))}</textarea>
                  </div>
                </div>
                <div class="chapter-detail-group__field chapter-detail-group__field--full">
                  <label class="label" for="chapter_detail_characters_{chapter_number}">Characters</label>
                  <textarea id="chapter_detail_characters_{chapter_number}" name="chapter_detail_characters_{chapter_number}" data-chapter="{chapter_number}" data-detail-path="characters" placeholder="Who this chapter should foreground.">{escape(str(characters or ""))}</textarea>
                </div>
                <div class="chapter-detail-group__field chapter-detail-group__field--full">
                  <label class="label" for="chapter_detail_setting_{chapter_number}">Setting</label>
                  <textarea id="chapter_detail_setting_{chapter_number}" name="chapter_detail_setting_{chapter_number}" data-chapter="{chapter_number}" data-detail-path="setting" placeholder="Specific location, atmosphere, and physical context.">{escape(str(setting or ""))}</textarea>
                </div>
                <div class="field-grid-two chapter-detail-group__field chapter-detail-group__field--full">
                  <div>
                    <label class="label" for="chapter_detail_guidance_emphasis_{chapter_number}">Guidance: Emphasis</label>
                    <textarea id="chapter_detail_guidance_emphasis_{chapter_number}" name="chapter_detail_guidance_emphasis_{chapter_number}" data-chapter="{chapter_number}" data-detail-path="chapter_guidance.emphasis" placeholder="Where the chapter should spend its weight.">{escape(str(chapter_guidance.get("emphasis", "") or ""))}</textarea>
                  </div>
                  <div>
                    <label class="label" for="chapter_detail_guidance_compression_{chapter_number}">Guidance: Compression</label>
                    <textarea id="chapter_detail_guidance_compression_{chapter_number}" name="chapter_detail_guidance_compression_{chapter_number}" data-chapter="{chapter_number}" data-detail-path="chapter_guidance.compression" placeholder="What should stay brief or tight.">{escape(str(chapter_guidance.get("compression", "") or ""))}</textarea>
                  </div>
                </div>
                <div class="field-grid-three chapter-detail-group__field chapter-detail-group__field--full">
                  <div>
                    <label class="label" for="chapter_detail_guidance_opening_{chapter_number}">Opening Share</label>
                    <input id="chapter_detail_guidance_opening_{chapter_number}" name="chapter_detail_guidance_opening_{chapter_number}" type="text" data-chapter="{chapter_number}" data-detail-path="chapter_guidance.word_count_distribution.opening" placeholder="15%" value="{escape(str(distribution.get("opening", "") or ""))}">
                  </div>
                  <div>
                    <label class="label" for="chapter_detail_guidance_middle_{chapter_number}">Middle Share</label>
                    <input id="chapter_detail_guidance_middle_{chapter_number}" name="chapter_detail_guidance_middle_{chapter_number}" type="text" data-chapter="{chapter_number}" data-detail-path="chapter_guidance.word_count_distribution.middle" placeholder="55%" value="{escape(str(distribution.get("middle", "") or ""))}">
                  </div>
                  <div>
                    <label class="label" for="chapter_detail_guidance_ending_{chapter_number}">Ending Share</label>
                    <input id="chapter_detail_guidance_ending_{chapter_number}" name="chapter_detail_guidance_ending_{chapter_number}" type="text" data-chapter="{chapter_number}" data-detail-path="chapter_guidance.word_count_distribution.ending" placeholder="30%" value="{escape(str(distribution.get("ending", "") or ""))}">
                  </div>
                </div>
                <div class="field-grid-two chapter-detail-group__field chapter-detail-group__field--full">
                  <div>
                    <label class="label" for="chapter_detail_must_include_{chapter_number}">Must Include</label>
                    <textarea id="chapter_detail_must_include_{chapter_number}" name="chapter_detail_must_include_{chapter_number}" data-chapter="{chapter_number}" data-detail-path="must_include" data-value-type="list" placeholder="One item per line.">{escape(_render_text_list(must_include))}</textarea>
                  </div>
                  <div>
                    <label class="label" for="chapter_detail_avoid_{chapter_number}">Avoid</label>
                    <textarea id="chapter_detail_avoid_{chapter_number}" name="chapter_detail_avoid_{chapter_number}" data-chapter="{chapter_number}" data-detail-path="avoid" data-value-type="list" placeholder="One item per line.">{escape(_render_text_list(avoid))}</textarea>
                  </div>
                </div>
                <div class="chapter-detail-group__field chapter-detail-group__field--full">
                  <div class="support-copy">All fields are optional. Old configs with only beats and target word count will continue to load correctly.</div>
                </div>
              </div>
            </div>
            """
        )
    return "".join(items)


def _render_chapter_tool_inputs(
    chapter_number: int,
    chapter_detail: Dict[str, object] | None,
    max_chapters: int,
    can_queue_chapter_advice: bool,
    can_regenerate_chapter: bool,
) -> str:
    details = chapter_detail if isinstance(chapter_detail, dict) else {}
    purpose = details.get("purpose", "")
    beats = details.get("beats", "")
    target_word_count = details.get("target_word_count", 0)
    characters = details.get("characters", "")
    setting = details.get("setting", "")
    tone = details.get("tone", "")
    must_include = details.get("must_include", [])
    avoid = details.get("avoid", [])
    chapter_guidance = details.get("chapter_guidance", {}) if isinstance(details.get("chapter_guidance"), dict) else {}
    distribution = chapter_guidance.get("word_count_distribution", {}) if isinstance(chapter_guidance.get("word_count_distribution"), dict) else {}
    selected_chapter = max(1, min(max_chapters, int(chapter_number or 1)))
    return f"""
              <form method="post" action="/chapter-advice" class="async-form" id="chapter-tools-form">
                <label class="label" for="chapter_tools_number">Chapter Number</label>
                <input id="chapter_tools_number" type="number" min="1" max="{max(1, max_chapters)}" name="chapter_number" value="{selected_chapter}">
                <div class="support-copy">The form loads the latest stored values for the selected chapter, whether they came from the original setup or a later queued advice update.</div>
                <div class="chapter-detail-group chapter-detail-group--tools">
                  <div class="chapter-detail-group__fields chapter-detail-group__fields--rich">
                    <div class="chapter-detail-group__field chapter-detail-group__field--full">
                      <label class="label" for="chapter_tool_purpose">Purpose</label>
                      <textarea id="chapter_tool_purpose" name="chapter_tool_purpose" placeholder="What this chapter needs to accomplish.">{escape(str(purpose or ""))}</textarea>
                    </div>
                    <div class="chapter-detail-group__field chapter-detail-group__field--full">
                      <label class="label" for="chapter_tool_beats">Beats</label>
                      <textarea id="chapter_tool_beats" name="chapter_tool_beats" placeholder="Required beats for this chapter.">{escape(str(beats or ""))}</textarea>
                    </div>
                    <div class="field-grid-two chapter-detail-group__field chapter-detail-group__field--full">
                      <div>
                        <label class="label" for="chapter_tool_target_word_count">Target Word Count</label>
                        <input id="chapter_tool_target_word_count" name="chapter_tool_target_word_count" type="number" min="0" max="50000" value="{int(target_word_count or 0)}">
                      </div>
                      <div>
                        <label class="label" for="chapter_tool_tone">Tone</label>
                        <textarea id="chapter_tool_tone" name="chapter_tool_tone" placeholder="Chapter-specific emotional and narrative tone.">{escape(str(tone or ""))}</textarea>
                      </div>
                    </div>
                    <div class="chapter-detail-group__field chapter-detail-group__field--full">
                      <label class="label" for="chapter_tool_characters">Characters</label>
                      <textarea id="chapter_tool_characters" name="chapter_tool_characters" placeholder="Who this chapter should foreground.">{escape(str(characters or ""))}</textarea>
                    </div>
                    <div class="chapter-detail-group__field chapter-detail-group__field--full">
                      <label class="label" for="chapter_tool_setting">Setting</label>
                      <textarea id="chapter_tool_setting" name="chapter_tool_setting" placeholder="Specific location, atmosphere, and physical context.">{escape(str(setting or ""))}</textarea>
                    </div>
                    <div class="field-grid-two chapter-detail-group__field chapter-detail-group__field--full">
                      <div>
                        <label class="label" for="chapter_tool_guidance_emphasis">Guidance: Emphasis</label>
                        <textarea id="chapter_tool_guidance_emphasis" name="chapter_tool_guidance_emphasis" placeholder="Where the chapter should spend its weight.">{escape(str(chapter_guidance.get("emphasis", "") or ""))}</textarea>
                      </div>
                      <div>
                        <label class="label" for="chapter_tool_guidance_compression">Guidance: Compression</label>
                        <textarea id="chapter_tool_guidance_compression" name="chapter_tool_guidance_compression" placeholder="What should stay brief or tight.">{escape(str(chapter_guidance.get("compression", "") or ""))}</textarea>
                      </div>
                    </div>
                    <div class="field-grid-three chapter-detail-group__field chapter-detail-group__field--full">
                      <div>
                        <label class="label" for="chapter_tool_guidance_opening">Opening Share</label>
                        <input id="chapter_tool_guidance_opening" name="chapter_tool_guidance_opening" type="text" placeholder="15%" value="{escape(str(distribution.get("opening", "") or ""))}">
                      </div>
                      <div>
                        <label class="label" for="chapter_tool_guidance_middle">Middle Share</label>
                        <input id="chapter_tool_guidance_middle" name="chapter_tool_guidance_middle" type="text" placeholder="55%" value="{escape(str(distribution.get("middle", "") or ""))}">
                      </div>
                      <div>
                        <label class="label" for="chapter_tool_guidance_ending">Ending Share</label>
                        <input id="chapter_tool_guidance_ending" name="chapter_tool_guidance_ending" type="text" placeholder="30%" value="{escape(str(distribution.get("ending", "") or ""))}">
                      </div>
                    </div>
                    <div class="field-grid-two chapter-detail-group__field chapter-detail-group__field--full">
                      <div>
                        <label class="label" for="chapter_tool_must_include">Must Include</label>
                        <textarea id="chapter_tool_must_include" name="chapter_tool_must_include" placeholder="One item per line.">{escape(_render_text_list(must_include))}</textarea>
                      </div>
                      <div>
                        <label class="label" for="chapter_tool_avoid">Avoid</label>
                        <textarea id="chapter_tool_avoid" name="chapter_tool_avoid" placeholder="One item per line.">{escape(_render_text_list(avoid))}</textarea>
                      </div>
                    </div>
                  </div>
                </div>
                <div class="controls chapter-action">
                  <button id="chapter-advice-button" {"disabled" if not can_queue_chapter_advice else ""}>&#10148; Queue Chapter Advice</button>
                  <button id="regen-chapter-button" formaction="/regenerate-chapter" class="secondary" {"disabled" if not can_regenerate_chapter else ""}>&#8635; Regenerate Chapter</button>
                </div>
              </form>
    """


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


def _render_outline_item(snapshot, selected: bool = False) -> str:
    selected_class = " selected" if selected else ""
    return (
        f'<li class="chapter chapter--outline{selected_class}" data-view="outline">'
        f'<button type="button" class="chapter-toggle chapter-toggle--outline" data-view="outline">'
        f'<span class="chapter-kicker">Reference</span>'
        f'<span class="chapter-title">Outline</span>'
        f'<span class="chapter-meta">{escape(_outline_status_text(snapshot))}</span>'
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


def serve(host: str = "127.0.0.1", port: int = WEB_UI_PORT) -> None:
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
