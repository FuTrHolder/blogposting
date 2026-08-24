// GET /api/calendar?days=7
// GET /api/calendar?from=2026-08-21&to=2026-08-28
//
// Cloudflare Pages Function
// Investing.com 한국어 미국(USD) 경제지표 → KST JSON
//
// 주요 변경사항
// 1. Investing.com importance 정확도 개선
// 2. USD 이벤트만 처리
// 3. 미국 증시에 의미 있는 주요 경제지표만 필터링
// 4. 동일 이벤트 중복 제거
// 5. 실제 / 예상 / 이전 값 유지
// 6. Investing.com → Forex Factory → fallback 순서 유지
// 7. 기존 API 응답 구조 최대한 유지

const INVESTING =
  "https://kr.investing.com/economic-calendar/Service/getCalendarFilteredData";

const FF_JSON =
  "https://nfs.faireconomy.media/ff_calendar_thisweek.json";

const WEEKDAY_KO = [
  "일",
  "월",
  "화",
  "수",
  "목",
  "금",
  "토",
];

/* =========================================================
 * 한국어 이벤트명 매핑
 * ======================================================= */

const KO_NAMES = [
  [/nonfarm payrolls|non-farm payrolls|\bnfp\b/i, "비농업 고용지수"],
  [/initial jobless claims|unemployment claims/i, "신규 실업수당 청구건수"],
  [/continuing (jobless )?claims/i, "연속 실업수당 청구건수"],
  [/4 week.*claims|four week.*claims/i, "4주 평균 실업수당청구건수"],
  [/unemployment rate/i, "실업률"],
  [/average hourly earnings/i, "평균 시간당 임금"],
  [/jolts/i, "JOLTS 구인건수"],
  [/adp.*employment|adp employment|adp weekly/i, "ADP 고용변화"],
  [/core pce/i, "근원 개인소비지출 물가지수"],
  [/\bpce\b|personal consumption expenditure/i, "개인소비지출 물가지수"],
  [/core cpi/i, "근원 소비자물가지수"],
  [/\bcpi\b|consumer price/i, "소비자물가지수"],
  [/core ppi/i, "근원 생산자물가지수"],
  [/\bppi\b|producer price/i, "생산자물가지수"],
  [/\bgdp\b/i, "국내총생산"],
  [/gdp price/i, "GDP 물가지수"],
  [/gdpnow/i, "애틀랜타 연방준비은행 GDPNow"],
  [/core retail sales/i, "근원 소매판매"],
  [/retail sales/i, "소매판매"],
  [/durable goods/i, "내구재 주문"],
  [/ism manufacturing/i, "ISM 제조업 PMI"],
  [/ism services|ism non-manufacturing/i, "ISM 서비스업 PMI"],
  [/industrial production/i, "산업생산"],
  [/capacity utilization/i, "설비가동률"],
  [/building permits|building approval/i, "건축허가"],
  [/housing starts/i, "주택착공"],
  [/existing home sales/i, "기존주택판매"],
  [/new home sales/i, "신규주택판매"],
  [/pending home sales/i, "계류주택판매"],
  [/consumer confidence/i, "CB 소비자신뢰지수"],
  [/michigan.*sentiment/i, "미시간대 소비자심리지수"],
  [/michigan.*consumer/i, "미시간대 소비자심리지수"],
  [/michigan.*inflation/i, "미시간대 인플레이션 기대치"],
  [/philadelphia fed|philly fed/i, "필라델피아 연준 제조업지수"],
  [/empire state/i, "뉴욕 연준 제조업지수"],
  [/chicago pmi|chicago purchasing/i, "시카고 PMI"],
  [/consumer price/i, "소비자물가지수"],
  [/fomc/i, "FOMC"],
  [/federal funds rate|interest rate decision/i, "FOMC 금리 결정"],
  [/fed chair|powell/i, "연준 의장 발언"],
  [/fed governor|fed official|fomc member/i, "연준 위원 발언"],
  [/beige book/i, "베이지북"],
  [/jackson hole/i, "잭슨홀 심포지엄"],
  [/crude oil inventories|eia crude|crude inventories/i, "원유재고"],
  [/natural gas storage/i, "천연가스 재고"],
  [/trade balance/i, "무역수지"],
  [/goods trade balance/i, "상품 무역수지"],
  [/retail inventories/i, "소매 재고"],
  [/wholesale inventories/i, "도매재고"],
  [/leading indicators|leading economic index/i, "경기선행지수"],
];

/* =========================================================
 * 주요 경제지표 판별
 *
 * true  → Dashboard 기본 노출
 * false → 원본에는 있지만 기본 화면에서 제외
 * ======================================================= */

