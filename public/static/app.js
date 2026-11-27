const $ = (selector) => document.querySelector(selector);
const welcome = $("#welcome");
const conversation = $("#conversation");
const library = $("#library");
const input = $("#questionInput");
const sendButton = $("#sendButton");
const state = { history: [], user: null, csrfToken: "", busy: false };

function headers(json = false) {
  const result = {};
  if (json) result["Content-Type"] = "application/json";
  if (state.csrfToken) result["X-CSRF-Token"] = state.csrfToken;
  return result;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

async function readJson(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("application/json")) {
    if (!response.ok) {
      throw new Error(`The server returned an error (${response.status}). Check Vercel function logs and DATABASE_URL_POOLED.`);
    }
    throw new Error("The server returned an unexpected response instead of JSON.");
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 3000);
}

function setView(view) {
  welcome.classList.toggle("hidden", view !== "welcome");
  conversation.classList.toggle("hidden", view !== "conversation");
  library.classList.toggle("hidden", view !== "library");
  $(".composer-wrap").classList.toggle("hidden", view !== "conversation");
  $("#headerState").textContent = view === "library" ? "Knowledge library" : view === "conversation" ? "Conversation" : "New conversation";
}

function statusClass(status) {
  return `status-${String(status).toLowerCase()}`;
}

async function loadDocuments() {
  const response = await fetch("/api/documents", { headers: headers() });
  const data = await readJson(response);
  $("#documentList").innerHTML = data.documents.map((doc) => `
    <a class="document-card" href="/api/source/${encodeURIComponent(doc.id)}" target="_blank" rel="noopener">
      <span><strong>${escapeHtml(doc.title)}</strong><small>${escapeHtml(doc.document_type)} · v${escapeHtml(doc.version)} · ${escapeHtml(doc.owner)} · ${escapeHtml(doc.repository)} · ${escapeHtml(doc.classification)}</small></span>
      <span class="status-pill ${statusClass(doc.status)}">${escapeHtml(doc.status)}</span>
    </a>
  `).join("") || '<p class="library-sub">No documents are available at this access level.</p>';
}

function addUserMessage(text) {
  const message = document.createElement("div");
  message.className = "message user-message";
  message.innerHTML = `<div class="user-bubble">${escapeHtml(text).replace(/\n/g, "<br>")}</div>`;
  conversation.append(message);
}

function renderAnswer(result) {
  const message = document.createElement("div");
  message.className = "message";
  const citedAnswer = escapeHtml(result.answer).replace(/\[(C\d+)\]/g, '<a href="#citation-$1">$1</a>');
  const warnings = result.warnings.map((warning) => `<div class="warning">⚠ ${escapeHtml(warning)}</div>`).join("");
  const citations = result.citations.length ? `
    <div class="citation-panel"><h3>SOURCE DOCUMENTS · ${result.citations.length}</h3>
      ${result.citations.map((citation) => `
        <a class="citation" id="citation-${escapeHtml(citation.ref)}" href="${escapeHtml(citation.source_url && citation.source_url.startsWith("https://") ? citation.source_url : `/api/source/${encodeURIComponent(citation.document_id)}/content`)}" target="_blank" rel="noopener">
          <span class="cite-ref">${escapeHtml(citation.ref)}</span><span class="cite-details"><strong>${escapeHtml(citation.title)}</strong>
          <small>${escapeHtml(citation.section)} · v${escapeHtml(citation.version)} · ${escapeHtml(citation.status)} · ${escapeHtml(citation.repository)}${citation.page_url ? ` · Source page: ${escapeHtml(citation.page_url)}` : ""}</small></span>
        </a>`).join("")}
    </div>` : "";
  message.innerHTML = `<div class="assistant-head"><span class="assistant-mark">G</span><strong>GPACT Assistant</strong><small>· ${escapeHtml(result.provider)}</small></div>
    ${warnings}<div class="answer-text">${citedAnswer}</div>${citations}
    <div class="answer-footer">${escapeHtml(result.disclaimer)}</div>`;
  conversation.append(message);
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  sendButton.disabled = true;
  setView("conversation");
  $("#library").classList.add("hidden");
  addUserMessage(question);
  const pending = document.createElement("div");
  pending.className = "typing";
  pending.textContent = "Searching authorised sources…";
  conversation.append(pending);
  conversation.scrollIntoView({ block: "end", behavior: "smooth" });
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({ question, history: state.history }),
    });
    const result = await readJson(response);
    pending.remove();
    renderAnswer(result);
    state.history.push({ role: "user", content: question }, { role: "assistant", content: result.answer });
    state.history = state.history.slice(-12);
  } catch (error) {
    pending.remove();
    const notice = document.createElement("div");
    notice.className = "warning";
    notice.textContent = error.message || "Unable to reach the assistant.";
    conversation.append(notice);
  } finally {
    state.busy = false;
    input.value = "";
    input.style.height = "auto";
    sendButton.disabled = true;
    input.focus();
    conversation.scrollIntoView({ block: "end", behavior: "smooth" });
  }
}

