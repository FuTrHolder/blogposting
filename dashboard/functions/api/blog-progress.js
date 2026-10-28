// GET /api/blog-progress?date=YYYY-MM-DD&mode=morning
// 블로그 원고 작성 워크플로우의 현재 진행 상태를 반환합니다. (Basic Auth 보호 대상)
// 테이블이 아직 없어도(schema.sql 미적용) 대시보드가 깨지지 않도록 exists:false 로 응답합니다.

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const postDate = url.searchParams.get("date");
  const mode = url.searchParams.get("mode");

  if (!postDate || !mode) {
    return json({ error: "date, mode는 필수입니다." }, 400);
  }

  try {
    if (!env.DB) return json({ error: "D1 바인딩(DB)이 설정되지 않았습니다." }, 500);

    const row = await env.DB.prepare(
      `SELECT id, post_date, mode, step, status, message,
              error_step, error_log, run_url, updated_at
       FROM blog_progress WHERE post_date = ?1 AND mode = ?2 LIMIT 1`
    )
      .bind(postDate, mode)
      .first();

    if (!row) return json({ exists: false, post_date: postDate, mode });
    return json({ exists: true, ...row });
  } catch (err) {
    const msg = String(err);
    if (/no such table/i.test(msg)) {
      return json({ exists: false, unavailable: true, post_date: postDate, mode });
    }
    return json({ error: msg }, 500);
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}
