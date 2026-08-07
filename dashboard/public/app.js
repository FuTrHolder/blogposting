let currentDate = null;
let currentMode = null;
let currentThumbnailUrl = "";

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
  const pngBlob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  await navigator.clipboard.write([new ClipboardItem({ "image/png": pngBlob })]);
}

// ── 탭 로딩 ────────────────────────────────────────────────────────────────

async function loadTabs() {
  const res = await fetch("/api/posts");
  const posts = await res.json();

  const seen = new Set();
  const tabs = [];
  for (const p of posts) {
    const key = `${p.post_date}|${p.mode}`;
    if (seen.has(key)) continue;
    seen.add(key);
    tabs.push(p);
  }

  const nav = document.getElementById("tabs");
  nav.innerHTML = "";

  if (tabs.length === 0) {
    nav.innerHTML = '<span class="muted" style="padding:8px 16px;">아직 등록된 포스트가 없습니다.</span>';
    return;
  }

  tabs.forEach((t, i) => {
    const btn = document.createElement("button");
    btn.className = "tab-btn" + (i === 0 ? " active" : "");
    btn.textContent = `${t.post_date} ${t.mode}`;
    btn.onclick = () => selectTab(t.post_date, t.mode, btn);
    nav.appendChild(btn);
  });

  selectTab(tabs[0].post_date, tabs[0].mode, nav.querySelector(".tab-btn"));
}

function selectTab(postDate, mode, btnEl) {
  currentDate = postDate;
  currentMode = mode;

  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
  if (btnEl) btnEl.classList.add("active");

  loadContent(postDate, mode);
  loadMarketing(postDate, mode);
}

// ── 콘텐츠(본문 + 썸네일 + 태그) 로딩 ─────────────────────────────────────────

let currentBlogUrl = "";

async function loadContent(postDate, mode) {
  const res = await fetch(`/api/posts?date=${postDate}&mode=${mode}`);
  const post = await res.json();

  document.getElementById("post-title").textContent = post
    ? `${postDate} ${mode}`
    : "콘텐츠 없음";
  document.getElementById("title-text").value = post?.title || "";
  document.getElementById("content-text").value = post?.content || "";
  currentBlogUrl = post?.blog_url || "";

  // ── 태그 칩 렌더링 ─────────────────────────────────────────────────────
  let tags = [];
  try {
    tags = JSON.parse(post?.tags || "[]");
  } catch (e) {
    tags = [];
  }
  const tagsChips = document.getElementById("tags-chips");
  const tagsEmpty = document.getElementById("tags-empty");
  tagsChips.innerHTML = "";
  if (tags.length === 0) {
    tagsEmpty.hidden = false;
  } else {
    tagsEmpty.hidden = true;
    tags.forEach((t) => {
      const chip = document.createElement("button");
      chip.className = "tag-chip";
      chip.textContent = `#${t}`;
      chip.addEventListener("click", async () => {
        await navigator.clipboard.writeText(t);
        const original = chip.textContent;
        chip.textContent = "복사됨!";
        setTimeout(() => (chip.textContent = original), 1000);
      });
      tagsChips.appendChild(chip);
    });
  }
  document.getElementById("tags-copy-all-btn").onclick = async () => {
    await navigator.clipboard.writeText(tags.join(" "));
    const btn = document.getElementById("tags-copy-all-btn");
    const original = btn.textContent;
    btn.textContent = "복사됨!";
    setTimeout(() => (btn.textContent = original), 1200);
  };

  // ── 썸네일 ────────────────────────────────────────────────────────────
  const img = document.getElementById("thumb-img");
  const empty = document.getElementById("thumb-empty");
  const downloadLink = document.getElementById("thumb-download-link");
  const copyBtn = document.getElementById("thumb-copy-btn");

  currentThumbnailUrl = post?.thumbnail_url || "";

  if (currentThumbnailUrl) {
    // GitHub Release 자산은 리다이렉트+CORS 문제로 <img>에서 직접 로드 불가.
    // /proxy-image 를 통해 same-origin으로 우회해서 표시.
    img.src = `/proxy-image?url=${encodeURIComponent(currentThumbnailUrl)}`;
    img.hidden = false;
    empty.hidden = true;
    downloadLink.href = currentThumbnailUrl;
    downloadLink.style.pointerEvents = "auto";
    downloadLink.style.opacity = "1";
    copyBtn.disabled = false;
  } else {
    img.hidden = true;
    empty.hidden = false;
    downloadLink.removeAttribute("href");
    downloadLink.style.pointerEvents = "none";
    downloadLink.style.opacity = "0.5";
    copyBtn.disabled = true;
  }

  // ── 카카오스토리채널 박스 갱신 (콘텐츠 로딩 시점에 우선 반영, 이후
  //    loadMarketing()에서 실제 kakao_post 캡션이 있으면 덮어씀) ───────────
  updateKakaoBlogUrl(currentBlogUrl);
}