function openModal(id) {
  $(id).classList.remove("hidden");
}

function closeModals() {
  document.querySelectorAll(".modal-backdrop").forEach((modal) => modal.classList.add("hidden"));
}

document.querySelectorAll("[data-question]").forEach((button) => {
  button.addEventListener("click", () => ask(button.dataset.question));
});
$("#newChat").addEventListener("click", () => {
  state.history = [];
  conversation.replaceChildren();
  setView("welcome");
});
$("#askQuestion").addEventListener("click", () => {
  setView("conversation");
  input.focus();
});
$("#searchPolicy").addEventListener("click", () => {
  setView("library");
  loadDocuments().catch((error) => showToast(error.message));
});
$("#uploadDocuments").addEventListener("click", () => {
  if (!state.user || !["Manager", "Administrator"].includes(state.user.role)) {
    showToast("A Manager or Administrator role is required to upload documents.");
    return;
  }
  openModal("#ingestModal");
});
$("#sendButton").addEventListener("click", () => ask(input.value));
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  sendButton.disabled = !input.value.trim() || state.busy;
});
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    ask(input.value);
  }
});
document.querySelectorAll('a[href="#library"]').forEach((link) => link.addEventListener("click", (event) => {
  event.preventDefault();
  setView("library");
  loadDocuments().catch((error) => showToast(error.message));
}));
$("#libraryAdd").addEventListener("click", () => openModal("#ingestModal"));
$("#settingsToggle")?.addEventListener("click", () => openModal("#settingsModal"));
$("#logoutButton")?.addEventListener("click", async () => {
  try {
    await fetch("/auth/logout", { method: "POST", headers: headers() });
    window.location.assign("/");
  } catch (error) {
    showToast(error.message || "Sign out failed.");
  }
});
$("#demoLogout")?.addEventListener("click", async () => {
  try {
    await fetch("/auth/logout", { method: "POST", headers: headers() });
    window.location.assign("/auth/account");
  } catch (error) {
    showToast(error.message || "Sign out failed.");
  }
});
$("#helpButton").addEventListener("click", () => showToast("Answers are informational. Follow source links and confirm decisions with an authorised official."));
document.querySelectorAll(".close-modal").forEach((button) => button.addEventListener("click", closeModals));
document.querySelectorAll(".modal-backdrop").forEach((backdrop) => backdrop.addEventListener("click", (event) => {
  if (event.target === backdrop) closeModals();
}));
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
  $("#fileForm").classList.toggle("hidden", tab.dataset.tab !== "file");
  $("#urlForm").classList.toggle("hidden", tab.dataset.tab !== "url");
  $("#govzaForm").classList.toggle("hidden", tab.dataset.tab !== "govza");
  $("#departmentForm").classList.toggle("hidden", tab.dataset.tab !== "department");
  if (tab.dataset.tab === "department") loadNationalDepartments();
}));
async function loadGovzaCategories() {
  try {
    const response = await fetch("/api/govza/categories", { headers: headers() });
    const data = await readJson(response);
    $("#govzaCategory").innerHTML = data.categories.map((category) =>
      `<option value="${escapeHtml(category)}">${escapeHtml(category)}</option>`
    ).join("");
  } catch (error) {
    $("#govzaError").textContent = error.message;
  }
}
let departmentListLoaded = false;
async function loadNationalDepartments() {
  if (departmentListLoaded) return;
  const selector = $("#departmentUrl");
  const error = $("#departmentError");
  error.textContent = "";
  selector.innerHTML = '<option value="">Loading official directory…</option>';
  try {
    const response = await fetch("/api/govza/departments", { headers: headers() });
    const data = await readJson(response);
    selector.innerHTML = data.departments.map((department) =>
      `<option value="${escapeHtml(department.url)}">${escapeHtml(department.name)} — ${escapeHtml(new URL(department.url).hostname)}</option>`
    ).join("");
    if (!data.departments.length) throw new Error("No national department websites were found in the official directory.");
    departmentListLoaded = true;
  } catch (error) {
    selector.innerHTML = '<option value="">Department directory unavailable</option>';
    error.message && ($("#departmentError").textContent = error.message);
  }
}
$("#fileInput").addEventListener("change", (event) => {
  $("#fileName").textContent = event.target.files[0]?.name || "";
});

