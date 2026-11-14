/* ==========================================================================
   CleanSpace — public site: catalog, multi-service order, estimate, contact
   details, preview and saving to the LOCAL server (Flask + SQLite).

   - Services, rates, units and limits come from GET /api/pricing (server config).
   - Money is handled in whole grosze; quantities as scaled integers (no float money).
     Line total = rate × quantity rounded HALF UP to 1 grosz (same rule as the server).
   - Only "Save demo request" sends data. The server re-checks and re-prices everything.
   - One save attempt = one requestToken. It changes only when the order data changes
     or after "Start a new request" — never on preview again or on a language change.
   - Nothing about the order is stored in the browser (localStorage keeps only the
     language choice, see i18n/i18n.js).
   - All user text is inserted with textContent.
   ========================================================================== */

"use strict";

const API = { pricing: "/api/pricing", requests: "/api/requests" };
const REQUEST_TIMEOUT_MS = 10000;

/** Catalog loaded from the server; null until loaded. */
let CATALOG = null;

const T = (key, params) => CSI18n.t(key, params);
const money = (grosze) => CSI18n.fmtMoney(grosze, CATALOG ? CATALOG.currency : "PLN");

document.documentElement.classList.add("js");

document.addEventListener("DOMContentLoaded", () => {
  initMobileNav();
  initOrder();
});

/* --------------------------------------------------------------------------
   Helpers
   -------------------------------------------------------------------------- */
function warsawToday() {
  const tz = (CATALOG && CATALOG.timezone) || "Europe/Warsaw";
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(new Date());
  const get = (type) => parts.find((p) => p.type === type).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function newRequestToken() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
  const b = window.crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal, cache: "no-store" });
  } finally {
    clearTimeout(timer);
  }
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ---- Quantities as scaled integers ("6.5" with 1 decimal -> 65) ---- */
function toScaled(text, decimals) {
  const [whole, frac = ""] = String(text).split(".");
  return Number(whole) * 10 ** decimals + Number((frac + "000").slice(0, decimals) || 0);
}

function fromScaled(scaled, decimals) {
  if (!decimals) return String(scaled);
  const scale = 10 ** decimals;
  return `${Math.floor(scaled / scale)}.${String(scaled % scale).padStart(decimals, "0")}`;
}

/** Parse what the user typed. Accepts "6.5" and "6,5". Returns {ok, scaled} or {ok:false, code, params}. */
function parseQuantity(service, raw) {
  const text = String(raw || "").trim().replace(",", ".");
  if (text === "") return { ok: false, code: "required" };
  if (!/^\d{1,6}(\.\d{1,6})?$/.test(text)) return { ok: false, code: "quantity_number" };
  const frac = text.split(".")[1] || "";
  if (frac.replace(/0+$/, "").length > service.decimals) {
    return service.decimals === 0
      ? { ok: false, code: "quantity_whole" }
      : { ok: false, code: "quantity_decimals", params: { decimals: service.decimals } };
  }
  const scaled = toScaled(text, service.decimals);
  if (scaled < toScaled(service.min, service.decimals) || scaled > toScaled(service.max, service.decimals)) {
    return { ok: false, code: "quantity_range", params: { min: CSI18n.ref.qty(service.min), max: CSI18n.ref.qty(service.max) } };
  }
  return { ok: true, scaled };
}

/** Same rounding as the server: half up to a whole grosz, integers only. */
function lineTotal(rate, scaled, decimals) {
  const scale = 10 ** decimals;
  return Math.floor((rate * scaled * 2 + scale) / (2 * scale));
}

/* --------------------------------------------------------------------------
   Catalog
   -------------------------------------------------------------------------- */
function parseCatalog(body) {
  const isPosInt = (n) => Number.isInteger(n) && n > 0;
  if (!body || !Array.isArray(body.services) || !Array.isArray(body.categories) ||
      !isPosInt(body.minimumOrderGrosze) || !isPosInt(body.maxItems) || typeof body.currency !== "string") {
    throw new Error("Invalid catalog");
  }
  const services = {};
  for (const s of body.services) {
    if (!s || typeof s.id !== "string" || typeof s.category !== "string" || typeof s.unit !== "string" ||
        !Number.isInteger(s.decimals) || typeof s.min !== "string" || typeof s.max !== "string") {
      throw new Error("Invalid service");
    }
    if (s.option) {
      if (!Array.isArray(s.option.choices) || !s.option.choices.every((c) => isPosInt(c.rateGrosze))) throw new Error("Invalid option");
    } else if (!isPosInt(s.rateGrosze)) {
      throw new Error("Invalid rate");
    }
    services[s.id] = s;
  }
  return {
    currency: body.currency,
    minimumOrderGrosze: body.minimumOrderGrosze,
    maxItems: body.maxItems,
    categories: body.categories,
    services,
    order: body.services.map((s) => s.id),
    preferredTimes: body.preferredTimes || [],
    timezone: body.timezone || "Europe/Warsaw",
    fieldLimits: body.fieldLimits || {},
  };
}

