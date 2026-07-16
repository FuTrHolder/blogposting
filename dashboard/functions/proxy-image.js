// GET /proxy-image?url=<GitHub Release 자산 URL>
// GitHub Release 자산은 CORS 헤더를 지원하지 않아 브라우저에서 fetch()로 직접
// 읽을 수 없습니다 (이미지 "복사" 기능이 실패하는 원인). 이 프록시가 서버 쪽에서
// 대신 가져와 같은 출처(same-origin)로 돌려줘서 클립보드 복사가 가능해집니다.
// 남용 방지를 위해 GitHub 관련 호스트만 허용합니다.
// _middleware.js의 Basic Auth 보호 대상에 포함되어 있어 로그인한 사람만 접근 가능합니다.

const ALLOWED_HOSTS = [
  "github.com",
  "objects.githubusercontent.com",
  "release-assets.githubusercontent.com",
  "raw.githubusercontent.com",
];

export async function onRequestGet(context) {
  const { request } = context;
  const reqUrl = new URL(request.url);
  const target = reqUrl.searchParams.get("url");

  if (!target) {
    return new Response("url 파라미터가 필요합니다.", { status: 400 });
  }

  let parsed;
  try {
    parsed = new URL(target);
  } catch (e) {
    return new Response("잘못된 URL입니다.", { status: 400 });
  }

  if (!ALLOWED_HOSTS.includes(parsed.hostname)) {
    return new Response("허용되지 않은 호스트입니다.", { status: 403 });
  }

  let upstream;
  try {
    upstream = await fetch(parsed.toString(), { redirect: "follow" });
  } catch (e) {
    return new Response("원본 이미지를 가져오지 못했습니다.", { status: 502 });
  }

  if (!upstream.ok) {
    return new Response("원본 이미지를 가져오지 못했습니다.", { status: 502 });
  }

  const headers = new Headers();
  headers.set("Content-Type", upstream.headers.get("Content-Type") || "application/octet-stream");
  headers.set("Cache-Control", "public, max-age=86400");

  return new Response(upstream.body, { headers });
}
