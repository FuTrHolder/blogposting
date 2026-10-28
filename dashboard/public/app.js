let currentDate = null;
let currentMode = null;
let currentThumbnailUrl = "";
let currentBlogUrl = "";
let contentRequestId = 0;
let marketingRequestId = 0;

// 현재 실행 중인 마케팅 작업의 진행률 polling 식별자
let marketingProgressPollId = 0;

// 브라우저 새로고침 후에도 실행 중인 마케팅 작업을 복원하기 위한 저장 키
const MARKETING_PROGRESS_STORAGE_KEY =
  "seedsup_marketing_progress_target";

// ── 이미지를 실제로 클립보드에 복사 ──────────────────────────────────────────

async function copyImageToClipboard(url) {
  const proxied = `/proxy-image?url=${encodeURIComponent(url)}`;
  const resp = await fetch(proxied);

  if (!resp.ok) {
    throw new Error(`이미지를 가져오지 못했습니다 (${resp.status})`);
  }

  const blob = await resp.blob();
  const bitmap = await createImageBitmap(blob);

  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;

  canvas.getContext("2d").drawImage(bitmap, 0, 0);

  const pngBlob = await new Promise((resolve) =>
    canvas.toBlob(resolve, "image/png")
  );

  if (!pngBlob) {
    throw new Error("이미지 변환에 실패했습니다.");
  }

  await navigator.clipboard.write([
    new ClipboardItem({ "image/png": pngBlob })
  ]);
}

// ── HTML Escape ─────────────────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ═════════════════════════════════════════════════════════════════════════════
// 블로그 원고 작성 진행 상황 (원고 패널 상단) + 탭 자동 동기화
//  - GET /api/blog-progress : 선택한 탭의 진행률/실패 로그 (실행 중에는 3초마다 갱신)
//  - syncTabs()             : 10초마다 탭 목록만 조용히 갱신 (선택 상태/본문은 건드리지 않음)
// ═════════════════════════════════════════════════════════════════════════════

const BLOG_PROGRESS_POLL_MS = 3000;
const TAB_SYNC_INTERVAL_MS = 10000;
// 워크플로우 timeout(15분)보다 길게 신호가 없으면 중단된 것으로 간주
const BLOG_PROGRESS_STALE_MS = 20 * 60 * 1000;

const BLOG_PROGRESS_MESSAGES = {
  0: "원고 작성 워크플로우 시작",
  1: "뉴스·시장 데이터 수집 완료",
  2: "사실 기준표 구성 완료",
  3: "블로그 원고 생성 완료",
  4: "연관 포스트 삽입 완료",
  5: "썸네일 생성 완료",
  6: "대시보드 원고 업로드 완료",
  7: "원고 작성 완료"
};

let blogProgressPollId = 0;
let blogProgressActive = false;
let lastTabsSignature = "";

// '마케팅 실행' 버튼 제어: 원고 작성 중이거나 원고가 아직 없으면 비활성화
let blogRunning = false;       // 현재 탭의 원고 작성이 진행 중인가
let blogFailed = false;        // 현재 탭의 마지막 실행이 실패/중단되었는가
let currentHasPost = null;     // null=확인 전, true/false=원고 존재 여부
let triggerBlockedByBlog = false;
const TRIGGER_BLOCK_NOTICE =
  "원고 작성이 진행 중이거나 실패했거나, 원고가 아직 없어 마케팅을 실행할 수 없습니다.";

function applyTriggerBlock() {
  const btn = document.getElementById("trigger-btn");
  const status = document.getElementById("trigger-status");
  if (!btn) return;

  const blocked = blogRunning || blogFailed || currentHasPost === false;

  if (blocked) {
    triggerBlockedByBlog = true;
    btn.disabled = true;
    btn.title = TRIGGER_BLOCK_NOTICE;
    if (status && !status.textContent) status.textContent = TRIGGER_BLOCK_NOTICE;
    return;
  }

  if (!triggerBlockedByBlog) return;
  triggerBlockedByBlog = false;
  btn.title = "";
  if (status && status.textContent === TRIGGER_BLOCK_NOTICE) status.textContent = "";

  // 마케팅 워크플로우가 이 탭에서 실제로 실행 중이면 계속 비활성 유지
  const target = typeof getMarketingProgressTarget === "function"
    ? getMarketingProgressTarget() : null;
  const marketingBusy =
    target && target.post_date === currentDate && target.mode === currentMode;
  btn.disabled = !!marketingBusy;
}

function tabLabel(t) {
  const suffix =
    t.progress_status === "running" ? " ⏳"
    : t.progress_status === "failed" ? " ❌"
    : "";
  return `${t.post_date} ${t.mode}${suffix}`;
}

function parseSqliteUtc(value) {
  if (!value) return NaN;
  return Date.parse(String(value).replace(" ", "T") + "Z");
}

function hideBlogProgress() {
  blogRunning = false;
  blogFailed = false;
  applyTriggerBlock();
  const box = document.getElementById("blog-progress-box");
  if (box) {
    box.hidden = true;
    box.classList.remove("failed");
  }
}

function renderBlogProgress(p, stale) {
  const box = document.getElementById("blog-progress-box");
  if (!box) return;

  const status = stale ? "failed" : String(p.status || "running");
  const step = Math.max(0, Math.min(7, Number(p.step) || 0));

  blogRunning = status === "running";
  blogFailed = status === "failed";
  applyTriggerBlock();

  // 작성 중에는 결과물(제목·본문·태그·썸네일)을 보여주지 않고 진행 과정만 표시합니다.
  // (이전 실행의 결과물이 새 실행과 섞여 보이는 것도 방지)
  if (blogRunning) showPendingPost();

  box.hidden = false;
  box.classList.toggle("failed", status === "failed");

  document
    .querySelectorAll("#blog-progress .marketing-progress-step")
    .forEach((el, i) => el.classList.toggle("active", i < step));

  const badge = document.getElementById("blog-progress-badge");
  if (badge) {
    badge.className = `blog-progress-badge ${status}`;
    badge.textContent =
      status === "running" ? `진행 중 ${step}/7`
      : status === "completed" ? "완료"
      : "실패";
  }

  const msg = document.getElementById("blog-progress-message");
  if (msg) {
    msg.classList.remove("running", "completed", "failed");
    msg.classList.add(status);
    msg.textContent = stale
      ? "신호가 20분 이상 없습니다. 워크플로우가 중단(시간 초과 등)되었을 수 있습니다. GitHub Actions를 확인하세요."
      : p.message || BLOG_PROGRESS_MESSAGES[step] || "원고 작성 중…";
  }

  const errBox = document.getElementById("blog-progress-error");
  const errStep = document.getElementById("blog-progress-error-step");
  const errLog = document.getElementById("blog-progress-log");
  const runLink = document.getElementById("blog-progress-run-link");

  const showError = status === "failed";
  if (errBox) errBox.hidden = !showError;

  if (showError) {
    // textContent 만 사용 — 로그 내용이 HTML로 해석되지 않음
    if (errStep) {
      errStep.textContent = stale
        ? "응답 없음 (마지막 단계: " + (BLOG_PROGRESS_MESSAGES[step] || step) + ")"
        : (p.error_step || "알 수 없음") + " — 이번 실행의 결과물은 전달되지 않았습니다";
    }
    if (errLog) {
      errLog.textContent =
        p.error_log ||
        "상세 로그가 기록되지 않았습니다. 아래 GitHub Actions 실행 페이지에서 확인하세요.";
    }
    if (runLink) {
      const url = String(p.run_url || "");
      if (url.startsWith("https://github.com/")) {
        runLink.href = url;
        runLink.hidden = false;
      } else {
        runLink.removeAttribute("href");
        runLink.hidden = true;
      }
    }
  }
}

