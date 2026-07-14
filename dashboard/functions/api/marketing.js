// GET /api/marketing?date=&mode= → 해당 탭의 채널별 마케팅 결과 목록

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const postDate = url.searchParams.get("date");
  const mode = url.searchParams.get("mode");

  if (!postDate || !mode) {
    return json({ error: "date, mode는 필수입니다." }, 400);
  }

  const { results } = await env.DB.prepare(
    `SELECT * FROM marketing_results WHERE post_date = ?1 AND mode = ?2 ORDER BY platform ASC`
  )
    .bind(postDate, mode)
    .all();

  return json(results);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
