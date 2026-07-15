// POST /api/ingest/content
// GitHub Actions(daily_email.yml)가 블로그 본문 + 썸네일 URL(GitHub Release 링크)을
// JSON으로 전송하는 엔드포인트. 더 이상 바이너리를 다루지 않습니다 (R2 미사용).
// Cloudflare Access 정책으로 이 경로를 Service Token 전용으로 막아두는 것을 권장합니다.

export async function onRequestPost(context) {
  const { request, env } = context;

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