document.getElementById("thumb-copy-btn").addEventListener("click", async (e) => {
  const btn = e.target;
  if (!currentThumbnailUrl) return;
  const original = btn.textContent;
  try {
    await copyImageToClipboard(currentThumbnailUrl);
    btn.textContent = "복사됨!";
  } catch (err) {
    btn.textContent = "복사 실패";
    console.error(err);
  }
  setTimeout(() => (btn.textContent = original), 1400);
});

// ── 카카오스토리채널 박스 ────────────────────────────────────────────────────

function updateKakaoBlogUrl(blogUrl) {
  const urlInput = document.getElementById("kakao-blog-url");
  urlInput.value = blogUrl || "";
  urlInput.placeholder = blogUrl ? "" : "블로그 링크 없음 (마케팅 실행 후 채워짐)";
}

function updateKakaoText(kakaoText) {
  document.getElementById("kakao-text").value = kakaoText || "";
}

document.getElementById("kakao-copy-btn").addEventListener("click", async (e) => {
  const btn = e.target;
  const text = document.getElementById("kakao-text").value;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    const original = btn.textContent;
    btn.textContent = "복사됨!";
    setTimeout(() => (btn.textContent = original), 1200);
  } catch (err) {
    console.error(err);
  }
});

document.getElementById("kakao-url-copy-btn").addEventListener("click", async (e) => {
  const btn = e.target;
  const url = document.getElementById("kakao-blog-url").value;
  if (!url) return;
  await navigator.clipboard.writeText(url);
  const original = btn.textContent;
  btn.textContent = "복사됨!";
  setTimeout(() => (btn.textContent = original), 1200);
});

// ── 마케팅 결과 로딩 ─────────────────────────────────────────────────────────

// 플랫폼별 표시 이름 및 아이콘
const PLATFORM_LABELS = {
  youtube:          "▶ YouTube",
  facebook:         "📘 Facebook",
  facebook_reels:   "📘 Facebook Reels",
  instagram:        "📸 Instagram",
  instagram_reels:  "📸 Instagram Reels",
  threads:          "🧵 Threads",
  threads_reels:    "🧵 Threads Reels",
  tiktok:           "🎵 TikTok",
};

