(() => {
  const initialState = window.__BOOK_UI_INITIAL_STATE__ || {};
  let lastPhaseVersion = Number(initialState.phase_version || 0);
  let lastAgent = initialState.progress?.current_agent || "";
  let lastKnownState = initialState;
  let lastWaitingForInput = Boolean(initialState.waiting_for_input);
  let lastCompletionSignal = completionSignalFor(initialState);
  let activeModalSourceId = "";
  let forceNextFormSync = false;
  let lastRenderedChapterDetailSignature = "";
  const initialReaderTarget = defaultReaderTargetFor(initialState);
  let selectedReaderView = initialReaderTarget.view;
  let selectedChapterNumber = initialReaderTarget.chapterNumber;
  let activeTab = "";
  let manualTabSelection = false;
  let manualReaderSelection = false;
  let waitingReminderTimer = null;
  const dirtyFields = new Set();

  function byId(id) {
    return document.getElementById(id);
  }

  function setBoundText(binding, value) {
    document.querySelectorAll(`[data-bind="${binding}"]`).forEach((node) => {
      node.textContent = value ?? "";
    });
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function statusLabel(status) {
    return String(status || "idle").replace(/_/g, " ").replace(/\b\w/g, (match) => match.toUpperCase());
  }

  function outlineStatusSummary(state) {
    if (state.outline_approved) return "Approved";
    if (state.awaiting_outline_approval) return "Awaiting review";
    if (state.outline_text) return "Draft ready";
    return "Not generated";
  }

  function completionSignalFor(state) {
    if (!state) return "";
    if (state.phase === "Generation complete") return `phase:${state.phase}`;
    if (state.current_checkpoint_title) return `checkpoint:${state.current_checkpoint_title}`;
    return "";
  }

  function bannerLevel(state) {
    return state.banner?.level || "accent";
  }

  function preferredTabForState(state) {
    if (state.awaiting_outline_approval) return "planning";
    if (state.phase_area) return String(state.phase_area).toLowerCase();
    if (state.current_chapter > 0 || state.outline_approved || state.resume_available) return "writing";
    return "planning";
  }

  function defaultReaderTargetFor(state) {
    const chapters = state?.chapters || [];
    const current = chapters.find((chapter) => chapter.number === Number(state?.current_chapter || 0));
    if (current) {
      return { view: "chapter", chapterNumber: current.number };
    }
    const firstWithText = chapters.find((chapter) => chapter.saved_text);
    if (firstWithText) {
      return { view: "chapter", chapterNumber: firstWithText.number };
    }
    if (state?.outline_text) {
      return { view: "outline", chapterNumber: 0 };
    }
    if (chapters.length) {
      return { view: "chapter", chapterNumber: chapters[0].number };
    }
    return { view: "outline", chapterNumber: 0 };
  }

  function livePhaseState(state) {
    if (state.latest_error || state.status === "failed") {
      return { tone: "error", label: "Error" };
    }
    if (state.waiting_for_input || state.awaiting_outline_approval || state.status === "waiting") {
      return { tone: "waiting", label: "Waiting" };
    }
    if (state.stop_requested && state.run_active) {
      return { tone: "idle", label: "Pause pending" };
    }
    if (state.run_active) {
      return { tone: "generating", label: "Generating" };
    }
    if (state.resume_available || state.status === "stopped") {
      return { tone: "idle", label: "Paused" };
    }
    return { tone: "idle", label: "Idle" };
  }

  function setActiveTab(name, userInitiated = false) {
    if (!name) return;
    activeTab = name;
    if (userInitiated) manualTabSelection = true;
    document.querySelectorAll(".tab-button").forEach((button) => {
      const isActive = button.dataset.tabTarget === name;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.tab === name);
    });
  }

  function bindTabs() {
    document.querySelectorAll(".tab-button").forEach((button) => {
      button.addEventListener("click", () => setActiveTab(button.dataset.tabTarget, true));
    });
    const initiallyActive = document.querySelector(".tab-button.active")?.dataset.tabTarget || preferredTabForState(initialState);
    setActiveTab(initiallyActive);
  }

  function bindSectionLinks() {
    document.querySelectorAll(".step-link").forEach((button) => {
      button.addEventListener("click", () => {
        const target = document.getElementById(button.dataset.scrollTarget || "");
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      });
    });
  }

  function playSoftCue() {
    try {
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
    } catch (_error) {
    }
  }

  function playCompleteCue() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const now = ctx.currentTime;
      [660, 880].forEach((freq, index) => {
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
      });
    } catch (_error) {
    }
  }

  function playWaitingCue() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const now = ctx.currentTime;
      [780, 620].forEach((freq, index) => {
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
      });
    } catch (_error) {
    }
  }

  function playReminderCue() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const now = ctx.currentTime;
      [430, 640, 520].forEach((freq, index) => {
        const oscillator = ctx.createOscillator();
        const gain = ctx.createGain();
        oscillator.type = "square";
        oscillator.frequency.setValueAtTime(freq, now + index * 0.22);
        gain.gain.setValueAtTime(0.0001, now + index * 0.22);
        gain.gain.exponentialRampToValueAtTime(0.09, now + index * 0.22 + 0.03);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.22 + 0.24);
        oscillator.connect(gain);
        gain.connect(ctx.destination);
        oscillator.start(now + index * 0.22);
        oscillator.stop(now + index * 0.22 + 0.24);
      });
    } catch (_error) {
    }
  }

  function speakAnnouncement(text) {
    if (!text || !("speechSynthesis" in window)) return;
    try {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 1;
      utterance.pitch = 0.95;
      utterance.volume = 1;
      window.speechSynthesis.speak(utterance);
    } catch (_error) {
    }
  }

  function completionAnnouncementText(state) {
    const title = String(state.current_checkpoint_title || "").trim();
    if (/^Outline ready for review$/i.test(title)) return "Outline generated";
    const chapterMatch = title.match(/^Chapter\s+(\d+)\s+(complete|regenerated)$/i);
    if (chapterMatch) return `Chapter ${chapterMatch[1]} generated`;
    if (state.phase === "Generation complete") return "Book generation complete";
    return "";
  }

  function syncWaitingReminder(state) {
    const shouldRemind = Boolean(state.waiting_for_input && (state.run_active || state.awaiting_outline_approval));
    if (!shouldRemind) {
      if (waitingReminderTimer) {
        window.clearInterval(waitingReminderTimer);
        waitingReminderTimer = null;
      }
      return;
    }
    if (!waitingReminderTimer) {
      waitingReminderTimer = window.setInterval(() => {
        const current = lastKnownState || {};
        if (current.waiting_for_input && (current.run_active || current.awaiting_outline_approval)) {
          playReminderCue();
        }
      }, 5 * 60 * 1000);
    }
  }

  function flashPhase() {
    const banner = byId("context-banner");
    if (!banner) return;
    banner.classList.remove("flash");
    void banner.offsetWidth;
    banner.classList.add("flash");
    window.setTimeout(() => banner.classList.remove("flash"), 900);
  }

  function modalSource() {
    return activeModalSourceId ? byId(activeModalSourceId) : null;
  }

  function getTextareaHeading(textarea) {
    const label = textarea.id ? document.querySelector(`label[for="${textarea.id}"]`) : null;
    return (label?.textContent || textarea.placeholder || "Expanded Editor").trim();
  }

  function openTextareaModal(textarea) {
    activeModalSourceId = textarea.id || "";
    byId("textarea-modal-title").textContent = getTextareaHeading(textarea);
    byId("textarea-modal-input").value = textarea.value || "";
    byId("textarea-modal").classList.add("open");
    byId("textarea-modal").setAttribute("aria-hidden", "false");
    byId("textarea-modal-input").focus();
  }

  function closeTextareaModal() {
    byId("textarea-modal").classList.remove("open");
    byId("textarea-modal").setAttribute("aria-hidden", "true");
    activeModalSourceId = "";
  }

  function syncModalToSource() {
    const source = modalSource();
    if (!source) return;
    source.value = byId("textarea-modal-input").value;
    if (source.id) dirtyFields.add(source.id);
  }

  function ensureTextareaExpandButtons(root = document) {
    root.querySelectorAll("textarea").forEach((textarea) => {
      if (textarea.id === "textarea-modal-input") return;
      let shell = textarea.parentElement;
      if (!shell || !shell.classList.contains("textarea-shell")) {
        shell = document.createElement("div");
        shell.className = "textarea-shell";
        textarea.parentNode.insertBefore(shell, textarea);
        shell.appendChild(textarea);
      }
      if (textarea.dataset.expandBound === "true") return;
      textarea.dataset.expandBound = "true";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary expand-textarea-button";
      button.innerHTML = "&#8599;";
      button.setAttribute("aria-label", "Expand editor");
      button.title = "Expand editor";
      button.addEventListener("click", () => openTextareaModal(textarea));
      shell.appendChild(button);
    });
  }

  function syncModels(models, selectedOutline, selectedWriter) {
    const outlineSelect = byId("outline_model");
    const writerSelect = byId("writer_model");
    if (!outlineSelect || !writerSelect) return;
    const list = (models && models.length ? models : [selectedOutline, selectedWriter]).filter(Boolean);
    const unique = [...new Set(list)];
    outlineSelect.innerHTML = unique.map((model) => `<option value="${escapeHtml(model)}"${model === selectedOutline ? " selected" : ""}>${escapeHtml(model)}</option>`).join("");
    writerSelect.innerHTML = unique.map((model) => `<option value="${escapeHtml(model)}"${model === selectedWriter ? " selected" : ""}>${escapeHtml(model)}</option>`).join("");
    outlineSelect.value = selectedOutline || unique[0] || "";
    writerSelect.value = selectedWriter || unique[0] || "";
  }

  function isFieldLocked(fieldId) {
    const element = byId(fieldId);
    return Boolean(element && (document.activeElement === element || dirtyFields.has(fieldId)));
  }

  function setFieldValue(fieldId, value) {
    const element = byId(fieldId);
    if (!element) return;
    if (!forceNextFormSync && isFieldLocked(fieldId)) return;
    element.value = value ?? "";
    dirtyFields.delete(fieldId);
  }

  function setSelectValue(fieldId, value) {
    setFieldValue(fieldId, value);
  }

  function collectChapterDetailsFields() {
    const details = {};
    document.querySelectorAll(".chapter-detail-beats-input, .chapter-detail-wordcount-input").forEach((input) => {
      const chapter = Number(input.dataset.chapter || "0");
      if (!chapter) return;
      details[chapter] = details[chapter] || { beats: "", target_word_count: 0 };
      if (input.classList.contains("chapter-detail-beats-input")) {
        details[chapter].beats = input.value;
      } else {
        details[chapter].target_word_count = Number(input.value || 0);
      }
    });
    Object.keys(details).forEach((chapter) => {
      const item = details[chapter];
      if (!(item.beats || item.target_word_count > 0)) delete details[chapter];
    });
    return details;
  }

  function renderChapterDetailEditors(numChapters, values) {
    const container = byId("chapter-details-editor");
    if (!container) return;
    const details = values || {};
    const existingDetails = collectChapterDetailsFields();
    const mergedDetails = forceNextFormSync ? details : { ...details, ...existingDetails };
    const total = Math.max(1, Number(numChapters || 1));
    const normalizedDetails = {};
    for (let chapter = 1; chapter <= total; chapter += 1) {
      const item = mergedDetails[chapter] ?? mergedDetails[String(chapter)] ?? {};
      normalizedDetails[chapter] = {
        beats: item.beats ?? "",
        target_word_count: Number(item.target_word_count ?? 0),
      };
    }
    const signature = JSON.stringify({ total, details: normalizedDetails });
    if (!forceNextFormSync && signature === lastRenderedChapterDetailSignature) return;
    let html = "";
    for (let chapter = 1; chapter <= total; chapter += 1) {
      const detail = normalizedDetails[chapter];
      html += `<div class="chapter-detail-group" data-chapter-group="${chapter}">`;
      html += `<div class="chapter-detail-group__heading">Chapter ${chapter}</div>`;
      html += `<div class="chapter-detail-group__fields">`;
      html += `<div class="chapter-detail-group__field chapter-detail-group__field--text">`;
      html += `<label class="label" for="chapter_detail_beats_${chapter}">Chapter Details</label>`;
      html += `<textarea id="chapter_detail_beats_${chapter}" name="chapter_detail_beats_${chapter}" class="chapter-detail-beats-input" data-chapter="${chapter}" placeholder="Required beats for Chapter ${chapter}.">${escapeHtml(detail.beats || "")}</textarea>`;
      html += `</div>`;
      html += `<div class="chapter-detail-group__field chapter-detail-group__field--compact">`;
      html += `<label class="label" for="chapter_detail_wordcount_${chapter}">Target Word Count</label>`;
      html += `<input id="chapter_detail_wordcount_${chapter}" name="chapter_detail_wordcount_${chapter}" type="number" min="0" max="50000" class="chapter-detail-wordcount-input" data-chapter="${chapter}" value="${Number(detail.target_word_count || 0)}">`;
      html += `</div>`;
      html += `</div>`;
      html += `</div>`;
    }
    container.innerHTML = html;
    lastRenderedChapterDetailSignature = signature;
    bindEditableFields(container);
    ensureTextareaExpandButtons(container);
  }

  function buildExternalConfigFromState(state) {
    const liveChapterDetails = collectChapterDetailsFields();
    return {
      name: byId("config_name")?.value.trim() || "external-config",
      created_at: new Date().toISOString(),
      endpoint_url: byId("endpoint_url")?.value || state.endpoint_url || "",
      outline_model: byId("outline_model")?.value || state.outline_model || "",
      writer_model: byId("writer_model")?.value || state.writer_model || "",
      num_chapters: Number(byId("num_chapters")?.value ?? state.num_chapters ?? 10),
      token_limit_enabled: byId("token_limit_enabled")?.value === "on",
      max_tokens: Number(byId("max_tokens")?.value ?? state.max_tokens ?? 4096),
      reduce_thinking: byId("reduce_thinking")?.value === "on",
      max_iterations: Number(byId("max_iterations")?.value ?? state.max_iterations ?? 5),
      chapter_target_word_count: Number(byId("chapter_target_word_count")?.value ?? state.chapter_target_word_count ?? 0),
      output_folder: state.output_folder || "",
      chapter_details: Object.keys(liveChapterDetails).length ? liveChapterDetails : (state.chapter_details || {}),
      prompt_sections: {
        premise: byId("premise")?.value || "",
        storylines: byId("storylines")?.value || "",
        setting: byId("setting")?.value || "",
        characters: byId("characters")?.value || "",
        writing_style: byId("writing_style")?.value || "",
        tone: byId("tone")?.value || "",
        plot_beats: byId("plot_beats")?.value || "",
        constraints: byId("constraints")?.value || "",
      },
    };
  }

  function ensureSelectedChapter(state) {
    const chapters = state.chapters || [];
    if (!chapters.length) {
      selectedChapterNumber = 0;
      return null;
    }
    const existing = chapters.find((chapter) => chapter.number === selectedChapterNumber);
    if (existing) return existing;
    const current = chapters.find((chapter) => chapter.number === Number(state.current_chapter || 0));
    const firstWithText = chapters.find((chapter) => chapter.saved_text);
    const nextChoice = current || firstWithText || chapters[0];
    selectedChapterNumber = nextChoice.number;
    return nextChoice;
  }

  function ensureSelectedReader(state) {
    if (selectedReaderView === "outline") {
      return { view: "outline", chapter: null };
    }
    const chapter = ensureSelectedChapter(state);
    if (chapter) {
      return { view: "chapter", chapter };
    }
    selectedReaderView = "outline";
    return { view: "outline", chapter: null };
  }

  function chapterHtml(chapter) {
    const status = chapter.status || "pending";
    const selected = selectedReaderView === "chapter" && Number(chapter.number) === Number(selectedChapterNumber) ? " selected" : "";
    return `<li class="chapter ${escapeHtml(status)}${selected}" data-chapter="${chapter.number}"><button type="button" class="chapter-toggle" data-chapter="${chapter.number}"><span class="chapter-kicker">Chapter ${chapter.number}</span><span class="chapter-title">${escapeHtml(chapter.title || `Chapter ${chapter.number}`)}</span><span class="chapter-meta">${escapeHtml(statusLabel(status))}</span></button></li>`;
  }

  function outlineNavHtml(state) {
    const selected = selectedReaderView === "outline" ? " selected" : "";
    return `<li class="chapter chapter--outline${selected}" data-view="outline"><button type="button" class="chapter-toggle chapter-toggle--outline" data-view="outline"><span class="chapter-kicker">Reference</span><span class="chapter-title">Outline</span><span class="chapter-meta">${escapeHtml(outlineStatusSummary(state))}</span></button></li>`;
  }

  function renderChapterList(state) {
    const list = byId("chapter-list");
    if (!list) return;
    const chapters = state.chapters || [];
    const items = [outlineNavHtml(state)];
    if (chapters.length) {
      items.push(chapters.map(chapterHtml).join(""));
    } else {
      items.push(`<li class="chapter chapter--placeholder"><span class="chapter-title">No chapters yet.</span></li>`);
    }
    list.innerHTML = items.join("");
    bindChapterToggles();
  }

  function syncSelectedChapterInputs(chapterNumber, state) {
    const adviceField = byId("chapter_advice_number");
    const regenField = byId("regen_chapter_number");
    if (adviceField && document.activeElement !== adviceField) {
      adviceField.value = chapterNumber || state.current_chapter || 1;
    }
    if (regenField && document.activeElement !== regenField) {
      regenField.value = chapterNumber || state.current_chapter || 1;
    }
  }

  function renderSelectedChapter(state) {
    const reader = ensureSelectedReader(state);
    if (reader.view === "outline") {
      byId("selected-chapter-title").textContent = "Outline";
      byId("selected-chapter-status").textContent = outlineStatusSummary(state);
      byId("selected-chapter-content").textContent = state.outline_text || "Outline not generated yet.";
      byId("chapter-artifacts").textContent = "Outline view does not have chapter artifacts.";
      syncSelectedChapterInputs(0, state);
      return;
    }

    const chapter = reader.chapter;
    const title = chapter ? `Chapter ${chapter.number}: ${chapter.title}` : "Chapter workspace";
    const status = chapter ? statusLabel(chapter.status) : "Not started";
    let text = "Select a chapter from the left rail or start a run to read generated text here.";
    if (chapter?.saved_text) {
      text = chapter.saved_text;
    } else if (chapter?.status === "in_progress") {
      text = `Chapter ${chapter.number} is currently being generated. The latest completed text will appear here when the checkpoint finishes.`;
    } else if (chapter?.status === "pending") {
      text = `Chapter ${chapter.number} is pending and has not been generated yet.`;
    } else if (chapter?.status === "failed") {
      text = `Chapter ${chapter.number} failed on the last attempt. Check Errors & Events for details.`;
    }
    const artifacts = chapter?.artifacts_text || "No chapter artifacts yet.";
    byId("selected-chapter-title").textContent = title;
    byId("selected-chapter-status").textContent = status;
    byId("selected-chapter-content").textContent = text;
    byId("chapter-artifacts").textContent = artifacts;
    syncSelectedChapterInputs(chapter?.number || 0, state);
  }

  function bindChapterToggles() {
    document.querySelectorAll(".chapter-toggle").forEach((button) => {
      button.onclick = () => {
        manualReaderSelection = true;
        if (button.dataset.view === "outline") {
          selectedReaderView = "outline";
        } else {
          selectedReaderView = "chapter";
          selectedChapterNumber = Number(button.dataset.chapter || "0");
        }
        renderChapterList(lastKnownState);
        renderSelectedChapter(lastKnownState);
      };
    });
  }

  function renderContextBanner(state) {
    const banner = state.banner || {};
    const element = byId("context-banner");
    if (!element) return;
    element.className = `context-banner context-banner--${bannerLevel(state)}`;
    byId("context-banner-title").textContent = banner.title || "";
    byId("context-banner-body").textContent = banner.body || "";
    byId("context-mode-label").textContent = state.mode_label || "";
  }

  function applyState(state) {
    lastKnownState = state;
    const waitingText = state.waiting_for_input ? "Yes" : "No";
    const completionAnnouncement = completionAnnouncementText(state);
    const isCompletionCheckpoint = Boolean(completionAnnouncement);
    setBoundText("project-name", state.project_name || "Current Book Project");
    setBoundText("status-display", state.status_display || statusLabel(state.status));
    setBoundText("phase-area", state.phase_area || preferredTabForState(state));
    setBoundText("mode-display", state.mode_display || (state.mode === "keep_going" ? "Auto" : "Guided"));
    setBoundText("waiting-input", waitingText);
    setBoundText("current-chapter", `${state.current_chapter || 0} / ${state.total_chapters || 0}`);
    setBoundText("outline-status-summary", outlineStatusSummary(state));
    const statusChip = byId("overall-status-chip");
    if (statusChip) {
      statusChip.className = `status-chip status-chip--${state.status_tone || "accent"}`;
    }
    byId("phase-status").textContent = state.phase || "Idle";
    renderContextBanner(state);

    if (!manualTabSelection) {
      setActiveTab(preferredTabForState(state));
    }
    if (!manualReaderSelection) {
      const nextReaderTarget = defaultReaderTargetFor(state);
      selectedReaderView = nextReaderTarget.view;
      selectedChapterNumber = nextReaderTarget.chapterNumber;
    }

    setFieldValue("endpoint_url", state.endpoint_url || "");
    setFieldValue("config_name", state.config_name || "");
    if (forceNextFormSync || (!isFieldLocked("outline_model") && !isFieldLocked("writer_model"))) {
      syncModels(state.available_models || [], state.outline_model || "", state.writer_model || "");
      dirtyFields.delete("outline_model");
      dirtyFields.delete("writer_model");
    }
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
    renderChapterDetailEditors(state.num_chapters ?? 10, state.chapter_details || {});
    setSelectValue("token_limit_enabled", state.token_limit_enabled ? "on" : "off");
    setFieldValue("max_tokens", state.max_tokens ?? 4096);
    setFieldValue("max_iterations", state.max_iterations ?? 5);
    setSelectValue("reduce_thinking", state.reduce_thinking ? "on" : "off");
    setFieldValue("outline_feedback", state.outline_feedback || "");

    byId("checkpoint-title").textContent = state.current_checkpoint_title || "Waiting for the first checkpoint.";
    byId("checkpoint-body").textContent = state.current_checkpoint_body || "Nothing to review yet.";
    byId("outline-text").textContent = state.outline_text || "Outline not generated yet.";
    byId("outline-reference").textContent = state.outline_text || "Outline not generated yet.";
    byId("last-advice").textContent = state.latest_advice || "No advice submitted yet.";
    byId("model-error").textContent = state.model_fetch_error || "No model errors.";
    byId("latest-error").textContent = state.latest_error || "No errors recorded.";
    byId("recent-events").textContent = state.recent_events || "No events yet.";
    byId("outline-approval-status").textContent = `Approved: ${state.outline_approved ? "Yes" : "No"} | Awaiting approval: ${state.awaiting_outline_approval ? "Yes" : "No"}`;
    byId("progress-agent").textContent = state.progress?.current_agent || "Idle";
    byId("progress-step").textContent = state.progress?.current_step || "Idle";
    byId("progress-iteration").textContent = `${state.progress?.iteration || 0}/${state.progress?.max_iterations || 0}`;
    byId("progress-stage").textContent = state.progress?.output_stage || "n/a";
    byId("progress-detail").textContent = state.progress?.detail || "No active detail yet.";
    byId("progress-events").textContent = state.progress_events || "No progress events yet.";
    byId("continuity-panel").textContent = state.continuity || "No continuity data yet.";

    renderChapterList(state);
    renderSelectedChapter(state);

    const maxTokensGroup = byId("max-tokens-group");
    if (maxTokensGroup) {
      maxTokensGroup.style.display = state.token_limit_enabled ? "block" : "none";
    }

    byId("start-button").disabled = state.run_active;
    byId("start-button").innerHTML = `&#9889; ${state.resume_available && !state.run_active ? "Start New Run" : "Start Run"}`;
    byId("keep-going-button").disabled = !state.run_active || state.mode === "keep_going";
    byId("ask-advice-button").disabled = !state.run_active || state.mode === "ask_for_advice";
    byId("continue-button").disabled = ((!state.run_active || (!state.waiting_for_input && !state.stop_requested)) && !state.resume_available) || state.awaiting_outline_approval;
    byId("continue-button").innerHTML = `&#9658; ${state.resume_available && !state.run_active ? "Resume Run" : "Continue"}`;
    byId("stop-button").disabled = !state.run_active || state.status === "completed" || state.status === "failed" || state.stop_requested;
    byId("stop-button").innerHTML = `&#9208; ${state.stop_requested ? "Pause Pending" : "Pause"}`;
    byId("approve-outline-button").disabled = !state.awaiting_outline_approval;
    byId("regen-outline-button").disabled = !state.awaiting_outline_approval;
    byId("chapter-advice-button").disabled = !state.run_active && !state.resume_available;
    byId("regen-chapter-button").disabled = state.run_active || !state.outline_approved;

    if (state.busy && state.progress?.current_agent && state.progress.current_agent !== "idle" && state.progress.current_agent !== lastAgent) {
      playSoftCue();
    }
    if (state.waiting_for_input && !lastWaitingForInput && !isCompletionCheckpoint) {
      playWaitingCue();
    }
    lastAgent = state.progress?.current_agent || "";
    lastWaitingForInput = Boolean(state.waiting_for_input);
    syncWaitingReminder(state);
    if (state.phase_version !== lastPhaseVersion) {
      lastPhaseVersion = state.phase_version;
      flashPhase();
      const checkpointTitle = state.current_checkpoint_title || "";
      const completionSignal = completionSignalFor(state);
      if (isCompletionCheckpoint && completionSignal && completionSignal !== lastCompletionSignal) {
        speakAnnouncement(completionAnnouncement);
      }
      if (isCompletionCheckpoint && completionSignal) {
        lastCompletionSignal = completionSignal;
      }
    }
    forceNextFormSync = false;
  }

  function bindEditableFields(root = document) {
    root.querySelectorAll("input, textarea, select").forEach((field) => {
      if (field.dataset.dirtyBound === "true" || field.id === "textarea-modal-input") return;
      field.dataset.dirtyBound = "true";
      field.addEventListener("input", () => field.id && dirtyFields.add(field.id));
      field.addEventListener("change", () => field.id && dirtyFields.add(field.id));
    });
  }

  function bindAsyncForms() {
    document.querySelectorAll("form.async-form").forEach((form) => {
      form.querySelectorAll("button, input[type='submit']").forEach((button) => {
        button.addEventListener("click", () => {
          form.dataset.submitAction = button.getAttribute("formaction") || form.getAttribute("action") || window.location.pathname;
        });
      });
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const action = form.dataset.submitAction || form.getAttribute("action") || window.location.pathname;
        const formData = new FormData(form);
        if (action === "/save-config") {
          const setupForm = byId("run-setup-form");
          if (setupForm) {
            new FormData(setupForm).forEach((value, key) => {
              if (!formData.has(key)) formData.append(key, value);
            });
          }
        }
        try {
          await fetch(action, {
            method: "POST",
            body: new URLSearchParams(formData).toString(),
            headers: {
              "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
              "X-Requested-With": "fetch",
            },
          });
          forceNextFormSync = true;
          Array.from(form.elements || []).forEach((field) => field.id && dirtyFields.delete(field.id));
        } catch (_error) {
        } finally {
          delete form.dataset.submitAction;
        }
      });
    });
  }

  bindTabs();
  bindSectionLinks();
  bindEditableFields();
  ensureTextareaExpandButtons();
  bindAsyncForms();

  byId("token_limit_enabled")?.addEventListener("change", (event) => {
    byId("max-tokens-group").style.display = event.target.value === "on" ? "block" : "none";
  });
  byId("num_chapters")?.addEventListener("input", (event) => {
    renderChapterDetailEditors(event.target.value, collectChapterDetailsFields());
  });
  byId("external-config-button")?.addEventListener("click", () => byId("external-config-input").click());
  byId("external-config-input")?.addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const text = await file.text();
    try {
      forceNextFormSync = true;
      dirtyFields.clear();
      await fetch("/load-external-config", {
        method: "POST",
        body: text,
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "fetch",
        },
      });
    } catch (_error) {
    } finally {
      event.target.value = "";
    }
  });
  byId("save-external-config-button")?.addEventListener("click", () => {
    const payload = buildExternalConfigFromState(lastKnownState || {});
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const safeName = (payload.name || "external-config").replace(/[^a-z0-9_-]+/gi, "_");
    link.href = url;
    link.download = `${safeName}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  });
  byId("textarea-modal-close")?.addEventListener("click", closeTextareaModal);
  byId("textarea-modal")?.addEventListener("click", (event) => {
    if (event.target.id === "textarea-modal") closeTextareaModal();
  });
  byId("textarea-modal-input")?.addEventListener("input", syncModalToSource);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && byId("textarea-modal")?.classList.contains("open")) closeTextareaModal();
  });

  applyState(initialState);

  const source = new EventSource("/events");
  source.onmessage = (event) => applyState(JSON.parse(event.data));
})();