async function loadBlogProgress(postDate, mode) {
  const pollId = ++blogProgressPollId;
  blogProgressActive = true;
  let seenRunning = false;
  let lastStep = null;
  let lastStatus = null;

  try {
    while (pollId === blogProgressPollId) {
      let p = null;

      try {
        const res = await fetch(
          `/api/blog-progress?date=${encodeURIComponent(postDate)}` +
            `&mode=${encodeURIComponent(mode)}&_=${Date.now()}`,
          { cache: "no-store", headers: { Accept: "application/json" } }
        );
        if (res.ok) p = await res.json();
      } catch (err) {
        console.warn("원고 진행률 조회 실패:", err);
      }

      if (pollId !== blogProgressPollId) return;

      if (!p || !p.exists) {
        // 진행 기록이 없는 과거 탭 — 패널 숨김. 일시 오류면 다시 시도하지 않고 종료.
        hideBlogProgress();
        return;
      }

      const age = Date.now() - parseSqliteUtc(p.updated_at);
      const stale =
        p.status === "running" && Number.isFinite(age) && age > BLOG_PROGRESS_STALE_MS;

      renderBlogProgress(p, stale);

      // 상태(진행 중/완료/실패)가 바뀌면 탭 표시(⏳/❌)만 갱신합니다.
      // 결과물은 모든 단계가 성공한 뒤 완료 시점에 한 번만 불러옵니다.
      if (lastStatus !== null && p.status !== lastStatus) {
        void syncTabs(true);
      }
      lastStep = p.step;
      lastStatus = p.status;

      if (p.status === "running" && !stale) {
        seenRunning = true;
        await new Promise((r) => setTimeout(r, BLOG_PROGRESS_POLL_MS));
        continue;
      }

      // 종료 상태(완료/실패/중단)
      if (seenRunning) {
        // 실행 중이던 작업이 끝났다면 원고/탭 목록을 새로 반영
        void loadContent(postDate, mode);
        void syncTabs(true);
      }
      return;
    }
  } finally {
    if (pollId === blogProgressPollId) blogProgressActive = false;
  }
}

function buildTabsFromPosts(posts) {
  const seen = new Set();
  const tabs = [];
  for (const p of posts) {
    if (!p || !p.post_date || !p.mode) continue;
    const key = `${p.post_date}|${p.mode}`;
    if (seen.has(key)) continue;
    seen.add(key);
    tabs.push(p);
  }
  return tabs;
}

// 탭 목록만 조용히 갱신합니다 (현재 선택/본문/마케팅 화면은 건드리지 않음).
async function syncTabs(force = false) {
  const nav = document.getElementById("tabs");
  if (!nav || (document.hidden && !force)) return;

  let posts;
  try {
    const res = await fetch("/api/posts", { cache: "no-store" });
    if (!res.ok) return;
    posts = await res.json();
    if (!Array.isArray(posts)) return;
  } catch (err) {
    return; // 일시 오류는 다음 주기에 재시도
  }

  const tabs = buildTabsFromPosts(posts);
  if (tabs.length === 0) return;

  const signature = JSON.stringify(
    tabs.map((t) => [t.post_date, t.mode, t.progress_status || ""])
  );

  if (signature !== lastTabsSignature) {
    lastTabsSignature = signature;
    nav.innerHTML = "";

    tabs.forEach((t) => {
      const btn = document.createElement("button");
      const isActive = t.post_date === currentDate && t.mode === currentMode;
      btn.className = "tab-btn" + (isActive ? " active" : "");
      btn.textContent = tabLabel(t);
      btn.onclick = () => selectTab(t.post_date, t.mode, btn);
      nav.appendChild(btn);
    });
  }

  // 첫 포스트가 처음 생긴 경우(선택된 탭 없음) 자동으로 첫 탭을 선택
  if (!currentDate) {
    const firstBtn = nav.querySelector(".tab-btn");
    selectTab(tabs[0].post_date, tabs[0].mode, firstBtn);
    return;
  }

  // 선택된 탭이 새로 '실행 중'이 되었는데 아직 폴링하지 않고 있다면 시작
  const current = tabs.find(
    (t) => t.post_date === currentDate && t.mode === currentMode
  );
  if (current && current.progress_status === "running" && !blogProgressActive) {
    void loadBlogProgress(currentDate, currentMode);
  }
}

function showPendingPost() {
  const postTitle = document.getElementById("post-title");
  const titleText = document.getElementById("title-text");
  const contentText = document.getElementById("content-text");
  const tagsChips = document.getElementById("tags-chips");
  const tagsEmpty = document.getElementById("tags-empty");
  const thumbImg = document.getElementById("thumb-img");
  const thumbEmpty = document.getElementById("thumb-empty");
  const thumbDownload = document.getElementById("thumb-download-link");
  const thumbCopy = document.getElementById("thumb-copy-btn");

  if (postTitle) {
    postTitle.textContent = blogRunning ? "원고 작성 중" : "원고 없음";
  }
  if (titleText) titleText.value = "";
  if (contentText) {
    contentText.value = blogRunning
      ? "원고를 작성하고 있습니다. 제목·본문·태그·썸네일은 모든 단계가 성공한 뒤 " +
        "한 번에 전달되며, 완료되면 이 화면이 자동으로 갱신됩니다. " +
        "위 '원고 작성 진행'에서 진행 과정을 확인하세요."
      : "대시보드에 전달된 원고가 없습니다. 위 '원고 작성 진행' 상태를 확인하세요.";
  }
  if (tagsChips) tagsChips.innerHTML = "";
  if (tagsEmpty) tagsEmpty.hidden = false;

  currentThumbnailUrl = "";
  if (thumbImg) {
    thumbImg.removeAttribute("src");
    thumbImg.hidden = true;
  }
  if (thumbEmpty) thumbEmpty.hidden = false;
  if (thumbDownload) {
    thumbDownload.removeAttribute("href");
    thumbDownload.style.pointerEvents = "none";
    thumbDownload.style.opacity = "0.5";
  }
  if (thumbCopy) thumbCopy.disabled = true;
}

function initBlogProgressButtons() {
  const btn = document.getElementById("blog-progress-log-copy");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const log = document.getElementById("blog-progress-log");
    if (!log || !log.textContent) return;
    const original = btn.textContent;
    try {
      await navigator.clipboard.writeText(log.textContent);
      btn.textContent = "복사됨!";
    } catch (err) {
      btn.textContent = "복사 실패";
      console.error(err);
    }
    setTimeout(() => { btn.textContent = original; }, 1200);
  });
}

// ── 탭 로딩 ─────────────────────────────────────────────────────────────────

