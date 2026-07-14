let currentDate = null;
let currentMode = null;

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
  tabs.forEach((t, i) => {
    const btn = document.createElement("button");
    btn.className = "tab-btn" + (i === 0 ? " active" : "");
    btn.textContent = `${t.post_date} ${t.mode}`;
    btn.onclick = () => selectTab(t.post_date, t.mode, btn);
    nav.appendChild(btn);
  });

  if (tabs.length > 0) {
    selectTab(tabs[0].post_date, tabs[0].mode, nav.querySelector(".tab-btn"));
  }
}

function selectTab(postDate, mode, btnEl) {
  currentDate = postDate;
  currentMode = mode;

  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
  if (btnEl) btnEl.classList.add("active");

  loadContent(postDate, mode);
  loadMarketing(postDate, mode);
}

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
  if (post?.thumbnail_key) {
    img.src = `/assets/${post.thumbnail_key}`;
    img.hidden = false;
  } else {
    img.hidden = true;
  }
}

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

    let media = "";
    if (r.video_key) {
      media = `<video src="/assets/${r.video_key}" controls></video>`;
    } else if (r.thumbnail_key) {
      media = `<img src="/assets/${r.thumbnail_key}" alt="${r.platform} 썸네일">`;
    }

    const statusClass =
      r.status === "ok" ? "status-ok" : r.status === "skip" ? "status-skip" : "status-error";

    card.innerHTML = `
      <h3>${r.platform}</h3>
      <span class="status-badge ${statusClass}">${r.status || "-"}</span>
      ${media}
      <p class="muted">${r.message || ""}</p>
      ${r.url ? `<a href="${r.url}" target="_blank" rel="noopener">게시물 열기 →</a>` : ""}
      ${r.content_text ? `<textarea rows="4" readonly>${r.content_text}</textarea>` : ""}
    `;
    grid.appendChild(card);
  });
}

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

document.querySelectorAll(".copy-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = document.getElementById(btn.dataset.target);
    await navigator.clipboard.writeText(target.value);
    const original = btn.textContent;
    btn.textContent = "복사됨!";
    setTimeout(() => (btn.textContent = original), 1200);
  });
});

loadTabs();
