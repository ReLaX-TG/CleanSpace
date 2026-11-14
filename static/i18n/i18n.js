/* ==========================================================================
   CleanSpace i18n — tiny dictionary-based translation layer (no external services).

   - Default language: English. The browser language or location is NEVER used.
   - An explicit choice is saved in localStorage ("cleanspace.lang"); if storage is
     unavailable the choice still works for the current page session (in memory).
   - The public site and the CRM share the same origin, so they share the setting.
   - Dictionaries live in static/i18n/<lang>.js and call CSI18n.register(...).
     To add a language: create <lang>.js, add it to LANGS below and load the file.
   ========================================================================== */

"use strict";

(function () {
  const STORAGE_KEY = "cleanspace.lang";
  const LANGS = [
    { code: "en", label: "English", short: "EN", htmlLang: "en", locale: "en-GB" },
    { code: "uk", label: "Українська", short: "UA", htmlLang: "uk", locale: "uk-UA" },
  ];
  const DEFAULT = "en";
  const dictionaries = {};
  let memoryChoice = null; // used when localStorage is unavailable

  function readStored() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  }

  function writeStored(code) {
    try {
      window.localStorage.setItem(STORAGE_KEY, code);
    } catch {
      /* storage blocked: keep the in-memory choice only */
    }
  }

  const isKnown = (code) => LANGS.some((l) => l.code === code);

  let current = (() => {
    const stored = readStored();
    return isKnown(stored) ? stored : DEFAULT; // invalid or missing -> English
  })();

  function info(code = current) {
    return LANGS.find((l) => l.code === code) || LANGS[0];
  }

  function lookup(dict, key) {
    return key.split(".").reduce((node, part) => (node && typeof node === "object" ? node[part] : undefined), dict);
  }

  /* Deferred parameter values. A message that is shown again later (after a language
     switch) must not keep text that was already translated or formatted, so it stores
     these small references (codes + raw values) instead; t() resolves them in the
     CURRENT language every time it renders. Plain strings/numbers are used as they are
     (e.g. text the user typed is never translated). */
  const ref = {
    t: (key, params = {}, fallback) => ({ $ref: "t", key, params, fallback }),
    money: (grosze, currency = "PLN") => ({ $ref: "money", grosze, currency }),
    qty: (text) => ({ $ref: "qty", text: String(text) }),
    date: (iso) => ({ $ref: "date", iso }),
    dateTime: (iso) => ({ $ref: "dateTime", iso }),
    /** Several parts joined by `sep`, each part resolved on its own. */
    join: (parts, sep = " ") => ({ $ref: "join", parts, sep }),
  };

  function resolve(value) {
    if (!value || typeof value !== "object" || typeof value.$ref !== "string") return value;
    switch (value.$ref) {
      case "t": return t(value.key, value.fallback !== undefined ? { ...value.params, fallback: value.fallback } : value.params);
      case "money": return fmtMoney(value.grosze, value.currency);
      case "qty": return fmtQuantity(value.text);
      case "date": return fmtDate(value.iso);
      case "dateTime": return fmtDateTime(value.iso);
      case "join": return (value.parts || []).map(resolve).join(value.sep);
      default: return "";
    }
  }

  /** Translate `key`; params fill {name} placeholders; `count` picks a plural form.
   *  Param values may be references from CSI18n.ref (resolved in the current language). */
  function t(key, params = {}) {
    params = params || {};
    let value = lookup(dictionaries[current], key);
    if (value === undefined) value = lookup(dictionaries[DEFAULT], key);
    if (value === undefined) return params.fallback !== undefined ? String(resolve(params.fallback)) : key;
    if (value && typeof value === "object") {
      const form = new Intl.PluralRules(info().locale).select(Number(resolve(params.count)) || 0);
      value = value[form] !== undefined ? value[form] : value.other;
    }
    return String(value).replace(/\{(\w+)\}/g, (m, name) => (params[name] !== undefined ? String(resolve(params[name])) : m));
  }

  function has(key) {
    return lookup(dictionaries[current], key) !== undefined || lookup(dictionaries[DEFAULT], key) !== undefined;
  }

  /** Apply translations to [data-i18n] (text) and [data-i18n-attr] ("attr:key;attr2:key2"). */
  function apply(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      let params = {};
      if (el.dataset.i18nParams) {
        try {
          params = JSON.parse(el.dataset.i18nParams);
        } catch {
          params = {};
        }
      }
      el.textContent = t(el.dataset.i18n, params);
    });
    root.querySelectorAll("[data-i18n-attr]").forEach((el) => {
      el.dataset.i18nAttr.split(";").forEach((pair) => {
        const [attr, key] = pair.split(":").map((s) => s.trim());
        if (attr && key) el.setAttribute(attr, t(key));
      });
    });
    document.documentElement.lang = info().htmlLang;
    document.querySelectorAll("[data-lang-option]").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.dataset.langOption === current));
    });
  }

  function setLang(code) {
    if (!isKnown(code)) return;
    current = code;
    memoryChoice = code;
    writeStored(code);
    apply();
    document.dispatchEvent(new CustomEvent("cs:langchange", { detail: { lang: code } }));
  }

  // ---- Formatting (currency stays PLN; dates are calendar dates or Warsaw time) ----
  function fmtMoney(grosze, currency = "PLN") {
    const whole = Math.trunc(grosze / 100);
    const rest = Math.abs(grosze % 100);
    const nf = new Intl.NumberFormat(info().locale, {
      minimumFractionDigits: rest ? 2 : 0,
      maximumFractionDigits: 2,
    });
    // Build the number from integers so no float rounding is involved.
    const value = rest ? Number(`${whole}.${String(rest).padStart(2, "0")}`) : whole;
    return `${nf.format(value)} ${currency}`;
  }

  /** "6.5" -> "6.5" (en) / "6,5" (uk). Input is a plain decimal string. */
  function fmtQuantity(text) {
    const n = Number(text);
    if (!Number.isFinite(n)) return String(text);
    const decimals = (String(text).split(".")[1] || "").length;
    return new Intl.NumberFormat(info().locale, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }).format(n);
  }

  function fmtDate(isoDate, opts = { weekday: "long", day: "numeric", month: "long", year: "numeric" }) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate || "");
    if (!m) return isoDate || "";
    return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).toLocaleDateString(info().locale, { timeZone: "UTC", ...opts });
  }

  function fmtDateTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(info().locale, {
      timeZone: "Europe/Warsaw",
      day: "numeric",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  window.CSI18n = {
    LANGS,
    register(code, dict) {
      dictionaries[code] = dict;
    },
    lang: () => current,
    info,
    t,
    ref,
    resolve,
    has,
    apply,
    setLang,
    fmtMoney,
    fmtQuantity,
    fmtDate,
    fmtDateTime,
    _memoryChoice: () => memoryChoice,
  };

  // Wire every language switcher on the page and translate once the DOM is ready.
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-lang-option]").forEach((btn) => {
      btn.addEventListener("click", () => setLang(btn.dataset.langOption));
    });
    apply();
  });
})();
