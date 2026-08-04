// GET /proxy-image?url=<GitHub Release 자산 URL>
// GitHub Release 자산은 두 가지 문제가 있어 브라우저 <img>가 직접 렌더링하지
// 못합니다:
//   1) CORS 헤더 미지원 → fetch()로 직접 읽기 불가 (이미지 "복사" 기능 실패 원인)
//   2) 리다이렉트 최종 응답의 Content-Type이 항상 application/octet-stream으로
//      내려옴 (실제 파일이 JPG/PNG여도 마찬가지) → 브라우저가 이미지로 인식 못함
// 이 프록시가 서버 쪽에서 대신 가져와 same-origin으로 돌려주고, 파일 확장자를
// 기준으로 올바른 이미지 Content-Type을 강제 지정해 두 문제를 모두 해결합니다.
// 남용 방지를 위해 GitHub 관련 호스트만 허용합니다.
// _middleware.js의 Basic Auth 보호 대상에 포함되어 있어 로그인한 사람만 접근 가능합니다.

const ALLOWED_HOSTS = [
  "github.com",
  "objects.githubusercontent.com",
  "release-assets.githubusercontent.com",
  "raw.githubusercontent.com",
];

// 확장자 → MIME 타입 매핑 (GitHub Release가 항상 octet-stream을 반환하므로
// upstream Content-Type을 신뢰하지 않고 파일명 확장자로 직접 판단)
const EXT_MIME_MAP = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  gif: "image/gif",
  webp: "image/webp",
  svg: "image/svg+xml",
  mp4: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
};

function guessMimeFromUrl(urlStr) {
  // rscd(response-content-disposition) 파라미터 안의 filename= 을 우선 확인
  // (release-assets.githubusercontent.com 리다이렉트 URL은 원본 경로가 아니라
  // 서명된 쿼리 파라미터 안에 실제 파일명이 들어있음)
  const filenameMatch = urlStr.match(/filename[%3D=]+([^&"]+)/i);
  const candidate = filenameMatch ? decodeURIComponent(filenameMatch[1]) : urlStr;
  const extMatch = candidate.match(/\.([a-zA-Z0-9]+)(?:[?&#]|$)/);
  const ext = extMatch ? extMatch[1].toLowerCase() : "";
  return EXT_MIME_MAP[ext] || null;
}

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
  let finalUrl = target;
  try {
    // manual 리다이렉트로 최종 URL을 직접 추적 (파일명이 담긴 서명 URL 확보 목적)
    let current = target;
    for (let i = 0; i < 5; i++) {
      const resp = await fetch(current, { redirect: "manual" });
      if (resp.status >= 300 && resp.status < 400) {
        const loc = resp.headers.get("Location");
        if (!loc) break;
        current = loc;
        finalUrl = loc;
        continue;
      }
      upstream = resp;
      break;
    }
    if (!upstream) {
      upstream = await fetch(current, { redirect: "follow" });
      finalUrl = current;
    }
  } catch (e) {
    return new Response("원본 이미지를 가져오지 못했습니다.", { status: 502 });
  }

  if (!upstream.ok) {
    return new Response("원본 이미지를 가져오지 못했습니다.", { status: 502 });
  }

  // 원본 URL(요청 시 전달된 target)과 최종 리다이렉트 URL 둘 다에서 확장자 추정 시도
  const guessedType =
    guessMimeFromUrl(target) || guessMimeFromUrl(finalUrl);
  const upstreamType = upstream.headers.get("Content-Type") || "";

  // upstream이 application/octet-stream 등 신뢰할 수 없는 타입이면 확장자 기반으로 강제 지정
  const isGenericType =
    !upstreamType ||
    upstreamType.startsWith("application/octet-stream") ||
    upstreamType.startsWith("text/html") ||
    upstreamType.startsWith("binary/");

  const finalType = isGenericType
    ? (guessedType || "application/octet-stream")
    : upstreamType;

  const headers = new Headers();
  headers.set("Content-Type", finalType);
  headers.set("Cache-Control", "public, max-age=86400");

  return new Response(upstream.body, { headers });
}
