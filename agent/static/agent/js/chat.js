$(function () {
  let projectId = null;
  const KEY_STORAGE_NAME = "excel_agent_gemini_key";

  // Restore any previously saved key for this browser
  const savedKey = window.localStorage.getItem(KEY_STORAGE_NAME);
  if (savedKey) $("#gemini-key-input").val(savedKey);

  $("#gemini-key-input").on("input", function () {
    const val = $(this).val().trim();
    if (val) {
      window.localStorage.setItem(KEY_STORAGE_NAME, val);
    } else {
      window.localStorage.removeItem(KEY_STORAGE_NAME);
    }
  });

  function currentApiKey() {
    return $("#gemini-key-input").val().trim();
  }

  function appendMessage(role, text, meta, isError, code) {
    const cls = "msg " + role + (isError ? " error" : "");
    const $msg = $("<div>").addClass(cls);

    if (role === "user") {
      $msg.text(text);
    } else {
      const $body = $("<div>").addClass("msg-body");
      if (typeof marked !== "undefined" && typeof marked.parse === "function") {
        $body.html(marked.parse(text));
      } else {
        $body.text(text);
      }
      $msg.append($body);

      if (code) {
        const $codeBox = $(`
          <details class="code-details">
            <summary>View Pandas Code</summary>
            <pre><code>${$("<div/>").text(code).html()}</code></pre>
          </details>
        `);
        $msg.append($codeBox);
      }
    }

    if (meta) {
      $msg.append($("<span>").addClass("meta").text(meta));
    }
    $("#messages").append($msg);
    $("#messages").scrollTop($("#messages")[0].scrollHeight);
  }

  $("#upload-btn").on("click", function () {
    const excelFiles = $("#excel-files")[0].files;
    const metadataFile = $("#metadata-file")[0].files[0];

    if (!excelFiles.length) {
      $("#upload-status").text("Please choose at least one Excel file.");
      return;
    }

    const formData = new FormData();
    for (const f of excelFiles) formData.append("excel_files", f);
    if (metadataFile) formData.append("metadata_file", metadataFile);

    $("#upload-status").text(`Uploading and parsing ${excelFiles.length} file(s)...`);
    $("#upload-btn").prop("disabled", true);
    $("#file-list").empty();

    $.ajax({
      url: "/api/upload/",
      type: "POST",
      data: formData,
      processData: false,
      contentType: false,
      success: function (resp) {
        projectId = resp.project_id;
        $("#upload-status").text(
          `Parsed ${resp.files.length} file(s), ${resp.variables.length} total sheet(s).` +
          (resp.warnings ? " Some files failed - see list below." : "")
        );
        $("#file-list").empty();
        resp.files.forEach(function (f) {
          $("#file-list").append($("<li>").text(f.name + " -> " + f.variables.join(", ")));
        });
        if (resp.warnings) {
          resp.warnings.forEach(function (w) {
            $("#file-list").append($("<li>").addClass("error").text("Skipped: " + w));
          });
        }
        $("#schema-preview").text(resp.schema_preview);
        $("#chat-card").show();
        $("#question-input, #ask-btn").prop("disabled", false);
      },
      error: function (xhr) {
        const err = xhr.responseJSON ? xhr.responseJSON.error : "Upload failed.";
        $("#upload-status").text("Error: " + err);
      },
      complete: function () {
        $("#upload-btn").prop("disabled", false);
      },
    });
  });

  function askQuestion() {
    const question = $("#question-input").val().trim();
    if (!question || !projectId) return;

    appendMessage("user", question);
    $("#question-input").val("");
    $("#ask-btn, #question-input").prop("disabled", true);
    appendMessage("agent", "Thinking and analyzing...", null, false);

    $.ajax({
      url: "/api/ask/",
      type: "POST",
      contentType: "application/json",
      data: JSON.stringify({ project_id: projectId, question: question, gemini_api_key: currentApiKey() }),
      success: function (resp) {
        $("#messages .msg.agent").last().remove();
        const meta = "attempts: " + resp.attempts + " | status: " + resp.status;
        appendMessage("agent", resp.answer, meta, resp.status !== "ok", resp.code);
      },
      error: function (xhr) {
        $("#messages .msg.agent").last().remove();
        const err = xhr.responseJSON ? xhr.responseJSON.error : "Something went wrong.";
        appendMessage("agent", err, null, true);
      },
      complete: function () {
        $("#ask-btn, #question-input").prop("disabled", false);
        $("#question-input").focus();
      },
    });
  }

  $("#ask-btn").on("click", askQuestion);
  $("#question-input").on("keydown", function (e) {
    if (e.key === "Enter") askQuestion();
  });
});
