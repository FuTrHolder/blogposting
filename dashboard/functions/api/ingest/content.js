// POST /api/ingest/content
// GitHub Actions(daily_email.yml)가 블로그 본문 + 썸네일을 전송하는 엔드포인트.
// Cloudflare Access 정책으로 이 경로를 Service Token 전용으로 막아두는 것을 권장합니다.

export async function onRequestPost(context) {
  const { request, env } = context;

  try {
    const form = await request.formData();
    const postDate = form.get("post_date");
    const mode = form.get("mode");
    const title = form.get("title") || "";
    const content = form.get("content") || "";
    const tags = form.get("tags") || "[]";
    const image = form.get("image");

    if (!postDate || !mode) {
      return json({ error: "post_date, mode는 필수입니다." }, 400);
    }

    const id = `${postDate}_${mode}`;
    let thumbnailKey = null;

    if (image && typeof image === "object" && image.size > 0) {
      const ext = (image.type || "").includes("png") ? "png" : "jpg";
      thumbnailKey = `thumbnails/${id}.${ext}`;
      await env.ASSETS.put(thumbnailKey, await image.arrayBuffer(), {
        httpMetadata: { contentType: image.type || "image/jpeg" },
      });
    }

    await env.DB.prepare(
      `INSERT INTO posts (id, post_date, mode, title, content, tags, thumbnail_key)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
       ON CONFLICT(id) DO UPDATE SET
         title = excluded.title,
         content = excluded.content,
         tags = excluded.tags,
         thumbnail_key = COALESCE(excluded.thumbnail_key, posts.thumbnail_key)`
    )
      .bind(id, postDate, mode, title, content, tags, thumbnailKey)
      .run();

    return json({ ok: true, id, thumbnail_key: thumbnailKey });
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
