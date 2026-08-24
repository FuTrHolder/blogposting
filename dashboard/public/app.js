*** Begin Patch
*** Update File: dashboard/public/app.js
@@
 let currentDate = null;
 let currentMode = null;
 let currentThumbnailUrl = "";
+let currentBlogUrl = "";
+let contentRequestId = 0;
+let marketingRequestId = 0;
@@
 async function loadTabs() {
   const nav = document.getElementById("tabs");
+  if (!nav) return;
@@
   selectTab(tabs[0].post_date, tabs[0].mode, nav.querySelector(".tab-btn"));
 }
+
+// ── 포스트 탭 선택 → 본문/썸네일/마케팅 동시 로딩 ────────────────────────────
+function selectTab(postDate, mode, btn) {
+  currentDate = postDate;
+  currentMode = mode;
+
+  document.querySelectorAll("#tabs .tab-btn").forEach((b) => {
+    b.classList.remove("active");
+  });
+  if (btn) btn.classList.add("active");
+
+  resetPostView();
+
+  // 본문과 마케팅은 독립적으로 로딩한다.
+  // 한쪽 API 실패가 다른 쪽 화면을 막지 않도록 Promise.all을 사용하지 않는다.
+  void loadContent(postDate, mode);
+  void loadMarketing(postDate, mode);
+}
+
+function resetPostView() {
+  const postTitle = document.getElementById("post-title");
+  const titleText = document.getElementById("title-text");
+  const contentText = document.getElementById("content-text");
+  const tagsChips = document.getElementById("tags-chips");
+  const tagsEmpty = document.getElementById("tags-empty");
+  const thumbImg = document.getElementById("thumb-img");
+  const thumbEmpty = document.getElementById("thumb-empty");
+  const thumbDownload = document.getElementById("thumb-download-link");
+  const thumbCopy = document.getElementById("thumb-copy-btn");
+  const marketingGrid = document.getElementById("marketing-grid");
+  const kakaoText = document.getElementById("kakao-text");
+
+  if (postTitle) postTitle.textContent = "불러오는 중…";
+  if (titleText) titleText.value = "";
+  if (contentText) contentText.value = "포스트 본문을 불러오는 중…";
+  if (tagsChips) tagsChips.innerHTML = "";
+  if (tagsEmpty) tagsEmpty.hidden = false;
+
+  currentThumbnailUrl = "";
+  currentBlogUrl = "";
+
+  if (thumbImg) {
+    thumbImg.removeAttribute("src");
+    thumbImg.hidden = true;
+  }
+  if (thumbEmpty) thumbEmpty.hidden = false;
+  if (thumbDownload) {
+    thumbDownload.removeAttribute("href");
+    thumbDownload.style.pointerEvents = "none";
+    thumbDownload.style.opacity = "0.5";
+  }
+  if (thumbCopy) thumbCopy.disabled = true;
+
+  updateKakaoBlogUrl("");
+  if (kakaoText) kakaoText.value = "";
+  if (marketingGrid) {
+    marketingGrid.innerHTML = '<p class="muted">마케팅 결과를 불러오는 중…</p>';
+  }
+}
@@
 async function loadContent(postDate, mode) {
+  const requestId = ++contentRequestId;
   let post;
   try {
     const res = await fetch(`/api/posts?date=${postDate}&mode=${mode}`);
@@
     }
     post = await res.json();
+    if (!post) {
+      throw new Error("해당 날짜/모드의 포스트가 존재하지 않습니다.");
+    }
   } catch (err) {
+    if (requestId !== contentRequestId) return;
     console.error("포스트 본문 로딩 실패:", err);
@@
     return;
   }

+  // 빠르게 다른 탭을 클릭했을 때 이전 요청의 늦은 응답이
+  // 현재 선택된 포스트를 덮어쓰지 않도록 방지한다.
+  if (requestId !== contentRequestId) return;
+
+  // posts.js의 상세 조회는 SELECT *이므로 blog_url을 그대로 받을 수 있다.
+  currentBlogUrl = post?.blog_url || "";
+
   // ── 태그 칩 렌더링 ─────────────────────────────────────────────────────
@@
 async function loadMarketing(postDate, mode) {
   const grid = document.getElementById("marketing-grid");
+  if (!grid) return;
+  const requestId = ++marketingRequestId;
@@
     }
     results = await res.json();
+    if (!Array.isArray(results)) {
+      throw new Error("마케팅 API가 배열 형식의 데이터를 반환하지 않았습니다.");
+    }
   } catch (err) {
+    if (requestId !== marketingRequestId) return;
     console.error("마케팅 결과 로딩 실패:", err);
@@
     return;
   }
+
+  if (requestId !== marketingRequestId) return;

   grid.innerHTML = "";
@@
   function eventRowHtml(ev) {
@@
-      esc(ev.time_kst || "—") +
+      escapeHtml(ev.time_kst || "—") +
@@
-      esc(ev.event) +
+      escapeHtml(ev.event) +
@@
-      esc(ev.currency || "USD") +
+      escapeHtml(ev.currency || "USD") +
@@
-      esc(ev.actual || "—") +
+      escapeHtml(ev.actual || "—") +
@@
-      esc(ev.forecast || "—") +
+      escapeHtml(ev.forecast || "—") +
@@
-      esc(ev.previous || "—") +
+      escapeHtml(ev.previous || "—") +
@@
-  function esc(s) {
-    return String(s)
-      .replace(/&/g, "&")
-      .replace(/</g, "<")
-      .replace(/>/g, ">")
-      .replace(/"/g, """);
-  }
-
   loadCalendar();
 }
*** End Patch
