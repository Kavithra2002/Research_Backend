import fs from "node:fs";
import path from "node:path";
import { jsPDF } from "jspdf";
import autoTable from "jspdf-autotable";

import type { MarianDataset } from "./marianMarketService";

/* ────────────────────────────────────────────────────────────────────────── *
 * Daily Market Wrap — server-side PDF (identical to the in-app download).
 *
 * Page 1: a dense three-column dashboard containing EVERY component (market
 * statistics, foreign activity, index returns, top 10 turnover/volume, top 5
 * positive/negative contributors, crossings, dividends, the index & turnover
 * /volume charts and the foreign buying/selling charts).
 * Page 2: company description (intentionally blank for now).
 *
 * Returns a Buffer so it can be attached to scheduled emails and streamed to
 * the browser for download from a single code path.
 * ────────────────────────────────────────────────────────────────────────── */

const MARGIN = 18;
const LEFT_BAR_W = 12; // colored spine down the left edge of every page
const MUTED: [number, number, number] = [120, 126, 138];
const INK: [number, number, number] = [25, 28, 32];
const LIGHT: [number, number, number] = [238, 243, 250]; // soft blue zebra striping
const POS: [number, number, number] = [16, 150, 90];
const NEG: [number, number, number] = [214, 60, 60];
const BLUE: [number, number, number] = [37, 99, 235];
const NAVY: [number, number, number] = [31, 78, 140]; // Ambeon brand blue
const ORANGE: [number, number, number] = [226, 114, 40]; // Ambeon brand orange

/* ── logos (cached base64) ───────────────────────────────────────────────── */

/* Header logo. Only the Ambeon Securities mark is rendered, right-aligned at the
 * top of the report. It is fitted inside a bounding box that preserves its
 * native aspect ratio, so it is never stretched/squashed. */
const LOGO_MAX_H = 20;
const AMBEON_MAX_W = 130; // wide landscape mark (right)

function loadLogoDataUrl(file: string): string | null {
  try {
    const p = path.join(__dirname, `../assets/${file}`);
    return `data:image/png;base64,${fs.readFileSync(p).toString("base64")}`;
  } catch {
    return null;
  }
}

let ambeonLogoCache: string | null | undefined;
function loadAmbeonLogo(): string | null {
  if (ambeonLogoCache === undefined) ambeonLogoCache = loadLogoDataUrl("logo_Ambeon_sec_trim.png");
  return ambeonLogoCache;
}

/* ── formatting helpers ──────────────────────────────────────────────────── */

const f2 = (n: number) =>
  Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
const f0 = (n: number) => Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(n);
const numOrNull = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;
const cell2 = (v: unknown): string => {
  const n = numOrNull(v);
  return n === null ? "-" : f2(n);
};
const intStr = (v: unknown): string => {
  const n = numOrNull(v);
  return n === null ? "-" : f0(n);
};
const toMn0 = (v: unknown): string => {
  const n = numOrNull(v);
  return n === null ? "-" : f0(n / 1e6);
};
const toBn0 = (v: unknown): string => {
  const n = numOrNull(v);
  return n === null ? "-" : f0(n / 1e9);
};
const pctStr = (n: number | null): string => (n === null ? "-" : `${f2(n)}%`);
const chgPct = (t: unknown, p: unknown): number | null => {
  const tt = numOrNull(t);
  const pp = numOrNull(p);
  if (tt === null || pp === null || pp === 0) return null;
  return ((tt - pp) / Math.abs(pp)) * 100;
};

type Row = Record<string, unknown>;
const asArray = (v: unknown): Row[] => (Array.isArray(v) ? (v as Row[]) : []);
const asObj = (v: unknown): Row => (v && typeof v === "object" ? (v as Row) : {});
const shortSymbol = (s: unknown): string => String(s ?? "").replace(/\.[NX]\d+$/i, "");

interface Col {
  x: number;
  w: number;
  y: number;
}

/* ── public API ──────────────────────────────────────────────────────────── */