async function loadMarketing(postDate, mode) {
  const res = await fetch(`/api/marketing?date=${postDate}&mode=${mode}`);
  const results = await res.json();

  const grid = document.getElementById("marketing-grid");
  grid.innerHTML = "";

  // ── 카카오 박스: main_marketing.py가 kakao_post 캡션을 platform="kakao",
  //    status="skip"으로 marketing_results에 명시적으로 저장해둡니다
  //    (실제 발행 API는 없지만 대시보드 상단 박스에서 수동 복사용으로 표시).
  const kakaoResult = results.find((r) => r.platform === "kakao");
  if (kakaoResult && kakaoResult.content_text) {
    updateKakaoText(kakaoResult.content_text);
  }

  if (!results || results.length === 0) {
    grid.innerHTML = '<p class="muted">아직 마케팅 결과가 없습니다.</p>';
    return;
  }

  results.forEach((r) => {
    if (r.platform === "kakao") return; // 카카오는 상단 전용 박스에서 처리

    const card = document.createElement("div");
    const isTiktok = r.platform === "tiktok";
    card.className = "platform-card" + (isTiktok ? " tiktok-card" : "");

    const statusClass =
      r.status === "ok" ? "status-ok" : r.status === "skip" ? "status-skip" : "status-error";

    const platformLabel = PLATFORM_LABELS[r.platform] || r.platform;

    // GitHub Release URL은 직접 로드 시 리다이렉트+CORS 문제 → /proxy-image 우회
    const proxyUrl = (url) => url ? `/proxy-image?url=${encodeURIComponent(url)}` : "";

    let mediaHtml = "";
    if (r.video_url) {
      mediaHtml = `
        <video src="${r.video_url}" controls playsinline></video>
        <div class="btn-row">
          <a class="copy-btn" href="${r.video_url}" download>⬇ 영상 다운로드</a>
          ${isTiktok
            ? `<a class="copy-btn tiktok-upload-btn" href="https://www.tiktok.com/upload"
                 target="_blank" rel="noopener">▶ TikTok 업로드</a>`
            : ""}
        </div>`;
    } else if (r.thumbnail_url) {
      mediaHtml = `
        <img src="${proxyUrl(r.thumbnail_url)}" alt="${r.platform} 썸네일">
        <div class="btn-row">
          <button class="copy-btn" data-copy-img="${r.thumbnail_url}">이미지 복사</button>
          <a class="copy-btn" href="${r.thumbnail_url}" download>다운로드</a>
        </div>`;
    }

    // TikTok 안내 박스
    const tiktokGuideHtml = isTiktok ? `
      <div class="tiktok-guide">
        <p>📋 <strong>TikTok 수동 업로드 안내</strong></p>
        <ol>
          <li>위 영상 다운로드 버튼으로 MP4 저장</li>
          <li>▶ TikTok 업로드 버튼 클릭 → 갤러리에서 선택</li>
          <li>또는 <a href="https://www.tiktok.com/tiktokstudio/upload" target="_blank" rel="noopener">TikTok Studio</a>에서 업로드</li>
          <li>아래 캡션 복사 후 붙여넣기</li>
        </ol>
      </div>` : "";

    card.innerHTML = `
      <h3>${platformLabel}</h3>
      <span class="status-badge ${statusClass}">${r.status || "-"}</span>
      ${mediaHtml}
      <p class="muted">${r.message || ""}</p>
      ${tiktokGuideHtml}
      ${!isTiktok && r.url
        ? `<a href="${r.url}" target="_blank" rel="noopener">게시물 열기 →</a>`
        : ""}
      ${r.content_text
        ? `<div class="field-head" style="margin-top:8px;">
             <span>캡션</span>
             <button class="copy-btn" data-copy-text>복사</button>
           </div>
           <textarea rows="4" readonly>${r.content_text}</textarea>`
        : ""}
    `;

    // 캡션 복사
    const textBtn = card.querySelector("[data-copy-text]");
    if (textBtn) {
      textBtn.addEventListener("click", async () => {
        const textarea = card.querySelector("textarea");
        await navigator.clipboard.writeText(textarea.value);
        const original = textBtn.textContent;
        textBtn.textContent = "복사됨!";
        setTimeout(() => (textBtn.textContent = original), 1200);
      });
    }

    // 썸네일 이미지 복사
    const imgBtn = card.querySelector("[data-copy-img]");
    if (imgBtn) {
      imgBtn.addEventListener("click", async () => {
        const original = imgBtn.textContent;
        try {
          await copyImageToClipboard(imgBtn.dataset.copyImg);
          imgBtn.textContent = "복사됨!";
        } catch (err) {
          imgBtn.textContent = "복사 실패";
          console.error(err);
        }
        setTimeout(() => (imgBtn.textContent = original), 1400);
      });
    }

    grid.appendChild(card);
  });
}

