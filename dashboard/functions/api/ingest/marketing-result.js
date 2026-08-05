// POST /api/ingest/marketing-result
// GitHub Actions(marketing_automation.yml)가 채널별 발행 결과 + 미디어 URL(GitHub
// Release 링크)을 JSON으로 전송하는 엔드포인트. X-Ingest-Secret 헤더로 검증합니다.
//
// blog_url이 함께 전달되면 posts 테이블의 blog_url도 갱신합니다. main.py는
// 원고만 생성하고 실제 티스토리 발행 URL을 모르지만, 마케팅 파이프라인
// (tistory_crawler)은 RSS로 실제 발행된 글의 URL을 알고 있으므로, 여기서
// posts.blog_url을 채워 대시보드(카카오 박스 등)에서 사용할 수 있게 합니다.

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
    const blogUrl = body.blog_url || "";

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

    if (blogUrl) {
      const postId = `${postDate}_${mode}`;
      await env.DB.prepare(
        `UPDATE posts SET blog_url = ?1 WHERE id = ?2`
      )
        .bind(blogUrl, postId)
        .run();
    }

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
