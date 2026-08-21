// GET /api/calendar?days=7
// GET /api/calendar?from=2026-08-21&to=2026-08-28
//
// Cloudflare Pages Function — kr.investing.com US 경제지표 → KST JSON
// 실패 시 Forex Factory JSON, 그래도 없으면 주간 전형 스케줄.

const INVESTING =
  "https://kr.investing.com/economic-calendar/Service/getCalendarFilteredData";
const FF_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json";

const WEEKDAY_KO = ["일", "월", "화", "수", "목", "금", "토"];

const KO_NAMES = [
  [/nonfarm payrolls|nfp/i, "비농업 고용지수"],
  [/initial jobless claims|unemployment claims/i, "신규 실업수당 청구건수"],
  [/continuing (jobless )?claims/i, "연속 실업수당 청구건수"],
  [/unemployment rate/i, "실업률"],
  [/average hourly earnings/i, "평균 시간당 임금"],
  [/jolts/i, "JOLTS 구인건수"],
  [/adp/i, "ADP 고용변화"],
  [/core pce/i, "근원 PCE 물가지수"],
  [/\bpce\b/i, "PCE 물가지수"],
  [/core cpi/i, "근원 소비자물가지수"],
  [/\bcpi\b|consumer price/i, "소비자물가지수"],
  [/core ppi/i, "근원 생산자물가지수"],
  [/\bppi\b|producer price/i, "생산자물가지수"],
  [/\bgdp\b/i, "국내총생산"],
  [/core retail sales/i, "근원 소매판매"],
  [/retail sales/i, "소매판매"],
  [/durable goods/i, "내구재 주문"],
  [/ism manufacturing/i, "ISM 제조업 PMI"],
  [/ism services|ism non-manufacturing/i, "ISM 서비스업 PMI"],
  [/industrial production/i, "산업생산"],
  [/capacity utilization/i, "설비가동률"],
  [/building permits/i, "건축허가"],
  [/housing starts/i, "주택착공"],
  [/existing home sales/i, "기존주택판매"],
  [/new home sales/i, "신규주택판매"],
  [/pending home sales/i, "계류주택판매"],
  [/consumer confidence/i, "컨퍼런스보드 소비자신뢰지수"],
  [/michigan/i, "미시간대 소비자심리지수"],
  [/philadelphia fed|philly fed/i, "필라델피아 연준 제조업지수"],
  [/empire state/i, "뉴욕 연준 제조업지수"],
  [/chicago pmi/i, "시카고 PMI"],
  [/fomc|federal funds rate|interest rate decision/i, "FOMC 금리 결정"],
  [/fed chair|powell/i, "연준 의장 발언"],
  [/beige book/i, "베이지북"],
  [/crude oil inventories|eia crude/i, "원유재고"],
  [/natural gas storage/i, "천연가스 재고"],
  [/trade balance/i, "무역수지"],
];

const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "X-Requested-With": "XMLHttpRequest",
  Accept: "text/html,application/json,*/*;q=0.8",
  "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
};

export async function onRequestGet(context) {
  const url = new URL(context.request.url);
  const cache = caches.default;
  const cacheKey = new Request(url.toString(), { method: "GET" });
  const hit = await cache.match(cacheKey);
  if (hit) return withCors(hit);

  const fromParam = url.searchParams.get("from");
  const toParam = url.searchParams.get("to");
  const days = Number(url.searchParams.get("days") || 7);
  const from = fromParam || formatYmdKst();
  const to = toParam || addDaysYmd(from, Math.max(0, days - 1));

  let events = [];
  let source = "fallback-schedule";

  try {
    events = await fetchInvesting(from, to);
    if (events.length) source = "kr.investing.com";
  } catch (err) {
    console.log("investing failed", err && err.message);
  }

  if (!events.length) {
    try {
      const res = await fetch(FF_JSON, {
        headers: { ...BROWSER_HEADERS, Accept: "application/json" },
      });
      if (res.ok) {
        events = parseFfJson(await res.json(), from, to);
        if (events.length) source = "forexfactory";
      }
    } catch (err) {
      console.log("ff failed", err && err.message);
    }
  }

  if (!events.length) events = fallbackUsWeek(from, to);

  const payload = {
    from,
    to,
    count: events.length,
    fetched_at: new Date().toISOString(),
    cached: false,
    source,
    events,
  };

  const response = json(payload);
  response.headers.set("Cache-Control", "public, max-age=300");
  context.waitUntil(cache.put(cacheKey, response.clone()));
  return withCors(response);
}