async function loadTabs() {
  const nav = document.getElementById("tabs");
  if (!nav) return;

  let posts;

  try {
    const res = await fetch("/api/posts", {
      cache: "no-store"
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }

    posts = await res.json();

    if (!Array.isArray(posts)) {
      throw new Error(
        "포스트 API가 배열 형식의 데이터를 반환하지 않았습니다."
      );
    }
  } catch (err) {
    console.error("포스트 목록 로딩 실패:", err);

    nav.innerHTML =
      `<span class="muted" style="padding:8px 16px;">` +
      `포스트 목록을 불러오지 못했습니다: ` +
      `${escapeHtml(String(err.message || err))} ` +
      `(콘솔 로그 확인)</span>`;

    return;
  }

  const tabs = buildTabsFromPosts(posts);

  lastTabsSignature = JSON.stringify(
    tabs.map((t) => [t.post_date, t.mode, t.progress_status || ""])
  );

  nav.innerHTML = "";

  if (tabs.length === 0) {
    nav.innerHTML =
      '<span class="muted" style="padding:8px 16px;">아직 등록된 포스트가 없습니다.</span>';

    return;
  }

  // 브라우저 새로고침 후에도 현재 실행 중인 마케팅 작업의
  // 날짜/모드를 복원합니다.
  let savedTarget = null;

  try {
    const raw =
      localStorage.getItem(
        MARKETING_PROGRESS_STORAGE_KEY
      );

    if (raw) {
      const parsed = JSON.parse(raw);

      if (
        parsed &&
        parsed.post_date &&
        parsed.mode
      ) {
        savedTarget = {
          post_date: String(parsed.post_date),
          mode: String(parsed.mode)
        };
      }
    }
  } catch (err) {
    console.warn(
      "마케팅 진행 대상 복원 실패:",
      err
    );
  }

  let initialTab = tabs[0];
  let initialIndex = 0;

  if (savedTarget) {
    const savedIndex = tabs.findIndex(
      (t) =>
        t.post_date === savedTarget.post_date &&
        t.mode === savedTarget.mode
    );

    if (savedIndex >= 0) {
      initialTab = tabs[savedIndex];
      initialIndex = savedIndex;
    }
  }

  tabs.forEach((t, i) => {
    const btn = document.createElement("button");

    btn.className =
      "tab-btn" +
      (i === initialIndex ? " active" : "");

    btn.textContent = tabLabel(t);

    btn.onclick = () =>
      selectTab(
        t.post_date,
        t.mode,
        btn
      );

    nav.appendChild(btn);
  });

  const initialButton =
    nav.querySelectorAll(".tab-btn")[initialIndex];

  selectTab(
    initialTab.post_date,
    initialTab.mode,
    initialButton
  );
}

// ── 포스트 탭 선택 ──────────────────────────────────────────────────────────

function selectTab(postDate, mode, btn) {
  currentDate = postDate;
  currentMode = mode;

  document
    .querySelectorAll("#tabs .tab-btn")
    .forEach((b) => {
      b.classList.remove("active");
    });

  if (btn) {
    btn.classList.add("active");
  }

  resetPostView();

  void loadContent(postDate, mode);
  void loadMarketing(postDate, mode);
  void loadBlogProgress(postDate, mode);

  // 현재 선택된 탭이 실행 중인 마케팅 작업이라면
  // 진행률 polling을 자동으로 복원합니다.
  void initMarketingProgress();
}

// ── 탭 전환 시 기존 화면 초기화 ─────────────────────────────────────────────

function resetPostView() {
  const postTitle = document.getElementById("post-title");
  const titleText = document.getElementById("title-text");
  const contentText = document.getElementById("content-text");
  const tagsChips = document.getElementById("tags-chips");
  const tagsEmpty = document.getElementById("tags-empty");
  const thumbImg = document.getElementById("thumb-img");
  const thumbEmpty = document.getElementById("thumb-empty");
  const thumbDownload = document.getElementById("thumb-download-link");
  const thumbCopy = document.getElementById("thumb-copy-btn");
  const marketingGrid = document.getElementById("marketing-grid");
  const kakaoText = document.getElementById("kakao-text");

  currentHasPost = null;
  hideBlogProgress();

  if (postTitle) {
    postTitle.textContent = "불러오는 중…";
  }

  if (titleText) {
    titleText.value = "";
  }

  if (contentText) {
    contentText.value = "포스트 본문을 불러오는 중…";
  }

  if (tagsChips) {
    tagsChips.innerHTML = "";
  }

  if (tagsEmpty) {
    tagsEmpty.hidden = false;
  }

  currentThumbnailUrl = "";
  currentBlogUrl = "";

  if (thumbImg) {
    thumbImg.removeAttribute("src");
    thumbImg.hidden = true;
  }

  if (thumbEmpty) {
    thumbEmpty.hidden = false;
  }

  if (thumbDownload) {
    thumbDownload.removeAttribute("href");
    thumbDownload.style.pointerEvents = "none";
    thumbDownload.style.opacity = "0.5";
  }

  if (thumbCopy) {
    thumbCopy.disabled = true;
  }

  updateKakaoBlogUrl("");

  if (kakaoText) {
    kakaoText.value = "";
  }

  if (marketingGrid) {
    marketingGrid.innerHTML =
      '<p class="muted">마케팅 결과를 불러오는 중…</p>';
  }
}

// ── 콘텐츠 로딩 ─────────────────────────────────────────────────────────────

async function loadContent(postDate, mode) {
  const requestId = ++contentRequestId;

  let post;

  try {
    const res = await fetch(
      `/api/posts?date=${encodeURIComponent(postDate)}&mode=${encodeURIComponent(mode)}`,
      {
        cache: "no-store"
      }
    );

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }

    post = await res.json();

    if (!post) {
      // 원고 작성이 진행 중이라 아직 posts 행이 없는 탭 (진행 상황 패널이 대신 표시됨)
      if (requestId !== contentRequestId) return;
      currentHasPost = false;
      applyTriggerBlock();
      showPendingPost();
      return;
    }
  } catch (err) {
    if (requestId !== contentRequestId) return;

    console.error("포스트 본문 로딩 실패:", err);

    const postTitle = document.getElementById("post-title");
    const titleText = document.getElementById("title-text");
    const contentText = document.getElementById("content-text");
    const tagsChips = document.getElementById("tags-chips");
    const tagsEmpty = document.getElementById("tags-empty");

    if (postTitle) {
      postTitle.textContent = "불러오기 실패";
    }

    if (titleText) {
      titleText.value = "";
    }

    if (contentText) {
      contentText.value = `에러: ${err.message || err}`;
    }

    if (tagsChips) {
      tagsChips.innerHTML = "";
    }

    if (tagsEmpty) {
      tagsEmpty.hidden = false;
    }

    return;
  }

  if (requestId !== contentRequestId) return;

  // 원고 작성이 진행 중이면 (이전 실행의 결과물이 있더라도) 표시하지 않습니다.
  if (blogRunning) {
    currentHasPost = !!post;
    applyTriggerBlock();
    showPendingPost();
    return;
  }

  currentBlogUrl = post?.blog_url || "";

  // ── 제목 + 본문 HTML ─────────────────────────────────────────────────────

  const postTitle = document.getElementById("post-title");
  const titleText = document.getElementById("title-text");
  const contentText = document.getElementById("content-text");

  const title = post?.title || "";
  const content = post?.content || "";

  if (postTitle) {
    postTitle.textContent = title || "제목 없음";
  }

  if (titleText) {
    titleText.value = title;
  }

  if (contentText) {
    contentText.value = content;
  }

  // ── 태그 ─────────────────────────────────────────────────────────────────

  let tags = [];

  try {
    if (Array.isArray(post?.tags)) {
      tags = post.tags;
    } else {
      tags = JSON.parse(post?.tags || "[]");
    }

    if (!Array.isArray(tags)) {
      tags = [];
    }

    tags = tags
      .map((tag) => String(tag).trim())
      .filter(Boolean);
  } catch (e) {
    console.warn("태그 JSON 파싱 실패:", e);
    tags = [];
  }

  const tagsChips = document.getElementById("tags-chips");
  const tagsEmpty = document.getElementById("tags-empty");

  if (tagsChips) {
    tagsChips.innerHTML = "";
  }

  if (!tags.length) {
    if (tagsEmpty) {
      tagsEmpty.hidden = false;
    }
  } else {
    if (tagsEmpty) {
      tagsEmpty.hidden = true;
    }

    tags.forEach((t) => {
      const chip = document.createElement("button");

      chip.className = "tag-chip";
      chip.textContent = `#${t}`;

      chip.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(t);

          const original = chip.textContent;
          chip.textContent = "복사됨!";

          setTimeout(() => {
            chip.textContent = original;
          }, 1000);
        } catch (err) {
          console.error("태그 복사 실패:", err);
        }
      });

      if (tagsChips) {
        tagsChips.appendChild(chip);
      }
    });
  }

  const tagsCopyAllBtn =
    document.getElementById("tags-copy-all-btn");

  if (tagsCopyAllBtn) {
    tagsCopyAllBtn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(tags.join(" "));

        const original = tagsCopyAllBtn.textContent;
        tagsCopyAllBtn.textContent = "복사됨!";

        setTimeout(() => {
          tagsCopyAllBtn.textContent = original;
        }, 1200);
      } catch (err) {
        console.error("전체 태그 복사 실패:", err);
      }
    };
  }

  // ── 썸네일 ───────────────────────────────────────────────────────────────

  const img = document.getElementById("thumb-img");
  const empty = document.getElementById("thumb-empty");
  const downloadLink =
    document.getElementById("thumb-download-link");
  const copyBtn =
    document.getElementById("thumb-copy-btn");

  currentThumbnailUrl =
    post?.thumbnail_url || "";

  if (currentThumbnailUrl) {
    if (img) {
      img.src =
        `/proxy-image?url=${encodeURIComponent(currentThumbnailUrl)}`;
      img.hidden = false;
    }

    if (empty) {
      empty.hidden = true;
    }

    if (downloadLink) {
      downloadLink.href = currentThumbnailUrl;
      downloadLink.style.pointerEvents = "auto";
      downloadLink.style.opacity = "1";
    }

    if (copyBtn) {
      copyBtn.disabled = false;
    }
  } else {
    if (img) {
      img.removeAttribute("src");
      img.hidden = true;
    }

    if (empty) {
      empty.hidden = false;
    }

    if (downloadLink) {
      downloadLink.removeAttribute("href");
      downloadLink.style.pointerEvents = "none";
      downloadLink.style.opacity = "0.5";
    }

    if (copyBtn) {
      copyBtn.disabled = true;
    }
  }

  updateKakaoBlogUrl(currentBlogUrl);

  currentHasPost = true;
  applyTriggerBlock();
}