async function submitIngestion(event, endpoint, errorSelector, formData = false) {
  event.preventDefault();
  const form = event.currentTarget;
  const error = $(errorSelector);
  error.textContent = "";
  const body = formData ? new FormData(form) : JSON.stringify(Object.fromEntries(new FormData(form)));
  try {
    const response = await fetch(endpoint, { method: "POST", headers: formData ? headers() : headers(true), body });
    const result = await readJson(response);
    closeModals();
    form.reset();
    $("#fileName").textContent = "";
    await loadDocuments();
    if (!library.classList.contains("hidden")) setView("library");
    showToast(result.message);
  } catch (err) {
    error.textContent = err.message;
  }
}
$("#fileForm").addEventListener("submit", (event) => submitIngestion(event, "/api/ingest", "#fileError", true));
$("#urlForm").addEventListener("submit", (event) => submitIngestion(event, "/api/ingest-url", "#urlError"));
$("#govzaForm").addEventListener("submit", (event) => submitIngestion(event, "/api/ingest-govza", "#govzaError"));
$("#departmentForm").addEventListener("submit", (event) => submitIngestion(event, "/api/ingest-department", "#departmentError"));
$("#saveSettings").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/demo-role", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({ role: $("#roleSelect").value }),
    });
    const result = await readJson(response);
    state.user = result.user;
    updateUserSummary();
    closeModals();
    state.history = [];
    conversation.replaceChildren();
    setView("welcome");
    await loadDocuments();
    showToast(`Access view set to ${result.user.role}.`);
  } catch (error) {
    showToast(error.message);
  }
});

function updateUserSummary() {
  if (!state.user) return;
  $("#userName").textContent = state.user.name;
  $("#welcomeName").textContent = state.user.name.split(/\s+/)[0];
  $(".avatar").textContent = state.user.name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
  const clearance = ["Public", "Internal", "Restricted"][state.user.clearance];
  $("#accessSummary").textContent = `${state.user.role} · ${clearance}`;
  if ($("#roleSelect")) $("#roleSelect").value = state.user.role;
  $("#libraryAdd").classList.toggle("hidden", !["Manager", "Administrator"].includes(state.user.role));
}

async function bootstrap() {
  const response = await fetch("/api/session");
  const result = await readJson(response);
  state.user = result.user;
  state.csrfToken = result.csrf_token;
  updateUserSummary();
  setView("welcome");
  await Promise.all([loadDocuments(), loadGovzaCategories()]);
}

bootstrap().catch((error) => showToast(error.message));