function serviceRate(service, options) {
  if (service.option) {
    const choice = service.option.choices.find((c) => c.id === options[service.option.id]);
    return choice ? choice.rateGrosze : null;
  }
  return service.rateGrosze;
}

const serviceName = (id) => T(`service.${id}`, { fallback: id });
const unitText = (unit, count) => T(`unit.${unit}`, { count, fallback: unit });

function lineLabel(line) {
  return CSI18n.resolve(lineRef(line));
}

/* Stored messages keep codes + raw values (CSI18n.ref), never translated text, so a
   language switch re-renders every part of them. */
const R = () => CSI18n.ref;
const serviceRef = (id) => R().t(`service.${id}`, {}, id);
const unitRef = (unit, count) => R().t(`unit.${unit}`, { count }, unit);

/** Translatable reference to a line's label ("Single mattress — Both sides"), built from codes. */
function lineRef(line) {
  const service = CATALOG.services[line.service];
  if (!service || !service.option) return serviceRef(line.service);
  const choice = line.options[service.option.id];
  return R().join([serviceRef(line.service), R().t(`option.${service.option.id}.${choice}`, {}, choice)], " — ");
}

function lineKey(line) {
  const opts = Object.keys(line.options).sort().map((k) => `${k}=${line.options[k]}`).join("&");
  return `${line.service}?${opts}`;
}

function priceCart(cart) {
  const lines = cart.map((line) => {
    const service = CATALOG.services[line.service];
    const rate = serviceRate(service, line.options);
    return { ...line, rate, total: lineTotal(rate, line.scaled, service.decimals) };
  });
  const subtotal = lines.reduce((sum, l) => sum + l.total, 0);
  const topup = lines.length ? Math.max(0, CATALOG.minimumOrderGrosze - subtotal) : 0;
  return { lines, subtotal, topup, total: subtotal + topup };
}

/* --------------------------------------------------------------------------
   Mobile navigation
   -------------------------------------------------------------------------- */
function initMobileNav() {
  const toggle = document.querySelector(".nav-toggle");
  const nav = document.getElementById("site-nav");
  if (!toggle || !nav) return;
  const desktopQuery = window.matchMedia("(min-width: 960px)");
  const setOpen = (open) => {
    toggle.setAttribute("aria-expanded", String(open));
    nav.classList.toggle("is-open", open);
  };
  const label = () => toggle.setAttribute("aria-label", T("common.menu"));
  label();
  document.addEventListener("cs:langchange", label);
  toggle.addEventListener("click", () => setOpen(toggle.getAttribute("aria-expanded") !== "true"));
  nav.addEventListener("click", (e) => { if (e.target.closest("a")) setOpen(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      setOpen(false);
      toggle.focus();
    }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".site-header")) setOpen(false); });
  desktopQuery.addEventListener("change", (e) => { if (e.matches) setOpen(false); });
}

/* --------------------------------------------------------------------------
   Order
   -------------------------------------------------------------------------- */
const CATEGORY_ICONS = {
  rooms: "M4 11.5 12 5l8 6.5V20a1 1 0 0 1-1 1h-5v-6h-4v6H5a1 1 0 0 1-1-1z",
  carpets: "M4 6h16v12H4zM4 10h16M4 14h16M8 6v12M16 6v12",
  upholstery: "M5 11V8a3 3 0 0 1 3-3h8a3 3 0 0 1 3 3v3M3 13a2 2 0 0 1 4 0v2h10v-2a2 2 0 0 1 4 0v5H3zM6 18v2M18 18v2",
  mattresses: "M3 9h18v8H3zM3 13h18M6 17v2M18 17v2M6 9V7h5v2",
};

