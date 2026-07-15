// GET /api/posts            → 최근 포스트 목록 (탭 구성용)
// GET /api/posts?date=&mode= → 특정 날짜/모드 포스트 상세

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const postDate = url.searchParams.get("date");
  const mode = url.searchParams.get("mode");

  if (postDate && mode) {
    const row = await env.DB.prepare(
      `SELECT * FROM posts WHERE post_date = ?1 AND mode = ?2`
    )
      .bind(postDate, mode)
      .first();
    return json(row || null);
  }

  const { results } = await env.DB.prepare(
    `SELECT id, post_date, mode, title, thumbnail_url, created_at
     FROM posts ORDER BY post_date DESC, mode ASC LIMIT 60`
  ).all();

  return json(results);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
