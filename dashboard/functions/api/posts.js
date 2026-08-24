// GET /api/posts            → 최근 포스트 목록 (탭 구성용)
// GET /api/posts?date=&mode= → 특정 날짜/모드 포스트 상세

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const postDate = url.searchParams.get("date");
  const mode = url.searchParams.get("mode");

  try {
    if (!env.DB) {
      return json(
        { error: "D1 바인딩(DB)이 설정되지 않았습니다. Cloudflare Pages > Settings > Functions > D1 database bindings를 확인하세요." },
        500
      );
    }

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
  } catch (err) {
    // 기존에는 이 지점의 예외가 그대로 흘러나가 브라우저에서 res.json() 파싱이
    // 깨지고, app.js에 catch가 없어 화면이 아무 안내 없이 빈 상태로 멈추는
    // 원인이었습니다. 항상 JSON + 상태코드로 응답해 프론트에서 원인을 표시할
    // 수 있게 합니다.
    return json({ error: String(err) }, 500);
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}