// ── 대표 썸네일 복사 ────────────────────────────────────────────────────────

function initThumbnailCopyButton() {
  const thumbCopyButton =
    document.getElementById("thumb-copy-btn");

  if (!thumbCopyButton) return;

  thumbCopyButton.addEventListener("click", async (e) => {
    const btn = e.currentTarget;

    if (!currentThumbnailUrl) return;

    const original = btn.textContent;

    try {
      await copyImageToClipboard(currentThumbnailUrl);
      btn.textContent = "복사됨!";
    } catch (err) {
      btn.textContent = "복사 실패";
      console.error(err);
    }

    setTimeout(() => {
      btn.textContent = original;
    }, 1400);
  });
}

// ── 카카오스토리채널 ────────────────────────────────────────────────────────

function updateKakaoBlogUrl(blogUrl) {
  const urlInput =
    document.getElementById("kakao-blog-url");

  if (!urlInput) return;

  urlInput.value = blogUrl || "";
  urlInput.placeholder = blogUrl
    ? ""
    : "블로그 링크 없음 (마케팅 실행 후 채워짐)";
}

function updateKakaoText(kakaoText) {
  const textarea =
    document.getElementById("kakao-text");

  if (textarea) {
    textarea.value = kakaoText || "";
  }
}

function initKakaoButtons() {
  const kakaoCopyButton =
    document.getElementById("kakao-copy-btn");

  if (kakaoCopyButton) {
    kakaoCopyButton.addEventListener("click", async (e) => {
      const btn = e.currentTarget;

      const textarea =
        document.getElementById("kakao-text");

      const text = textarea?.value || "";

      if (!text) return;

      try {
        await navigator.clipboard.writeText(text);

        const original = btn.textContent;
        btn.textContent = "복사됨!";

        setTimeout(() => {
          btn.textContent = original;
        }, 1200);
      } catch (err) {
        console.error("카카오 본문 복사 실패:", err);
      }
    });
  }

  const kakaoUrlCopyButton =
    document.getElementById("kakao-url-copy-btn");

  if (kakaoUrlCopyButton) {
    kakaoUrlCopyButton.addEventListener("click", async (e) => {
      const btn = e.currentTarget;

      const input =
        document.getElementById("kakao-blog-url");

      const url = input?.value || "";

      if (!url) return;

      try {
        await navigator.clipboard.writeText(url);

        const original = btn.textContent;
        btn.textContent = "복사됨!";

        setTimeout(() => {
          btn.textContent = original;
        }, 1200);
      } catch (err) {
        console.error("카카오 URL 복사 실패:", err);
      }
    });
  }
}

// ── 마케팅 플랫폼 표시 이름 ─────────────────────────────────────────────────

const PLATFORM_LABELS = {
  youtube: "▶ YouTube",
  facebook: "📘 Facebook",
  facebook_reels: "📘 Facebook Reels",
  instagram: "📸 Instagram",
  instagram_reels: "📸 Instagram Reels",
  threads: "🧵 Threads",
  threads_reels: "🧵 Threads Reels",
  tiktok: "🎵 TikTok"
};

// ── 마케팅 결과 로딩 ────────────────────────────────────────────────────────