const MAJOR_EVENT_PATTERNS = [
  /* 고용 */
  /nonfarm payrolls|non-farm payrolls|\bnfp\b/i,
  /unemployment rate/i,
  /average hourly earnings/i,
  /initial jobless claims/i,
  /continuing claims/i,
  /jolts/i,
  /adp.*employment|adp employment/i,

  /* 물가 */
  /\bcpi\b|consumer price/i,
  /core cpi/i,
  /\bppi\b|producer price/i,
  /core ppi/i,
  /\bpce\b|personal consumption expenditure/i,
  /core pce/i,

  /* 성장 */
  /\bgdp\b/i,
  /gdpnow/i,
  /gdp price/i,

  /* 소비 */
  /retail sales/i,
  /core retail sales/i,
  /consumer confidence/i,
  /michigan/i,

  /* 제조 / 경기 */
  /ism manufacturing/i,
  /ism services/i,
  /ism non-manufacturing/i,
  /industrial production/i,
  /capacity utilization/i,
  /durable goods/i,
  /philadelphia fed|philly fed/i,
  /empire state/i,
  /chicago pmi|chicago purchasing/i,
  /leading indicators|leading economic index/i,

  /* 주택 */
  /building permits|building approval/i,
  /housing starts/i,
  /existing home sales/i,
  /new home sales/i,
  /pending home sales/i,

  /* 연준 */
  /fomc/i,
  /federal funds rate/i,
  /interest rate decision/i,
  /fed chair/i,
  /powell/i,
  /fomc member/i,
  /fed governor/i,
  /beige book/i,
  /jackson hole/i,

  /* 소비 / 무역 */
  /trade balance/i,
  /goods trade balance/i,

  /* 에너지 */
  /crude oil inventories|eia crude|crude inventories/i,
  /natural gas storage/i,
];

/* =========================================================
 * 보조 이벤트
 *
 * 주요지표에는 포함하지만 중요도는 한 단계 낮게 처리
 * ======================================================= */

const SECONDARY_EVENT_PATTERNS = [
  /new home sales/i,
  /pending home sales/i,
  /existing home sales/i,
  /building permits/i,
  /housing starts/i,
  /consumer confidence/i,
  /michigan/i,
  /chicago pmi/i,
  /trade balance/i,
  /wholesale inventories/i,
  /retail inventories/i,
  /natural gas/i,
];

/* =========================================================
 * 중요 이벤트
 * ======================================================= */

const HIGH_IMPORTANCE_PATTERNS = [
  /nonfarm payrolls|non-farm payrolls|\bnfp\b/i,
  /unemployment rate/i,
  /cpi|consumer price/i,
  /ppi|producer price/i,
  /pce|personal consumption expenditure/i,
  /gdp/i,
  /fomc/i,
  /federal funds rate/i,
  /interest rate decision/i,
  /powell/i,
  /fed chair/i,
  /ism manufacturing/i,
  /ism services/i,
  /retail sales/i,
  /initial jobless claims/i,
  /durable goods/i,
  /jackson hole/i,
];

/* =========================================================
 * 브라우저 헤더
 * ======================================================= */

const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",

  "X-Requested-With": "XMLHttpRequest",

  Accept: "text/html,application/json,*/*;q=0.8",

  "Accept-Language":
    "ko-KR,ko;q=0.9,en-US;q=0.8",
};

/* =========================================================
 * GET /api/calendar
 * ======================================================= */

export async function onRequestGet(context) {
  const url = new URL(context.request.url);

  const cache = caches.default;

  const cacheKey = new Request(url.toString(), {
    method: "GET",
  });

  const hit = await cache.match(cacheKey);

  if (hit) {
    return withCors(hit);
  }

  const fromParam = url.searchParams.get("from");
  const toParam = url.searchParams.get("to");

  const requestedDays = Number(
    url.searchParams.get("days") || 7
  );

  const days =
    Number.isFinite(requestedDays) && requestedDays > 0
      ? Math.min(requestedDays, 31)
      : 7;

  const from =
    fromParam || formatYmdKst();

  const to =
    toParam ||
    addDaysYmd(
      from,
      Math.max(0, days - 1)
    );

  let events = [];

  let source = "fallback-schedule";

  /* =======================================================
   * 1. Investing.com
   * ===================================================== */

  try {
    events = await fetchInvesting(from, to);

    if (events.length) {
      source = "kr.investing.com";
    }
  } catch (err) {
    console.log(
      "investing failed",
      err && err.message
    );
  }

  /* =======================================================
   * 2. Forex Factory fallback
   * ===================================================== */

  if (!events.length) {
    try {
      const res = await fetch(
        FF_JSON,
        {
          headers: {
            ...BROWSER_HEADERS,
            Accept: "application/json",
          },
        }
      );

      if (res.ok) {
        events = parseFfJson(
          await res.json(),
          from,
          to
        );

        if (events.length) {
          source = "forexfactory";
        }
      }
    } catch (err) {
      console.log(
        "ff failed",
        err && err.message
      );
    }
  }

  /* =======================================================
   * 3. 자체 fallback
   * ===================================================== */

  if (!events.length) {
    events = fallbackUsWeek(
      from,
      to
    );
  }

  /* =======================================================
   * 최종 정제
   *
   * Investing / FF / fallback 모두 동일한 기준 적용
   * ===================================================== */

  events = normalizeAndFilterEvents(
    events,
    from,
    to
  );

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

  /*
   * 경제지표는 실시간성이 중요하므로
   * 기존 5분 캐시 유지.
   */
  response.headers.set(
    "Cache-Control",
    "public, max-age=300"
  );

  context.waitUntil(
    cache.put(
      cacheKey,
      response.clone()
    )
  );

  return withCors(response);
}

