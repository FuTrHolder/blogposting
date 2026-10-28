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

    // 원고 작성 워크플로우가 시작되면(blog_progress 행 생성) 아직 posts 행이 없어도
    // 탭이 바로 보이도록 합쳐서 반환합니다. blog_progress 테이블이 아직 없거나
    // 조회가 실패해도 기존 쿼리로 폴백하므로 대시보드 탭 목록은 절대 깨지지 않습니다.
    try {
      const { results } = await env.DB.prepare(
        `SELECT p.id AS id, p.post_date AS post_date, p.mode AS mode, p.title AS title,
                p.thumbnail_url AS thumbnail_url, p.created_at AS created_at,
                bp.status AS progress_status
         FROM posts p LEFT JOIN blog_progress bp ON bp.id = p.id
         UNION ALL
         SELECT b.id, b.post_date, b.mode, NULL, NULL, b.updated_at, b.status
         FROM blog_progress b WHERE b.id NOT IN (SELECT id FROM posts)
         ORDER BY post_date DESC, mode ASC LIMIT 60`
      ).all();
      return json(results);
    } catch (e) {
      console.warn("blog_progress 병합 조회 실패 — 기존 쿼리로 폴백:", String(e));
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