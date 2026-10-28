// POST /api/ingest/blog-progress
//
// GitHub Actions(daily_post.yml)와 main.py가 블로그 원고 작성 단계마다 호출합니다.
// X-Ingest-Secret 헤더로 인증하며, `${post_date}_${mode}` 기준으로 upsert 합니다.
//
//  - step < 0        : 기존 step 유지 (워크플로우 레벨 실패 보고용)
//  - keep_existing_error=true : 이미 main.py가 상세 실패 로그를 남긴 경우 덮어쓰지 않음
//  - error_log는 서버에서도 6000자로 한 번 더 절단합니다 (클라이언트 마스킹이 1차 방어).

const MAX_LOG = 6000;

export async function onRequestPost(context) {
  const { request, env } = context;

  const secret = request.headers.get("X-Ingest-Secret") || "";
  if (!env.INGEST_SECRET || secret !== env.INGEST_SECRET) {
    return json({ error: "인증 실패" }, 401);
  }

  try {
    const body = await request.json();

    const postDate = String(body.post_date || "");
    const mode = String(body.mode || "");

    if (!/^\d{4}-\d{2}-\d{2}$/.test(postDate) || !["morning", "evening"].includes(mode)) {
      return json({ error: "post_date(YYYY-MM-DD), mode(morning|evening)가 올바르지 않습니다." }, 400);
    }

    let step = Number(body.step);
    if (!Number.isFinite(step)) step = 0;
    step = Math.min(7, Math.floor(step));

    const status = String(body.status || "running").toLowerCase();
    if (!["running", "completed", "failed"].includes(status)) {
      return json({ error: "status는 running, completed, failed 중 하나여야 합니다." }, 400);
    }

    if (!env.DB) return json({ error: "D1 바인딩(DB)이 설정되지 않았습니다." }, 500);

    const message = String(body.message || "").slice(0, 500);
    const errorStep = String(body.error_step || "").slice(0, 200);
    const errorLog = String(body.error_log || "").slice(-MAX_LOG); // 끝부분(실패 직전)을 보존
    const runUrl = String(body.run_url || "").slice(0, 500);
    const keep = body.keep_existing_error ? 1 : 0;

    const id = `${postDate}_${mode}`;

    await env.DB.prepare(
      `INSERT INTO blog_progress
         (id, post_date, mode, step, status, message, error_step, error_log, run_url, updated_at)
       VALUES (?1, ?2, ?3, MAX(?4, 0), ?5, ?6, ?7, ?8, ?9, datetime('now'))
       ON CONFLICT(id) DO UPDATE SET
         step = CASE WHEN ?4 < 0 THEN blog_progress.step ELSE ?4 END,
         status = excluded.status,
         message = excluded.message,
         error_step = excluded.error_step,
         error_log = excluded.error_log,
         run_url = CASE WHEN excluded.run_url != '' THEN excluded.run_url ELSE blog_progress.run_url END,
         updated_at = datetime('now')
       WHERE ?10 = 0
          OR NOT (blog_progress.status = 'failed' AND IFNULL(blog_progress.error_log, '') != '')`
    )
      .bind(id, postDate, mode, step, status, message, errorStep, errorLog, runUrl, keep)
      .run();

    return json({ ok: true, id, step, status });
  } catch (err) {
    return json({ error: String(err) }, 500);
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}
