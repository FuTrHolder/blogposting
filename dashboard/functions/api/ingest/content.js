// POST /api/ingest/content
// GitHub Actions(daily_email.yml)가 블로그 본문 + 썸네일 URL(GitHub Release 링크)을
// JSON으로 전송하는 엔드포인트. X-Ingest-Secret 헤더로 검증합니다 (Cloudflare Access 불필요).

export async function onRequestPost(context) {
  const { request, env } = context;

  const secret = request.headers.get("X-Ingest-Secret") || "";
  if (!env.INGEST_SECRET || secret !== env.INGEST_SECRET) {
    return json({ error: "인증 실패" }, 401);
  }

  try {
    const body = await request.json();
    const postDate = body.post_date;
    const mode = body.mode;
    const title = body.title || "";
    const content = body.content || "";
    const tags = Array.isArray(body.tags) ? body.tags : [];
    const thumbnailUrl = body.thumbnail_url || "";

    if (!postDate || !mode) {
      return json({ error: "post_date, mode는 필수입니다." }, 400);
    }

    const id = `${postDate}_${mode}`;

    await env.DB.prepare(
      `INSERT INTO posts (id, post_date, mode, title, content, tags, thumbnail_url)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
       ON CONFLICT(id) DO UPDATE SET
         title = excluded.title,
         content = excluded.content,
         tags = excluded.tags,
         thumbnail_url = CASE WHEN excluded.thumbnail_url != ''
                              THEN excluded.thumbnail_url
                              ELSE posts.thumbnail_url END`
    )
      .bind(id, postDate, mode, title, content, JSON.stringify(tags), thumbnailUrl)
      .run();

    return json({ ok: true, id, thumbnail_url: thumbnailUrl });
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
