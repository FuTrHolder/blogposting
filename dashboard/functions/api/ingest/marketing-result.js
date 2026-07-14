// POST /api/ingest/marketing-result
// GitHub Actions(marketing_automation.yml)가 채널별 발행 결과를 전송하는 엔드포인트.
// Cloudflare Access 정책으로 이 경로를 Service Token 전용으로 막아두는 것을 권장합니다.

export async function onRequestPost(context) {
  const { request, env } = context;

  try {
    const form = await request.formData();
    const postDate = form.get("post_date");
    const mode = form.get("mode");
    const platform = form.get("platform");
    const status = form.get("status") || "";
    const message = form.get("message") || "";
    const url = form.get("url") || "";
    const contentText = form.get("content_text") || "";
    const thumbnail = form.get("thumbnail");
    const video = form.get("video");

    if (!postDate || !mode || !platform) {
      return json({ error: "post_date, mode, platform은 필수입니다." }, 400);
    }

    const id = `${postDate}_${mode}_${platform}`;
    let thumbnailKey = null;
    let videoKey = null;

    if (thumbnail && typeof thumbnail === "object" && thumbnail.size > 0) {
      const ext = (thumbnail.type || "").includes("png") ? "png" : "jpg";
      thumbnailKey = `marketing/${id}.${ext}`;
      await env.ASSETS.put(thumbnailKey, await thumbnail.arrayBuffer(), {
        httpMetadata: { contentType: thumbnail.type || "image/jpeg" },
      });
    }

    if (video && typeof video === "object" && video.size > 0) {
      videoKey = `marketing/${id}.mp4`;
      await env.ASSETS.put(videoKey, await video.arrayBuffer(), {
        httpMetadata: { contentType: "video/mp4" },
      });
    }

    await env.DB.prepare(
      `INSERT INTO marketing_results
         (id, post_date, mode, platform, status, message, url, thumbnail_key, video_key, content_text)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
       ON CONFLICT(id) DO UPDATE SET
         status = excluded.status,
         message = excluded.message,
         url = excluded.url,
         thumbnail_key = COALESCE(excluded.thumbnail_key, marketing_results.thumbnail_key),
         video_key = COALESCE(excluded.video_key, marketing_results.video_key),
         content_text = excluded.content_text`
    )
      .bind(id, postDate, mode, platform, status, message, url, thumbnailKey, videoKey, contentText)
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