async function loadMarketing(postDate, mode) {
  const grid =
    document.getElementById("marketing-grid");

  if (!grid) return;

  const requestId = ++marketingRequestId;

  let results;

  try {
    const res = await fetch(
      `/api/marketing?date=${encodeURIComponent(postDate)}&mode=${encodeURIComponent(mode)}`,
      {
        cache: "no-store"
      }
    );

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));

      throw new Error(
        body.error || `HTTP ${res.status}`
      );
    }

    results = await res.json();

    if (!Array.isArray(results)) {
      throw new Error(
        "마케팅 API가 배열 형식의 데이터를 반환하지 않았습니다."
      );
    }
  } catch (err) {
    if (requestId !== marketingRequestId) return;

    console.error(
      "마케팅 결과 로딩 실패:",
      err
    );

    grid.innerHTML =
      `<p class="muted">` +
      `마케팅 결과를 불러오지 못했습니다: ` +
      `${escapeHtml(String(err.message || err))}` +
      `</p>`;

    return;
  }

  if (requestId !== marketingRequestId) return;

  grid.innerHTML = "";

  const kakaoResult =
    results.find((r) => r.platform === "kakao");

  if (
    kakaoResult &&
    kakaoResult.content_text
  ) {
    updateKakaoText(
      kakaoResult.content_text
    );
  }

  if (!results || results.length === 0) {
    grid.innerHTML =
      '<p class="muted">아직 마케팅 결과가 없습니다.</p>';

    return;
  }

  results.forEach((r) => {
    if (r.platform === "kakao") return;

    const card =
      document.createElement("div");

    const isTiktok =
      r.platform === "tiktok";

    card.className =
      "platform-card" +
      (isTiktok ? " tiktok-card" : "");

    const statusClass =
      r.status === "ok"
        ? "status-ok"
        : r.status === "skip"
        ? "status-skip"
        : "status-error";

    const platformLabel =
      PLATFORM_LABELS[r.platform] ||
      r.platform;

    const proxyUrl = (url) =>
      url
        ? `/proxy-image?url=${encodeURIComponent(url)}`
        : "";

    let mediaHtml = "";

    if (r.video_url) {
      mediaHtml = `
        <video
          src="${escapeHtml(r.video_url)}"
          controls
          playsinline
        ></video>

        <div class="btn-row">
          <a
            class="copy-btn"
            href="${escapeHtml(r.video_url)}"
            download
          >
            ⬇ 영상 다운로드
          </a>

          ${
            isTiktok
              ? `
                <a
                  class="copy-btn tiktok-upload-btn"
                  href="https://www.tiktok.com/upload"
                  target="_blank"
                  rel="noopener"
                >
                  ▶ TikTok 업로드
                </a>
              `
              : ""
          }
        </div>
      `;
    } else if (r.thumbnail_url) {
      mediaHtml = `
        <img
          src="${proxyUrl(r.thumbnail_url)}"
          alt="${escapeHtml(r.platform)} 썸네일"
        >

        <div class="btn-row">
          <button
            class="copy-btn"
            data-copy-img="${escapeHtml(r.thumbnail_url)}"
          >
            이미지 복사
          </button>

          <a
            class="copy-btn"
            href="${escapeHtml(r.thumbnail_url)}"
            download
          >
            다운로드
          </a>
        </div>
      `;
    }

    const tiktokGuideHtml =
      isTiktok
        ? `
          <div class="tiktok-guide">
            <p>
              📋 <strong>TikTok 수동 업로드 안내</strong>
            </p>

            <ol>
              <li>
                위 영상 다운로드 버튼으로 MP4 저장
              </li>

              <li>
                ▶ TikTok 업로드 버튼 클릭 →
                갤러리에서 선택
              </li>

              <li>
                또는
                <a
                  href="https://www.tiktok.com/tiktokstudio/upload"
                  target="_blank"
                  rel="noopener"
                >
                  TikTok Studio
                </a>
                에서 업로드
              </li>

              <li>
                아래 캡션 복사 후 붙여넣기
              </li>
            </ol>
          </div>
        `
        : "";

    card.innerHTML = `
      <h3>${escapeHtml(platformLabel)}</h3>

      <span class="status-badge ${statusClass}">
        ${escapeHtml(r.status || "-")}
      </span>

      ${mediaHtml}

      <p class="muted">
        ${escapeHtml(r.message || "")}
      </p>

      ${tiktokGuideHtml}

      ${
        !isTiktok && r.url
          ? `
            <a
              href="${escapeHtml(r.url)}"
              target="_blank"
              rel="noopener"
            >
              게시물 열기 →
            </a>
          `
          : ""
      }

      ${
        r.content_text
          ? `
            <div
              class="field-head"
              style="margin-top:8px;"
            >
              <span>캡션</span>

              <button
                class="copy-btn"
                data-copy-text
              >
                복사
              </button>
            </div>

            <textarea
              rows="4"
              readonly
            ></textarea>
          `
          : ""
      }
    `;

    if (r.content_text) {
      const textarea =
        card.querySelector("textarea");

      if (textarea) {
        textarea.value = r.content_text;
      }
    }

    const textBtn =
      card.querySelector("[data-copy-text]");

    if (textBtn) {
      textBtn.addEventListener(
        "click",
        async () => {
          const textarea =
            card.querySelector("textarea");

          if (!textarea) return;

          try {
            await navigator.clipboard.writeText(
              textarea.value
            );

            const original =
              textBtn.textContent;

            textBtn.textContent = "복사됨!";

            setTimeout(() => {
              textBtn.textContent =
                original;
            }, 1200);
          } catch (err) {
            console.error(
              "캡션 복사 실패:",
              err
            );
          }
        }
      );
    }

    const imgBtn =
      card.querySelector("[data-copy-img]");

    if (imgBtn) {
      imgBtn.addEventListener(
        "click",
        async () => {
          const original =
            imgBtn.textContent;

          try {
            await copyImageToClipboard(
              imgBtn.dataset.copyImg
            );

            imgBtn.textContent = "복사됨!";
          } catch (err) {
            imgBtn.textContent = "복사 실패";
            console.error(err);
          }

          setTimeout(() => {
            imgBtn.textContent =
              original;
          }, 1400);
        }
      );
    }

    grid.appendChild(card);
  });
}

// ── 마케팅 워크플로우 ───────────────────────────────────────────────────────

const MARKETING_PROGRESS_POLL_INTERVAL_MS = 3000;

const MARKETING_PROGRESS_MESSAGES = {
  0: "마케팅 워크플로우 시작",
  1: "티스토리 포스트 확인 완료",
  2: "플랫폼별 콘텐츠 생성 완료",
  3: "영상 생성 완료",
  4: "SNS 썸네일 생성 완료",
  5: "발행용 공개 URL 확보 완료",
  6: "플랫폼 발행 처리 완료",
  7: "마케팅 워크플로우 완료"
};


// ── 진행 중인 마케팅 작업 저장 ─────────────────────────────────────────────

function setMarketingProgressTarget(
  postDate,
  mode
) {
  if (!postDate || !mode) return;

  try {
    localStorage.setItem(
      MARKETING_PROGRESS_STORAGE_KEY,
      JSON.stringify({
        post_date: postDate,
        mode
      })
    );
  } catch (err) {
    console.warn(
      "마케팅 진행 대상 저장 실패:",
      err
    );
  }
}


function getMarketingProgressTarget() {
  try {
    const raw =
      localStorage.getItem(
        MARKETING_PROGRESS_STORAGE_KEY
      );

    if (!raw) {
      return null;
    }

    const parsed = JSON.parse(raw);

    if (
      !parsed ||
      !parsed.post_date ||
      !parsed.mode
    ) {
      return null;
    }

    return {
      post_date: String(parsed.post_date),
      mode: String(parsed.mode)
    };
  } catch (err) {
    console.warn(
      "마케팅 진행 대상 읽기 실패:",
      err
    );

    return null;
  }
}


function clearMarketingProgressTarget() {
  try {
    localStorage.removeItem(
      MARKETING_PROGRESS_STORAGE_KEY
    );
  } catch (err) {
    console.warn(
      "마케팅 진행 대상 삭제 실패:",
      err
    );
  }
}


// ── 진행률 UI ───────────────────────────────────────────────────────────────

function updateMarketingProgress(
  step,
  status = "running",
  message = ""
) {
  const progress =
    document.getElementById(
      "marketing-progress"
    );

  const messageElement =
    document.getElementById(
      "marketing-progress-message"
    );

  if (!progress) return;

  const steps =
    progress.querySelectorAll(
      ".marketing-progress-step"
    );

  const numericStep =
    Number(step);

  const safeStep =
    Number.isFinite(numericStep)
      ? Math.max(
          0,
          Math.min(7, numericStep)
        )
      : 0;

  steps.forEach((element, index) => {
    element.classList.toggle(
      "active",
      index < safeStep
    );
  });

  if (!messageElement) {
    return;
  }

  messageElement.classList.remove(
    "running",
    "completed",
    "failed"
  );

  if (
    status === "running" ||
    status === "completed" ||
    status === "failed"
  ) {
    messageElement.classList.add(
      status
    );
  }

  messageElement.textContent =
    message ||
    MARKETING_PROGRESS_MESSAGES[safeStep] ||
    "마케팅 워크플로우 실행 중...";
}


// ── 진행률 API 조회 ─────────────────────────────────────────────────────────

async function fetchMarketingProgress(
  postDate,
  mode
) {
  const query =
    `/api/marketing-progress` +
    `?date=${encodeURIComponent(postDate)}` +
    `&mode=${encodeURIComponent(mode)}` +
    `&_=${Date.now()}`;

  const res =
    await fetch(query, {
      method: "GET",
      cache: "no-store",
      headers: {
        Accept: "application/json"
      }
    });

  if (!res.ok) {
    throw new Error(
      `진행률 조회 실패 (${res.status})`
    );
  }

  return await res.json();
}