export async function onRequestOptions() {
  return withCors(new Response(null, { status: 204 }));
}

async function fetchInvesting(from, to) {
  const body = new URLSearchParams();
  body.append("country[]", "5");
  body.append("dateFrom", from);
  body.append("dateTo", to);
  body.append("timeZone", "88");
  body.append("timeFilter", "timeOnly");
  body.append("currentTab", "custom");
  body.append("limit_from", "0");
  body.append("submitFilters", "1");
  body.append("importance[]", "1");
  body.append("importance[]", "2");
  body.append("importance[]", "3");

  const res = await fetch(INVESTING, {
    method: "POST",
    headers: {
      ...BROWSER_HEADERS,
      "Content-Type": "application/x-www-form-urlencoded",
      Referer: "https://kr.investing.com/economic-calendar/",
      Origin: "https://kr.investing.com",
    },
    body,
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  const payload = await res.json();
  if (!payload || typeof payload.data !== "string") throw new Error("no html");
  return parseInvestingHtml(payload.data).filter((e) => e.currency === "USD");
}

function parseInvestingHtml(html) {
  const events = [];
  const rowRegex = /<tr[^>]*class="[^"]*js-event-item[^"]*"[^>]*>([\s\S]*?)<\/tr>/gi;
  let match;
  while ((match = rowRegex.exec(html)) !== null) {
    const full = match[0];
    const inner = match[1];
    const datetimeAttr = attr(full, "data-event-datetime");
    const currencyRaw =
      grab(inner, /class="[^"]*flagCur[^"]*"[^>]*>[\s\S]*?<\/span>\s*([A-Z]{3})/i) ||
      (clean(grab(inner, /class="[^"]*flagCur[^"]*"[^>]*>([\s\S]*?)<\/td>/i)) || "")
        .replace(/[^A-Z]/g, "")
        .slice(-3);
    const rawName = clean(grab(inner, /class="[^"]*event[^"]*"[^>]*>([\s\S]*?)<\/td>/i));
    if (!rawName) continue;
    const timeCell = grab(inner, /class="[^"]*time[^"]*"[^>]*>([\s\S]*?)<\/td>/i);
    events.push({
      ...kstParts(datetimeAttr, timeCell),
      currency: (currencyRaw || "USD").replace(/[^A-Z]/g, "").slice(0, 3) || "USD",
      importance: countImportance(inner),
      event: translateEvent(rawName),
      actual: clean(grab(inner, /class="[^"]*act[^"]*"[^>]*>([\s\S]*?)<\/td>/i)),
      forecast: clean(grab(inner, /class="[^"]*fore[^"]*"[^>]*>([\s\S]*?)<\/td>/i)),
      previous: clean(grab(inner, /class="[^"]*prev[^"]*"[^>]*>([\s\S]*?)<\/td>/i)),
    });
  }
  events.sort((a, b) => (a.datetime_utc || "").localeCompare(b.datetime_utc || ""));
  return events;
}

function parseFfJson(raw, from, to) {
  if (!Array.isArray(raw)) return [];
  const events = [];
  for (const r of raw) {
    if (!r) continue;
    const country = String(r.country || r.currency || "");
    if (!/USD|United States|US/i.test(country)) continue;
    const title = String(r.title || r.event || "");
    if (!title) continue;
    const d = new Date(r.date || r.datetime || "");
    if (Number.isNaN(d.getTime())) continue;
    const parts = fromUtcDate(d);
    if (parts.date_kst && (parts.date_kst < from || parts.date_kst > to)) continue;
    const impact = String(r.impact || "").toLowerCase();
    events.push({
      ...parts,
      currency: "USD",
      importance: /high/.test(impact) ? 3 : /medium|mod/.test(impact) ? 2 : 1,
      event: translateEvent(title),
      actual: r.actual != null ? String(r.actual) : null,
      forecast: r.forecast != null ? String(r.forecast) : null,
      previous: r.previous != null ? String(r.previous) : null,
    });
  }
  return events;
}

function fallbackUsWeek(from, to) {
  const events = [];
  let cursor = from;
  while (cursor <= to) {
    const wd = new Date(cursor + "T12:00:00+09:00").getDay();
    const push = (hhmm, importance, event, forecast, previous) => {
      const [h, mi] = hhmm.split(":");
      events.push({
        ...kstParts(cursor + " " + h + ":" + mi + ":00"),
        currency: "USD",
        importance,
        event,
        actual: null,
        forecast: forecast || null,
        previous: previous || null,
      });
    };
    if (wd === 2) {
      push("21:30", 2, "건축허가");
      push("21:30", 2, "주택착공");
    }
    if (wd === 3) push("22:00", 2, "기존주택판매");
    if (wd === 4) {
      push("21:30", 3, "신규 실업수당 청구건수", "230K", "227K");
      push("23:00", 2, "경기선행지수");
    }
    if (wd === 5) {
      const dayNum = Number(cursor.slice(-2));
      if (dayNum <= 7) {
        push("21:30", 3, "비농업 고용지수");
        push("21:30", 3, "실업률");
      } else {
        push("21:30", 3, "개인소비지출(PCE)");
        push("22:45", 2, "시카고 PMI");
      }
    }
    cursor = addDaysYmd(cursor, 1);
  }
  return events;
}

function translateEvent(name) {
  const trimmed = String(name).trim();
  if (/[가-힣]/.test(trimmed)) return trimmed;
  for (const [re, ko] of KO_NAMES) {
    if (re.test(trimmed)) {
      const period = trimmed.match(/\(([^)]+)\)\s*$/);
      return period ? ko + " (" + period[1] + ")" : ko;
    }
  }
  return trimmed;
}

function kstParts(datetimeAttr, timeCell) {
  if (!datetimeAttr) {
    return { datetime_utc: null, time_kst: clean(timeCell), date_kst: null, weekday_kst: null };
  }
  const normalized = datetimeAttr.replace(/\//g, "-").trim();
  const m = normalized.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!m) return { datetime_utc: null, time_kst: clean(timeCell), date_kst: null, weekday_kst: null };
  const y = m[1], mo = m[2], d = m[3], h = m[4], mi = m[5], s = m[6] || "00";
  const kstAsUtc = Date.UTC(+y, +mo - 1, +d, +h, +mi, +s);
  const utc = new Date(kstAsUtc - 9 * 60 * 60 * 1000);
  return {
    datetime_utc: utc.toISOString(),
    time_kst: h + ":" + mi,
    date_kst: y + "-" + mo + "-" + d,
    weekday_kst: WEEKDAY_KO[new Date(y + "-" + mo + "-" + d + "T12:00:00+09:00").getDay()],
  };
}

function fromUtcDate(d) {
  const kst = new Date(d.getTime() + 9 * 60 * 60 * 1000);
  const y = kst.getUTCFullYear();
  const mo = String(kst.getUTCMonth() + 1).padStart(2, "0");
  const day = String(kst.getUTCDate()).padStart(2, "0");
  const h = String(kst.getUTCHours()).padStart(2, "0");
  const mi = String(kst.getUTCMinutes()).padStart(2, "0");
  return {
    datetime_utc: d.toISOString(),
    time_kst: h + ":" + mi,
    date_kst: y + "-" + mo + "-" + day,
    weekday_kst: WEEKDAY_KO[new Date(y + "-" + mo + "-" + day + "T12:00:00+09:00").getDay()],
  };
}

function formatYmdKst() {
  const kst = new Date(Date.now() + 9 * 60 * 60 * 1000);
  return (
    kst.getUTCFullYear() +
    "-" +
    String(kst.getUTCMonth() + 1).padStart(2, "0") +
    "-" +
    String(kst.getUTCDate()).padStart(2, "0")
  );
}

function addDaysYmd(ymd, days) {
  const p = ymd.split("-").map(Number);
  const dt = new Date(Date.UTC(p[0], p[1] - 1, p[2] + days));
  return (
    dt.getUTCFullYear() +
    "-" +
    String(dt.getUTCMonth() + 1).padStart(2, "0") +
    "-" +
    String(dt.getUTCDate()).padStart(2, "0")
  );
}

function clean(s) {
  if (!s) return null;
  const t = String(s)
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&/g, "&")
    .replace(/\s+/g, " ")
    .trim();
  return t || null;
}

function attr(html, name) {
  const m = html.match(new RegExp(name + '="([^"]*)"', "i"));
  return m ? m[1] : null;
}

function grab(html, re) {
  const m = html.match(re);
  return m ? m[1] : null;
}

function countImportance(inner) {
  const bull = (inner.match(/class="[^"]*(?:grayFullBullishIcon|bull)[^"]*"/gi) || []).length;
  if (bull >= 3) return 3;
  if (bull === 2) return 2;
  if (bull === 1) return 1;
  return 0;
}

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function withCors(response) {
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Access-Control-Allow-Methods", "GET, OPTIONS");
  headers.set("Access-Control-Allow-Headers", "Content-Type, Authorization");
  return new Response(response.body, { status: response.status, headers });
}
