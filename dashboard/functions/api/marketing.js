// GET /api/marketing?date=&mode= → 해당 탭의 채널별 마케팅 결과 목록

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const postDate = url.searchParams.get("date");
  const mode = url.searchParams.get("mode");

  if (!postDate || !mode) {
    return json({ error: "date, mode는 필수입니다." }, 400);
  }

  try {
    if (!env.DB) {
      return json(
        { error: "D1 바인딩(DB)이 설정되지 않았습니다. Cloudflare Pages > Settings > Functions > D1 database bindings를 확인하세요." },
        500
      );
    }

    const { results } = await env.DB.prepare(
      `SELECT * FROM marketing_results WHERE post_date = ?1 AND mode = ?2 ORDER BY platform ASC`
    )
      .bind(postDate, mode)
      .all();

    return json(results);
  } catch (err) {
    return json({ error: String(err) }, 500);
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}