// ── 진행률 polling ──────────────────────────────────────────────────────────

async function pollMarketingProgress(
  postDate,
  mode
) {
  const pollId =
    ++marketingProgressPollId;

  const statusElement =
    document.getElementById(
      "trigger-status"
    );

  const triggerButton =
    document.getElementById(
      "trigger-btn"
    );

  while (
    pollId === marketingProgressPollId
  ) {
    try {
      const progress =
        await fetchMarketingProgress(
          postDate,
          mode
        );

      // 다른 작업이 시작되어 polling 대상이 바뀌었다면 종료
      if (
        pollId !== marketingProgressPollId
      ) {
        return;
      }

      if (
        progress &&
        progress.exists
      ) {
        updateMarketingProgress(
          progress.step,
          progress.status,
          progress.message
        );

        if (
          progress.status === "completed"
        ) {
          if (statusElement) {
            statusElement.textContent =
              "마케팅 워크플로우 완료. 결과를 불러오는 중...";
          }

          if (triggerButton) {
            triggerButton.disabled = false;
          }

          clearMarketingProgressTarget();

          // 결과 D1 반영이 완료된 뒤 포스트 정보와
          // 마케팅 결과를 함께 다시 불러옵니다.
          // 새로 생성된 티스토리 원문 링크(blog_url)도 즉시 반영합니다.
          setTimeout(() => {
            if (
              pollId === marketingProgressPollId
            ) {
              void loadContent(
                postDate,
                mode
              );
          
              void loadMarketing(
                postDate,
                mode
              );
            }
          }, 500);

          return;
        }

        if (
          progress.status === "failed"
        ) {
          if (statusElement) {
            statusElement.textContent =
              progress.message ||
              "마케팅 워크플로우 실행 중 오류가 발생했습니다.";
          }

          if (triggerButton) {
            triggerButton.disabled = false;
          }

          clearMarketingProgressTarget();

          return;
        }

        if (
          statusElement &&
          progress.message
        ) {
          statusElement.textContent =
            progress.message;
        }
      } else {
        // 아직 GitHub Actions에서 첫 번째 진행률 신호가 오지 않았더라도
        // D1에 저장된 실행 요청 상태를 유지하면서 계속 조회합니다.
        updateMarketingProgress(
          0,
          "running",
          "마케팅 워크플로우 실행 준비 중..."
        );
      }

    } catch (err) {
      console.warn(
        "마케팅 진행률 조회 실패:",
        err
      );
    }

    await new Promise((resolve) => {
      setTimeout(
        resolve,
        MARKETING_PROGRESS_POLL_INTERVAL_MS
      );
    });
  }
}


// ── 진행률 polling 시작 ─────────────────────────────────────────────────────

function startMarketingProgressPolling(
  postDate,
  mode
) {
  if (!postDate || !mode) {
    return;
  }

  // 기존 polling을 무효화합니다.
  ++marketingProgressPollId;

  // 새 실행 대상을 저장합니다.
  setMarketingProgressTarget(
    postDate,
    mode
  );

  const triggerButton =
    document.getElementById(
      "trigger-btn"
    );

  if (triggerButton) {
    triggerButton.disabled = true;
  }

  void pollMarketingProgress(
    postDate,
    mode
  );
}


// ── 새로고침 후 진행률 복원 ─────────────────────────────────────────────────

async function initMarketingProgress() {
  const target =
    getMarketingProgressTarget();

  if (!target) {
    return;
  }

  // 현재 선택된 탭과 실행 중인 작업이 다르면
  // 해당 작업의 UI를 건드리지 않습니다.
  if (
    currentDate !== target.post_date ||
    currentMode !== target.mode
  ) {
    return;
  }

  try {
    const progress =
      await fetchMarketingProgress(
        target.post_date,
        target.mode
      );

    if (
      !progress ||
      !progress.exists
    ) {
      return;
    }

    updateMarketingProgress(
      progress.step,
      progress.status,
      progress.message
    );

    const triggerButton =
      document.getElementById(
        "trigger-btn"
      );

    if (
      progress.status === "running"
    ) {
      if (triggerButton) {
        triggerButton.disabled = true;
      }

      const statusElement =
        document.getElementById(
          "trigger-status"
        );

      if (statusElement) {
        statusElement.textContent =
          progress.message ||
          "마케팅 워크플로우 실행 중...";
      }

      startMarketingProgressPolling(
        target.post_date,
        target.mode
      );

      return;
    }

    if (
      progress.status === "completed"
    ) {
      if (triggerButton) {
        triggerButton.disabled = false;
      }

      clearMarketingProgressTarget();

      return;
    }

    if (
      progress.status === "failed"
    ) {
      if (triggerButton) {
        triggerButton.disabled = false;
      }

      clearMarketingProgressTarget();

      return;
    }

  } catch (err) {
    console.warn(
      "마케팅 진행률 복원 실패:",
      err
    );

    // 일시적인 네트워크 오류일 수 있으므로
    // 기존 실행 대상은 삭제하지 않습니다.
    startMarketingProgressPolling(
      target.post_date,
      target.mode
    );
  }
}


// ── 마케팅 실행 버튼 ─────────────────────────────────────────────────────────

function initMarketingTrigger() {
  const triggerButton =
    document.getElementById(
      "trigger-btn"
    );

  if (!triggerButton) return;

  triggerButton.addEventListener(
    "click",
    async () => {
      if (
        !currentDate ||
        !currentMode
      ) {
        return;
      }

      // 원고 작성 중이거나 원고가 없으면 실행하지 않음 (버튼 비활성화의 이중 안전장치)
      if (blogRunning || blogFailed || currentHasPost === false) {
        applyTriggerBlock();
        return;
      }

      const postDate =
        currentDate;

      const mode =
        currentMode;

      const statusElement =
        document.getElementById(
          "trigger-status"
        );

      // 실행 대상을 먼저 저장합니다.
      // 이후 브라우저를 새로고침해도 복원할 수 있습니다.
      setMarketingProgressTarget(
        postDate,
        mode
      );

      // 기존 polling을 무효화합니다.
      ++marketingProgressPollId;

      triggerButton.disabled = true;

      updateMarketingProgress(
        0,
        "running",
        "마케팅 워크플로우 실행 요청 중..."
      );

      if (statusElement) {
        statusElement.textContent =
          "GitHub Actions 실행 요청 중...";
      }

      try {
        const res =
          await fetch(
            "/api/marketing-trigger",
            {
              method: "POST",
              headers: {
                "Content-Type":
                  "application/json",
                Accept:
                  "application/json"
              },
              body: JSON.stringify({
                post_date:
                  postDate,
                mode:
                  mode
              })
            }
          );

        const data =
          await res.json().catch(
            () => ({})
          );

        if (
          !res.ok ||
          !data.ok
        ) {
          throw new Error(
            data.error ||
            "마케팅 워크플로우 실행 요청에 실패했습니다."
          );
        }

        updateMarketingProgress(
          0,
          "running",
          "GitHub Actions 실행 시작"
        );

        if (statusElement) {
          statusElement.textContent =
            "요청 완료. 마케팅 워크플로우 진행 상황을 확인하고 있습니다...";
        }

        startMarketingProgressPolling(
          postDate,
          mode
        );

      } catch (err) {
        updateMarketingProgress(
          0,
          "failed",
          "마케팅 워크플로우 실행 요청 실패"
        );

        if (statusElement) {
          statusElement.textContent =
            `실행 실패: ${
              err.message || err
            }`;
        }

        triggerButton.disabled =
          false;

        clearMarketingProgressTarget();
      }
    }
  );
}

// ── 텍스트 복사 버튼 ────────────────────────────────────────────────────────

