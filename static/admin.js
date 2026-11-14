/* ==========================================================================
   CleanSpace Admin CRM — list and detail pages.
   - All request data is inserted with textContent (never innerHTML).
   - Every change is sent with the CSRF token in the X-CSRFToken header.
   - Success is shown only after the server confirms the database write.
   - Texts come from the i18n dictionaries; status/service/unit CODES from the API
     are translated here. Customer text and notes are never translated.
   ========================================================================== */

"use strict";

const TIMEOUT_MS = 10000;
const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.content || "";
const T = (key, params) => CSI18n.t(key, params);

document.addEventListener("DOMContentLoaded", () => {
  const root = document.querySelector("[data-page]");
  if (!root) return;
  if (root.dataset.page === "list") initListPage();
  if (root.dataset.page === "detail") initDetailPage(root);
});

/* --------------------------------------------------------------------------
   Helpers
   -------------------------------------------------------------------------- */
const $ = (id) => document.getElementById(id);

class NetworkError extends Error {}

async function api(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.method && options.method !== "GET") {
    headers["X-CSRFToken"] = CSRF_TOKEN;
    headers["Content-Type"] = "application/json";
  }
  let response;
  try {
    response = await fetch(url, { ...options, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal });
  } catch {
    throw new NetworkError("network");
  } finally {
    clearTimeout(timer);
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return { status: response.status, ok: response.ok, body };
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

const money = (g, cur = "PLN") => CSI18n.fmtMoney(g, cur);
const statusText = (code) => T(`status.${code}`, { fallback: code });
const serviceText = (item) => T(`service.${item.service}`, { fallback: item.label || item.service });
const notProvided = () => T("common.notProvided");

function itemLabel(item) {
  let label = serviceText(item);
  for (const [group, choice] of Object.entries(item.options || {})) {
    label += ` — ${T(`option.${group}.${choice}`, { fallback: choice })}`;
  }
  return label;
}

function itemQuantity(item) {
  return `${CSI18n.fmtQuantity(item.quantity)} ${T(`unit.${item.unit}`, { count: Number(item.quantity), fallback: item.unit })}`;
}

function preferredText(r) {
  const date = r.preferredDate ? CSI18n.fmtDate(r.preferredDate, { weekday: "short", day: "numeric", month: "short", year: "numeric" }) : "";
  const time = r.preferredTime ? T(`times.${r.preferredTime}`) : notProvided();
  return `${date}, ${time}`;
}

function statusBadge(status) {
  return el("span", `status-badge status-${status}`, statusText(status));
}

function goToLogin() {
  window.location.assign("/admin/login");
}

/** messages: [{key, params} | {text}] — re-rendered on language change. */
function setMessage(box, kind, messages) {
  box.className = `save-status is-${kind}`;
  box._messages = { kind, messages };
  box.replaceChildren(...messages.map((m) => el("p", "", messageText(m))));
  box.hidden = false;
}

function rerenderMessage(box) {
  if (!box.hidden && box._messages) setMessage(box, box._messages.kind, box._messages.messages);
}

/* A stored message is {key, params} (params may be CSI18n.ref values) or
   {serverError: {body, field}} (the server's error code, translated on every render).
   Already translated text is never stored, so a language switch updates every part. */
function messageText(m) {
  if (m.serverError) return errorMessage(m.serverError.body, m.serverError.field);
  return T(m.key, m.params);
}

function errorMessage(body, key) {
  const code = body && body.fieldCodes && body.fieldCodes[key];
  if (code && CSI18n.has(`errors.${code.code}`)) return T(`errors.${code.code}`, code.params || {});
  return (body && body.fields && body.fields[key]) || "";
}

/* --------------------------------------------------------------------------
   List page
   -------------------------------------------------------------------------- */
function initListPage() {
  const ui = {
    form: $("filters"), q: $("filter-q"), status: $("filter-status"), reset: $("filter-reset"),
    list: $("request-list"), summary: $("results-summary"),
    loading: $("state-loading"), error: $("state-error"), errorText: $("state-error-text"), retry: $("retry-load"),
    empty: $("state-empty"), noResults: $("state-no-results"), clearFilters: $("clear-filters"),
    pagination: $("pagination"), prev: $("page-prev"), next: $("page-next"), pageInfo: $("page-info"),
  };
  const state = { status: "", q: "", page: 1, totalPages: 1, loadId: 0, data: null, errorKey: null };

  function readUrl() {
    const params = new URLSearchParams(window.location.search);
    state.status = params.get("status") || "";
    state.q = params.get("q") || "";
    const page = Number.parseInt(params.get("page") || "1", 10);
    state.page = Number.isInteger(page) && page > 0 ? page : 1;
    ui.q.value = state.q;
    ui.status.value = [...ui.status.options].some((o) => o.value === state.status) ? state.status : "";
  }

  function queryString() {
    const params = new URLSearchParams();
    if (state.status) params.set("status", state.status);
    if (state.q) params.set("q", state.q);
    if (state.page > 1) params.set("page", String(state.page));
    return params.toString();
  }

  function showOnly(which) {
    for (const key of ["loading", "error", "empty", "noResults"]) ui[key].hidden = key !== which;
  }

  function renderItem(item, backQuery) {
    const li = el("li", "request-card");
    const head = el("div", "card-head");
    const link = el("a", "request-link", `#${item.id}`);
    link.href = `/admin/requests/${item.id}${backQuery ? `?back=${encodeURIComponent(backQuery)}` : ""}`;
    link.setAttribute("aria-label", T("admin.list.open", { id: item.id }));
    head.append(link, statusBadge(item.status));

    const name = el("p", "card-name", item.name);

    const facts = el("dl", "card-facts");
    const add = (label, value, cls) => {
      const wrap = el("div", cls || "");
      wrap.append(el("dt", "", label), el("dd", "", value));
      facts.append(wrap);
    };
    const first = item.items[0];
    let services = first ? `${itemLabel(first)} · ${itemQuantity(first)}` : notProvided();
    if (item.items.length > 1) services += ` ${T("admin.list.more", { count: item.items.length - 1 })}`;
    add(T("admin.list.created"), CSI18n.fmtDateTime(item.createdAt));
    add(T("admin.list.services"), services, "fact-services");
    const total = item.agreedTotalGrosze
      ? `${money(item.agreedTotalGrosze, item.currency)} (${T("admin.list.agreed")})`
      : money(item.totalGrosze, item.currency);
    add(T("admin.list.total"), total, "fact-price");
    add(T("admin.list.preferred"), preferredText(item));
    li.append(head, name, facts);
    return li;
  }

  function render() {
    const data = state.data;
    if (state.errorKey) {
      ui.errorText.textContent = messageText(state.errorKey);
      return;
    }
    if (!data) return;
    document.querySelectorAll("[data-count]").forEach((node) => {
      const value = (data.counts || {})[node.dataset.count];
      node.textContent = Number.isInteger(value) ? String(value) : "—";
    });
    const qs = queryString();
    ui.list.replaceChildren(...data.items.map((item) => renderItem(item, qs)));
    const filtered = Boolean(state.status || state.q);
    if (data.total === 0) {
      ui.summary.textContent = filtered ? T("admin.list.zeroFiltered") : "";
    } else {
      const from = (data.page - 1) * data.pageSize + 1;
      const to = from + data.items.length - 1;
      ui.summary.textContent = T(filtered ? "admin.list.summaryFiltered" : "admin.list.summary", { from, to, total: data.total });
    }
    ui.pageInfo.textContent = T("admin.list.page", { page: data.page, pages: data.totalPages });
  }

  async function load({ push = false, focusList = false } = {}) {
    const loadId = ++state.loadId;
    const qs = queryString();
    if (push) history.pushState(null, "", qs ? `?${qs}` : window.location.pathname);
    showOnly("loading");
    ui.list.setAttribute("aria-busy", "true");
    ui.summary.textContent = "";
    state.errorKey = null;

    let res;
    try {
      res = await api(`/admin/api/requests${qs ? `?${qs}` : ""}`);
    } catch {
      if (loadId !== state.loadId) return;
      ui.list.replaceChildren();
      ui.pagination.hidden = true;
      state.data = null;
      state.errorKey = { key: "admin.list.errorNetwork" };
      render();
      showOnly("error");
      ui.list.removeAttribute("aria-busy");
      return;
    }
    if (loadId !== state.loadId) return;
    ui.list.removeAttribute("aria-busy");
    if (res.status === 401) return goToLogin();
    if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
      ui.list.replaceChildren();
      ui.pagination.hidden = true;
      state.data = null;
      const fieldKey = res.status === 400 && res.body && res.body.fields ? Object.keys(res.body.fields)[0] : null;
      state.errorKey = fieldKey ? { serverError: { body: res.body, field: fieldKey } } : { key: "admin.list.errorServer" };
      render();
      showOnly("error");
      return;
    }
    const data = res.body;
    state.data = data;
    state.page = data.page;
    state.totalPages = data.totalPages;
    const finalQs = queryString();
    if (finalQs !== qs) history.replaceState(null, "", finalQs ? `?${finalQs}` : window.location.pathname);
    const filtered = Boolean(state.status || state.q);
    showOnly(data.total === 0 ? (filtered ? "noResults" : "empty") : null);
    ui.pagination.hidden = data.totalPages <= 1;
    ui.prev.disabled = data.page <= 1;
    ui.next.disabled = data.page >= data.totalPages;
    render();
    if (focusList) (ui.list.querySelector("a") || ui.summary).focus?.();
  }

  ui.form.addEventListener("submit", (event) => {
    event.preventDefault();
    state.q = ui.q.value.trim();
    state.status = ui.status.value;
    state.page = 1;
    load({ push: true });
  });
  ui.status.addEventListener("change", () => {
    state.status = ui.status.value;
    state.q = ui.q.value.trim();
    state.page = 1;
    load({ push: true });
  });
  const clearAll = () => {
    ui.q.value = "";
    ui.status.value = "";
    Object.assign(state, { q: "", status: "", page: 1 });
    load({ push: true });
    ui.q.focus();
  };
  ui.reset.addEventListener("click", clearAll);
  ui.clearFilters.addEventListener("click", clearAll);
  ui.retry.addEventListener("click", () => load());
  ui.prev.addEventListener("click", () => {
    if (state.page > 1) { state.page -= 1; load({ push: true, focusList: true }); }
  });
  ui.next.addEventListener("click", () => {
    if (state.page < state.totalPages) { state.page += 1; load({ push: true, focusList: true }); }
  });
  window.addEventListener("popstate", () => { readUrl(); load(); });
  document.addEventListener("cs:langchange", render);

  readUrl();
  load();
}