/* =========================================================
 * OPTIONS
 * ======================================================= */

export async function onRequestOptions() {
  return withCors(
    new Response(null, {
      status: 204,
    })
  );
}

/* =========================================================
 * Investing.com 요청
 * ======================================================= */

async function fetchInvesting(
  from,
  to
) {
  const body =
    new URLSearchParams();

  /*
   * Investing.com 미국 국가 코드
   */
  body.append(
    "country[]",
    "5"
  );

  body.append(
    "dateFrom",
    from
  );

  body.append(
    "dateTo",
    to
  );

  /*
   * 한국시간
   */
  body.append(
    "timeZone",
    "88"
  );

  body.append(
    "timeFilter",
    "timeOnly"
  );

  body.append(
    "currentTab",
    "custom"
  );

  body.append(
    "limit_from",
    "0"
  );

  body.append(
    "submitFilters",
    "1"
  );

  /*
   * Investing.com 원본에서는
   * 중요도 1~3 모두 요청.
   *
   * 이후 실제 HTML에서 importance를
   * 다시 판별한다.
   */
  body.append(
    "importance[]",
    "1"
  );

  body.append(
    "importance[]",
    "2"
  );

  body.append(
    "importance[]",
    "3"
  );

  const res =
    await fetch(
      INVESTING,
      {
        method: "POST",

        headers: {
          ...BROWSER_HEADERS,

          "Content-Type":
            "application/x-www-form-urlencoded",

          Referer:
            "https://kr.investing.com/economic-calendar/",

          Origin:
            "https://kr.investing.com",
        },

        body,
      }
    );

  if (!res.ok) {
    throw new Error(
      "HTTP " + res.status
    );
  }

  const payload =
    await res.json();

  if (
    !payload ||
    typeof payload.data !==
      "string"
  ) {
    throw new Error(
      "no html"
    );
  }

  return parseInvestingHtml(
    payload.data
  ).filter(
    (e) =>
      e.currency ===
      "USD"
  );
}

/* =========================================================
 * Investing.com HTML 파싱
 * ======================================================= */

function parseInvestingHtml(html) {
  const events = [];

  /*
   * Investing.com event row
   */
  const rowRegex =
    /<tr[^>]*class="[^"]*js-event-item[^"]*"[^>]*>([\s\S]*?)<\/tr>/gi;

  let match;

  while (
    (match =
      rowRegex.exec(html)) !== null
  ) {
    const full =
      match[0];

    const inner =
      match[1];

    const datetimeAttr =
      attr(
        full,
        "data-event-datetime"
      );

    const currencyRaw =
      grab(
        inner,
        /class="[^"]*flagCur[^"]*"[^>]*>[\s\S]*?<span[^>]*>\s*([A-Z]{3})\s*<\/span>/i
      ) ||
      grab(
        inner,
        /class="[^"]*flagCur[^"]*"[^>]*>([\s\S]*?)<\/td>/i
      ) ||
      "";

    const currency =
      currencyRaw
        .replace(/[^A-Z]/g, "")
        .slice(-3);

    const rawName =
      clean(
        grab(
          inner,
          /class="[^"]*event[^"]*"[^>]*>([\s\S]*?)<\/td>/i
        )
      );

    if (!rawName) {
      continue;
    }

    /*
     * 시간 셀
     */
    const timeCell =
      grab(
        inner,
        /class="[^"]*time[^"]*"[^>]*>([\s\S]*?)<\/td>/i
      );

    /*
     * 실제 / 예상 / 이전
     */
    const actual =
      clean(
        grab(
          inner,
          /class="[^"]*act[^"]*"[^>]*>([\s\S]*?)<\/td>/i
        )
      );

    const forecast =
      clean(
        grab(
          inner,
          /class="[^"]*fore[^"]*"[^>]*>([\s\S]*?)<\/td>/i
        )
      );

    const previous =
      clean(
        grab(
          inner,
          /class="[^"]*prev[^"]*"[^>]*>([\s\S]*?)<\/td>/i
        )
      );

    /*
     * 원본 이벤트명
     */
    const translated =
      translateEvent(
        rawName
      );

    /*
     * 중요도
     */
    const importance =
      parseImportance(
        full,
        inner,
        rawName
      );

    const parts =
      kstParts(
        datetimeAttr,
        timeCell
      );

    events.push({
      ...parts,

      currency:
        currency || "USD",

      importance,

      event:
        translated,

      actual,

      forecast,

      previous,
    });
  }

  events.sort(
    (a, b) =>
      (a.datetime_utc || "")
        .localeCompare(
          b.datetime_utc || ""
        )
  );

  return events;
}