function initTextCopyButtons() {
  document
    .querySelectorAll(".copy-btn[data-target]")
    .forEach((btn) => {
      btn.addEventListener(
        "click",
        async () => {
          const target =
            document.getElementById(
              btn.dataset.target
            );

          if (!target) return;

          try {
            await navigator.clipboard.writeText(
              target.value
            );

            const original =
              btn.textContent;

            btn.textContent = "복사됨!";

            setTimeout(() => {
              btn.textContent = original;
            }, 1200);
          } catch (err) {
            console.error(
              "텍스트 복사 실패:",
              err
            );
          }
        }
      );
    });
}

// ── 상단 시계 ───────────────────────────────────────────────────────────────

function updateClock() {
  const now = new Date();

  const kst =
    new Intl.DateTimeFormat("ko-KR", {
      timeZone: "Asia/Seoul",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).format(now);

  const et =
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    }).format(now);

  const kstEl =
    document.getElementById("clock-kst");

  const etEl =
    document.getElementById("clock-et");

  if (kstEl) {
    kstEl.textContent = kst;
  }

  if (etEl) {
    etEl.textContent = et;
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 미국 경제지표 캘린더
// /api/calendar → Investing.com KR
// ═════════════════════════════════════════════════════════════════════════════

function initEconomicCalendar() {
  const list =
    document.getElementById("economic-calendar-list");

  if (!list) {
    console.warn(
      "[EconomicCalendar] #economic-calendar-list를 찾을 수 없습니다."
    );
    return;
  }

  let days = 7;
  let imp = "all";
  let allEvents = [];
  let source = "";

  const rangeButtons =
    document.querySelectorAll(".cal-range-btn");

  const importanceButtons =
    document.querySelectorAll(".cal-imp-btn");

  const rangeLabel =
    document.getElementById("calendar-range-label");

  const countLabel =
    document.getElementById("calendar-count");

  const updatedLabel =
    document.getElementById("calendar-updated");

  // ── 로딩 상태 ────────────────────────────────────────────────────────────

  function setLoading(message = "경제지표를 불러오는 중…") {
    if (rangeLabel) {
      rangeLabel.textContent = message;
    }

    if (countLabel) {
      countLabel.textContent = "-";
    }

    if (updatedLabel) {
      updatedLabel.textContent = "업데이트 대기";
    }

    list.innerHTML =
      `<div class="calendar-loading">` +
      `<span class="calendar-spinner"></span>` +
      `${escapeHtml(message)}` +
      `</div>`;
  }

  // ── 기간 버튼 ────────────────────────────────────────────────────────────

  rangeButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const value =
        Number(btn.dataset.days);

      if (
        !Number.isFinite(value) ||
        value <= 0
      ) {
        return;
      }

      days = value;

      rangeButtons.forEach((b) => {
        b.classList.remove("active");
      });

      btn.classList.add("active");

      void loadCalendar();
    });
  });

  // 기본 7일 선택
  rangeButtons.forEach((b) => {
    b.classList.remove("active");
  });

  const sevenButton =
    document.querySelector(
      '.cal-range-btn[data-days="7"]'
    );

  if (sevenButton) {
    sevenButton.classList.add("active");
  }

  // ── 중요도 버튼 ──────────────────────────────────────────────────────────

  importanceButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const value =
        btn.dataset.importance || "all";

      imp = value;

      importanceButtons.forEach((b) => {
        b.classList.remove("active");
      });

      btn.classList.add("active");

      renderCalendar();
    });
  });

  importanceButtons.forEach((b) => {
    b.classList.remove("active");
  });

  const allImportance =
    document.querySelector(
      '.cal-imp-btn[data-importance="all"]'
    );

  if (allImportance) {
    allImportance.classList.add("active");
  }

  // ── API 호출 ─────────────────────────────────────────────────────────────

  async function loadCalendar() {
    setLoading("경제지표를 불러오는 중…");

    try {
      const url =
        `/api/calendar?days=${encodeURIComponent(days)}`;

      console.log(
        "[EconomicCalendar] fetching:",
        url
      );

      const res =
        await fetch(url, {
          method: "GET",
          headers: {
            Accept: "application/json"
          },
          cache: "no-store"
        });

      console.log(
        "[EconomicCalendar] HTTP:",
        res.status
      );

      if (!res.ok) {
        const body =
          await res.json().catch(() => ({}));

        throw new Error(
          body.error ||
          `HTTP ${res.status}`
        );
      }

      const data =
        await res.json();

      console.log(
        "[EconomicCalendar] response:",
        data
      );

      if (
        !data ||
        typeof data !== "object"
      ) {
        throw new Error(
          "경제 캘린더 API 응답이 객체가 아닙니다."
        );
      }

      if (!Array.isArray(data.events)) {
        throw new Error(
          "경제 캘린더 API의 events 배열을 찾을 수 없습니다."
        );
      }

      // USD만 표시
      allEvents =
        data.events
          .filter(Boolean)
          .filter((event) => {
            return (
              !event.currency ||
              String(event.currency).toUpperCase() === "USD"
            );
          });

      source =
        data.source || "";

      // 중복 제거
      allEvents =
        dedupeCalendarEvents(allEvents);

      // 상단 표시
      if (rangeLabel) {
        if (data.from && data.to) {
          rangeLabel.textContent =
            `${data.from} ~ ${data.to}`;
        } else {
          rangeLabel.textContent =
            `${days}일`;
        }
      }

      if (countLabel) {
        countLabel.textContent =
          `${allEvents.length}건`;
      }

      if (updatedLabel) {
        const fetchedAt =
          data.fetched_at
            ? new Date(data.fetched_at)
            : null;

        if (
          fetchedAt &&
          !Number.isNaN(fetchedAt.getTime())
        ) {
          updatedLabel.textContent =
            `업데이트 ${fetchedAt.toLocaleTimeString(
              "ko-KR",
              {
                hour: "2-digit",
                minute: "2-digit"
              }
            )}`;
        } else {
          updatedLabel.textContent =
            "업데이트 완료";
        }
      }

      renderCalendar();

    } catch (err) {
      console.error(
        "[EconomicCalendar] 로딩 실패:",
        err
      );

      if (rangeLabel) {
        rangeLabel.textContent =
          "불러오기 실패";
      }

      if (countLabel) {
        countLabel.textContent =
          "-";
      }

      if (updatedLabel) {
        updatedLabel.textContent =
          "업데이트 실패";
      }

      list.innerHTML =
        `<div class="cal-empty">` +
        `<strong>경제지표를 불러오지 못했습니다.</strong>` +
        `<br>` +
        `${escapeHtml(
          String(err.message || err)
        )}` +
        `<br><br>` +
        `<button type="button" class="cal-retry-btn">` +
        `다시 불러오기` +
        `</button>` +
        `</div>`;

      const retryButton =
        list.querySelector(".cal-retry-btn");

      if (retryButton) {
        retryButton.addEventListener(
          "click",
          () => {
            void loadCalendar();
          }
        );
      }
    }
  }

  // ── 중복 제거 ─────────────────────────────────────────────────────────────

  function dedupeCalendarEvents(events) {
    const seen = new Set();
    const result = [];

    for (const ev of events) {
      if (
        !ev ||
        typeof ev !== "object"
      ) {
        continue;
      }

      const key = [
        ev.datetime_utc || "",
        ev.date_kst || "",
        ev.time_kst || "",
        ev.currency || "",
        String(ev.event || "")
          .trim()
          .toLowerCase()
      ].join("|");

      if (seen.has(key)) {
        continue;
      }

      seen.add(key);
      result.push(ev);
    }

    return result;
  }

  // ── 캘린더 렌더링 ─────────────────────────────────────────────────────────

  function renderCalendar() {
    const min =
      imp === "all"
        ? 0
        : normalizeImportance(imp);

    const events =
      allEvents
        .filter((e) => {
          const importance =
            normalizeImportance(
              e.importance
            );

          return importance >= min;
        })
        .sort((a, b) => {
          const da =
            Date.parse(
              a.datetime_utc || ""
            );

          const db =
            Date.parse(
              b.datetime_utc || ""
            );

          if (
            Number.isFinite(da) &&
            Number.isFinite(db)
          ) {
            return da - db;
          }

          const dateCompare =
            String(
              a.date_kst || ""
            ).localeCompare(
              String(b.date_kst || "")
            );

          if (dateCompare !== 0) {
            return dateCompare;
          }

          return String(
            a.time_kst || ""
          ).localeCompare(
            String(b.time_kst || "")
          );
        });

    if (rangeLabel) {
      rangeLabel.textContent =
        `${days}일`;
    }

    if (countLabel) {
      countLabel.textContent =
        `${events.length}건`;
    }

    if (!events.length) {
      list.innerHTML =
        '<p class="cal-empty">해당 기간에 미국 주요 지표가 없습니다.</p>';

      return;
    }

    const groups = [];
    const map = new Map();

    events.forEach((ev) => {
      const date =
        ev.date_kst || "미정";

      if (!map.has(date)) {
        const group = {
          date,
          weekday:
            ev.weekday_kst || "",
          items: []
        };

        map.set(date, group);
        groups.push(group);
      }

      map.get(date).items.push(ev);
    });

    list.innerHTML =
      groups
        .map((group) => {
          const head =
            `<div class="cal-day-head">` +
            `<span class="cal-day-date">` +
            `${formatKoDate(group.date)}` +
            `</span>` +
            `<span class="cal-day-wd">` +
            `${
              group.weekday
                ? `${escapeHtml(group.weekday)}요일`
                : ""
            }` +
            `</span>` +
            `</div>`;

          const rows =
            group.items
              .map(eventRowHtml)
              .join("");

          return (
            `<div class="cal-day">` +
            head +
            rows +
            `</div>`
          );
        })
        .join("");
  }

  // ── 중요도 정규화 ─────────────────────────────────────────────────────────

  function normalizeImportance(value) {
    if (
      value === null ||
      value === undefined ||
      value === ""
    ) {
      return 0;
    }

    if (
      typeof value === "number" &&
      Number.isFinite(value)
    ) {
      return Math.max(
        0,
        Math.min(3, Math.round(value))
      );
    }

    const text =
      String(value)
        .trim()
        .toLowerCase();

    if (
      text === "high" ||
      text === "높음" ||
      text === "3"
    ) {
      return 3;
    }

    if (
      text === "medium" ||
      text === "중간" ||
      text === "2"
    ) {
      return 2;
    }

    if (
      text === "low" ||
      text === "낮음" ||
      text === "1"
    ) {
      return 1;
    }

    const parsed =
      Number.parseInt(text, 10);

    if (!Number.isFinite(parsed)) {
      return 0;
    }

    return Math.max(
      0,
      Math.min(3, parsed)
    );
  }

  // ── 일정 행 ───────────────────────────────────────────────────────────────

  function eventRowHtml(ev) {
    const importance =
      normalizeImportance(
        ev.importance
      );

    const up =
      valueClass(
        ev.actual,
        ev.forecast
      );

    return (
      `<article class="cal-row imp-${importance}">` +

      `<time class="cal-time">` +
      `${escapeHtml(ev.time_kst || "—")}` +
      `</time>` +

      `<span class="cal-imp cal-imp-${importance}" ` +
      `aria-label="중요도 ${importance}">` +
      `<i></i><i></i><i></i>` +
      `</span>` +

      `<div class="cal-event">` +
      `<strong>` +
      `${escapeHtml(ev.event || "미정")}` +
      `</strong>` +
      `<span class="cal-ccy">` +
      `${escapeHtml(ev.currency || "USD")}` +
      `</span>` +
      `</div>` +

      `<dl class="cal-nums">` +

      `<div>` +
      `<dt>실제</dt>` +
      `<dd class="${up}">` +
      `${escapeHtml(ev.actual ?? "—")}` +
      `</dd>` +
      `</div>` +

      `<div>` +
      `<dt>예상</dt>` +
      `<dd>` +
      `${escapeHtml(ev.forecast ?? "—")}` +
      `</dd>` +
      `</div>` +

      `<div>` +
      `<dt>이전</dt>` +
      `<dd>` +
      `${escapeHtml(ev.previous ?? "—")}` +
      `</dd>` +
      `</div>` +

      `</dl>` +

      `</article>`
    );
  }

  // ── 날짜 ──────────────────────────────────────────────────────────────────

  function formatKoDate(ymd) {
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(ymd)
    ) {
      return escapeHtml(ymd);
    }

    const parts =
      ymd.split("-");

    return (
      `${Number(parts[1])}월 ` +
      `${Number(parts[2])}일`
    );
  }

  // ── 실제 / 예상 비교 ──────────────────────────────────────────────────────

  function valueClass(actual, forecast) {
    if (
      actual === null ||
      actual === undefined ||
      actual === "" ||
      forecast === null ||
      forecast === undefined ||
      forecast === ""
    ) {
      return "";
    }

    const a =
      parseFloat(
        String(actual).replace(
          /[^0-9.\-]/g,
          ""
        )
      );

    const f =
      parseFloat(
        String(forecast).replace(
          /[^0-9.\-]/g,
          ""
        )
      );

    if (
      Number.isNaN(a) ||
      Number.isNaN(f)
    ) {
      return "";
    }

    if (a > f) {
      return "is-up";
    }

    if (a < f) {
      return "is-down";
    }

    return "";
  }

  // 최초 로딩
  void loadCalendar();
}

