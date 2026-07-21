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
    const rawToken = env.GH_DISPATCH_TOKEN || "";
    const owner = env.GITHUB_OWNER || "";
    const repo = env.GITHUB_REPO || "";

    const missing = [];
    if (!rawToken) missing.push("GH_DISPATCH_TOKEN");
    if (!owner) missing.push("GITHUB_OWNER");
    if (!repo) missing.push("GITHUB_REPO");
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

    // 앞뒤 공백/줄바꿈이 섞여 들어온 경우를 자동으로 제거 (흔한 복붙 실수 방어).
    const token = rawToken.trim();
    const hadWhitespace = token !== rawToken;

    // 실제 값은 절대 노출하지 않고, 진단에 필요한 최소 정보만 마스킹해서 보여줍니다.
    const tokenPreview =
      token.length > 10
        ? `${token.slice(0, 6)}...${token.slice(-4)} (길이 ${token.length}자)`
        : `(길이 ${token.length}자 — 너무 짧습니다)`;

    const resp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
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
          ` → 저장된 토큰 미리보기: ${tokenPreview}` +
          (hadWhitespace ? " [앞뒤 공백/줄바꿈이 있었으나 자동 제거 후에도 실패]" : "") +
          `. classic 토큰이면 'ghp_'로, fine-grained면 'github_pat_'로 시작해야 정상입니다. ` +
          `https://github.com/settings/tokens 에서 이 토큰이 아직 살아있는지(만료/취소 여부) 확인하세요. ` +
          `저장소(${owner}/${repo})가 조직(Organization) 소유라면, 토큰 목록에서 이 토큰 옆에 ` +
          `"Enable SSO" 또는 "Authorize"가 떠 있지 않은지도 확인하세요 (SSO 미인증이면 유효한 토큰도 401을 반환합니다).`;
      } else if (resp.status === 404) {
        hint =
          ` → GITHUB_OWNER(${owner})/GITHUB_REPO(${repo}) 값이 정확한지, 토큰이 이 저장소에 대한 ` +
          `repo 스코프 권한을 가졌는지 확인하세요.`;
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
