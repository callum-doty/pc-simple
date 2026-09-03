/*
 * Client-side rendering for Metric envelopes.
 *
 * The server guarantees every metric carries the population it was computed
 * over (services/metrics/envelope.py). That guarantee only reaches the screen
 * if the renderer honours it, so these helpers are the client half of the same
 * rule: there is no function here that renders a value on its own.
 *
 * renderMetric() emits value, denominator label, and any caveat together, as
 * one block. Calling it is the only supported way to put a metric on the page.
 * A panel that wants just the number has to reach into the envelope itself,
 * which is visible in review in a way that a bare `{{ value }}` never was.
 */

(function (global) {
  "use strict";

  /* ---- formatting ------------------------------------------------------- */

  function fmtCount(n) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString();
  }

  /**
   * Durations, chosen by magnitude. Seconds for anything under a couple of
   * minutes, then minutes, then hours: a queue wait of "5040s" is technically
   * accurate and practically unreadable.
   */
  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const s = Number(seconds);
    if (s < 1) return s.toFixed(2) + "s";
    if (s < 120) return s.toFixed(1) + "s";
    if (s < 7200) return (s / 60).toFixed(1) + "m";
    return (s / 3600).toFixed(1) + "h";
  }

  function fmtPct(p) {
    if (p === null || p === undefined) return "—";
    return Number(p).toFixed(1) + "%";
  }

  /** Compact token counts — 48.2M reads better than 48,231,904 on a tile. */
  function fmtTokens(n) {
    if (n === null || n === undefined) return "—";
    const v = Number(n);
    if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(v);
  }

  const FORMATTERS = {
    count: fmtCount,
    duration: fmtDuration,
    pct: fmtPct,
    tokens: fmtTokens,
  };

  function escapeHtml(text) {
    if (text === null || text === undefined) return "";
    const d = document.createElement("div");
    d.textContent = String(text);
    return d.innerHTML;
  }

  /* ---- the envelope renderer -------------------------------------------- */

  /**
   * Render one Metric envelope as HTML.
   *
   * @param {object} metric  An envelope: {value, denominator, denominator_label,
   *                         pct, scope, as_of, caveat}.
   * @param {object} [opts]
   * @param {string} [opts.label]   Human name for the metric. Falls back to the
   *                                envelope key if the caller passes one.
   * @param {string} [opts.format]  One of count | duration | pct | tokens.
   * @param {boolean} [opts.showPct] Also show value as a share of denominator.
   *
   * A missing denominator_label throws rather than rendering. The server
   * refuses to construct such an envelope, so reaching this branch means a
   * payload was hand-assembled somewhere and bypassed the metrics layer —
   * which is exactly the thing worth failing loudly on.
   */
  function renderMetric(metric, opts) {
    opts = opts || {};
    if (!metric || typeof metric !== "object") {
      throw new Error("renderMetric: no metric envelope supplied");
    }
    if (!metric.denominator_label) {
      throw new Error(
        "renderMetric: envelope has no denominator_label — a value may not be " +
          "rendered without naming the population it came from"
      );
    }

    const format = FORMATTERS[opts.format] || fmtCount;
    const value = format(metric.value);
    const showPct = opts.showPct && metric.pct !== null && metric.pct !== undefined;

    const parts = [];
    parts.push('<div class="metric">');
    if (opts.label) {
      parts.push('<div class="metric-label">' + escapeHtml(opts.label) + "</div>");
    }
    parts.push('<div class="metric-value">' + escapeHtml(value) + "</div>");
    if (showPct) {
      parts.push(
        '<div class="metric-pct">' + escapeHtml(fmtPct(metric.pct)) + "</div>"
      );
    }
    // The denominator line is not optional and has no opts flag to suppress it.
    parts.push(
      '<div class="metric-denominator">' +
        (metric.denominator !== null && metric.denominator !== undefined
          ? escapeHtml(fmtCount(metric.denominator)) + " "
          : "") +
        escapeHtml(metric.denominator_label) +
        "</div>"
    );
    if (metric.caveat) {
      parts.push(
        '<div class="metric-caveat" title="' +
          escapeHtml(metric.caveat) +
          '">' +
          escapeHtml(metric.caveat) +
          "</div>"
      );
    }
    parts.push("</div>");
    return parts.join("");
  }

  /* ---- Zone 0 pairs ----------------------------------------------------- */

  /**
   * Render a paired gauge and its verdict.
   *
   * Zone 0 shows two numbers per tile because for these gauges the agreement
   * is the signal: Redis leases against status=PROCESSING, broker depth
   * against database backlog. The server computes the verdict (the thresholds
   * are unit-tested in Python); this only draws it.
   *
   * A null row value renders "—", never 0. An unreadable broker reporting as
   * an empty queue is the conflation this whole zone exists to prevent.
   */
  function renderPair(pair) {
    const rows = (pair.rows || [])
      .map(function (r) {
        const unit = r.unit === "s" ? fmtDuration(r.value) : fmtCount(r.value);
        return (
          '<div class="pair-row"><span>' +
          escapeHtml(r.label) +
          "</span><b>" +
          escapeHtml(unit) +
          "</b></div>"
        );
      })
      .join("");

    return (
      '<div class="pair pair-' +
      escapeHtml(pair.state) +
      '">' +
      '<div class="pair-label">' +
      escapeHtml(pair.label) +
      "</div>" +
      '<div class="pair-rows">' +
      rows +
      "</div>" +
      '<div class="verdict verdict-' +
      escapeHtml(pair.state) +
      '">' +
      escapeHtml(pair.detail) +
      "</div>" +
      "</div>"
    );
  }

  /* ---- staleness -------------------------------------------------------- */

  /**
   * Human age of an ISO timestamp, for showing a cached payload's real age
   * instead of implying every figure is live.
   */
  function renderAsOf(isoString, cached) {
    if (!isoString) return "";
    const age = (Date.now() - new Date(isoString).getTime()) / 1000;
    const when = age < 60 ? "just now" : fmtDuration(age) + " ago";
    return cached ? "cached · read " + when : "read " + when;
  }

  global.Metrics = {
    renderMetric: renderMetric,
    renderPair: renderPair,
    renderAsOf: renderAsOf,
    fmtCount: fmtCount,
    fmtDuration: fmtDuration,
    fmtPct: fmtPct,
    fmtTokens: fmtTokens,
    escapeHtml: escapeHtml,
  };
})(window);