// ── 초기화 ──────────────────────────────────────────────────────────────────

function initDashboard() {
  try {
    updateClock();

    setInterval(
      updateClock,
      1000
    );
  } catch (err) {
    console.error(
      "시계 초기화 실패:",
      err
    );
  }

  try {
    initThumbnailCopyButton();
  } catch (err) {
    console.error(
      "썸네일 버튼 초기화 실패:",
      err
    );
  }

  try {
    initKakaoButtons();
  } catch (err) {
    console.error(
      "카카오 버튼 초기화 실패:",
      err
    );
  }

  try {
    initMarketingTrigger();
  } catch (err) {
    console.error(
      "마케팅 버튼 초기화 실패:",
      err
    );
  }

  try {
    initTextCopyButtons();
  } catch (err) {
    console.error(
      "텍스트 복사 버튼 초기화 실패:",
      err
    );
  }

  try {
    initBlogProgressButtons();
  } catch (err) {
    console.error("원고 진행 버튼 초기화 실패:", err);
  }

  void loadTabs();

  // 새로 시작된 원고 작성 워크플로우의 탭이 자동으로 나타나도록 주기적 동기화
  setInterval(() => {
    void syncTabs();
  }, TAB_SYNC_INTERVAL_MS);

  try {
    initEconomicCalendar();
  } catch (err) {
    console.error(
      "경제 캘린더 초기화 실패:",
      err
    );

    const list =
      document.getElementById("economic-calendar-list");

    if (list) {
      list.innerHTML =
        `<p class="cal-empty">` +
        `경제 캘린더 초기화 오류: ` +
        `${escapeHtml(String(err.message || err))}` +
        `</p>`;
    }
  }
}

if (
  document.readyState === "loading"
) {
  document.addEventListener(
    "DOMContentLoaded",
    initDashboard,
    { once: true }
  );
} else {
  initDashboard();
}
