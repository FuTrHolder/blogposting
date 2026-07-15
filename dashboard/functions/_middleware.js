// 모든 요청 앞단에서 실행되는 미들웨어.
// /api/ingest/* (GitHub Actions 전용)만 제외하고, 나머지 전체를 HTTP Basic Auth로 막습니다.
// Cloudflare Access/Zero Trust를 전혀 쓰지 않으므로 카드 등록이 필요 없습니다.

export async function onRequest(context) {
  const { request, env, next } = context;
  const url = new URL(request.url);

  // GitHub Actions가 호출하는 수집/트리거 API는 별도의 시크릿 헤더로 보호되므로 여기서는 통과.
  if (url.pathname.startsWith("/api/ingest/")) {
    return next();
  }

  const expectedUser = env.DASHBOARD_USER || "";
  const expectedPass = env.DASHBOARD_PASSWORD || "";

  if (!expectedUser || !expectedPass) {
    return new Response(
      "대시보드 계정(DASHBOARD_USER/DASHBOARD_PASSWORD)이 아직 설정되지 않았습니다.",
      { status: 503 }
    );
  }

  const authHeader = request.headers.get("Authorization") || "";
  if (authHeader.startsWith("Basic ")) {
    try {
      const decoded = atob(authHeader.slice(6));
      const sep = decoded.indexOf(":");
      const user = decoded.slice(0, sep);
      const pass = decoded.slice(sep + 1);
      if (user === expectedUser && pass === expectedPass) {
        return next();
      }
    } catch (e) {
      // 디코딩 실패 시 그대로 401 처리
    }
  }

  return new Response("인증이 필요합니다.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="seedsup dashboard"' },
  });
}