/* =========================================================
 * 중요도 정확도 개선
 *
 * 우선순위:
 *
 * 1. data-importance
 * 2. aria-label/title
 * 3. importance 관련 class
 * 4. 중요도 셀 내부 아이콘
 * 5. 이벤트명 기반 fallback
 *
 * 기존 코드의 문제:
 *
 * inner 전체에서 bull 아이콘을 세었기 때문에
 * 다른 영역의 아이콘까지 포함될 수 있었다.
 * ======================================================= */

function parseImportance(
  full,
  inner,
  rawName
) {
  /*
   * ---------------------------------------------
   * 1. data-importance
   * ---------------------------------------------
   */

  const dataImportance =
    firstNumber(
      [
        attr(
          full,
          "data-importance"
        ),
        attr(
          full,
          "data-impact"
        ),
        attr(
          full,
          "data-event-importance"
        ),
      ]
    );

  if (
    dataImportance >= 1 &&
    dataImportance <= 3
  ) {
    return dataImportance;
  }

  /*
   * ---------------------------------------------
   * 2. aria-label / title
   * ---------------------------------------------
   */

  const labelText =
    extractImportanceText(
      full
    );

  const labelImportance =
    importanceFromText(
      labelText
    );

  if (labelImportance) {
    return labelImportance;
  }

  /*
   * ---------------------------------------------
   * 3. importance 관련 class
   * ---------------------------------------------
   */

  const importanceArea =
    findImportanceArea(
      inner
    );

  if (importanceArea) {
    const areaImportance =
      parseImportanceIcons(
        importanceArea
      );

    if (areaImportance) {
      return areaImportance;
    }
  }

  /*
   * ---------------------------------------------
   * 4. 별도 중요도 셀 탐색
   * ---------------------------------------------
   */

  const iconImportance =
    parseImportanceIcons(
      inner
    );

  if (iconImportance) {
    return iconImportance;
  }

  /*
   * ---------------------------------------------
   * 5. 이벤트명 기반 fallback
   *
   * Investing HTML 구조가 변경되어
   * importance 메타데이터가 사라져도
   * 주요 이벤트는 합리적인 중요도를 유지.
   * ---------------------------------------------
   */

  return inferImportanceFromEvent(
    rawName
  );
}

/* =========================================================
 * data-* 숫자 추출
 * ======================================================= */

function firstNumber(values) {
  for (const value of values) {
    if (
      value == null
    ) {
      continue;
    }

    const m =
      String(value).match(
        /[123]/
      );

    if (m) {
      return Number(
        m[0]
      );
    }
  }

  return 0;
}

/* =========================================================
 * aria-label / title 등에서 중요도 추출
 * ======================================================= */

function extractImportanceText(
  html
) {
  const matches = [];

  const patterns = [
    /aria-label="([^"]*)"/gi,
    /title="([^"]*)"/gi,
    /data-tooltip="([^"]*)"/gi,
    /data-original-title="([^"]*)"/gi,
  ];

  for (
    const re of patterns
  ) {
    let m;

    while (
      (m =
        re.exec(html)) !==
      null
    ) {
      matches.push(
        m[1]
      );
    }
  }

  return matches.join(
    " "
  );
}

/* =========================================================
 * 텍스트 기반 중요도
 * ======================================================= */

function importanceFromText(
  text
) {
  if (!text) {
    return 0;
  }

  const s =
    String(text)
      .toLowerCase();

  /*
   * 별 3개
   */
  if (
    /3\s*(star|stars|stars?)|three\s*star|high|높음|매우\s*중요|중요도\s*3/.test(
      s
    )
  ) {
    return 3;
  }

  /*
   * 별 2개
   */
  if (
    /2\s*(star|stars?)|two\s*star|medium|보통|중요도\s*2/.test(
      s
    )
  ) {
    return 2;
  }

  /*
   * 별 1개
   */
  if (
    /1\s*(star|stars?)|one\s*star|low|낮음|중요도\s*1/.test(
      s
    )
  ) {
    return 1;
  }

  return 0;
}

/* =========================================================
 * importance 영역 찾기
 * ======================================================= */

function findImportanceArea(
  inner
) {
  const patterns = [
    /<td[^>]*class="[^"]*(?:importance|impact|sentiment)[^"]*"[^>]*>[\s\S]*?<\/td>/i,

    /<span[^>]*class="[^"]*(?:importance|impact|sentiment)[^"]*"[^>]*>[\s\S]*?<\/span>/i,

    /<div[^>]*class="[^"]*(?:importance|impact|sentiment)[^"]*"[^>]*>[\s\S]*?<\/div>/i,
  ];

  for (
    const re of patterns
  ) {
    const m =
      inner.match(re);

    if (m) {
      return m[0];
    }
  }

  return null;
}

