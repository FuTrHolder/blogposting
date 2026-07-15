let currentDate = null;
let currentMode = null;
let currentThumbnailUrl = "";

// ── 이미지를 실제로 클립보드에 복사 (PNG로 변환해서 어디든 붙여넣기 가능하게) ──────
async function copyImageToClipboard(url) {
  const resp = await fetch(url);
  const blob = await resp.blob();
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  canvas.getContext("2d").drawImage(bitmap, 0, 0);
  const pngBlob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  await navigator.clipboard.write([new ClipboardItem({ "image/png": pngBlob })]);
}

async function flashButton(btn, successText, failText) {
  const original = btn.textContent;
  try {
    await Promise.resolve();
    btn.textContent = successText;
  } catch (e) {
    btn.textContent = failText;
  }
  setTimeout(() => (btn.textContent = original), 1400);
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

// ── 콘텐츠(본문 + 썸네일) 로딩 ───────────────────────────────────────────────

async function loadContent(postDate, mode) {
  const res = await fetch(`/api/posts?date=${postDate}&mode=${mode}`);
  const post = await res.json();

  document.getElementById("post-title").textContent = post
    ? `${postDate} ${mode}`
    : "콘텐츠 없음";
  document.getElementById("title-text").value = post?.title || "";
  document.getElementById("content-text").value = post?.content || "";

  let tags = [];
  try {
    tags = JSON.parse(post?.tags || "[]");
  } catch (e) {
    tags = [];
  }
  document.getElementById("tags-text").value = tags.map((t) => `#${t}`).join(" ");

  const img = document.getElementById("thumb-img");
  const empty = document.getElementById("thumb-empty");
  const downloadLink = document.getElementById("thumb-download-link");
  const copyBtn = document.getElementById("thumb-copy-btn");

  currentThumbnailUrl = post?.thumbnail_url || "";

  if (currentThumbnailUrl) {
    img.src = currentThumbnailUrl;
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

// ── 마케팅 결과 로딩 ─────────────────────────────────────────────────────────

async function loadMarketing(postDate, mode) {
  const res = await fetch(`/api/marketing?date=${postDate}&mode=${mode}`);
  const results = await res.json();

  const grid = document.getElementById("marketing-grid");
  grid.innerHTML = "";

  if (!results || results.length === 0) {
    grid.innerHTML = '<p class="muted">아직 마케팅 결과가 없습니다.</p>';
    return;
  }

  results.forEach((r) => {
    const card = document.createElement("div");
    card.className = "platform-card";

    const statusClass =
      r.status === "ok" ? "status-ok" : r.status === "skip" ? "status-skip" : "status-error";

    let mediaHtml = "";
    if (r.video_url) {
      mediaHtml = `
        <video src="${r.video_url}" controls></video>
        <div class="btn-row">
          <a class="copy-btn" href="${r.video_url}" download>영상 다운로드</a>
        </div>`;
    } else if (r.thumbnail_url) {
      mediaHtml = `
        <img src="${r.thumbnail_url}" alt="${r.platform} 썸네일">
        <div class="btn-row">
          <button class="copy-btn" data-copy-img="${r.thumbnail_url}">이미지 복사</button>
          <a class="copy-btn" href="${r.thumbnail_url}" download>다운로드</a>
        </div>`;
    }

    card.innerHTML = `
      <h3>${r.platform}</h3>
      <span class="status-badge ${statusClass}">${r.status || "-"}</span>
      ${mediaHtml}
      <p class="muted">${r.message || ""}</p>
      ${r.url ? `<a href="${r.url}" target="_blank" rel="noopener">게시물 열기 →</a>` : ""}
      ${
        r.content_text
          ? `<div class="field-head" style="margin-top:8px;">
               <span>캡션</span>
               <button class="copy-btn" data-copy-text>복사</button>
             </div>
             <textarea rows="4" readonly>${r.content_text}</textarea>`
          : ""
      }
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

// ── 마케팅 워크플로우 트리거 ─────────────────────────────────────────────────

document.getElementById("trigger-btn").addEventListener("click", async () => {
  if (!currentDate || !currentMode) return;
  const status = document.getElementById("trigger-status");
  status.textContent = "마케팅 워크플로우 실행 요청 중...";

  try {
    const res = await fetch("/api/marketing-trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ post_date: currentDate, mode: currentMode }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      status.textContent = "요청 완료! GitHub Actions가 곧 실행됩니다. (완료까지 몇 분 소요, 이 탭을 다시 눌러 새로고침하세요)";
    } else {
      status.textContent = `실행 실패: ${data.error || "알 수 없는 오류"}`;
    }
  } catch (e) {
    status.textContent = `실행 실패: ${e}`;
  }
});

// ── 텍스트 복사 버튼 (제목/본문/태그) ────────────────────────────────────────

document.querySelectorAll(".copy-btn[data-target]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = document.getElementById(btn.dataset.target);
    await navigator.clipboard.writeText(target.value);
    const original = btn.textContent;
    btn.textContent = "복사됨!";
    setTimeout(() => (btn.textContent = original), 1200);
  });
});

loadTabs();
