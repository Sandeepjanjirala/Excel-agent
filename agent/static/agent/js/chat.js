$(function () {
  let projectId = null;
  let stagedExcelFiles = []; // Array of File objects
  let activeProjectStats = null;
  const KEY_STORAGE_NAME = "excel_agent_gemini_key";

  // -----------------------------------------------------------------
  // 1. API Key Management
  // -----------------------------------------------------------------
  function updateApiKeyUI() {
    const savedKey = window.localStorage.getItem(KEY_STORAGE_NAME) || "";
    $("#gemini-key-input").val(savedKey);
    if (savedKey.trim()) {
      $("#api-key-status-label").text("Custom Key");
      $("#api-key-dot").addClass("active").attr("title", "Custom Gemini API Key Active");
    } else {
      $("#api-key-status-label").text("Server Key");
      $("#api-key-dot").removeClass("active").attr("title", "Using Default Server Key");
    }
  }
  updateApiKeyUI();

  $("#open-key-modal-btn").on("click", function () {
    updateApiKeyUI();
    $("#key-modal").fadeIn(150);
  });

  $("#close-key-modal-btn").on("click", function () {
    $("#key-modal").fadeOut(150);
  });

  $("#key-modal").on("click", function (e) {
    if ($(e.target).is("#key-modal")) {
      $("#key-modal").fadeOut(150);
    }
  });

  $("#toggle-key-visibility").on("click", function () {
    const $inp = $("#gemini-key-input");
    const isPass = $inp.attr("type") === "password";
    $inp.attr("type", isPass ? "text" : "password");
    $(this).text(isPass ? "🔒" : "👁️");
  });

  $("#save-key-btn").on("click", function () {
    const val = $("#gemini-key-input").val().trim();
    if (val) {
      window.localStorage.setItem(KEY_STORAGE_NAME, val);
    } else {
      window.localStorage.removeItem(KEY_STORAGE_NAME);
    }
    updateApiKeyUI();
    $("#key-modal").fadeOut(150);
  });

  $("#clear-key-btn").on("click", function () {
    window.localStorage.removeItem(KEY_STORAGE_NAME);
    $("#gemini-key-input").val("");
    updateApiKeyUI();
    $("#key-modal").fadeOut(150);
  });

  function currentApiKey() {
    return (window.localStorage.getItem(KEY_STORAGE_NAME) || "").trim();
  }

  // -----------------------------------------------------------------
  // 2. File Selection & Drag-and-Drop Handling
  // -----------------------------------------------------------------
  const $dropZone = $("#drop-zone");
  const $fileInput = $("#excel-files");

  $fileInput.on("change", function () {
    const files = Array.from(this.files || []);
    if (files.length > 0) {
      addFilesToStaging(files);
    }
  });

  $dropZone.on("dragover dragenter", function (e) {
    e.preventDefault();
    e.stopPropagation();
    $dropZone.addClass("dragover");
  });

  $dropZone.on("dragleave drop", function (e) {
    e.preventDefault();
    e.stopPropagation();
    $dropZone.removeClass("dragover");
    if (e.type === "drop") {
      const dt = e.originalEvent.dataTransfer;
      if (dt && dt.files && dt.files.length) {
        const files = Array.from(dt.files).filter(f => f.name.match(/\.(xlsx|xls)$/i));
        if (files.length) {
          addFilesToStaging(files);
        }
      }
    }
  });

  function formatBytes(bytes) {
    if (!bytes || bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
  }

  function addFilesToStaging(newFiles) {
    newFiles.forEach(f => {
      // Prevent duplicates by filename & size
      const exists = stagedExcelFiles.some(existing => existing.name === f.name && existing.size === f.size);
      if (!exists) {
        stagedExcelFiles.push(f);
      }
    });
    renderStagedFiles();
  }

  function renderStagedFiles() {
    const $list = $("#selected-files-list");
    $list.empty();

    if (stagedExcelFiles.length === 0) {
      $list.hide();
      $("#upload-count-badge").text("0 files");
      $("#upload-btn").prop("disabled", true);
      return;
    }

    $list.show();
    $("#upload-count-badge").text(`${stagedExcelFiles.length} file${stagedExcelFiles.length > 1 ? "s" : ""}`);
    $("#upload-btn").prop("disabled", false);

    stagedExcelFiles.forEach((file, index) => {
      const $item = $(`
        <div class="file-preview-item">
          <div class="file-preview-left">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
              <polyline points="14 2 14 8 20 8"></polyline>
            </svg>
            <span class="file-preview-name" title="${file.name}">${file.name}</span>
            <span class="file-preview-size">${formatBytes(file.size)}</span>
          </div>
          <button type="button" class="file-preview-remove" data-index="${index}" title="Remove file">&times;</button>
        </div>
      `);
      $list.append($item);
    });

    $(".file-preview-remove").on("click", function (e) {
      e.stopPropagation();
      const idx = parseInt($(this).data("index"), 10);
      stagedExcelFiles.splice(idx, 1);
      renderStagedFiles();
    });
  }

  $("#metadata-file").on("change", function () {
    const file = this.files[0];
    if (file) {
      $("#metadata-filename").text(file.name + ` (${formatBytes(file.size)})`);
    } else {
      $("#metadata-filename").text("No file selected");
    }
  });

  // -----------------------------------------------------------------
  // 3. Upload & Batch Parsing Execution
  // -----------------------------------------------------------------
  $("#upload-btn").on("click", function () {
    let filesToSend = stagedExcelFiles;
    if (!filesToSend.length) {
      const inputFiles = $("#excel-files")[0].files;
      if (inputFiles && inputFiles.length > 0) {
        filesToSend = Array.from(inputFiles);
      }
    }

    if (!filesToSend.length) {
      showStatus("Please select at least one Excel file to continue.", "error");
      return;
    }

    const metadataFile = $("#metadata-file")[0].files[0];
    const formData = new FormData();
    for (const f of filesToSend) {
      formData.append("excel_files", f);
    }
    if (metadataFile) {
      formData.append("metadata_file", metadataFile);
    }
    const userApiKey = currentApiKey();
    if (userApiKey) {
      formData.append("gemini_api_key", userApiKey);
    }

    // UI Loading state
    $("#upload-btn").prop("disabled", true);
    $("#upload-spinner").show();
    $("#upload-btn .btn-text").text(metadataFile ? "Parsing files & rules..." : "Analyzing workbooks...");
    showStatus(
      `Uploading ${filesToSend.length} file(s)` + (metadataFile ? ` and extracting rules from ${metadataFile.name}...` : "..."),
      "info"
    );

    $.ajax({
      url: "/api/upload/",
      type: "POST",
      data: formData,
      processData: false,
      contentType: false,
      success: function (resp) {
        projectId = resp.project_id;
        activeProjectStats = resp;

        showStatus(
          `Successfully loaded ${resp.total_files} file(s) across ${resp.total_sheets} sheet(s)` +
          (resp.has_metadata ? " with active business rules." : "."),
          "success"
        );

        // Render Active Dataset Panel
        renderDatasetOverview(resp);

        // Update technical schema in collapsible accordion (NOT dumped in chat!)
        $("#schema-debug-text").text(resp.schema_preview || "");

        // Transition chat view
        $("#empty-state").hide();
        $("#suggestions-box").slideDown(200);
        $("#question-input, #ask-btn, #clear-chat-btn").prop("disabled", false);
        $("#question-input").attr("placeholder", "Ask anything about your uploaded Excel data...");
        $("#chat-subhead").text(`${resp.total_files} file(s) · ${resp.total_sheets} sheet(s) · ${(resp.total_rows || 0).toLocaleString()} rows`);
        $("#session-tag").text(`Active • Auto-expires in 12h`).attr("title", `Session ID: ${projectId} (Data automatically cleared after 12h)`);

        $("#question-input").focus();
      },
      error: function (xhr) {
        const err = xhr.responseJSON ? xhr.responseJSON.error : "Upload failed. Please check the Excel format.";
        showStatus("Error: " + err, "error");
      },
      complete: function () {
        $("#upload-spinner").hide();
        $("#upload-btn .btn-text").text("Parse & Load Dataset");
        $("#upload-btn").prop("disabled", stagedExcelFiles.length === 0);
      }
    });
  });

  function showStatus(text, type) {
    const $status = $("#upload-status");
    $status.removeClass("success error info").text(text);
    if (type === "success") {
      $status.addClass("success").fadeIn();
    } else if (type === "error") {
      $status.addClass("error").fadeIn();
    } else {
      $status.fadeIn();
    }
  }

  function renderDatasetOverview(resp) {
    $("#dataset-panel").slideDown(200);

    $("#stat-files").text(resp.total_files || (resp.files ? resp.files.length : 0));
    $("#stat-sheets").text(resp.total_sheets || 0);
    $("#stat-rows").text((resp.total_rows || 0).toLocaleString());

    const $cardsContainer = $("#file-cards-container");
    $cardsContainer.empty();

    if (!resp.files) return;

    resp.files.forEach(f => {
      const $card = $(`
        <div class="dataset-file-card">
          <div class="df-header">
            <svg class="df-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
              <polyline points="14 2 14 8 20 8"></polyline>
              <line x1="8" y1="13" x2="16" y2="13"></line>
            </svg>
            <div class="df-name" title="${f.name}">${f.name}</div>
          </div>
          <div class="sheet-badge-list"></div>
        </div>
      `);

      const $sheetList = $card.find(".sheet-badge-list");

      if (f.sheets && f.sheets.length > 0) {
        f.sheets.forEach((s, sIdx) => {
          const sheetId = `sheet_cols_${Math.random().toString(36).substring(2, 9)}`;
          const $sheetItem = $(`
            <div class="sheet-badge-item">
              <div class="sheet-badge-top">
                <span class="sheet-badge-title">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect></svg>
                  ${s.sheet_name}
                </span>
                <span class="sheet-badge-counts">${(s.rows || 0).toLocaleString()} rows &bull; ${s.cols || 0} cols</span>
              </div>
              <button type="button" class="columns-toggle-btn" data-target="#${sheetId}">
                Show ${s.total_columns || (s.columns ? s.columns.length : 0)} columns &darr;
              </button>
              <div class="columns-preview-chips" id="${sheetId}" style="display:none;"></div>
            </div>
          `);

          const $chipsBox = $sheetItem.find(`#${sheetId}`);
          if (s.columns && s.columns.length > 0) {
            s.columns.forEach(col => {
              $chipsBox.append($("<span>").addClass("column-chip").text(col));
            });
            if (s.total_columns > s.columns.length) {
              $chipsBox.append($("<span>").addClass("column-chip").text(`+${s.total_columns - s.columns.length} more`));
            }
          }

          $sheetList.append($sheetItem);
        });
      } else if (f.variables) {
        // Fallback
        const $sheetItem = $(`
          <div class="sheet-badge-item">
            <div class="sheet-badge-top">
              <span class="sheet-badge-title">${f.variables.join(", ")}</span>
            </div>
          </div>
        `);
        $sheetList.append($sheetItem);
      }

      $cardsContainer.append($card);
    });

    // Handle column drawer toggles
    $(".columns-toggle-btn").on("click", function () {
      const target = $(this).data("target");
      const $box = $(target);
      if ($box.is(":visible")) {
        $box.slideUp(150);
        $(this).html($(this).html().replace("↑", "↓").replace("Hide", "Show"));
      } else {
        $box.slideDown(150);
        $(this).html($(this).html().replace("↓", "↑").replace("Show", "Hide"));
      }
    });
  }

  // -----------------------------------------------------------------
  // 4. Message Rendering & Chat
  // -----------------------------------------------------------------
  function appendMessage(role, text, meta, isError, code) {
    const $row = $("<div>").addClass("msg-row " + role + (isError ? " error" : ""));

    if (role === "agent") {
      const $avatar = $(`
        <div class="msg-avatar">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83"></path>
          </svg>
        </div>
      `);
      $row.append($avatar);
    }

    const $bubble = $("<div>").addClass("msg-bubble");

    if (role === "user") {
      $bubble.text(text);
    } else {
      const $body = $("<div>").addClass("msg-body");
      if (typeof marked !== "undefined" && typeof marked.parse === "function") {
        $body.html(marked.parse(text));
        $body.find("table").each(function () {
          if (!$(this).parent().hasClass("table-scroll-wrapper")) {
            $(this).wrap('<div class="table-scroll-wrapper"></div>');
          }
        });
      } else {
        $body.text(text);
      }
      $bubble.append($body);

      // Clean, subtle chatbot action bar
      const uniqueCodeId = "code_" + Math.random().toString(36).substring(2, 9);
      const $actions = $(`
        <div class="msg-actions">
          <button type="button" class="btn-msg-action btn-copy-msg" title="Copy answer">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
            <span>Copy</span>
          </button>
        </div>
      `);

      if (code) {
        const $codeBtn = $(`
          <button type="button" class="btn-msg-action btn-toggle-code" data-target="#${uniqueCodeId}" title="View query code">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="16 18 22 12 16 6"></polyline>
              <polyline points="8 6 2 12 8 18"></polyline>
            </svg>
            <span>Code</span>
          </button>
        `);
        $actions.append($codeBtn);

        const $codeBox = $(`
          <div class="msg-code-drawer" id="${uniqueCodeId}" style="display:none;">
            <div class="msg-code-drawer-header">
              <span>Python / Pandas Query</span>
              <button type="button" class="btn-copy-snippet" data-target="#${uniqueCodeId}_code">Copy Code</button>
            </div>
            <pre><code id="${uniqueCodeId}_code">${$("<div/>").text(code).html()}</code></pre>
          </div>
        `);
        $bubble.append($actions);
        $bubble.append($codeBox);
      } else {
        $bubble.append($actions);
      }
    }

    $row.append($bubble);
    $("#messages").append($row);

    // Event handlers for copy and toggle code
    $row.find(".btn-copy-msg").on("click", function () {
      const textToCopy = $bubble.find(".msg-body").text();
      const $btn = $(this);
      navigator.clipboard.writeText(textToCopy).then(() => {
        const $span = $btn.find("span");
        $span.text("Copied! ✓");
        setTimeout(() => $span.text("Copy"), 2000);
      });
    });

    $row.find(".btn-toggle-code").on("click", function () {
      const target = $(this).data("target");
      const $drawer = $(target);
      if ($drawer.is(":visible")) {
        $drawer.slideUp(150);
        $(this).removeClass("active");
      } else {
        $drawer.slideDown(150);
        $(this).addClass("active");
      }
      scrollChatToBottom();
    });

    $row.find(".btn-copy-snippet").on("click", function () {
      const target = $(this).data("target");
      const snippetText = $(target).text();
      const $btn = $(this);
      navigator.clipboard.writeText(snippetText).then(() => {
        $btn.text("Copied! ✓");
        setTimeout(() => $btn.text("Copy Code"), 2000);
      });
    });

    scrollChatToBottom();
  }

  function scrollChatToBottom() {
    const $container = $("#messages-container");
    $container.stop().animate({ scrollTop: $container[0].scrollHeight }, 200);
  }

  // -----------------------------------------------------------------
  // 5. Ask Question & Sandbox Workflow
  // -----------------------------------------------------------------
  let typingStatusInterval = null;
  const TYPING_STAGES = [
    "Analyzing dataset columns & schema...",
    "Drafting Pandas query...",
    "Running code in Python sandbox...",
    "Verifying output & formatting answer..."
  ];

  function startTypingAnimation() {
    let stageIdx = 0;
    $("#typing-status").text(TYPING_STAGES[0]);
    $("#typing-indicator").fadeIn(150);
    scrollChatToBottom();

    typingStatusInterval = setInterval(() => {
      stageIdx = (stageIdx + 1) % TYPING_STAGES.length;
      $("#typing-status").text(TYPING_STAGES[stageIdx]);
    }, 2800);
  }

  function stopTypingAnimation() {
    if (typingStatusInterval) {
      clearInterval(typingStatusInterval);
      typingStatusInterval = null;
    }
    $("#typing-indicator").hide();
  }

  function askQuestion(overrideText) {
    const question = (overrideText || $("#question-input").val()).trim();
    if (!question || !projectId) return;

    // Remove the extra suggested inquiries box once conversation starts
    $("#suggestions-box").slideUp(150);

    appendMessage("user", question);
    $("#question-input").val("");
    $("#ask-btn, #question-input").prop("disabled", true);
    autoResizeInput();

    startTypingAnimation();

    $.ajax({
      url: "/api/ask/",
      type: "POST",
      contentType: "application/json",
      data: JSON.stringify({
        project_id: projectId,
        question: question,
        gemini_api_key: currentApiKey()
      }),
      success: function (resp) {
        stopTypingAnimation();
        appendMessage("agent", resp.answer, null, resp.status !== "ok", resp.code);
      },
      error: function (xhr) {
        stopTypingAnimation();
        const err = xhr.responseJSON ? xhr.responseJSON.error : "Unable to process query.";
        appendMessage("agent", "❌ " + err, null, true, null);
      },
      complete: function () {
        $("#ask-btn, #question-input").prop("disabled", false);
        $("#question-input").focus();
        scrollChatToBottom();
      }
    });
  }

  $("#ask-btn").on("click", function () {
    askQuestion();
  });

  // Suggestion Chips Click
  $(document).on("click", ".chip-btn", function () {
    const query = $(this).data("query");
    if (query && projectId) {
      askQuestion(query);
    }
  });

  // Textarea auto-resize and Enter key behavior
  const $questionInput = $("#question-input");
  function autoResizeInput() {
    $questionInput.css("height", "auto");
    const newHeight = Math.min($questionInput[0].scrollHeight, 120);
    $questionInput.css("height", (newHeight > 34 ? newHeight : 34) + "px");
  }

  $questionInput.on("input", autoResizeInput);

  $questionInput.on("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      askQuestion();
    }
  });

  // Clear chat button
  $("#clear-chat-btn").on("click", function () {
    if (confirm("Clear conversation history for this session?")) {
      $("#messages").empty();
      if (activeProjectStats) {
        appendMessage(
          "agent",
          `Conversation cleared. You can ask new questions about your loaded **${activeProjectStats.total_files} file(s)**.`,
          null,
          false,
          null
        );
      }
    }
  });

  // New session button
  $("#new-session-btn").on("click", function () {
    if (confirm("Start a new session? This will reset the workspace and purge session data from the server.")) {
      const oldProjId = projectId;
      if (oldProjId) {
        // Immediately delete previous session files and data from server
        $.ajax({
          url: "/api/project/delete/",
          type: "POST",
          contentType: "application/json",
          data: JSON.stringify({ project_id: oldProjId }),
          error: function (e) {
            console.warn("Could not delete previous session from server:", e);
          }
        });
      }

      projectId = null;
      activeProjectStats = null;
      stagedExcelFiles = [];
      renderStagedFiles();
      $("#dataset-panel").hide();
      $("#suggestions-box").hide();
      $("#empty-state").show();
      $("#messages").empty();
      $("#chat-subhead").text("Upload Excel files to begin querying");
      $("#session-tag").text("No dataset loaded");
      $("#question-input, #ask-btn, #clear-chat-btn").prop("disabled", true);
      $("#question-input").attr("placeholder", "Upload Excel files to ask questions...").val("");
      $("#upload-status").hide();
      $("#metadata-file").val("");
      $("#metadata-filename").text("No file selected");
    }
  });

});