function initOrder() {
  const form = document.getElementById("booking-form");
  if (!form) return;
  const $ = (id) => document.getElementById(id);

  const ui = {
    cards: $("category-grid"),
    pricingStatus: $("pricing-status"),
    pricingRetry: $("pricing-retry"),
    builder: $("builder"),
    category: $("b-category"),
    service: $("b-service"),
    optionField: $("b-option-field"),
    option: $("b-option"),
    quantity: $("b-quantity"),
    quantityLabel: $("b-quantity-label"),
    quantityHint: $("b-quantity-hint"),
    unit: $("b-unit"),
    builderError: $("b-error"),
    add: $("b-add"),
    cancel: $("b-cancel"),
    cart: $("cart"),
    cartEmpty: $("cart-empty"),
    itemsError: $("items-error"),
    cartStatus: $("cart-status"),
    estimate: $("estimate"),
    estSubtotal: $("est-subtotal"),
    estTopupRow: $("est-topup-row"),
    estTopupLabel: $("est-topup-label"),
    estTopup: $("est-topup"),
    estTotal: $("est-total"),
    estNote: $("est-note"),
    details: $("details"),
    previewBtn: $("preview-btn"),
    preview: $("preview"),
    previewNotice: $("preview-notice"),
    saveBtn: $("save-btn"),
    newRequestBtn: $("new-request-btn"),
    saveStatus: $("save-status"),
  };

  const fields = {
    name: { input: $("name"), error: $("name-error") },
    email: { input: $("email"), error: $("email-error") },
    phone: { input: $("phone"), error: $("phone-error") },
    city: { input: $("city"), error: $("city-error") },
    street: { input: $("street"), error: $("street-error") },
    apartment: { input: $("apartment"), error: $("apartment-error") },
    date: { input: $("date"), error: $("date-error") },
    time: { input: [...form.querySelectorAll('input[name="time"]')], error: $("time-error") },
    comment: { input: $("comment"), error: $("comment-error") },
  };

  /* ---- State (translated text is never stored: only keys + params) ---- */
  let cart = [];              // [{service, scaled, options}]
  let editingIndex = null;
  let cartStatus = null;      // {key, params}
  let builderError = null;    // {code, params}
  let itemsErrors = [];       // [{code, params, line}]
  const fieldErrors = {};     // field -> {code, params, fallback}
  let pricingState = "loading";
  let submitAttempted = false;
  let saving = false;
  /** One save attempt = one token. Replaced only when the data changes or on "Start a new request". */
  let attempt = null;

  /* ---------------- Rendering helpers ---------------- */
  const errorText = (err) => {
    if (!err) return "";
    if (err.key) return T(err.key, err.params || {});
    return CSI18n.has(`errors.${err.code}`) ? T(`errors.${err.code}`, err.params || {}) : err.fallback || err.code;
  };

  function setInvalid(inputs, on) {
    [].concat(inputs).forEach((i) => (on ? i.setAttribute("aria-invalid", "true") : i.removeAttribute("aria-invalid")));
  }

  function renderFieldErrors() {
    for (const [key, f] of Object.entries(fields)) {
      f.error.textContent = errorText(fieldErrors[key]);
      setInvalid(f.input, Boolean(fieldErrors[key]));
    }
  }

  function setFieldError(key, err) {
    if (err) fieldErrors[key] = err;
    else delete fieldErrors[key];
    renderFieldErrors();
  }

  function renderStatusLine() {
    ui.cartStatus.textContent = cartStatus ? T(cartStatus.key, cartStatus.params) : "";
  }

  /* ---------------- Catalog-driven UI ---------------- */
  function renderCategoryCards() {
    if (!CATALOG) {
      ui.cards.replaceChildren();
      return;
    }
    ui.cards.replaceChildren(...CATALOG.categories.map((cat) => {
      const li = el("li", "category-card");
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", CATEGORY_ICONS[cat] || CATEGORY_ICONS.rooms);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "1.5");
      path.setAttribute("stroke-linejoin", "round");
      svg.append(path);
      const h = el("h3", "", T(`category.${cat}`));
      const p = el("p", "", T(`categoryDesc.${cat}`));
      const rates = el("ul", "service-rates");
      rates.setAttribute("role", "list");
      for (const id of CATALOG.order.filter((sid) => CATALOG.services[sid].category === cat)) {
        const s = CATALOG.services[id];
        const lowest = s.option ? Math.min(...s.option.choices.map((c) => c.rateGrosze)) : s.rateGrosze;
        const row = el("li");
        row.append(el("span", "", serviceName(id)),
          el("span", "rate", T(s.option ? "services.rateFrom" : "services.rate",
            { price: money(lowest), unit: T(`unitPer.${s.unit}`) })));
        rates.append(row);
      }
      const btn = el("a", "btn btn-secondary", T("services.add"));
      btn.href = "#order";
      btn.setAttribute("aria-label", T("services.addAria", { category: T(`category.${cat}`) }));
      btn.addEventListener("click", () => {
        ui.category.value = cat;
        fillServiceSelect();
        requestAnimationFrame(() => ui.service.focus({ preventScroll: true }));
      });
      li.append(svg, h, p, rates, btn);
      return li;
    }));
  }

  function fillCategorySelect() {
    const current = ui.category.value;
    ui.category.replaceChildren(...CATALOG.categories.map((cat) => {
      const o = el("option", "", T(`category.${cat}`));
      o.value = cat;
      return o;
    }));
    if (current && CATALOG.categories.includes(current)) ui.category.value = current;
  }

  function fillServiceSelect(keep) {
    const cat = ui.category.value;
    const ids = CATALOG.order.filter((id) => CATALOG.services[id].category === cat);
    const current = keep || ui.service.value;
    ui.service.replaceChildren(...ids.map((id) => {
      const o = el("option", "", serviceName(id));
      o.value = id;
      return o;
    }));
    if (ids.includes(current)) ui.service.value = current;
    renderServiceDetails();
  }

  function currentService() {
    return CATALOG ? CATALOG.services[ui.service.value] : null;
  }

  function selectedOptions(service) {
    if (!service || !service.option) return {};
    const checked = ui.option.querySelector("input:checked");
    return checked ? { [service.option.id]: checked.value } : {};
  }

  function renderServiceDetails(keepOption) {
    const service = currentService();
    if (!service) return;
    // Option radios (mattress sides)
    if (service.option) {
      const chosen = keepOption || (ui.option.querySelector("input:checked") || {}).value || service.option.choices[0].id;
      ui.option.replaceChildren(...service.option.choices.map((c) => {
        const label = el("label", "choice");
        const input = el("input");
        input.type = "radio";
        input.name = "b-option";
        input.value = c.id;
        input.checked = c.id === chosen;
        label.append(input, el("span", "", `${T(`option.${service.option.id}.${c.id}`)} · ${money(c.rateGrosze)}`));
        return label;
      }));
      ui.optionField.hidden = false;
    } else {
      ui.option.replaceChildren();
      ui.optionField.hidden = true;
    }
    ui.quantityLabel.textContent = T(`unitQuantity.${service.unit}`);
    const unitWord = unitText(service.unit, 5);
    ui.quantityHint.textContent = service.decimals
      ? T("order.quantityHintDecimal", { min: CSI18n.fmtQuantity(service.min), max: CSI18n.fmtQuantity(service.max), unit: unitWord, step: CSI18n.fmtQuantity(service.step) })
      : T("order.quantityHint", { min: service.min, max: service.max, unit: unitWord });
    ui.unit.textContent = unitWord;
    ui.quantity.inputMode = service.decimals ? "decimal" : "numeric";
  }

  function renderBuilderState() {
    ui.add.textContent = T(editingIndex === null ? "order.add" : "order.update");
    ui.cancel.hidden = editingIndex === null;
    ui.builderError.textContent = errorText(builderError);
    setInvalid(ui.quantity, Boolean(builderError));
  }

  /* ---------------- Cart + estimate ---------------- */
  function renderCart() {
    ui.cartEmpty.hidden = cart.length > 0;
    if (!CATALOG) {
      ui.cart.replaceChildren();
      return;
    }
    const priced = priceCart(cart);
    ui.cart.replaceChildren(...priced.lines.map((line, i) => {
      const service = CATALOG.services[line.service];
      const li = el("li", `cart-line${i === editingIndex ? " is-editing" : ""}`);
      const info = el("div");
      const qty = fromScaled(line.scaled, service.decimals);
      info.append(
        el("div", "line-name", lineLabel(line)),
        el("div", "line-meta", T("order.lineMeta", {
          quantity: CSI18n.fmtQuantity(qty), unit: unitText(service.unit, Number(qty)), rate: money(line.rate),
        })),
      );
      const total = el("div", "line-total", money(line.total));
      const actions = el("div", "line-actions");
      const edit = el("button", "btn btn-ghost", T("order.edit"));
      edit.type = "button";
      edit.setAttribute("aria-label", T("order.editAria", { service: lineLabel(line) }));
      edit.addEventListener("click", () => startEdit(i));
      const remove = el("button", "btn btn-ghost", T("order.remove"));
      remove.type = "button";
      remove.setAttribute("aria-label", T("order.removeAria", { service: lineLabel(line) }));
      remove.addEventListener("click", () => removeLine(i));
      actions.append(edit, remove);
      li.append(info, total, actions);
      return li;
    }));
    ui.itemsError.textContent = itemsErrors.map((e) =>
      (e.line !== undefined ? `${T("order.lineNumber", { n: e.line + 1 })}: ` : "") + errorText(e)).join(" ");
  }

  function renderEstimate() {
    if (!CATALOG) {
      ui.estimate.classList.add("is-invalid");
      ui.estSubtotal.textContent = "—";
      ui.estTopupRow.hidden = true;
      ui.estTotal.textContent = pricingState === "error" ? T("order.pricesUnavailable") : T("order.pricesLoading");
      ui.estNote.textContent = pricingState === "error" ? T("order.calcDisabled") : "";
      return;
    }
    ui.estimate.classList.remove("is-invalid");
    const p = priceCart(cart);
    ui.estSubtotal.textContent = cart.length ? money(p.subtotal) : "—";
    ui.estTopupRow.hidden = !p.topup;
    ui.estTopupLabel.textContent = T("order.topup", { min: money(CATALOG.minimumOrderGrosze) });
    ui.estTopup.textContent = money(p.topup);
    ui.estTotal.textContent = cart.length ? money(p.total) : "—";
    ui.estNote.textContent = `${T("order.minimumNote", { min: money(CATALOG.minimumOrderGrosze) })} ${T("order.roundingNote")}`;
  }

  function orderChanged() {
    renderCart();
    renderEstimate();
    renderBuilderState();
    renderStatusLine();
    hidePreview(); // the old preview is out of date
  }

  function resetBuilder() {
    editingIndex = null;
    builderError = null;
    ui.quantity.value = "";
    ui.category.disabled = false;
    ui.service.disabled = false;
  }

  function addOrUpdate() {
    if (!CATALOG) return;
    const service = currentService();
    const options = selectedOptions(service);
    const parsed = parseQuantity(service, ui.quantity.value);
    if (!parsed.ok) {
      builderError = { code: parsed.code, params: parsed.params };
      renderBuilderState();
      ui.quantity.focus();
      return;
    }
    const line = { service: service.id, scaled: parsed.scaled, options };
    const key = lineKey(line);
    const maxScaled = toScaled(service.max, service.decimals);
    const existing = cart.findIndex((l, i) => i !== editingIndex && lineKey(l) === key);

    if (existing >= 0) {
      // Same service with the same options: merge into that line instead of a duplicate.
      const sum = cart[existing].scaled + line.scaled;
      if (sum > maxScaled) {
        builderError = { code: "merge", key: "order.mergeTooBig", params: {
          total: R().qty(fromScaled(sum, service.decimals)), unit: unitRef(service.unit, 5),
          service: serviceRef(service.id), max: R().qty(service.max) } };
        renderBuilderState();
        ui.quantity.focus();
        return;
      }
      cart[existing].scaled = sum;
      if (editingIndex !== null) cart.splice(editingIndex, 1);
      const merged = cart.find((l) => lineKey(l) === key);
      cartStatus = { key: "order.merged", params: { service: lineRef(merged),
        quantity: R().qty(fromScaled(merged.scaled, service.decimals)), unit: unitRef(service.unit, 5) } };
    } else if (editingIndex !== null) {
      cart[editingIndex] = line;
      cartStatus = { key: "order.updated", params: { service: lineRef(line) } };
    } else {
      if (cart.length >= CATALOG.maxItems) {
        builderError = { code: "items_too_many", params: { max: CATALOG.maxItems } };
        renderBuilderState();
        return;
      }
      cart.push(line);
      cartStatus = { key: "order.added", params: { service: lineRef(line) } };
    }
    itemsErrors = [];
    resetBuilder();
    orderChanged();
    ui.quantity.focus();
  }

  function startEdit(index) {
    const line = cart[index];
    const service = CATALOG.services[line.service];
    editingIndex = index;
    builderError = null;
    ui.category.value = service.category;
    fillServiceSelect(line.service);
    renderServiceDetails(service.option ? line.options[service.option.id] : undefined);
    ui.quantity.value = CSI18n.fmtQuantity(fromScaled(line.scaled, service.decimals));
    ui.category.disabled = true;   // the service of a line stays; quantity/options can change
    ui.service.disabled = true;
    cartStatus = { key: "order.editing", params: { service: lineRef(line) } };
    renderCart();
    renderBuilderState();
    renderStatusLine();
    ui.quantity.focus();
    ui.quantity.select();
  }

  function removeLine(index) {
    const label = lineRef(cart[index]);
    cart.splice(index, 1);
    if (editingIndex !== null) resetBuilder();
    itemsErrors = [];
    cartStatus = { key: "order.removed", params: { service: label } };
    orderChanged();
    (ui.cart.querySelector("button") || ui.quantity).focus();
  }

  ui.category.addEventListener("change", () => fillServiceSelect());
  ui.service.addEventListener("change", () => {
    builderError = null;
    renderServiceDetails();
    renderBuilderState();
  });
  ui.add.addEventListener("click", addOrUpdate);
  ui.quantity.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addOrUpdate();
    }
  });
  ui.cancel.addEventListener("click", () => {
    resetBuilder();
    cartStatus = null;
    renderCart();
    renderBuilderState();
    renderStatusLine();
    ui.quantity.focus();
  });

  /* ---------------- Catalog loading ---------------- */
  function setOrderingEnabled(on) {
    ui.builder.disabled = !on;
    ui.previewBtn.disabled = !on;
  }

  async function loadCatalog() {
    CATALOG = null;
    pricingState = "loading";
    setOrderingEnabled(false);
    hidePreview();
    ui.pricingStatus.hidden = true;
    ui.pricingRetry.disabled = true;
    renderEstimate();
    try {
      const response = await fetchWithTimeout(API.pricing, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      CATALOG = parseCatalog(await readJson(response));
    } catch {
      CATALOG = null;
      pricingState = "error";
      ui.pricingStatus.hidden = false;
      ui.pricingRetry.disabled = false;
      renderEstimate();
      renderCategoryCards();
      return;
    }
    pricingState = "ready";
    ui.pricingRetry.disabled = false;
    // Drop lines whose service no longer exists in the catalog.
    cart = cart.filter((l) => CATALOG.services[l.service]);
    const limits = CATALOG.fieldLimits;
    if (limits.commentMax) {
      fields.comment.input.maxLength = limits.commentMax;
      document.getElementById("comment-hint").dataset.i18nParams = JSON.stringify({ max: limits.commentMax });
      CSI18n.apply(document.getElementById("details"));
    }
    if (limits.nameMax) fields.name.input.maxLength = limits.nameMax;
    if (limits.emailMax) fields.email.input.maxLength = limits.emailMax;
    if (limits.phoneMax) fields.phone.input.maxLength = limits.phoneMax;
    if (limits.cityMax) fields.city.input.maxLength = limits.cityMax;
    if (limits.streetMax) fields.street.input.maxLength = limits.streetMax;
    if (limits.apartmentMax) fields.apartment.input.maxLength = limits.apartmentMax;
    fields.date.input.min = warsawToday();
    fillCategorySelect();
    fillServiceSelect();
    renderCategoryCards();
    setOrderingEnabled(true);
    renderCart();
    renderEstimate();
    renderBuilderState();
  }

  ui.pricingRetry.addEventListener("click", () => loadCatalog().then(() => { if (CATALOG) ui.category.focus(); }));

  /* ---------------- Contact validation (browser side; the server checks again) ---------------- */
  const L = () => (CATALOG && CATALOG.fieldLimits) || {};
  const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

  const validators = {
    name() {
      const v = fields.name.input.value.trim();
      const min = L().nameMin || 2;
      const max = L().nameMax || 80;
      if (!v) return { code: "required" };
      if (v.length < min || v.length > max) return { code: "name_length", params: { min, max } };
      return null;
    },
    email() {
      const v = fields.email.input.value.trim();
      if (!v) return { code: "required" };
      if (v.length > (L().emailMax || 120)) return { code: "email_too_long", params: { max: L().emailMax || 120 } };
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v)) return { code: "email_invalid" };
      return null;
    },
    phone() {
      const v = fields.phone.input.value.trim().replace(/\s+/g, " ");
      const digits = (v.match(/\d/g) || []).length;
      const min = L().phoneDigitsMin || 7;
      const max = L().phoneDigitsMax || 15;
      if (!v) return { code: "required" };
      if (v.length > (L().phoneMax || 25)) return { code: "phone_too_long", params: { max: L().phoneMax || 25 } };
      if (!/^\+?[0-9 ()-]+$/.test(v) || (v.match(/\+/g) || []).length > 1) return { code: "phone_invalid" };
      if (digits < min || digits > max) return { code: "phone_digits", params: { min, max } };
      return null;
    },
    city() {
      const v = fields.city.input.value.trim();
      const min = L().cityMin || 2;
      const max = L().cityMax || 80;
      if (!v) return { code: "required" };
      if (CONTROL.test(v)) return { code: "text_invalid_chars" };
      if (v.length < min || v.length > max) return { code: "length_range", params: { min, max } };
      return null;
    },
    street() {
      const v = fields.street.input.value.trim();
      const min = L().streetMin || 3;
      const max = L().streetMax || 120;
      if (!v) return { code: "required" };
      if (CONTROL.test(v)) return { code: "text_invalid_chars" };
      if (v.length < min || v.length > max) return { code: "length_range", params: { min, max } };
      return null;
    },
    apartment() {
      const v = fields.apartment.input.value.trim();
      const max = L().apartmentMax || 20;
      if (v.length > max) return { code: "too_long", params: { max } };
      return null;
    },
    date() {
      const input = fields.date.input;
      const v = input.value;
      if (!v) return { code: input.validity.badInput ? "date_incomplete" : "required" };
      if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) return { code: "date_invalid" };
      const today = warsawToday();
      input.min = today;
      if (v < today) return { code: "date_past" };
      return null;
    },
    time() {
      return fields.time.input.some((r) => r.checked) ? null : { code: "time_invalid" };
    },
    comment() {
      const v = fields.comment.input.value.trim();
      const max = L().commentMax || 500;
      if (v.length > max) return { code: "too_long", params: { max } };
      return null;
    },
  };

  function validateField(key) {
    const err = validators[key]();
    setFieldError(key, err);
    return !err;
  }

  /* ---------------- Server field errors ---------------- */
  function applyServerErrors(body) {
    const codes = body.fieldCodes || {};
    const messages = body.fields || {};
    let first = null;
    itemsErrors = [];
    for (const key of Object.keys(messages)) {
      const params = { ...((codes[key] || {}).params || {}) };
      // Numeric limits from the server ("1", "60.0") are formatted for the current language at render time.
      for (const name of ["min", "max"]) {
        if (typeof params[name] === "string" && /^\d+(\.\d+)?$/.test(params[name])) params[name] = CSI18n.ref.qty(params[name]);
      }
      const err = { code: (codes[key] || {}).code || "server", params, fallback: messages[key] };
      const m = /^items(?:\.(\d+))?/.exec(key);
      if (m) {
        itemsErrors.push({ ...err, line: m[1] !== undefined ? Number(m[1]) : undefined });
        first = first || ui.quantity;
      } else if (fields[key]) {
        fieldErrors[key] = err;
        first = first || [].concat(fields[key].input)[0];
      }
    }
    renderFieldErrors();
    renderCart();
    return first;
  }

  /* ---------------- Preview + save ---------------- */
  function hidePreview() {
    ui.preview.hidden = true;
  }

  function makeAttempt(payload) {
    return { token: newRequestToken(), payload, key: JSON.stringify(payload), state: "new", lines: [] };
  }

  function currentPayload() {
    const items = cart.map((line) => ({
      service: line.service,
      quantity: fromScaled(line.scaled, CATALOG.services[line.service].decimals),
      options: line.options,
    }));
    const value = (k) => fields[k].input.value.trim();
    const time = (fields.time.input.find((r) => r.checked) || {}).value || "";
    // Prices are NOT sent: the server calculates them itself.
    return {
      items,
      name: value("name"), email: value("email"), phone: value("phone").replace(/\s+/g, " "),
      city: value("city"), street: value("street"), apartment: value("apartment"),
      date: fields.date.input.value, time, comment: value("comment"),
    };
  }

  function setSaveStatus(kind, lines) {
    ui.saveStatus.className = `save-status is-${kind}`;
    ui.saveStatus.replaceChildren(...lines.map((l) => el("p", "", T(l.key, l.params))));
    ui.saveStatus.hidden = false;
  }

  function renderAttempt() {
    const a = attempt;
    ui.saveBtn.removeAttribute("aria-busy");
    ui.newRequestBtn.hidden = true;
    ui.saveStatus.hidden = true;
    ui.saveStatus.replaceChildren();
    ui.previewNotice.hidden = false;
    ui.saveBtn.hidden = false;
    ui.saveBtn.disabled = saving;
    ui.saveBtn.textContent = T(saving ? "save.saving" : "save.save");
    if (!a) return;
    if (saving) {
      ui.saveBtn.setAttribute("aria-busy", "true");
      setSaveStatus("pending", [{ key: "save.savingStatus" }]);
      return;
    }
    if (a.state === "saved") {
      // Confirmed by the server: this attempt cannot be saved again by accident.
      ui.saveBtn.hidden = true;
      ui.previewNotice.hidden = true;
      ui.newRequestBtn.hidden = false;
      setSaveStatus("success", a.lines);
    } else if (a.state === "uncertain") {
      ui.previewNotice.hidden = true;
      ui.saveBtn.textContent = T("save.retrySame");
      setSaveStatus("warning", a.lines);
    } else if (a.state === "failed") {
      ui.saveBtn.textContent = T("save.trySaving");
      setSaveStatus("error", a.lines);
    } else if (a.state === "rejected") {
      ui.saveBtn.disabled = true;
      setSaveStatus("error", a.lines);
    } else if (a.lines.length) {
      setSaveStatus("info", a.lines);
    }
  }

  function renderPreview() {
    if (!attempt || !CATALOG) return;
    const p = attempt.payload;
    const priced = priceCart(cart);
    const set = (id, text) => { document.getElementById(id).textContent = text; };
    document.getElementById("pv-items").replaceChildren(...priced.lines.map((line) => {
      const service = CATALOG.services[line.service];
      const qty = fromScaled(line.scaled, service.decimals);
      const li = el("li");
      li.append(el("span", "", `${lineLabel(line)} — ${CSI18n.fmtQuantity(qty)} ${unitText(service.unit, Number(qty))}`),
        el("span", "", money(line.total)));
      return li;
    }));
    const totals = document.getElementById("pv-totals");
    const row = (label, value, cls) => {
      const d = el("div", cls || "");
      d.append(el("dt", "", label), el("dd", "", value));
      return d;
    };
    const rows = [row(T("order.subtotal"), money(priced.subtotal))];
    if (priced.topup) rows.push(row(T("order.topup", { min: money(CATALOG.minimumOrderGrosze) }), money(priced.topup)));
    rows.push(row(T("order.total"), money(priced.total), "estimate-total"));
    totals.replaceChildren(...rows);
    set("pv-name", p.name);
    set("pv-email", p.email);
    set("pv-phone", p.phone);
    set("pv-address", [p.street, p.apartment, p.city].filter(Boolean).join(", "));
    set("pv-when", T("preview.whenValue", { date: CSI18n.fmtDate(p.date), time: T(`times.${p.time}`) }));
    set("pv-comment", p.comment || T("common.none"));
  }

  function showPreview() {
    const payload = currentPayload();
    // Same data as the current attempt -> keep it (same token and state).
    if (!attempt || attempt.key !== JSON.stringify(payload)) attempt = makeAttempt(payload);
    renderPreview();
    renderAttempt();
    ui.preview.hidden = false;
    ui.preview.focus();
  }

  ui.newRequestBtn.addEventListener("click", () => {
    if (saving || !attempt) return;
    attempt = makeAttempt(attempt.payload);
    attempt.lines = [{ key: "save.newStarted" }];
    renderAttempt();
    ui.saveBtn.focus();
  });

  function setSaving(on) {
    saving = on;
    ui.builder.disabled = on || !CATALOG;
    ui.details.disabled = on;
    ui.cart.querySelectorAll("button").forEach((b) => { b.disabled = on; });
    renderAttempt();
  }

  async function saveRequest() {
    if (saving || !attempt || attempt.state === "saved" || attempt.state === "rejected" || !CATALOG) return;
    const current = attempt;
    setSaving(true);

    let response = null;
    let body = null;
    let lostAnswer = false;
    try {
      response = await fetchWithTimeout(API.requests, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ requestToken: current.token, ...current.payload }),
      });
      body = await readJson(response);
    } catch {
      lostAnswer = true; // the request MAY have reached the server and been saved
    }
    setSaving(false);
    const status = response ? response.status : 0;

    if (!lostAnswer && (status === 201 || status === 200) && body && Number.isInteger(body.id)) {
      current.state = "saved";
      current.lines = [{ key: body.duplicate ? "save.savedDuplicate" : "save.saved", params: { id: body.id } }];
      if (Number.isInteger(body.totalGrosze)) {
        // Codes and raw values only: "New" / "Нова" and the amount are produced at render time.
        current.lines.push({ key: "save.savedPrice", params: {
          total: CSI18n.ref.money(body.totalGrosze, CATALOG ? CATALOG.currency : "PLN"),
          status: CSI18n.ref.t(`status.${body.status || "new"}`, {}, body.status || "new") } });
      }
    } else if (!lostAnswer && status === 422 && body && body.error === "outdated_form") {
      current.state = "rejected";
      current.lines = [{ key: "save.notSaved" }, { key: "save.outdated" }];
    } else if (!lostAnswer && status === 422 && body && body.fields) {
      const firstInvalid = applyServerErrors(body);
      const onlyToken = Object.keys(body.fields).every((k) => k === "requestToken");
      current.state = "rejected";
      current.lines = [{ key: "save.notSaved" }, { key: onlyToken ? "save.attemptInvalid" : "save.fixFields" }];
      if (onlyToken) attempt = null; // the next preview gets a fresh token
      renderAttempt();
      ui.saveBtn.disabled = true;
      setSaveStatus("error", current.lines);
      (firstInvalid || ui.saveStatus).focus();
      return;
    } else if (!lostAnswer && status === 409) {
      current.state = "rejected";
      current.lines = [{ key: "save.notSaved" }, { key: "save.conflict" }];
      attempt = null;
      ui.saveBtn.disabled = true;
      setSaveStatus("error", current.lines);
      ui.saveStatus.focus();
      return;
    } else if (lostAnswer || status >= 500 || status === 200 || status === 201) {
      // No answer / server error / unreadable answer: retrying with the SAME token is safe.
      current.state = "uncertain";
      current.lines = [{ key: "save.uncertainTitle" },
        { key: lostAnswer ? "save.uncertainLost" : "save.uncertainServer" }, { key: "save.uncertainHelp" }];
    } else {
      current.state = "failed";
      current.lines = [{ key: "save.notSaved" }, { key: "save.refused" }];
    }
    renderAttempt();
    ui.saveStatus.focus();
  }

  ui.saveBtn.addEventListener("click", saveRequest);

  /* ---------------- Form events ---------------- */
  form.addEventListener("input", (event) => {
    const t = event.target;
    if (t.closest("#builder")) return; // builder edits change the order only when a line is added
    const key = t.name === "time" ? "time" : t.id;
    if (fields[key]) {
      if (submitAttempted) validateField(key);
      else setFieldError(key, null);
    }
    hidePreview(); // any change makes the preview outdated
  });

  form.addEventListener("change", (event) => {
    if (event.target.name === "time") {
      if (submitAttempted) validateField("time");
      else setFieldError("time", null);
      hidePreview();
    }
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault(); // never submit the form in the classic way
    if (saving) return;
    if (!CATALOG) {
      ui.pricingStatus.hidden = false;
      ui.pricingRetry.focus();
      return;
    }
    submitAttempted = true;
    const ok = Object.keys(fields).map(validateField).every(Boolean);
    itemsErrors = cart.length ? [] : [{ code: "items_required" }];
    renderCart();
    if (!cart.length || !ok) {
      hidePreview();
      const firstInvalid = !cart.length ? ui.quantity : form.querySelector('#details [aria-invalid="true"]');
      if (firstInvalid) firstInvalid.focus();
      return;
    }
    showPreview();
  });

  /* ---------------- Language change: re-render text only ---------------- */
  document.addEventListener("cs:langchange", () => {
    if (CATALOG) {
      const cat = ui.category.value;
      const sid = ui.service.value;
      const opt = (ui.option.querySelector("input:checked") || {}).value;
      fillCategorySelect();
      ui.category.value = cat;
      fillServiceSelect(sid);
      renderServiceDetails(opt);
    }
    renderCategoryCards();
    renderCart();
    renderEstimate();
    renderBuilderState();
    renderStatusLine();
    renderFieldErrors();
    if (!ui.preview.hidden) renderPreview();
    if (attempt) renderAttempt();
    // Note: the form values, the cart and the save attempt (token) are NOT touched.
  });

  renderEstimate();
  loadCatalog();
}