// ── 마케팅 워크플로우 트리거 + 완료 후 자동 새로고침 ─────────────────────────
//
// GitHub Actions(repository_dispatch)는 완료 시점을 대시보드로 직접 알려주는
// 웹훅이 없으므로, 트리거 직후부터 짧은 간격으로 /api/marketing을 폴링해
// "이전에 없던 새 결과가 채워졌는지"를 감지하는 방식으로 완료를 판단합니다.
// 워크플로우 실행에는 보통 수 분이 걸리므로 최대 6분(20회 × 18초)까지 폴링합니다.

const MARKETING_POLL_INTERVAL_MS = 18000;
const MARKETING_POLL_MAX_TRIES = 20;

async function pollForMarketingCompletion(postDate, mode, previousCount) {
  const status = document.getElementById("trigger-status");

  for (let i = 1; i <= MARKETING_POLL_MAX_TRIES; i++) {
    await new Promise((resolve) => setTimeout(resolve, MARKETING_POLL_INTERVAL_MS));

    let results;
    try {
      const res = await fetch(`/api/marketing?date=${postDate}&mode=${mode}`);
      results = await res.json();
    } catch (e) {
      continue; // 일시적 네트워크 오류는 무시하고 계속 폴링
    }

    const okCount = (results || []).filter((r) => r.status === "ok").length;

    if (okCount > previousCount) {
      status.textContent = "✅ 마케팅 워크플로우 완료! 대시보드를 새로고침합니다...";
      setTimeout(() => window.location.reload(), 1200);
      return;
    }

    status.textContent =
      `마케팅 워크플로우 실행 중... (${i}/${MARKETING_POLL_MAX_TRIES}, 자동 새로고침 대기 중)`;
  }

  status.textContent =
    "워크플로우가 아직 진행 중이거나 시간이 오래 걸리고 있습니다. " +
    "잠시 후 탭을 다시 눌러 수동으로 새로고침해주세요.";
}

document.getElementById("trigger-btn").addEventListener("click", async () => {
  if (!currentDate || !currentMode) return;
  const status = document.getElementById("trigger-status");
  status.textContent = "마케팅 워크플로우 실행 요청 중...";

  // 폴링 기준선을 잡기 위해 트리거 직전의 성공 건수를 먼저 확인
  let previousCount = 0;
  try {
    const beforeRes = await fetch(`/api/marketing?date=${currentDate}&mode=${currentMode}`);
    const beforeResults = await beforeRes.json();
    previousCount = (beforeResults || []).filter((r) => r.status === "ok").length;
  } catch (e) {
    // 무시 — 0건 기준으로 폴링 계속
  }

  try {
    const res = await fetch("/api/marketing-trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ post_date: currentDate, mode: currentMode }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      status.textContent = "요청 완료! GitHub Actions 실행 중... (완료되면 자동으로 새로고침됩니다)";
      pollForMarketingCompletion(currentDate, currentMode, previousCount);
    } else {
      status.textContent = `실행 실패: ${data.error || "알 수 없는 오류"}`;
    }
  } catch (e) {
    status.textContent = `실행 실패: ${e}`;
  }
});

// ── 텍스트 복사 버튼 (제목/본문) ─────────────────────────────────────────────

document.querySelectorAll(".copy-btn[data-target]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = document.getElementById(btn.dataset.target);
    await navigator.clipboard.writeText(target.value);
    const original = btn.textContent;
    btn.textContent = "복사됨!";
    setTimeout(() => (btn.textContent = original), 1200);
  });
});

// ── 상단 시계 (KST / ET) ────────────────────────────────────────────────────

function updateClock() {
  const now = new Date();
  const kst = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(now);
  const et = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(now);
  const kstEl = document.getElementById("clock-kst");
  const etEl = document.getElementById("clock-et");
  if (kstEl) kstEl.textContent = kst;
  if (etEl) etEl.textContent = et;
}
updateClock();
setInterval(updateClock, 1000);

loadTabs();
