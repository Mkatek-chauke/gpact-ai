const $ = (selector) => document.querySelector(selector);
const welcome = $("#welcome");
const conversation = $("#conversation");
const library = $("#library");
const input = $("#questionInput");
const sendButton = $("#sendButton");
const state = {
  history: [],
  recentSearches: [],
  documents: [],
  sourceFilters: ["all"],
  user: null,
  csrfToken: "",
  liveSearchEnabled: false,
  busy: false,
  demoAuth: false,
  documentContextId: "",
  documentContextTitle: "",
};
const SOURCE_CATEGORIES = [
  { name: "Government Websites", icon: "🏛", filter: "department_websites", matches: (doc) => Boolean(doc.source_url || doc.page_url) || /department|intranet/i.test(doc.repository || "") },
  { name: "Policy Library", icon: "📚", filter: "policies", matches: (doc) => /policy/i.test(doc.repository || "") || doc.document_type === "Policy" },
  { name: "Government Gazette", icon: "📄", filter: "gazettes", matches: (doc) => /gazette/i.test(doc.repository || "") },
  { name: "Legislation", icon: "⚖", filter: "legislation", matches: (doc) => /\b(act|bill|legislation|regulation|statute)\b/i.test(`${doc.title} ${doc.document_type}`) },
  { name: "NSFAS Information", icon: "🎓", filter: "grants_benefits", matches: (doc) => /\bnsfas\b/i.test(`${doc.title} ${doc.document_type}`) },
  { name: "Housing Programmes", icon: "🏠", filter: "grants_benefits", matches: (doc) => /\bhousing\b/i.test(`${doc.title} ${doc.document_type}`) },
  { name: "SASSA Grants", icon: "👶", filter: "grants_benefits", matches: (doc) => /\b(sassa|grant|grants|social assistance)\b/i.test(`${doc.title} ${doc.document_type}`) },
  { name: "Employment & Labour", icon: "💼", filter: "department_websites", matches: (doc) => /\b(employment|labour|leave|workplace)\b/i.test(`${doc.title} ${doc.document_type} ${doc.owner}`) },
  { name: "Water & Sanitation", icon: "🚰", filter: "department_websites", matches: (doc) => /\b(water|sanitation)\b/i.test(`${doc.title} ${doc.document_type} ${doc.owner}`) },
  { name: "Health Services", icon: "🏥", filter: "department_websites", matches: (doc) => /\b(health|medical|clinic)\b/i.test(`${doc.title} ${doc.document_type} ${doc.owner}`) },
];

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
  const views = {
    welcome,
    library,
    sources: $("#sourcesView"),
    saved: $("#savedView"),
    recent: $("#recentView"),
    conversations: $("#conversationsView"),
  };
  welcome.classList.toggle("hidden", view !== "welcome");
  Object.entries(views).forEach(([name, element]) => {
    element.classList.toggle("hidden", name !== view);
  });
  const showPortal = view === "conversation" && conversation.children.length === 0;
  $("#conversationStart").classList.toggle("hidden", !showPortal);
  conversation.classList.toggle("hidden", view !== "conversation" || showPortal);
  $(".composer-wrap").classList.toggle("hidden", view !== "conversation");
  $(".main").classList.toggle("portal-mode", showPortal);
  const labels = {
    welcome: "Dashboard",
    conversation: showPortal ? "Government search" : "Conversation",
    library: "Policy Library",
    sources: "Government Sources",
    saved: "Saved Documents",
    recent: "Recent Searches",
    conversations: "Conversations",
  };
  $("#headerState").textContent = labels[view] || "Dashboard";
  document.querySelectorAll(".workspace-nav .nav-item").forEach((item) => item.classList.remove("active"));
  const activeItems = {
    welcome: "#dashboardNavigation",
    conversation: "#conversationsNavigation",
    library: "#libraryNavigation",
    sources: "#sourcesNavigation",
    saved: "#savedNavigation",
    recent: "#recentNavigation",
    conversations: "#conversationsNavigation",
  };
  if (activeItems[view]) $(activeItems[view]).classList.add("active");
}

function statusClass(status) {
  return `status-${String(status).toLowerCase()}`;
}

