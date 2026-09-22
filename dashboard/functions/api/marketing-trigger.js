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
      return json(
        {
          error: "post_date, mode는 필수입니다."
        },
        400
      );
    }

    // ─────────────────────────────────────────────────────────────────────
    // 환경변수 확인
    // ─────────────────────────────────────────────────────────────────────

    const rawToken = env.GH_DISPATCH_TOKEN || "";
    const owner = env.GITHUB_OWNER || "";
    const repo = env.GITHUB_REPO || "";

    const missing = [];

    if (!rawToken) {
      missing.push("GH_DISPATCH_TOKEN");
    }

    if (!owner) {
      missing.push("GITHUB_OWNER");
    }

    if (!repo) {
      missing.push("GITHUB_REPO");
    }

    if (missing.length > 0) {
      return json(
        {
          error:
            `Cloudflare Pages 환경변수가 비어있습니다: ${missing.join(", ")}. ` +
            `Settings > Environment variables에서 값을 추가한 뒤, ` +
            `Production 환경에도 설정됐는지 확인하고 재배포(Retry deployment)하세요.`
        },
        500
      );
    }

    // 앞뒤 공백/줄바꿈 제거
    const token = rawToken.trim();

    const hadWhitespace =
      token !== rawToken;

    // 실제 토큰 값은 노출하지 않음
    const tokenPreview =
      token.length > 10
        ? `${token.slice(0, 6)}...${token.slice(-4)} (길이 ${token.length}자)`
        : `(길이 ${token.length}자 — 너무 짧습니다)`;

    const progressId =
      `${postDate}_${mode}`;

    // ─────────────────────────────────────────────────────────────────────
    // 중요:
    // GitHub Actions가 첫 번째 progress 신호를 보내기 전에
    // 브라우저가 새로고침되어도 "대기 중"으로 돌아가지 않도록
    // D1에 먼저 step 0 / running 상태를 기록합니다.
    // ─────────────────────────────────────────────────────────────────────

    if (!env.DB) {
      return json(
        {
          error:
            "D1 바인딩(DB)이 설정되지 않았습니다."
        },
        500
      );
    }

    await env.DB.prepare(
      `INSERT INTO marketing_progress
         (
           id,
           post_date,
           mode,
           step,
           status,
           message,
           updated_at
         )
       VALUES (
         ?1,
         ?2,
         ?3,
         0,
         'running',
         ?4,
         datetime('now')
       )
       ON CONFLICT(id) DO UPDATE SET
         step = 0,
         status = 'running',
         message = excluded.message,
         updated_at = datetime('now')`
    )
      .bind(
        progressId,
        postDate,
        mode,
        "GitHub Actions 실행 요청 중..."
      )
      .run();

    // ─────────────────────────────────────────────────────────────────────
    // GitHub Actions 실행
    // ─────────────────────────────────────────────────────────────────────

    const resp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/dispatches`,
      {
        method: "POST",

        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "blogposting-dashboard"
        },

        body: JSON.stringify({
          event_type: "dashboard-marketing-trigger",

          client_payload: {
            post_date: postDate,
            mode
          }
        })
      }
    );

    // ─────────────────────────────────────────────────────────────────────
    // GitHub dispatch 실패
    // ─────────────────────────────────────────────────────────────────────

    if (!resp.ok) {
      const errText = await resp.text();

      let hint = "";

      if (resp.status === 401) {
        hint =
          ` → 저장된 토큰 미리보기: ${tokenPreview}` +
          (
            hadWhitespace
              ? " [앞뒤 공백/줄바꿈이 있었으나 자동 제거 후에도 실패]"
              : ""
          ) +
          `. classic 토큰이면 'ghp_'로, fine-grained면 'github_pat_'로 시작해야 정상입니다. ` +
          `https://github.com/settings/tokens 에서 이 토큰이 아직 살아있는지(만료/취소 여부) 확인하세요. ` +
          `저장소(${owner}/${repo})가 조직(Organization) 소유라면, 토큰 목록에서 이 토큰 옆에 ` +
          `"Enable SSO" 또는 "Authorize"가 떠 있지 않은지도 확인하세요 ` +
          `(SSO 미인증이면 유효한 토큰도 401을 반환합니다).`;
      } else if (resp.status === 404) {
        hint =
          ` → GITHUB_OWNER(${owner})/GITHUB_REPO(${repo}) 값이 정확한지, ` +
          `토큰이 이 저장소에 대한 repo 스코프 권한을 가졌는지 확인하세요.`;
      }

      // D1에도 실패 상태 기록
      await env.DB.prepare(
        `UPDATE marketing_progress
         SET
           status = 'failed',
           message = ?1,
           updated_at = datetime('now')
         WHERE id = ?2`
      )
        .bind(
          `GitHub Actions 실행 요청 실패: HTTP ${resp.status}`,
          progressId
        )
        .run();

      return json(
        {
          error:
            errText + hint,
          status: resp.status
        },
        502
      );
    }

    // ─────────────────────────────────────────────────────────────────────
    // GitHub dispatch 성공
    // ─────────────────────────────────────────────────────────────────────

    await env.DB.prepare(
      `UPDATE marketing_progress
       SET
         status = 'running',
         message = ?1,
         updated_at = datetime('now')
       WHERE id = ?2`
    )
      .bind(
        "GitHub Actions 실행 시작. 단계 진행 신호를 기다리는 중...",
        progressId
      )
      .run();

    return json({
      ok: true,
      post_date: postDate,
      mode,
      id: progressId
    });

  } catch (err) {
    return json(
      {
        error: String(err)
      },
      500
    );
  }
}

function json(body, status = 200) {
  return new Response(
    JSON.stringify(body),
    {
      status,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store"
      }
    }
  );
}