// POST /api/marketing-trigger
// 대시보드의 "마케팅 실행" 버튼이 호출. GitHub repository_dispatch로
// marketing_automation.yml을 원격 트리거합니다.
//
// 필요한 Cloudflare Pages 환경변수 (Settings > Environment variables):
//   GH_DISPATCH_TOKEN : repo 스코프를 가진 GitHub PAT (Secret)
//   GITHUB_OWNER      : 예) FuTrHolder
//   GITHUB_REPO       : 예) blogposting

export async function onRequestPost(context) {
  const { request, env } = context;

  try {
    const body = await request.json().catch(() => ({}));
    const postDate = body.post_date;
    const mode = body.mode;

    if (!postDate || !mode) {
      return json({ error: "post_date, mode는 필수입니다." }, 400);
    }

    // 환경변수 자체가 비어있으면 GitHub까지 가지 않고 바로 원인을 알려줍니다.
    // (Bearer undefined 형태로 호출되면 GitHub는 항상 401 Bad credentials만 반환하므로,
    //  여기서 미리 걸러내지 않으면 "토큰 누락"과 "토큰 값 오류"를 구분할 수 없습니다.)
    const missing = [];
    if (!env.GH_DISPATCH_TOKEN) missing.push("GH_DISPATCH_TOKEN");
    if (!env.GITHUB_OWNER) missing.push("GITHUB_OWNER");
    if (!env.GITHUB_REPO) missing.push("GITHUB_REPO");
    if (missing.length > 0) {
      return json(
        {
          error:
            `Cloudflare Pages 환경변수가 비어있습니다: ${missing.join(", ")}. ` +
            `Settings > Environment variables에서 값을 추가한 뒤, ` +
            `Production 환경에도 설정됐는지 확인하고 재배포(Retry deployment)하세요.`,
        },
        500
      );
    }

    const resp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "blogposting-dashboard",
        },
        body: JSON.stringify({
          event_type: "dashboard-marketing-trigger",
          client_payload: { post_date: postDate, mode },
        }),
      }
    );

    if (!resp.ok) {
      const errText = await resp.text();
      let hint = "";
      if (resp.status === 401) {
        hint =
          " → GH_DISPATCH_TOKEN 값이 잘못됐거나 만료됐을 가능성이 높습니다. " +
          "복사 시 앞뒤 공백/줄바꿈이 섞이지 않았는지, Production 환경에도 " +
          "저장돼 있는지 확인 후 새 토큰으로 교체해보세요.";
      } else if (resp.status === 404) {
        hint =
          " → GITHUB_OWNER/GITHUB_REPO 값이 정확한지, 토큰이 이 저장소에 대한 " +
          "repo 스코프 권한을 가졌는지 확인하세요.";
      }
      return json({ error: errText + hint, status: resp.status }, 502);
    }

    return json({ ok: true });
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
