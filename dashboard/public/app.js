let currentDate = null;
let currentMode = null;
let currentThumbnailUrl = "";
let currentBlogUrl = "";
let contentRequestId = 0;
let marketingRequestId = 0;

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

  const seen = new Set();
  const tabs = [];

  for (const p of posts) {
    if (!p || !p.post_date || !p.mode) continue;

    const key = `${p.post_date}|${p.mode}`;

    if (seen.has(key)) continue;

    seen.add(key);
    tabs.push(p);
  }

  nav.innerHTML = "";

  if (tabs.length === 0) {
    nav.innerHTML =
      '<span class="muted" style="padding:8px 16px;">아직 등록된 포스트가 없습니다.</span>';

    return;
  }

  tabs.forEach((t, i) => {
    const btn = document.createElement("button");

    btn.className = "tab-btn" + (i === 0 ? " active" : "");
    btn.textContent = `${t.post_date} ${t.mode}`;

    btn.onclick = () =>
      selectTab(t.post_date, t.mode, btn);

    nav.appendChild(btn);
  });

  selectTab(
    tabs[0].post_date,
    tabs[0].mode,
    nav.querySelector(".tab-btn")
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
      throw new Error(
        "해당 날짜/모드의 포스트가 존재하지 않습니다."
      );
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

const MARKETING_POLL_INTERVAL_MS = 18000;
const MARKETING_POLL_MAX_TRIES = 20;

async function pollForMarketingCompletion(
  postDate,
  mode,
  previousCount
) {
  const status =
    document.getElementById("trigger-status");

  for (
    let i = 1;
    i <= MARKETING_POLL_MAX_TRIES;
    i++
  ) {
    await new Promise((resolve) =>
      setTimeout(
        resolve,
        MARKETING_POLL_INTERVAL_MS
      )
    );

    let results;

    try {
      const res = await fetch(
        `/api/marketing?date=${encodeURIComponent(postDate)}&mode=${encodeURIComponent(mode)}`,
        {
          cache: "no-store"
        }
      );

      results = await res.json();
    } catch (e) {
      continue;
    }

    const okCount =
      (results || []).filter(
        (r) => r.status === "ok"
      ).length;

    if (okCount > previousCount) {
      if (status) {
        status.textContent =
          "✅ 마케팅 워크플로우 완료! 대시보드를 새로고침합니다...";
      }

      setTimeout(
        () => window.location.reload(),
        1200
      );

      return;
    }

    if (status) {
      status.textContent =
        `마케팅 워크플로우 실행 중... ` +
        `(${i}/${MARKETING_POLL_MAX_TRIES}, ` +
        `자동 새로고침 대기 중)`;
    }
  }

  if (status) {
    status.textContent =
      "워크플로우가 아직 진행 중이거나 시간이 오래 걸리고 있습니다. " +
      "잠시 후 탭을 다시 눌러 수동으로 새로고침해주세요.";
  }
}

// ── 마케팅 실행 버튼 ─────────────────────────────────────────────────────────

function initMarketingTrigger() {
  const triggerButton =
    document.getElementById("trigger-btn");

  if (!triggerButton) return;

  triggerButton.addEventListener(
    "click",
    async () => {
      if (!currentDate || !currentMode) return;

      const status =
        document.getElementById("trigger-status");

      if (status) {
        status.textContent =
          "마케팅 워크플로우 실행 요청 중...";
      }

      let previousCount = 0;

      try {
        const beforeRes =
          await fetch(
            `/api/marketing?date=${encodeURIComponent(currentDate)}&mode=${encodeURIComponent(currentMode)}`,
            {
              cache: "no-store"
            }
          );

        const beforeResults =
          await beforeRes.json();

        previousCount =
          (beforeResults || []).filter(
            (r) => r.status === "ok"
          ).length;
      } catch (e) {
        // 0건 기준으로 계속
      }

      try {
        const res =
          await fetch(
            "/api/marketing-trigger",
            {
              method: "POST",
              headers: {
                "Content-Type":
                  "application/json"
              },
              body: JSON.stringify({
                post_date: currentDate,
                mode: currentMode
              })
            }
          );

        const data =
          await res.json();

        if (res.ok && data.ok) {
          if (status) {
            status.textContent =
              "요청 완료! GitHub Actions 실행 중... " +
              "(완료되면 자동으로 새로고침됩니다)";
          }

          void pollForMarketingCompletion(
            currentDate,
            currentMode,
            previousCount
          );
        } else {
          if (status) {
            status.textContent =
              `실행 실패: ${
                data.error ||
                "알 수 없는 오류"
              }`;
          }
        }
      } catch (e) {
        if (status) {
          status.textContent =
            `실행 실패: ${e}`;
        }
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
    document.getElementById("cal-list");

  if (!list) {
    console.warn(
      "[EconomicCalendar] #cal-list를 찾을 수 없습니다."
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

  // ── 공통 로딩 상태 ───────────────────────────────────────────────────────

  function setLoading(message = "지표 불러오는 중…") {
    const status =
      document.getElementById("cal-status");

    const meta =
      document.getElementById("cal-meta");

    if (status) {
      status.textContent = message;
    }

    if (meta) {
      meta.textContent = "";
    }

    list.innerHTML =
      `<p class="cal-empty">${escapeHtml(message)}</p>`;
  }

  // ── 기간 버튼 ─────────────────────────────────────────────────────────────

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

      rangeButtons.forEach((b) =>
        b.classList.remove("active")
      );

      btn.classList.add("active");

      void loadCalendar();
    });
  });

  const seven =
    document.querySelector(
      '.cal-range-btn[data-days="7"]'
    );

  rangeButtons.forEach((b) =>
    b.classList.remove("active")
  );

  if (seven) {
    seven.classList.add("active");
  }

  // ── 중요도 버튼 ───────────────────────────────────────────────────────────

  importanceButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const value =
        btn.dataset.imp || "all";

      imp = value;

      importanceButtons.forEach((b) =>
        b.classList.remove("active")
      );

      btn.classList.add("active");

      renderCalendar();
    });
  });

  importanceButtons.forEach((b) =>
    b.classList.remove("active")
  );

  const allImportance =
    document.querySelector(
      '.cal-imp-btn[data-imp="all"]'
    );

  if (allImportance) {
    allImportance.classList.add("active");
  }

  // ── API 호출 ─────────────────────────────────────────────────────────────

  async function loadCalendar() {
    setLoading("지표 불러오는 중…");

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

      if (
        !Array.isArray(data.events)
      ) {
        throw new Error(
          "경제 캘린더 API의 events 배열을 찾을 수 없습니다."
        );
      }

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

      allEvents =
        dedupeCalendarEvents(
          allEvents
        );

      const meta =
        document.getElementById("cal-meta");

      if (meta) {
        meta.textContent =
          `${allEvents.length}건 · ${sourceLabel(source)}`;
      }

      renderCalendar();

    } catch (err) {
      console.error(
        "[EconomicCalendar] 로딩 실패:",
        err
      );

      const status =
        document.getElementById("cal-status");

      if (status) {
        status.textContent =
          "불러오기 실패";
      }

      list.innerHTML =
        `<div class="cal-empty">` +
        `<strong>경제지표를 불러오지 못했습니다.</strong><br>` +
        `${escapeHtml(String(err.message || err))}` +
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

      /*
       * 같은 일정이 Investing.com 데이터에
       * 중복으로 들어오는 경우 제거한다.
       *
       * 실제/예상/이전 값이 업데이트되더라도
       * 같은 이벤트라면 하나로 처리한다.
       */
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

    const status =
      document.getElementById("cal-status");

    if (status) {
      status.textContent =
        `${events.length}개 일정 · 시각은 KST`;
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
        const g = {
          date,
          weekday:
            ev.weekday_kst || "",
          items: []
        };

        map.set(date, g);
        groups.push(g);
      }

      map.get(date).items.push(ev);
    });

    list.innerHTML =
      groups
        .map((g) => {
          const head =
            '<div class="cal-day-head">' +
            '<span class="cal-day-date">' +
            formatKoDate(g.date) +
            "</span>" +
            '<span class="cal-day-wd">' +
            (g.weekday
              ? g.weekday + "요일"
              : "") +
            "</span>" +
            "</div>";

          const rows =
            g.items
              .map(eventRowHtml)
              .join("");

          return (
            '<div class="cal-day">' +
            head +
            rows +
            "</div>"
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
      '<article class="cal-row imp-' +
      importance +
      '">' +

      '<time class="cal-time">' +
      escapeHtml(
        ev.time_kst || "—"
      ) +
      "</time>" +

      '<span class="cal-imp cal-imp-' +
      importance +
      '" aria-label="중요도 ' +
      importance +
      '">' +
      "<i></i><i></i><i></i>" +
      "</span>" +

      '<div class="cal-event">' +
      "<strong>" +
      escapeHtml(
        ev.event || "미정"
      ) +
      "</strong>" +
      '<span class="cal-ccy">' +
      escapeHtml(
        ev.currency || "USD"
      ) +
      "</span>" +
      "</div>" +

      '<dl class="cal-nums">' +

      "<div>" +
      "<dt>실제</dt>" +
      '<dd class="' +
      up +
      '">' +
      escapeHtml(
        ev.actual ?? "—"
      ) +
      "</dd>" +
      "</div>" +

      "<div>" +
      "<dt>예상</dt>" +
      "<dd>" +
      escapeHtml(
        ev.forecast ?? "—"
      ) +
      "</dd>" +
      "</div>" +

      "<div>" +
      "<dt>이전</dt>" +
      "<dd>" +
      escapeHtml(
        ev.previous ?? "—"
      ) +
      "</dd>" +
      "</div>" +

      "</dl>" +

      "</article>"
    );
  }

  // ── 날짜 ──────────────────────────────────────────────────────────────────

  function formatKoDate(ymd) {
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(ymd)
    ) {
      return ymd;
    }

    const p =
      ymd.split("-");

    return (
      Number(p[1]) +
      "월 " +
      Number(p[2]) +
      "일"
    );
  }

  // ── 출처 ──────────────────────────────────────────────────────────────────

  function sourceLabel(src) {
    const value =
      String(src || "").toLowerCase();

    if (
      value.indexOf("investing") !== -1
    ) {
      return "Investing.com KR";
    }

    return "Investing.com KR";
  }

  // ── 실제/예상 비교 ────────────────────────────────────────────────────────

  function valueClass(
    actual,
    forecast
  ) {
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

  void loadTabs();

  try {
    initEconomicCalendar();
  } catch (err) {
    console.error(
      "경제 캘린더 초기화 실패:",
      err
    );

    const list =
      document.getElementById("cal-list");

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