/* =========================================================
 * 중요도 아이콘 파싱
 *
 * 반드시 importance 영역 우선.
 * ======================================================= */

function parseImportanceIcons(
  html
) {
  if (!html) {
    return 0;
  }

  /*
   * 3단계 아이콘 패턴
   */
  const fullIconPatterns = [
    /grayFullBullishIcon/gi,
    /fullBullishIcon/gi,
    /bullishFull/gi,
    /bullFull/gi,
  ];

  let fullCount = 0;

  for (
    const re of fullIconPatterns
  ) {
    fullCount +=
      (
        html.match(re) ||
        []
      ).length;
  }

  if (
    fullCount >= 3
  ) {
    return 3;
  }

  if (
    fullCount === 2
  ) {
    return 2;
  }

  if (
    fullCount === 1
  ) {
    return 1;
  }

  /*
   * filled star / impact icon
   */
  const starMatches =
    html.match(
      /(?:star|bullish|impact)[^"]*(?:full|active|filled)|(?:full|active|filled)[^"]*(?:star|bullish|impact)/gi
    );

  if (
    starMatches &&
    starMatches.length > 0
  ) {
    return Math.min(
      3,
      starMatches.length
    );
  }

  return 0;
}

/* =========================================================
 * 이벤트명 기반 중요도 fallback
 * ======================================================= */

function inferImportanceFromEvent(
  name
) {
  const text =
    String(name || "");

  if (
    HIGH_IMPORTANCE_PATTERNS.some(
      (re) =>
        re.test(text)
    )
  ) {
    return 3;
  }

  if (
    SECONDARY_EVENT_PATTERNS.some(
      (re) =>
        re.test(text)
    )
  ) {
    return 2;
  }

  return 1;
}

/* =========================================================
 * 최종 이벤트 정제
 * ======================================================= */

function normalizeAndFilterEvents(
  events,
  from,
  to
) {
  if (
    !Array.isArray(events)
  ) {
    return [];
  }

  /*
   * 1. 날짜 범위
   * 2. USD
   * 3. 주요 이벤트
   * 4. 중요도 보정
   * 5. 중복 제거
   */

  const filtered = [];

  for (
    const event of events
  ) {
    if (!event) {
      continue;
    }

    /*
     * USD만
     */
    if (
      String(
        event.currency || ""
      ).toUpperCase() !==
      "USD"
    ) {
      continue;
    }

    /*
     * 날짜가 존재하면 범위 확인
     */
    if (
      event.date_kst &&
      (
        event.date_kst <
          from ||
        event.date_kst >
          to
      )
    ) {
      continue;
    }

    const eventName =
      String(
        event.event || ""
      ).trim();

    if (!eventName) {
      continue;
    }

    /*
     * 주요 미국 증시 이벤트인지 확인
     */
    if (
      !isMajorUsEvent(
        eventName
      )
    ) {
      continue;
    }

    /*
     * 중요도 보정
     */
    let importance =
      Number(
        event.importance
      ) || 0;

    if (
      importance < 1 ||
      importance > 3
    ) {
      importance = 1;
    }

    /*
     * 주요 이벤트인데 원본 importance가
     * 비정상적으로 0/1이면 이름 기반 보정.
     */
    const inferred =
      inferImportanceFromEvent(
        eventName
      );

    if (
      inferred > importance
    ) {
      importance =
        inferred;
    }

    /*
     * 이벤트명 정규화
     */
    const normalizedEvent =
      normalizeEventName(
        eventName
      );

    filtered.push({
      ...event,

      event:
        normalizedEvent,

      importance,
    });
  }

  /*
   * 중복 제거
   */
  const unique =
    dedupeEvents(
      filtered
    );

  /*
   * 날짜 + 시간 + 중요도 + 이벤트
   */
  unique.sort(
    (a, b) => {
      const da =
        a.datetime_utc ||
        `${a.date_kst || ""} ${a.time_kst || ""}`;

      const db =
        b.datetime_utc ||
        `${b.date_kst || ""} ${b.time_kst || ""}`;

      const dateCompare =
        da.localeCompare(
          db
        );

      if (
        dateCompare !== 0
      ) {
        return dateCompare;
      }

      if (
        b.importance !==
        a.importance
      ) {
        return (
          b.importance -
          a.importance
        );
      }

      return String(
        a.event || ""
      ).localeCompare(
        String(
          b.event || ""
        )
      );
    }
  );

  return unique;
}

/* =========================================================
 * 주요 미국 증시 이벤트 판별
 * ======================================================= */