/* --------------------------------------------------------------------------
   Detail page
   -------------------------------------------------------------------------- */
function initDetailPage(root) {
  const id = Number.parseInt(root.dataset.requestId, 10);
  const ui = {
    back: $("back-link"), loading: $("state-loading"), error: $("state-error"), errorText: $("state-error-text"),
    retry: $("retry-load"), detail: $("detail"), form: $("edit-form"),
    status: $("edit-status"), statusError: $("edit-status-error"),
    note: $("edit-note"), noteCount: $("edit-note-count"), noteError: $("edit-note-error"),
    agreedDate: $("edit-agreed-date"), agreedDateError: $("edit-agreed-date-error"),
    agreedTime: $("edit-agreed-time"), agreedTimeError: $("edit-agreed-time-error"),
    agreedTotal: $("edit-agreed-total"), agreedTotalError: $("edit-agreed-total-error"),
    reason: $("edit-price-reason"), reasonError: $("edit-price-reason-error"),
    updated: $("d-updated"), save: $("save-btn"), message: $("save-status"),
    conflict: $("conflict-actions"), loadLatest: $("load-latest"),
    history: $("history"), historyEmpty: $("history-empty"),
  };
  const errorFields = {
    status: [ui.status, ui.statusError], adminNote: [ui.note, ui.noteError],
    agreedDate: [ui.agreedDate, ui.agreedDateError], agreedTime: [ui.agreedTime, ui.agreedTimeError],
    agreedTotal: [ui.agreedTotal, ui.agreedTotalError], priceReason: [ui.reason, ui.reasonError],
  };

  const back = new URLSearchParams(window.location.search).get("back");
  if (back && /^[\w%=&+.\-@]*$/.test(back)) ui.back.href = `/admin?${back}`;

  let request = null;   // last version confirmed by the server
  let historyRows = [];
  let notification = null;
  let noteMax = Number.parseInt(ui.note.getAttribute("maxlength"), 10) || 2000;
  let saving = false;
  let conflict = false;
  let fieldErrorBody = null;
  let loadErrorKey = null;

  const normalize = (text) => text.replace(/\r\n/g, "\n").trim();
  const grosze = (g) => (g === null || g === undefined ? "" : (g % 100 ? (g / 100).toFixed(2) : String(g / 100)));
  const parsedTotal = () => {
    const v = ui.agreedTotal.value.trim().replace(",", ".").replace(/\s/g, "");
    if (!v) return null;
    return /^\d{1,9}(\.\d{1,2})?$/.test(v) ? Math.round(Number(v) * 100) : NaN;
  };

  function formValues() {
    return {
      status: ui.status.value,
      adminNote: normalize(ui.note.value),
      agreedDate: ui.agreedDate.value || null,
      agreedTime: ui.agreedTime.value.trim() || null,
      agreedTotal: parsedTotal(),
    };
  }

  const isDirty = () => {
    if (!request) return false;
    const v = formValues();
    return v.status !== request.status || v.adminNote !== request.adminNote ||
      v.agreedDate !== (request.agreedDate || null) || v.agreedTime !== (request.agreedTime || null) ||
      !Object.is(v.agreedTotal, request.agreedTotalGrosze ?? null) || ui.reason.value.trim() !== "";
  };

  function updateNoteCount() {
    ui.noteCount.textContent = T("admin.detail.noteCount", { count: ui.note.value.length, max: noteMax });
  }

  function updateSaveButton() {
    ui.save.disabled = saving || conflict || !isDirty();
    ui.save.textContent = T(saving ? "admin.detail.saving" : "admin.detail.save");
  }

  function clearFieldErrors() {
    fieldErrorBody = null;
    for (const [input, err] of Object.values(errorFields)) {
      err.textContent = "";
      input.removeAttribute("aria-invalid");
    }
  }

  function renderFieldErrors() {
    if (!fieldErrorBody) return;
    for (const [key, [input, err]] of Object.entries(errorFields)) {
      const text = errorMessage(fieldErrorBody, key);
      err.textContent = text;
      if (text) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  }

  /* ---- Read-only parts ---- */
  function renderReadOnly() {
    const r = request;
    const set = (key, text) => { $(key).textContent = text; };
    set("d-id", `#${r.id}`);
    set("d-created", `${CSI18n.fmtDateTime(r.createdAt)} ${T("admin.detail.warsaw")}`);
    set("d-name", r.name);
    set("d-email", r.email);
    set("d-phone", r.phone || notProvided());
    set("d-address", r.street || r.city ? [r.street, r.apartment, r.city].filter(Boolean).join(", ") : notProvided());
    set("d-preferred", `${preferredText(r)} ${T("admin.detail.preferredHint")}`);
    set("d-comment", r.comment ? r.comment : T("common.none"));

    $("d-items").replaceChildren(...r.items.map((item) => {
      const tr = el("tr");
      const name = el("th", "", itemLabel(item));
      name.scope = "row";
      tr.append(name,
        el("td", "num", itemQuantity(item)),
        el("td", "num", `${money(item.unitRateGrosze, r.currency)} / ${T(`unitPer.${item.unit}`)}`),
        el("td", "num", money(item.lineTotalGrosze, r.currency)));
      return tr;
    }));

    const rows = [];
    const row = (label, value, cls) => {
      const d = el("div", cls || "");
      d.append(el("dt", "", label), el("dd", "", value));
      rows.push(d);
    };
    row(T("admin.detail.subtotal"), money(r.subtotalGrosze, r.currency));
    if (r.minimumTopupGrosze) row(T("admin.detail.topup"), money(r.minimumTopupGrosze, r.currency));
    row(T("admin.detail.estimate"), money(r.totalGrosze, r.currency), r.agreedTotalGrosze ? "" : "estimate-total");
    if (r.agreedTotalGrosze) {
      row(T("admin.detail.agreedTotal"), money(r.agreedTotalGrosze, r.currency), "estimate-total");
    }
    const prices = $("d-prices");
    prices.replaceChildren(...rows);
    if (r.agreedTotalGrosze && r.agreedTotalReason) {
      const d = el("div", "agreed-reason");
      d.append(el("dt", "", ""), el("dd", "", T("admin.detail.agreedReasonShown", { reason: r.agreedTotalReason })));
      prices.append(d);
    }

    ui.updated.textContent = r.updatedAt
      ? T("admin.detail.lastChanged", { time: CSI18n.fmtDateTime(r.updatedAt) })
      : T("admin.detail.notChanged");
  }

  function historyValue(field, value) {
    if (value === null || value === undefined || value === "") return T("admin.history.empty_value");
    if (field === "status") return statusText(value);
    if (field === "agreed_total_grosze") return money(value);
    if (field === "agreed_date") return CSI18n.fmtDate(value, { day: "numeric", month: "short", year: "numeric" });
    if (field === "admin_note") return `“${value.length > 120 ? `${value.slice(0, 120)}…` : value}”`;
    return String(value);
  }

  function renderHistory() {
    ui.historyEmpty.hidden = historyRows.length > 0;
    ui.history.replaceChildren(...historyRows.map((h) => {
      const li = el("li", "history-item");
      const head = el("p", "history-head");
      head.append(el("strong", "", T(`admin.history.action.${h.action}`, { fallback: h.action })),
        el("span", "history-meta", ` · ${CSI18n.fmtDateTime(h.createdAt)} · ${T("admin.history.by")}`));
      li.append(head);
      const list = el("ul", "history-changes");
      for (const c of h.changes || []) {
        list.append(el("li", "", T("admin.history.change", {
          field: T(`admin.history.field.${c.field}`, { fallback: c.field }),
          from: historyValue(c.field, c.from), to: historyValue(c.field, c.to),
        })));
      }
      li.append(list);
      if (h.reason) li.append(el("p", "history-reason", T("admin.history.reason", { reason: h.reason })));
      return li;
    }));
  }

  function fill(r, { keepInputs = false } = {}) {
    request = r;
    renderReadOnly();
    if (!keepInputs) {
      ui.status.value = r.status;
      ui.note.value = r.adminNote;
      ui.agreedDate.value = r.agreedDate || "";
      ui.agreedTime.value = r.agreedTime || "";
      ui.agreedTotal.value = grosze(r.agreedTotalGrosze);
      ui.reason.value = "";
    }
    updateNoteCount();
    updateSaveButton();
  }

  async function load() {
    ui.loading.hidden = false;
    ui.error.hidden = true;
    let res;
    try {
      res = await api(`/admin/api/requests/${id}`);
    } catch {
      ui.loading.hidden = true;
      loadErrorKey = { key: "admin.detail.errorNetwork" };
      ui.errorText.textContent = T(loadErrorKey.key);
      ui.error.hidden = false;
      return false;
    }
    ui.loading.hidden = true;
    if (res.status === 401) { goToLogin(); return false; }
    if (res.status === 404) {
      loadErrorKey = { key: "admin.detail.notFound", params: { id } };
      ui.errorText.textContent = T(loadErrorKey.key, loadErrorKey.params);
      ui.retry.hidden = true;
      ui.error.hidden = false;
      return false;
    }
    if (!res.ok || !res.body || !res.body.request) {
      loadErrorKey = { key: "admin.detail.errorServer" };
      ui.errorText.textContent = T(loadErrorKey.key);
      ui.error.hidden = false;
      return false;
    }
    loadErrorKey = null;
    if (Number.isInteger(res.body.noteMax)) {
      noteMax = res.body.noteMax;
      ui.note.maxLength = noteMax;
    }
    historyRows = res.body.history || [];
    conflict = false;
    fill(res.body.request);
    renderHistory();
    renderNotification(res.body.notification);
    ui.detail.hidden = false;
    return true;
  }

  function setBusy(on) {
    saving = on;
    for (const input of [ui.status, ui.agreedDate]) input.disabled = on;
    for (const input of [ui.note, ui.agreedTime, ui.agreedTotal, ui.reason]) input.readOnly = on;
    if (on) ui.save.setAttribute("aria-busy", "true");
    else ui.save.removeAttribute("aria-busy");
    updateSaveButton();
  }

  ui.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || !request || conflict || !isDirty()) return;
    clearFieldErrors();
    ui.conflict.hidden = true;

    const v = formValues();
    const body = { version: request.version };
    if (v.status !== request.status) body.status = v.status;
    if (v.adminNote !== request.adminNote) body.adminNote = v.adminNote;
    if (v.agreedDate !== (request.agreedDate || null)) body.agreedDate = v.agreedDate;
    if (v.agreedTime !== (request.agreedTime || null)) body.agreedTime = v.agreedTime;
    if (!Object.is(v.agreedTotal, request.agreedTotalGrosze ?? null)) {
      // Send the text as typed; the server parses it exactly (no float money).
      body.agreedTotal = ui.agreedTotal.value.trim() || null;
      body.priceReason = ui.reason.value.trim();
    }

    setBusy(true);
    setMessage(ui.message, "pending", [{ key: "admin.detail.saving" }]);
    let res;
    try {
      res = await api(`/admin/api/requests/${id}`, { method: "PATCH", body: JSON.stringify(body) });
    } catch {
      setBusy(false);
      setMessage(ui.message, "error", [{ key: "admin.detail.notSaved" }, { key: "admin.detail.networkError" }]);
      ui.message.focus();
      return;
    }
    setBusy(false);

    if (res.ok && res.body && res.body.request && Number.isInteger(res.body.request.version)) {
      historyRows = res.body.history || historyRows;
      fill(res.body.request);
      renderHistory();
      setMessage(ui.message, "success", [{ key: res.body.unchanged ? "admin.detail.unchanged" : "admin.detail.savedOk" }]);
      ui.message.focus();
      return;
    }
    if (res.status === 409 && res.body && res.body.current) {
      const mine = { status: ui.status.value, note: ui.note.value, date: ui.agreedDate.value,
        time: ui.agreedTime.value, total: ui.agreedTotal.value, reason: ui.reason.value };
      fill(res.body.current, { keepInputs: true });
      Object.assign(ui.status, { value: mine.status });
      ui.note.value = mine.note;
      ui.agreedDate.value = mine.date;
      ui.agreedTime.value = mine.time;
      ui.agreedTotal.value = mine.total;
      ui.reason.value = mine.reason;
      conflict = true; // saving stays blocked until the latest version is loaded
      setMessage(ui.message, "error", [{ key: "admin.detail.conflict" },
        { key: "admin.detail.conflictState", params: {
          status: CSI18n.ref.t(`status.${res.body.current.status}`, {}, res.body.current.status) } },
        { key: "admin.detail.conflictHelp" }]);
      ui.conflict.hidden = false;
      updateSaveButton();
      ui.message.focus();
      return;
    }
    if (res.status === 422 && res.body && res.body.fields) {
      fieldErrorBody = res.body;
      renderFieldErrors();
      const msgs = [{ key: "admin.detail.notSaved" }];
      msgs.push(res.body.fields.version ? { serverError: { body: res.body, field: "version" } } : { key: "admin.detail.fixFields" });
      setMessage(ui.message, "error", msgs);
      const firstKey = Object.keys(errorFields).find((k) => res.body.fields[k]);
      (firstKey ? errorFields[firstKey][0] : ui.message).focus();
      return;
    }
    if (res.status === 401) {
      setMessage(ui.message, "error", [{ key: "admin.detail.sessionExpired" }]);
      const link = el("a", "", T("admin.detail.goLogin"));
      link.href = "/admin/login";
      const p = el("p");
      p.append(link);
      ui.message.append(p);
      ui.message.focus();
      return;
    }
    const key = res.body && res.body.error === "csrf_failed" ? "admin.detail.csrfExpired"
      : res.status === 404 ? "admin.detail.missingRequest" : "admin.detail.serverError";
    setMessage(ui.message, "error", [{ key: "admin.detail.notSaved" }, { key }]);
    ui.message.focus();
  });

  const onInput = () => {
    updateNoteCount();
    updateSaveButton();
  };
  [ui.status, ui.agreedDate].forEach((i) => i.addEventListener("change", onInput));
  [ui.note, ui.agreedTime, ui.agreedTotal, ui.reason].forEach((i) => i.addEventListener("input", onInput));

  ui.loadLatest.addEventListener("click", async () => {
    ui.conflict.hidden = true;
    ui.message.hidden = true;
    clearFieldErrors();
    if (await load()) {
      setMessage(ui.message, "info", [{ key: "admin.detail.loadedLatest" }]);
      ui.status.focus();
    }
  });
  ui.retry.addEventListener("click", load);

  window.addEventListener("beforeunload", (event) => {
    if (isDirty() && !saving) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  /* ---- Telegram notification (delivery status, separate from the cleaning status) ---- */
  const notif = {
    badge: $("notif-badge"), explain: $("notif-explain"), facts: $("notif-facts"), uncertain: $("notif-uncertain"),
    retryBox: $("notif-retry"), retryBtn: $("notif-retry-btn"), refreshBtn: $("notif-refresh"), message: $("notif-message"),
  };

  function renderNotification(n) {
    notification = n;
    const state = n && typeof n.state === "string" ? n.state : "unknown";
    notif.badge.textContent = T(`admin.notif.state.${state}`, { fallback: (n && n.label) || state });
    notif.badge.className = `notif-badge is-${state}`;
    notif.explain.textContent = T(`admin.notif.explain.${state}`, { fallback: "" });
    if (n && n.blockedUntil) {
      // Telegram's 429 pause is enforced by the server; this only explains it.
      notif.explain.textContent += ` ${T(state === "failed" ? "admin.notif.pauseFailed" : "admin.notif.pausePending")}`;
    }
    const rows = [];
    const add = (label, value, extraClass) => {
      if (value === null || value === undefined || value === "") return;
      const wrap = el("div", extraClass || "");
      wrap.append(el("dt", "", label), el("dd", "", String(value)));
      rows.push(wrap);
    };
    if (n && state !== "not_configured" && state !== "unknown") {
      let attempts = T("admin.notif.attemptsValue", { attempts: n.attempts, max: n.maxAttempts });
      if (n.manualRetries) attempts += ` ${T("admin.notif.manualRetries", { count: n.manualRetries })}`;
      add(T("admin.notif.attempts"), attempts);
      add(T("admin.notif.queued"), n.queuedAt ? `${CSI18n.fmtDateTime(n.queuedAt)} ${T("admin.detail.warsaw")}` : "");
      if (state === "pending" && n.attempts > 0 && n.nextAttemptAt) {
        add(T("admin.notif.next"), `${CSI18n.fmtDateTime(n.nextAttemptAt)} ${T("admin.detail.warsaw")}`);
      }
      if (n.blockedUntil) add(T("admin.notif.pauseUntil"), `${CSI18n.fmtDateTime(n.blockedUntil)} ${T("admin.detail.warsaw")}`);
      add(T("admin.notif.sentAt"), n.sentAt ? `${CSI18n.fmtDateTime(n.sentAt)} ${T("admin.detail.warsaw")}` : "");
      if (state !== "sent" && n.lastError) add(`${T("admin.notif.lastError")} ${T("admin.notif.lastErrorNote")}`, n.lastError);
    }
    notif.facts.replaceChildren(...rows);
    // Kept even after "Sent": an earlier, unconfirmed attempt may also have arrived.
    notif.uncertain.textContent = T(state === "sent" ? "admin.notif.uncertainSent" : "admin.notif.uncertain");
    notif.uncertain.hidden = !(n && n.deliveryUncertain);
    notif.retryBox.hidden = !(n && n.canRetry);
    notif.retryBtn.disabled = false;
  }

  async function refreshNotification() {
    notif.refreshBtn.disabled = true;
    try {
      const res = await api(`/admin/api/requests/${id}`);
      if (res.status === 401) return goToLogin();
      if (res.ok && res.body) {
        renderNotification(res.body.notification);
        notif.message.hidden = true;
      } else {
        setMessage(notif.message, "error", [{ key: "admin.notif.refreshError" }]);
      }
    } catch {
      setMessage(notif.message, "error", [{ key: "admin.notif.networkError" }]);
    } finally {
      notif.refreshBtn.disabled = false;
    }
  }
  notif.refreshBtn.addEventListener("click", refreshNotification);

  notif.retryBtn.addEventListener("click", async () => {
    notif.retryBtn.disabled = true;
    setMessage(notif.message, "pending", [{ key: "admin.notif.queuing" }]);
    let res;
    try {
      res = await api(`/admin/api/requests/${id}/notification/retry`, { method: "POST", body: "{}" });
    } catch {
      notif.retryBtn.disabled = false;
      setMessage(notif.message, "error", [{ key: "admin.notif.notQueued" }, { key: "admin.notif.networkError" }]);
      notif.message.focus();
      return;
    }
    if (res.status === 401) return goToLogin();
    if (res.ok && res.body && res.body.notification) {
      const n = res.body.notification;
      renderNotification(n);
      setMessage(notif.message, "success", [n.blockedUntil
        ? { key: "admin.notif.queuedAfterPause", params: { time: CSI18n.ref.dateTime(n.blockedUntil) } }
        : { key: "admin.notif.queuedAgain" }]);
    } else {
      if (res.body && res.body.notification) renderNotification(res.body.notification);
      notif.retryBtn.disabled = false;
      const code = res.body && res.body.error;
      const key = code === "csrf_failed" ? "admin.notif.csrf" : code === "not_failed" ? "admin.notif.onlyFailed"
        : code === "no_notification" ? "admin.notif.none" : "admin.detail.serverError";
      setMessage(notif.message, "error", [{ key: "admin.notif.notQueued" }, { key }]);
    }
    notif.message.focus();
  });

  /* ---- Language change: re-render text only (inputs and drafts are kept) ---- */
  document.addEventListener("cs:langchange", () => {
    if (loadErrorKey && !ui.error.hidden) ui.errorText.textContent = T(loadErrorKey.key, loadErrorKey.params);
    if (request) renderReadOnly();
    renderHistory();
    if (notification !== null) renderNotification(notification);
    updateNoteCount();
    updateSaveButton();
    renderFieldErrors();
    rerenderMessage(ui.message);
    rerenderMessage(notif.message);
  });

  load();
}
