// POST /api/ingest/cleanup
// GitHub Actions(cleanup_assets.yml)가 매일 호출하는 D1 데이터 정리 엔드포인트.
// post_date가 보관 기간(기본 7일)보다 오래된 posts / marketing_results 행을 삭제해,
// 대시보드 상단 탭이 무한정 쌓이는 것을 방지합니다.
// GitHub Release 자산 정리(cleanup_release_assets.py)와 같은 주기로 함께 실행됩니다.
// /api/ingest/* 경로라 X-Ingest-Secret 헤더로 검증하고, Basic Auth는 우회합니다.

export async function onRequestPost(context) {
  const { request, env } = context;

  const secret = request.headers.get("X-Ingest-Secret") || "";
  if (!env.INGEST_SECRET || secret !== env.INGEST_SECRET) {
    return json({ error: "인증 실패" }, 401);
  }

  try {
    const body = await request.json().catch(() => ({}));
    const retentionDays = Number(body.retention_days) > 0 ? Number(body.retention_days) : 7;

    const cutoff = new Date();
    cutoff.setUTCDate(cutoff.getUTCDate() - retentionDays);
    const cutoffStr = cutoff.toISOString().slice(0, 10); // "YYYY-MM-DD"

    const postsResult = await env.DB.prepare(
      `DELETE FROM posts WHERE post_date < ?1`
    )
      .bind(cutoffStr)
      .run();

    const marketingResult = await env.DB.prepare(
      `DELETE FROM marketing_results WHERE post_date < ?1`
    )
      .bind(cutoffStr)
      .run();

    return json({
      ok: true,
      cutoff: cutoffStr,
      retention_days: retentionDays,
      deleted_posts: postsResult.meta?.changes ?? null,
      deleted_marketing_results: marketingResult.meta?.changes ?? null,
    });
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