function isMajorUsEvent(
  name
) {
  const text =
    String(name || "")
      .trim();

  if (!text) {
    return false;
  }

  /*
   * 이미 한국어로 변환된 이름
   */
  const koreanMajorPatterns = [
    /비농업 고용/,
    /실업률/,
    /평균 시간당 임금/,
    /신규 실업수당/,
    /연속 실업수당/,
    /JOLTS/,
    /ADP 고용/,
    /소비자물가지수/,
    /생산자물가지수/,
    /개인소비지출/,
    /PCE/,
    /국내총생산/,
    /GDP/,
    /소매판매/,
    /내구재 주문/,
    /ISM/,
    /산업생산/,
    /설비가동률/,
    /건축허가/,
    /주택착공/,
    /기존주택판매/,
    /신규주택판매/,
    /계류주택판매/,
    /소비자신뢰/,
    /미시간대/,
    /필라델피아 연준/,
    /뉴욕 연준/,
    /시카고 PMI/,
    /FOMC/,
    /연준 의장/,
    /연준 위원/,
    /베이지북/,
    /잭슨홀/,
    /원유재고/,
    /천연가스 재고/,
    /무역수지/,
    /상품 무역수지/,
    /경기선행/,
  ];

  if (
    koreanMajorPatterns.some(
      (re) =>
        re.test(text)
    )
  ) {
    return true;
  }

  /*
   * 영어 원본이 남은 경우
   */
  if (
    MAJOR_EVENT_PATTERNS.some(
      (re) =>
        re.test(text)
    )
  ) {
    return true;
  }

  return false;
}

/* =========================================================
 * 이벤트명 정규화
 * ======================================================= */

function normalizeEventName(
  name
) {
  let value =
    String(name || "")
      .replace(
        /\s+/g,
        " "
      )
      .trim();

  /*
   * 동일한 이벤트명 앞뒤 공백 제거
   */
  value =
    value.replace(
      /^\s+|\s+$/g,
      ""
    );

  return value;
}

/* =========================================================
 * 중복 이벤트 제거
 *
 * 동일 날짜 + 시간 + 이벤트를 기준으로 제거.
 *
 * 단,
 * - MoM
 * - YoY
 * - QoQ
 *
 * 등이 다른 이벤트는 이벤트명이 다르므로 유지.
 * ======================================================= */

function dedupeEvents(
  events
) {
  const map =
    new Map();

  for (
    const event of events
  ) {
    const date =
      event.date_kst ||
      "";

    const time =
      event.time_kst ||
      "";

    const name =
      normalizeEventName(
        event.event
      );

    /*
     * actual / forecast / previous가
     * 다르면 최신/완전한 데이터 우선.
     */
    const key =
      [
        date,
        time,
        name,
      ].join(
        "|"
      );

    const existing =
      map.get(key);

    if (!existing) {
      map.set(
        key,
        event
      );
      continue;
    }

    /*
     * 데이터가 더 풍부한 쪽을 유지
     */
    const existingScore =
      dataCompleteness(
        existing
      );

    const currentScore =
      dataCompleteness(
        event
      );

    if (
      currentScore >
      existingScore
    ) {
      map.set(
        key,
        event
      );
      continue;
    }

    /*
     * importance가 더 높은 쪽
     */
    if (
      currentScore ===
        existingScore &&
      Number(
        event.importance
      ) >
        Number(
          existing.importance
        )
    ) {
      map.set(
        key,
        event
      );
    }
  }

  return Array.from(
    map.values()
  );
}

/* =========================================================
 * 데이터 완성도
 * ======================================================= */

function dataCompleteness(
  event
) {
  let score = 0;

  if (
    event.actual !=
      null &&
    event.actual !==
      ""
  ) {
    score += 4;
  }

  if (
    event.forecast !=
      null &&
    event.forecast !==
      ""
  ) {
    score += 2;
  }

  if (
    event.previous !=
      null &&
    event.previous !==
      ""
  ) {
    score += 1;
  }

  if (
    event.datetime_utc
  ) {
    score += 1;
  }

  return score;
}

/* =========================================================
 * Forex Factory fallback
 * ======================================================= */

function parseFfJson(
  raw,
  from,
  to
) {
  if (
    !Array.isArray(raw)
  ) {
    return [];
  }

  const events = [];

  for (
    const r of raw
  ) {
    if (!r) {
      continue;
    }

    const country =
      String(
        r.country ||
          r.currency ||
          ""
      );

    if (
      !/USD|United States|US/i.test(
        country
      )
    ) {
      continue;
    }

    const title =
      String(
        r.title ||
          r.event ||
          ""
      );

    if (!title) {
      continue;
    }

    const d =
      new Date(
        r.date ||
          r.datetime ||
          ""
      );

    if (
      Number.isNaN(
        d.getTime()
      )
    ) {
      continue;
    }

    const parts =
      fromUtcDate(d);

    if (
      parts.date_kst &&
      (
        parts.date_kst <
          from ||
        parts.date_kst >
          to
      )
    ) {
      continue;
    }

    /*
     * Forex Factory importance
     */
    const impact =
      String(
        r.impact || ""
      ).toLowerCase();

    let importance = 1;

    if (
      /high/.test(
        impact
      )
    ) {
      importance = 3;
    } else if (
      /medium|mod/.test(
        impact
      )
    ) {
      importance = 2;
    }

    events.push({
      ...parts,

      currency:
        "USD",

      importance,

      event:
        translateEvent(
          title
        ),

      actual:
        r.actual != null
          ? String(
              r.actual
            )
          : null,

      forecast:
        r.forecast != null
          ? String(
              r.forecast
            )
          : null,

      previous:
        r.previous != null
          ? String(
              r.previous
            )
          : null,
    });
  }

  return events;
}

