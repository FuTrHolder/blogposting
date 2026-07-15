// POST /api/ingest/marketing-result
// GitHub Actions(marketing_automation.yml)가 채널별 발행 결과 + 미디어 URL(GitHub
// Release 링크)을 JSON으로 전송하는 엔드포인트. X-Ingest-Secret 헤더로 검증합니다.

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
    const platform = body.platform;
    const status = body.status || "";
    const message = body.message || "";
    const url = body.url || "";
    const contentText = body.content_text || "";
    const thumbnailUrl = body.thumbnail_url || "";
    const videoUrl = body.video_url || "";

    if (!postDate || !mode || !platform) {
      return json({ error: "post_date, mode, platform은 필수입니다." }, 400);
    }

    const id = `${postDate}_${mode}_${platform}`;

    await env.DB.prepare(
      `INSERT INTO marketing_results
         (id, post_date, mode, platform, status, message, url, thumbnail_url, video_url, content_text)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
       ON CONFLICT(id) DO UPDATE SET
         status = excluded.status,
         message = excluded.message,
         url = excluded.url,
         thumbnail_url = CASE WHEN excluded.thumbnail_url != ''
                              THEN excluded.thumbnail_url
                              ELSE marketing_results.thumbnail_url END,
         video_url = CASE WHEN excluded.video_url != ''
                          THEN excluded.video_url
                          ELSE marketing_results.video_url END,
         content_text = excluded.content_text`
    )
      .bind(id, postDate, mode, platform, status, message, url, thumbnailUrl, videoUrl, contentText)
      .run();

    return json({ ok: true, id });
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