async function loadDocuments() {
  const response = await fetch("/api/documents", { headers: headers() });
  const data = await readJson(response);
  state.documents = data.documents;
  $("#documentList").innerHTML = data.documents.map((doc) => `
    <a class="document-card" href="/documents/${encodeURIComponent(doc.id)}">
      <span><strong>${escapeHtml(doc.title)}</strong><small>${escapeHtml(doc.document_type)} · v${escapeHtml(doc.version)} · ${escapeHtml(doc.owner)} · ${escapeHtml(doc.repository)} · ${escapeHtml(doc.classification)}</small></span>
      <span class="status-pill ${statusClass(doc.status)}">${escapeHtml(doc.status)}</span>
    </a>
  `).join("") || '<p class="library-sub">No documents are available at this access level.</p>';
  renderSourceCoverage();
}

function latestIndexedDate(documents) {
  const timestamps = documents
    .map((doc) => doc.uploaded_at)
    .filter((timestamp) => typeof timestamp === "string" && !Number.isNaN(Date.parse(timestamp)))
    .sort();
  return timestamps.length
    ? new Date(timestamps[timestamps.length - 1]).toLocaleDateString()
    : "Index date unavailable";
}

function renderSourceCard(category) {
  const matches = state.documents.filter(category.matches);
  const lastIndexed = latestIndexedDate(matches);
  return `
    <button class="source-card" type="button" data-source-filter="${category.filter}" aria-label="Search ${escapeHtml(category.name)}; ${matches.length} indexed documents">
      <span class="source-card-icon" aria-hidden="true">${category.icon}</span>
      <strong>${escapeHtml(category.name)}</strong>
      <span class="source-card-count">${matches.length} indexed ${matches.length === 1 ? "document" : "documents"}</span>
      <small>Last indexed: ${escapeHtml(lastIndexed)}</small>
    </button>
  `;
}

function renderSourceCoverage() {
  $("#sourceCards").innerHTML = SOURCE_CATEGORIES.map(renderSourceCard).join("");
  $("#allSourceCards").innerHTML = SOURCE_CATEGORIES.map(renderSourceCard).join("");
  const repositories = new Set(state.documents.map((doc) => doc.repository).filter(Boolean));
  const categoriesWithContent = SOURCE_CATEGORIES.filter((category) => state.documents.some(category.matches));
  $("#indexedDocumentCount").textContent = state.documents.length.toLocaleString();
  $("#indexedRepositoryCount").textContent = repositories.size.toLocaleString();
  $("#indexedSourceCategoryCount").textContent = categoriesWithContent.length.toLocaleString();
  $("#indexedUpdatedDate").textContent = latestIndexedDate(state.documents);
  updateSourceTransparency();
}

function selectedSourceFilters() {
  return [...document.querySelectorAll('input[name="sourceFilter"]:checked')].map((checkbox) => checkbox.value);
}

function updateSourceTransparency() {
  if (!$("#sourceTransparency")) return;
  const filters = selectedSourceFilters();
  const label = filters.includes("all") ? "all indexed source categories" : `${filters.length} selected indexed ${filters.length === 1 ? "category" : "categories"}`;
  const searchWebsite = state.liveSearchEnabled && !state.documentContextId;
  const webSources = searchWebsite ? " + public web" : "";
  $("#sourceTransparency").textContent = `Searching ${label}${webSources} · ${state.documents.length} accessible indexed ${state.documents.length === 1 ? "document" : "documents"}`;
}

function selectSourceFilter(sourceFilter) {
  const allSources = document.querySelector('input[name="sourceFilter"][value="all"]');
  const selected = document.querySelector(`input[name="sourceFilter"][value="${sourceFilter}"]`);
  if (!selected) return;
  selected.checked = true;
  if (sourceFilter === "all") {
    document.querySelectorAll('input[name="sourceFilter"]:not([value="all"])').forEach((checkbox) => { checkbox.checked = false; });
  } else {
    allSources.checked = false;
  }
  state.sourceFilters = selectedSourceFilters();
  updateSourceTransparency();
}

function renderSavedDocuments() {
  const saved = state.documents.filter((doc) => localStorage.getItem(`gpact-saved-document-${doc.id}`) === "true");
  $("#savedDocumentList").innerHTML = saved.map((doc) => `
    <a class="document-card" href="/documents/${encodeURIComponent(doc.id)}">
      <span><strong>${escapeHtml(doc.title)}</strong><small>${escapeHtml(doc.document_type)} · v${escapeHtml(doc.version)} · ${escapeHtml(doc.repository)}</small></span>
      <span class="status-pill ${statusClass(doc.status)}">${escapeHtml(doc.status)}</span>
    </a>
  `).join("") || '<p class="library-sub">No saved documents on this device.</p>';
}