/* =========================================================
 * 자체 fallback
 * ======================================================= */

function fallbackUsWeek(
  from,
  to
) {
  const events = [];

  let cursor =
    from;

  while (
    cursor <= to
  ) {
    const wd =
      new Date(
        cursor +
          "T12:00:00+09:00"
      ).getDay();

    const push =
      (
        hhmm,
        importance,
        event,
        forecast,
        previous
      ) => {
        const [
          h,
          mi,
        ] =
          hhmm.split(
            ":"
          );

        events.push({
          ...kstParts(
            cursor +
              " " +
              h +
              ":" +
              mi +
              ":00"
          ),

          currency:
            "USD",

          importance,

          event,

          actual:
            null,

          forecast:
            forecast ||
            null,

          previous:
            previous ||
            null,
        });
      };

    /*
     * 화요일
     */
    if (
      wd === 2
    ) {
      push(
        "21:30",
        2,
        "건축허가"
      );

      push(
        "21:30",
        2,
        "주택착공"
      );
    }

    /*
     * 수요일
     */
    if (
      wd === 3
    ) {
      push(
        "22:00",
        2,
        "기존주택판매"
      );
    }

    /*
     * 목요일
     */
    if (
      wd === 4
    ) {
      push(
        "21:30",
        3,
        "신규 실업수당 청구건수",
        "230K",
        "227K"
      );

      push(
        "23:00",
        2,
        "경기선행지수"
      );
    }

    /*
     * 금요일
     */
    if (
      wd === 5
    ) {
      const dayNum =
        Number(
          cursor.slice(
            -2
          )
        );

      if (
        dayNum <= 7
      ) {
        push(
          "21:30",
          3,
          "비농업 고용지수"
        );

        push(
          "21:30",
          3,
          "실업률"
        );
      } else {
        push(
          "21:30",
          3,
          "개인소비지출(PCE)"
        );

        push(
          "22:45",
          2,
          "시카고 PMI"
        );
      }
    }

    cursor =
      addDaysYmd(
        cursor,
        1
      );
  }

  return events;
}

/* =========================================================
 * 한국어 이벤트명 변환
 * ======================================================= */

function translateEvent(
  name
) {
  const trimmed =
    String(name)
      .trim();

  /*
   * 이미 한국어면 그대로 유지.
   */
  if (
    /[가-힣]/.test(
      trimmed
    )
  ) {
    return trimmed;
  }

  for (
    const [
      re,
      ko,
    ] of KO_NAMES
  ) {
    if (
      re.test(
        trimmed
      )
    ) {
      /*
       * 괄호 안의 기간은 보존.
       *
       * 예:
       * GDP (QoQ)
       * → 국내총생산 (QoQ)
       */
      const period =
        trimmed.match(
          /\(([^)]+)\)\s*$/
        );

      return period
        ? ko +
            " (" +
            period[1] +
            ")"
        : ko;
    }
  }

  return trimmed;
}

/* =========================================================
 * Investing.com datetime → KST
 *
 * Investing.com의 timeZone=88 응답을
 * 한국시간 기준으로 처리.
 * ======================================================= */

function kstParts(
  datetimeAttr,
  timeCell
) {
  if (
    !datetimeAttr
  ) {
    return {
      datetime_utc:
        null,

      time_kst:
        clean(
          timeCell
        ),

      date_kst:
        null,

      weekday_kst:
        null,
    };
  }

  const normalized =
    datetimeAttr
      .replace(
        /\//g,
        "-"
      )
      .trim();

  const m =
    normalized.match(
      /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/
    );

  if (!m) {
    return {
      datetime_utc:
        null,

      time_kst:
        clean(
          timeCell
        ),

      date_kst:
        null,

      weekday_kst:
        null,
    };
  }

  const y =
    m[1];

  const mo =
    m[2];

  const d =
    m[3];

  const h =
    m[4];

  const mi =
    m[5];

  const s =
    m[6] ||
    "00";

  /*
   * Investing timeZone=88은 KST 기준.
   *
   * KST를 UTC로 변환.
   */
  const kstAsUtc =
    Date.UTC(
      +y,
      +mo - 1,
      +d,
      +h,
      +mi,
      +s
    );

  const utc =
    new Date(
      kstAsUtc -
        9 *
          60 *
          60 *
          1000
    );

  return {
    datetime_utc:
      utc.toISOString(),

    time_kst:
      h +
      ":" +
      mi,

    date_kst:
      y +
      "-" +
      mo +
      "-" +
      d,

    weekday_kst:
      WEEKDAY_KO[
        new Date(
          y +
            "-" +
            mo +
            "-" +
            d +
            "T12:00:00+09:00"
        ).getDay()
      ],
  };
}

