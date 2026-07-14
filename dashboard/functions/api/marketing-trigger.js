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
      return json({ error: errText }, 502);
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
