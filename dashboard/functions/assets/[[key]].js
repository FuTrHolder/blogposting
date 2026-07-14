// GET /assets/<key>
// R2에 저장된 썸네일/영상을 그대로 서빙합니다.
// Meta(Threads/Instagram/Facebook) 서버가 이 URL로 직접 접근해 이미지를 가져가야 하므로
// Cloudflare Access 정책에서 이 경로(/assets/*)는 반드시 "Bypass"로 예외 처리하세요.

export async function onRequestGet(context) {
  const { params, env } = context;
  const key = Array.isArray(params.key) ? params.key.join("/") : params.key;

  if (!key) {
    return new Response("Not found", { status: 404 });
  }

  const object = await env.ASSETS.get(key);
  if (!object) {
    return new Response("Not found", { status: 404 });
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("Cache-Control", "public, max-age=31536000, immutable");

  return new Response(object.body, { headers });
}