/* =========================================================
 * UTC Date → KST
 * ======================================================= */

function fromUtcDate(
  d
) {
  const kst =
    new Date(
      d.getTime() +
        9 *
          60 *
          60 *
          1000
    );

  const y =
    kst.getUTCFullYear();

  const mo =
    String(
      kst.getUTCMonth() +
        1
    ).padStart(
      2,
      "0"
    );

  const day =
    String(
      kst.getUTCDate()
    ).padStart(
      2,
      "0"
    );

  const h =
    String(
      kst.getUTCHours()
    ).padStart(
      2,
      "0"
    );

  const mi =
    String(
      kst.getUTCMinutes()
    ).padStart(
      2,
      "0"
    );

  return {
    datetime_utc:
      d.toISOString(),

    time_kst:
      h +
      ":" +
      mi,

    date_kst:
      y +
      "-" +
      mo +
      "-" +
      day,

    weekday_kst:
      WEEKDAY_KO[
        new Date(
          y +
            "-" +
            mo +
            "-" +
            day +
            "T12:00:00+09:00"
        ).getDay()
      ],
  };
}

/* =========================================================
 * 현재 KST 날짜
 * ======================================================= */

function formatYmdKst() {
  const kst =
    new Date(
      Date.now() +
        9 *
          60 *
          60 *
          1000
    );

  return (
    kst.getUTCFullYear() +
    "-" +
    String(
      kst.getUTCMonth() +
        1
    ).padStart(
      2,
      "0"
    ) +
    "-" +
    String(
      kst.getUTCDate()
    ).padStart(
      2,
      "0"
    )
  );
}

/* =========================================================
 * 날짜 더하기
 * ======================================================= */

function addDaysYmd(
  ymd,
  days
) {
  const p =
    ymd
      .split("-")
      .map(Number);

  const dt =
    new Date(
      Date.UTC(
        p[0],
        p[1] - 1,
        p[2] + days
      )
    );

  return (
    dt.getUTCFullYear() +
    "-" +
    String(
      dt.getUTCMonth() +
        1
    ).padStart(
      2,
      "0"
    ) +
    "-" +
    String(
      dt.getUTCDate()
    ).padStart(
      2,
      "0"
    )
  );
}

/* =========================================================
 * HTML → 텍스트
 * ======================================================= */

function clean(
  s
) {
  if (!s) {
    return null;
  }

  const t =
    String(s)
      .replace(
        /<[^>]+>/g,
        ""
      )
      .replace(
        /&nbsp;/gi,
        " "
      )
      .replace(
        /&amp;/gi,
        "&"
      )
      .replace(
        /&quot;/gi,
        '"'
      )
      .replace(
        /&#39;/gi,
        "'"
      )
      .replace(
        /&lt;/gi,
        "<"
      )
      .replace(
        /&gt;/gi,
        ">"
      )
      .replace(
        /\s+/g,
        " "
      )
      .trim();

  return (
    t || null
  );
}

/* =========================================================
 * HTML attribute
 * ======================================================= */

function attr(
  html,
  name
) {
  if (!html) {
    return null;
  }

  const escaped =
    name.replace(
      /[-/\\^$*+?.()|[\]{}]/g,
      "\\$&"
    );

  const re =
    new RegExp(
      escaped +
        '="([^"]*)"',
      "i"
    );

  const m =
    html.match(
      re
    );

  return m
    ? m[1]
    : null;
}

/* =========================================================
 * 정규식 grab
 * ======================================================= */

function grab(
  html,
  re
) {
  if (!html) {
    return null;
  }

  const m =
    html.match(
      re
    );

  return m
    ? m[1]
    : null;
}

/* =========================================================
 * JSON response
 * ======================================================= */

function json(
  body,
  status
) {
  return new Response(
    JSON.stringify(
      body
    ),
    {
      status:
        status || 200,

      headers: {
        "Content-Type":
          "application/json; charset=utf-8",
      },
    }
  );
}

/* =========================================================
 * CORS
 * ======================================================= */

function withCors(
  response
) {
  const headers =
    new Headers(
      response.headers
    );

  headers.set(
    "Access-Control-Allow-Origin",
    "*"
  );

  headers.set(
    "Access-Control-Allow-Methods",
    "GET, OPTIONS"
  );

  headers.set(
    "Access-Control-Allow-Headers",
    "Content-Type, Authorization"
  );

  return new Response(
    response.body,
    {
      status:
        response.status,

      headers,
    }
  );
}