export function buildDailyMarketWrapPdfBuffer(
  dataset: MarianDataset,
  opts: { brand?: string; generatedAt?: Date } = {},
): Buffer {
  const doc = new jsPDF({ unit: "pt", format: "a4" });
  const pageW = doc.internal.pageSize.getWidth();
  const pageH = doc.internal.pageSize.getHeight();
  const CONTENT_X = LEFT_BAR_W + 14; // left content edge (clears the spine)
  const RIGHT_X = pageW - MARGIN; // right content edge
  const CONTENT_W = RIGHT_X - CONTENT_X;
  const brand = opts.brand ?? "Ambeon Securities";
  const generatedAt = opts.generatedAt ?? new Date();
  const dateCompact = generatedAt
    .toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric" })
    .replace(/\//g, ".");

  /* Draw the Ambeon logo, right-aligned at `topY`, scaled to fit its box
   * without distortion. */
  const drawHeaderLogos = (topY: number) => {
    const rightX = pageW - MARGIN;
    const place = (dataUrl: string | null, maxW: number) => {
      if (!dataUrl) return;
      try {
        const props = doc.getImageProperties(dataUrl);
        const scale = Math.min(maxW / props.width, LOGO_MAX_H / props.height);
        const w = props.width * scale;
        const h = props.height * scale;
        const x = rightX - w;
        const y = topY + (LOGO_MAX_H - h) / 2; // vertically centre within the band
        doc.addImage(dataUrl, "PNG", x, y, w, h, undefined, "FAST");
      } catch {
        /* ignore a bad logo and continue */
      }
    };
    place(loadAmbeonLogo(), AMBEON_MAX_W);
  };

  const data = dataset.data ?? {};
  const has = (id: string) => data[id] !== undefined && data[id] !== null;
  const requested = new Set(dataset.sections ?? []);

  /* colored spine down the left edge (navy with an orange accent block) */
  const drawLeftBar = () => {
    doc.setFillColor(...NAVY);
    doc.rect(0, 0, LEFT_BAR_W, pageH, "F");
    doc.setFillColor(...ORANGE);
    doc.rect(0, pageH * 0.1, LEFT_BAR_W, pageH * 0.16, "F");
  };

  /* header — navy title, orange date, Ambeon logo, navy divider */
  const drawHeader = () => {
    const y = MARGIN;
    drawHeaderLogos(y);
    doc.setFont("helvetica", "bold");
    doc.setFontSize(16);
    doc.setTextColor(...NAVY);
    doc.text("DAILY MARKET WRAP", CONTENT_X, y + 13);
    doc.setFont("helvetica", "bold");
    doc.setFontSize(9);
    doc.setTextColor(...ORANGE);
    doc.text(dateCompact, CONTENT_X, y + 26);
    const dividerY = y + 33;
    doc.setDrawColor(...NAVY);
    doc.setLineWidth(1.3);
    doc.line(CONTENT_X, dividerY, RIGHT_X, dividerY);
    return dividerY + 10;
  };

  drawLeftBar();
  const headerBottom = drawHeader();

  /* panel primitives (single page — no auto page break) */
  const panelTitle = (col: Col, text: string, accent?: [number, number, number]) => {
    doc.setFont("helvetica", "bold");
    doc.setFontSize(8.5);
    doc.setTextColor(...(accent ?? NAVY));
    doc.text(text, col.x, col.y + 7);
    col.y += 12;
  };

  /* Section header band — a filled bar with white title, used for the left-hand
   * table sections (matches the reference design). */
  const sectionBand = (col: Col, text: string, fill: [number, number, number] = NAVY) => {
    const h = 14;
    doc.setFillColor(...fill);
    doc.rect(col.x, col.y, col.w, h, "F");
    doc.setFont("helvetica", "bold");
    doc.setFontSize(8);
    doc.setTextColor(255, 255, 255);
    doc.text(text, col.x + 4, col.y + h - 4.5);
    col.y += h;
  };

  const note = (col: Col, text: string) => {
    doc.setFont("helvetica", "italic");
    doc.setFontSize(5);
    doc.setTextColor(...MUTED);
    for (const line of doc.splitTextToSize(text, col.w)) {
      doc.text(line, col.x, col.y + 4.5);
      col.y += 5.6;
    }
    col.y += 2;
  };

  const miniTable = (
    col: Col,
    head: string[],
    body: string[][],
    opts2: {
      align?: ("left" | "right")[];
      headFill?: [number, number, number];
      minRowH?: number;
      gap?: number;
    } = {},
  ) => {
    const columnStyles: Record<number, { halign: "left" | "right" }> = {};
    (opts2.align ?? []).forEach((a, i) => {
      columnStyles[i] = { halign: a };
    });
    const pad = opts2.minRowH ? Math.max((opts2.minRowH - 8) / 2, 2) : 2.6;
    autoTable(doc, {
      head: head.some((h) => h !== "") ? [head] : undefined,
      body,
      startY: col.y,
      // Include a small bottom margin so autoTable doesn't break the last row
      // onto a new page (its default bottom margin is 40pt).
      margin: { left: col.x, right: pageW - (col.x + col.w), top: MARGIN, bottom: MARGIN },
      tableWidth: col.w,
      styles: {
        fontSize: 7,
        cellPadding: { top: pad, bottom: pad, left: 3, right: 3 },
        overflow: "ellipsize",
        lineColor: [216, 224, 234],
        lineWidth: 0.3,
        textColor: INK,
      },
      headStyles: {
        fillColor: opts2.headFill ?? NAVY,
        textColor: 255,
        fontStyle: "bold",
        fontSize: 7,
        cellPadding: { top: 2.6, bottom: 2.6, left: 3, right: 3 },
      },
      alternateRowStyles: { fillColor: LIGHT },
      columnStyles,
      theme: "grid",
    });
    const finalY = (doc as unknown as { lastAutoTable?: { finalY: number } }).lastAutoTable
      ?.finalY;
    col.y = (finalY ?? col.y) + (opts2.gap ?? 8);
  };

  const drawChart = (
    col: Col,
    o: {
      type: "line" | "combo";
      categories: string[];
      height?: number;
      bars?: { name: string; values: (number | null)[]; color: [number, number, number] };
      line?: { name: string; values: (number | null)[]; color: [number, number, number] };
    },
  ) => {
    const H = o.height ?? 50;
    const x0 = col.x;
    const top = col.y;
    const padL = 26;
    const padR = o.type === "combo" ? 26 : 6;
    const padT = 5;
    const padB = 12;
    const plotW = col.w - padL - padR;
    const plotH = H - padT - padB;

    const barVals = (o.bars?.values ?? []).filter((v): v is number => typeof v === "number");
    const lineVals = (o.line?.values ?? []).filter((v): v is number => typeof v === "number");
    const leftSource = o.bars ? barVals : lineVals;
    const lMax = Math.max(0, ...leftSource);
    const lMin = Math.min(0, ...leftSource);
    const lTop = lMax === 0 ? 1 : lMax * 1.12;
    const lBot = lMin < 0 ? lMin * 1.12 : 0;
    const lRange = lTop - lBot || 1;
    const yL = (v: number) => top + padT + plotH - ((v - lBot) / lRange) * plotH;
    const rMax = Math.max(0, ...lineVals);
    const rTop = rMax === 0 ? 1 : rMax * 1.12;
    const yR = (v: number) => top + padT + plotH - (v / rTop) * plotH;

    doc.setDrawColor(220, 224, 228);
    doc.setLineWidth(0.4);
    const zeroY = yL(0);
    doc.line(x0 + padL, top + padT, x0 + padL, top + padT + plotH);
    doc.line(x0 + padL, zeroY, x0 + col.w - padR, zeroY);

    doc.setFont("helvetica", "normal");
    doc.setFontSize(4.6);
    doc.setTextColor(...MUTED);
    for (let i = 0; i <= 2; i += 1) {
      const v = lBot + (lRange * i) / 2;
      const ty = yL(v);
      doc.setDrawColor(238, 240, 242);
      doc.line(x0 + padL, ty, x0 + col.w - padR, ty);
      doc.text(f0(v), x0 + padL - 2.5, ty + 1.5, { align: "right" });
    }

    const n = o.categories.length;
    const slot = plotW / Math.max(n, 1);
    o.categories.forEach((c, i) => {
      doc.setFontSize(4.6);
      doc.setTextColor(...MUTED);
      doc.text(c, x0 + padL + slot * (i + 0.5), top + padT + plotH + 7, { align: "center" });
    });

    if (o.bars) {
      const barW = slot * 0.5;
      o.categories.forEach((_, i) => {
        const v = o.bars!.values[i];
        if (typeof v !== "number") return;
        const c = o.bars!.color;
        const bx = x0 + padL + slot * (i + 0.5) - barW / 2;
        const by = yL(v);
        doc.setFillColor(c[0], c[1], c[2]);
        doc.rect(bx, Math.min(by, zeroY), barW, Math.max(Math.abs(by - zeroY), 0.6), "F");
      });
    }
    if (o.line) {
      const useRight = o.type === "combo";
      const c = o.line.color;
      doc.setDrawColor(c[0], c[1], c[2]);
      doc.setFillColor(c[0], c[1], c[2]);
      doc.setLineWidth(1);
      let prev: { x: number; y: number } | null = null;
      o.line.values.forEach((v, i) => {
        if (typeof v !== "number") {
          prev = null;
          return;
        }
        const px = x0 + padL + slot * (i + 0.5);
        const py = useRight ? yR(v) : yL(v);
        if (prev) doc.line(prev.x, prev.y, px, py);
        doc.circle(px, py, 1.3, "F");
        prev = { x: px, y: py };
      });
      if (useRight) {
        doc.setFontSize(4.6);
        doc.setTextColor(...MUTED);
        for (let i = 0; i <= 2; i += 1) {
          const v = (rTop * i) / 2;
          doc.text(f0(v), x0 + col.w - padR + 2.5, yR(v) + 1.5, { align: "left" });
        }
      }
    }

    col.y = top + H;
    const legend: { name: string; color: [number, number, number] }[] = [];
    if (o.bars) legend.push({ name: o.bars.name, color: o.bars.color });
    if (o.line) legend.push({ name: o.line.name, color: o.line.color });
    if (legend.length) {
      let lx = x0 + padL;
      doc.setFontSize(4.6);
      legend.forEach((it) => {
        doc.setFillColor(it.color[0], it.color[1], it.color[2]);
        doc.rect(lx, col.y - 3.5, 4, 4, "F");
        doc.setTextColor(...MUTED);
        doc.text(it.name, lx + 5.5, col.y, { align: "left" });
        lx += 7 + doc.getTextWidth(it.name) + 6;
      });
      col.y += 5;
    }
    col.y += 4;
  };

  const placeholderChart = (col: Col, title: string, msg: string, height = 30) => {
    panelTitle(col, title);
    const H = height;
    doc.setDrawColor(225, 228, 232);
    doc.setLineWidth(0.4);
    doc.setFillColor(250, 251, 251);
    doc.rect(col.x, col.y, col.w, H, "FD");
    doc.setFont("helvetica", "italic");
    doc.setFontSize(5);
    doc.setTextColor(...MUTED);
    const lines = doc.splitTextToSize(msg, col.w - 8);
    let ly = col.y + H / 2 - (lines.length - 1) * 3;
    for (const line of lines) {
      doc.text(line, col.x + col.w / 2, ly, { align: "center" });
      ly += 6;
    }
    col.y += H + 6;
  };

  /* ── two sections (left tables · right charts) + bottom band ───────────── */
  const GUTTER2 = 16;
  const leftW = Math.round(CONTENT_W * 0.56);
  const rightW = CONTENT_W - leftW - GUTTER2;
  const L: Col = { x: CONTENT_X, w: leftW, y: headerBottom };
  const R: Col = { x: CONTENT_X + leftW + GUTTER2, w: rightW, y: headerBottom };

  /* Reserve a full-width band at the bottom for Crossings + Dividends so the
   * two columns above stretch to (nearly) fill the page. Sized to keep the whole
   * band (band header + 5 rows) comfortably on page 1, clear of the footer. */
  const bottomBandTop = pageH - MARGIN - 138;

  const stats = asObj(data.market_statistics);
  const today = asObj(stats.today);
  const prev = asObj(stats.previous);

  /* ── LEFT SECTION — statistics & tables ─────────────────────────────────── */
  if (has("market_statistics")) {
    sectionBand(L, "Market statistics");
    miniTable(
      L,
      ["", "Today", "Prev. day", "Change (%)"],
      [
        ["ASPI", cell2(today.asi), cell2(prev.asi), pctStr(chgPct(today.asi, prev.asi))],
        ["S&P SL20", cell2(today.spsl20), cell2(prev.spsl20), pctStr(chgPct(today.spsl20, prev.spsl20))],
        ["Turnover (LKR Mn)", toMn0(today.marketTurnover), toMn0(prev.marketTurnover), pctStr(chgPct(today.marketTurnover, prev.marketTurnover))],
        ["Volume (Mn)", toMn0(today.shareVolume), toMn0(prev.shareVolume), pctStr(chgPct(today.shareVolume, prev.shareVolume))],
        ["MCAP (LKR Bn)", toBn0(today.marketCap), toBn0(prev.marketCap), pctStr(chgPct(today.marketCap, prev.marketCap))],
        ["Market PE", cell2(today.per), cell2(prev.per), pctStr(chgPct(today.per, prev.per))],
        ["Market PBV", cell2(today.pbv), cell2(prev.pbv), pctStr(chgPct(today.pbv, prev.pbv))],
      ],
      { align: ["left", "right", "right", "right"], minRowH: 17 },
    );
  }
  if (has("foreign_activity")) {
    const fa = asObj(data.foreign_activity);
    const ft = asObj(fa.today);
    const fp = asObj(fa.previous);
    sectionBand(L, "Foreign activity (LKR Mn)");
    miniTable(
      L,
      ["", "Today", "Prev. day", "Change (%)"],
      [
        ["Foreign buying", toMn0(ft.buying), toMn0(fp.buying), pctStr(chgPct(ft.buying, fp.buying))],
        ["Foreign selling", toMn0(ft.selling), toMn0(fp.selling), pctStr(chgPct(ft.selling, fp.selling))],
        ["Net foreign flow", toMn0(ft.net), toMn0(fp.net), pctStr(chgPct(ft.net, fp.net))],
      ],
      { align: ["left", "right", "right", "right"], minRowH: 16 },
    );
  }
  if (requested.has("returns")) {
    sectionBand(L, "Return");
    miniTable(
      L,
      ["", "YTD", "1-month", "1-year"],
      [
        ["ASPI", "-", "-", "-"],
        ["S&P SL20", "-", "-", "-"],
      ],
      { align: ["left", "right", "right", "right"], minRowH: 16, gap: 4 },
    );
    note(L, "Index YTD / 1-month / 1-year returns are not available from the live CSE feed.");
  }

  /* Top 10 turnover / volume side by side */
  if (has("top_turnover") || has("top_volume")) {
    const subGap = 10;
    const subW = (L.w - subGap) / 2;
    const tcol: Col = { x: L.x, w: subW, y: L.y };
    const vcol: Col = { x: L.x + subW + subGap, w: subW, y: L.y };
    sectionBand(tcol, "Top 10 turnover (LKR)");
    miniTable(
      tcol,
      ["#", "Symbol", "Turnover"],
      asArray(data.top_turnover)
        .slice(0, 10)
        .map((r, i) => [String(i + 1), shortSymbol(r.symbol), toMn0(r.turnover)]),
      { align: ["left", "left", "right"], minRowH: 15 },
    );
    sectionBand(vcol, "Top 10 volume");
    miniTable(
      vcol,
      ["#", "Symbol", "Volume"],
      asArray(data.top_volume)
        .slice(0, 10)
        .map((r, i) => [String(i + 1), shortSymbol(r.symbol), toMn0(r.shareVolume)]),
      { align: ["left", "left", "right"], minRowH: 15 },
    );
    L.y = Math.max(tcol.y, vcol.y);
  }

  /* Positive / negative contributors side by side */
  if (has("contributors")) {
    const c = asObj(data.contributors);
    const pos = asArray(c.positive).slice(0, 5);
    const neg = asArray(c.negative).slice(0, 5);
    const posCount = numOrNull(c.positiveCount);
    const negCount = numOrNull(c.negativeCount);
    const subGap = 10;
    const subW = (L.w - subGap) / 2;
    const pcol: Col = { x: L.x, w: subW, y: L.y };
    const ncol: Col = { x: L.x + subW + subGap, w: subW, y: L.y };
    sectionBand(pcol, `Positive contributors = ${posCount ?? "-"}`, POS);
    miniTable(
      pcol,
      ["#", "Symbol", "Index pts"],
      Array.from({ length: 5 }, (_, i) =>
        pos[i] ? [String(i + 1), shortSymbol(pos[i].symbol), cell2(pos[i].points)] : ["-", "-", "-"],
      ),
      { align: ["left", "left", "right"], headFill: POS, minRowH: 16 },
    );
    sectionBand(ncol, `Negative contributors = ${negCount ?? "-"}`, NEG);
    miniTable(
      ncol,
      ["#", "Symbol", "Index pts"],
      Array.from({ length: 5 }, (_, i) =>
        neg[i] ? [String(i + 1), shortSymbol(neg[i].symbol), cell2(neg[i].points)] : ["-", "-", "-"],
      ),
      { align: ["left", "left", "right"], headFill: NEG, minRowH: 16 },
    );
    L.y = Math.max(pcol.y, ncol.y);
    note(L, "Index-point contribution approximated from market cap x price change.");
  }

  /* ── RIGHT SECTION — charts ─────────────────────────────────────────────── */
  const indexLine = (id: string, title: string, color: [number, number, number]) => {
    const c = asObj(data[id]);
    const pts = asArray(c.points);
    if (pts.length === 0) return;
    panelTitle(R, title);
    drawChart(R, {
      type: "line",
      height: 88,
      categories: pts.map((p) => String(p.label ?? "")),
      line: { name: title, values: pts.map((p) => numOrNull(p.value)), color },
    });
    note(R, "Only today & previous day are available; longer history is not exposed by the feed.");
  };
  if (has("aspi_chart")) indexLine("aspi_chart", "ASPI Index Trend", NAVY);
  if (has("snp_chart")) indexLine("snp_chart", "S&P SL20 Index Trend", BLUE);
  if (has("turnover_volume_chart")) {
    const c = asObj(data.turnover_volume_chart);
    const pts = asArray(c.points);
    if (pts.length) {
      panelTitle(R, "Turnover & Volume");
      drawChart(R, {
        type: "combo",
        height: 100,
        categories: pts.map((p) => String(p.label ?? "")),
        bars: {
          name: "Turnover Mn (L)",
          values: pts.map((p) => (numOrNull(p.turnover) ?? 0) / 1e6),
          color: NAVY,
        },
        line: {
          name: "Volume Mn (R)",
          values: pts.map((p) => (numOrNull(p.volume) ?? 0) / 1e6),
          color: ORANGE,
        },
      });
    }
  }
  if (requested.has("foreign_buying_chart")) {
    placeholderChart(R, "Top 5 foreign buying (LKR Mn)", "Per-company foreign buying is not available from the live CSE feed.", 90);
  }
  if (requested.has("foreign_selling_chart")) {
    placeholderChart(R, "Top 5 foreign selling (LKR Mn)", "Per-company foreign selling is not available from the live CSE feed.", 90);
  }

  /* ── BOTTOM BAND — Crossings (left) + Dividends (right), full width ──────── */
  {
    const halfGap = 16;
    const halfW = (CONTENT_W - halfGap) / 2;
    const crossCol: Col = { x: CONTENT_X, w: halfW, y: bottomBandTop };
    const divCol: Col = { x: CONTENT_X + halfW + halfGap, w: halfW, y: bottomBandTop };

    const cr = asObj(data.crossings);
    const filled = asArray(cr.crossings).slice(0, 5);
    const crossRows: string[][] = Array.from({ length: 5 }, (_, i) => {
      const r = filled[i];
      return r
        ? [shortSymbol(r.symbol), cell2(r.price), intStr(r.crossingVolume), toMn0(r.approxValue)]
        : ["-", "-", "-", "-"];
    });
    sectionBand(crossCol, "Crossings");
    miniTable(crossCol, ["Symbol", "Price", "Qty", "Value (LKR Mn)"], crossRows, {
      align: ["left", "right", "right", "right"],
      minRowH: 15,
      gap: 0,
    });

    sectionBand(divCol, "Dividends");
    miniTable(
      divCol,
      ["Sym", "Amount", "Description", "XD date"],
      Array.from({ length: 5 }, () => ["-", "-", "-", "-"]),
      { align: ["left", "right", "left", "right"], minRowH: 15, gap: 0 },
    );
  }

  /* page-1 footer */
  doc.setFont("helvetica", "normal");
  doc.setFontSize(6.5);
  doc.setTextColor(...MUTED);
  doc.text(`${brand} · Daily Market Wrap`, CONTENT_X, pageH - 12);
  doc.text("1 of 2", RIGHT_X, pageH - 12, { align: "right" });

  /* ── page 2: company description (blank placeholder) ───────────────────── */
  doc.addPage();
  {
    drawLeftBar();
    const y = MARGIN;
    drawHeaderLogos(y);
    doc.setFont("helvetica", "bold");
    doc.setFontSize(16);
    doc.setTextColor(...NAVY);
    doc.text("DAILY MARKET WRAP", CONTENT_X, y + 13);
    doc.setFont("helvetica", "bold");
    doc.setFontSize(9);
    doc.setTextColor(...ORANGE);
    doc.text(dateCompact, CONTENT_X, y + 26);
    const dividerY = y + 33;
    doc.setDrawColor(...NAVY);
    doc.setLineWidth(1.3);
    doc.line(CONTENT_X, dividerY, RIGHT_X, dividerY);

    doc.setFont("helvetica", "italic");
    doc.setFontSize(9);
    doc.setTextColor(...MUTED);
    doc.text("Company description", pageW / 2, pageH / 2, { align: "center" });

    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.5);
    doc.setTextColor(...MUTED);
    doc.text(`${brand} · Daily Market Wrap`, CONTENT_X, pageH - 12);
    doc.text("2 of 2", RIGHT_X, pageH - 12, { align: "right" });
  }

  const arrayBuffer = doc.output("arraybuffer");
  return Buffer.from(arrayBuffer);
}