function renderRecentSearches() {
  $("#recentSearchList").innerHTML = state.recentSearches.map((question) => `
    <button class="recent-search-item" type="button" data-question="${escapeHtml(question)}"><span>${escapeHtml(question)}</span><b>↗</b></button>
  `).join("") || '<p class="library-sub">No searches in this session yet.</p>';
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
  const hasEvidence = result.citations.length > 0;
  const citedAnswer = escapeHtml(result.answer).replace(/\[(C\d+)\]/g, '<a href="#citation-$1">$1</a>');
  const warnings = result.warnings.map((warning) => `<div class="warning">⚠ ${escapeHtml(warning)}</div>`).join("");
  const citedDocumentIds = new Set(result.citations.map((citation) => citation.document_id));
  const citations = result.citations.length ? `
    <div class="citation-panel"><h3>SOURCES USED · ${result.citations.length}</h3>
      ${result.citations.map((citation) => `
        <a class="citation" id="citation-${escapeHtml(citation.ref)}" href="${escapeHtml(citation.source_url && citation.source_url.startsWith("https://") ? citation.source_url : `/documents/${encodeURIComponent(citation.document_id)}`)}" target="_blank" rel="noopener">
          <span class="cite-ref">${escapeHtml(citation.ref)}</span><span class="cite-details"><strong>${escapeHtml(citation.title)}</strong>
          <small>${citation.source_type === "live_web"
            ? `Public website · ${escapeHtml(citation.owner || "Publisher not identified")} · ${escapeHtml(citation.status)}`
            : `${escapeHtml(citation.section)} · v${escapeHtml(citation.version)} · ${escapeHtml(citation.status)} · ${escapeHtml(citation.repository)} · ${escapeHtml(citation.owner || "Department not specified")}${citation.page_url ? ` · Source page: ${escapeHtml(citation.page_url)}` : ""}`}</small></span>
        </a>`).join("")}
    </div>` : "";
  const relatedDocuments = (result.related_documents || []).filter((doc) => !citedDocumentIds.has(doc.id));
  const related = hasEvidence ? `
    <div class="related-panel"><h3>RELATED DOCUMENTS</h3>
      ${relatedDocuments.length ? relatedDocuments.map((doc) => `
        <a class="related-document" href="/documents/${encodeURIComponent(doc.id)}">
          <strong>${escapeHtml(doc.title)}</strong>
          <small>${escapeHtml(doc.document_type)} · v${escapeHtml(doc.version)} · ${escapeHtml(doc.repository)}</small>
        </a>`).join("") : '<p>No additional related documents were found in the selected sources.</p>'}
    </div>` : "";
  const confidence = result.confidence_score == null ? "—" : `${result.confidence_score}%`;
  const publisher = result.relevant_department || "Not identified in the matched source metadata";
  const publisherLabel = result.citations.some((citation) => citation.source_type === "live_web")
    ? "Publisher / Website"
    : "Relevant Government Department";
  const answerMetadata = hasEvidence ? `
    <div class="answer-metrics">
      <div><small>Confidence Score</small><strong>${confidence}</strong><span>Retrieval match only; not a measure of correctness.</span></div>
      <div><small>${publisherLabel}</small><strong>${escapeHtml(publisher)}</strong></div>
    </div>
    ${citations}${related}
    <div class="answer-footer">${escapeHtml(result.disclaimer)}</div>` : "";
  message.innerHTML = `<div class="assistant-head"><span class="assistant-mark">G</span><strong>GPACT Assistant</strong><small>· ${escapeHtml(result.provider)}</small></div>
    ${warnings}<div class="answer-panel"><strong>AI Answer</strong><div class="answer-text">${citedAnswer}</div></div>
    ${answerMetadata}`;
  conversation.append(message);
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  sendButton.disabled = true;
  setView("conversation");
  $("#conversationStart").classList.add("hidden");
  $(".main").classList.remove("portal-mode");
  $("#library").classList.add("hidden");
  state.recentSearches = [question, ...state.recentSearches.filter((item) => item !== question)].slice(0, 10);
  renderRecentSearches();
  addUserMessage(question);
  setView("conversation");
  const pending = document.createElement("div");
  pending.className = "typing";
  const includesLiveWeb = state.liveSearchEnabled && !state.documentContextId;
  pending.textContent = includesLiveWeb
    ? "Searching indexed documents and the public web…"
    : "Searching authorised indexed sources…";
  conversation.append(pending);
  conversation.scrollIntoView({ block: "end", behavior: "smooth" });
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({
        question,
        history: state.history,
        source_filters: state.sourceFilters,
        ...(state.documentContextId ? { document_id: state.documentContextId } : {}),
      }),
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

document.addEventListener("click", (event) => {
  const searchButton = event.target.closest("[data-question]");
  if (searchButton) ask(searchButton.dataset.question);
});
$("#newChat").addEventListener("click", () => {
  state.history = [];
  state.documentContextId = "";
  state.documentContextTitle = "";
  sessionStorage.removeItem("gpact-document-context");
  $("#chatContext").classList.add("hidden");
  conversation.replaceChildren();
  setView("conversation");
});
$("#dashboardNavigation").addEventListener("click", () => setView("welcome"));
$("#conversationsNavigation").addEventListener("click", () => setView("conversations"));
$("#libraryNavigation").addEventListener("click", () => {
  setView("library");
  loadDocuments().catch((error) => showToast(error.message));
});
$("#sourcesNavigation").addEventListener("click", () => setView("sources"));
$("#savedNavigation").addEventListener("click", () => {
  renderSavedDocuments();
  setView("saved");
});
$("#recentNavigation").addEventListener("click", () => {
  renderRecentSearches();
  setView("recent");
});
$("#settingsNavigation").addEventListener("click", () => {
  if (state.demoAuth) openModal("#settingsModal");
  else showToast("Account settings are managed by your department administrator.");
});
$("#askQuestion").addEventListener("click", () => {
  state.history = [];
  conversation.replaceChildren();
  state.documentContextId = "";
  state.documentContextTitle = "";
  $("#chatContext").classList.add("hidden");
  setView("conversation");
  input.focus();
});
$("#startSearchFromConversations").addEventListener("click", () => {
  state.history = [];
  conversation.replaceChildren();
  state.documentContextId = "";
  state.documentContextTitle = "";
  $("#chatContext").classList.add("hidden");
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
$("#clearChatContext").addEventListener("click", () => {
  state.documentContextId = "";
  state.documentContextTitle = "";
  $("#chatContext").classList.add("hidden");
});
document.querySelectorAll('input[name="sourceFilter"]').forEach((checkbox) => {
  checkbox.addEventListener("change", () => {
    if (checkbox.value === "all" && checkbox.checked) {
      document.querySelectorAll('input[name="sourceFilter"]:not([value="all"])').forEach((item) => { item.checked = false; });
    } else if (checkbox.value !== "all" && checkbox.checked) {
      document.querySelector('input[name="sourceFilter"][value="all"]').checked = false;
    }
    const checked = document.querySelectorAll('input[name="sourceFilter"]:checked');
    if (!checked.length) document.querySelector('input[name="sourceFilter"][value="all"]').checked = true;
    state.sourceFilters = selectedSourceFilters();
    updateSourceTransparency();
  });
});
document.querySelectorAll("#sourceCards, #allSourceCards").forEach((sourceCards) => {
  sourceCards.addEventListener("click", (event) => {
    const card = event.target.closest("[data-source-filter]");
    if (!card) return;
    selectSourceFilter(card.dataset.sourceFilter);
    setView("conversation");
    input.focus();
  });
});
$("#viewAllSources").addEventListener("click", () => setView("sources"));
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
  state.demoAuth = result.demo;
  state.liveSearchEnabled = result.live_search_enabled === true;
  updateUserSummary();
  setView("welcome");
  const savedDocumentContext = sessionStorage.getItem("gpact-document-context");
  if (savedDocumentContext) {
    try {
      const context = JSON.parse(savedDocumentContext);
      if (typeof context.id === "string" && context.id) {
        state.documentContextId = context.id;
        state.documentContextTitle = typeof context.title === "string" ? context.title : "Selected document";
        $("#chatContextTitle").textContent = state.documentContextTitle;
        $("#chatContext").classList.remove("hidden");
        setView("conversation");
        input.focus();
      }
    } finally {
      sessionStorage.removeItem("gpact-document-context");
    }
  }
  await Promise.all([loadDocuments(), loadGovzaCategories()]);
}

bootstrap().catch((error) => showToast(error.message));
