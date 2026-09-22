// POST /api/ingest/marketing-progress
//
// GitHub Actions의 marketing/main_marketing.py가
// 각 단계 완료 시 호출합니다.
//
// X-Ingest-Secret 헤더로 인증하고,
// D1의 marketing_progress를 `${post_date}_${mode}` 기준으로
// upsert합니다.

export async function onRequestPost(context) {
  const { request, env } = context;

  const secret = request.headers.get("X-Ingest-Secret") || "";

  if (!env.INGEST_SECRET || secret !== env.INGEST_SECRET) {
    return json({ error: "인증 실패" }, 401);
  }

  try {
    const body = await request.json();

    const postDate = body.post_date;
    const mode = body.mode;

    let step = Number(body.step);
    const status = String(body.status || "running").toLowerCase();
    const message = body.message || "";

    if (!postDate || !mode) {
      return json(
        {
          error: "post_date, mode는 필수입니다.",
        },
        400
      );
    }

    if (!Number.isFinite(step)) {
      step = 0;
    }

    step = Math.max(0, Math.min(7, Math.floor(step)));

    if (!["running", "completed", "failed"].includes(status)) {
      return json(
        {
          error:
            "status는 running, completed, failed 중 하나여야 합니다.",
        },
        400
      );
    }

    if (!env.DB) {
      return json(
        {
          error:
            "D1 바인딩(DB)이 설정되지 않았습니다.",
        },
        500
      );
    }

    const id = `${postDate}_${mode}`;

    await env.DB.prepare(
      `INSERT INTO marketing_progress
         (id, post_date, mode, step, status, message, updated_at)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, datetime('now'))
       ON CONFLICT(id) DO UPDATE SET
         step = excluded.step,
         status = excluded.status,
         message = excluded.message,
         updated_at = datetime('now')`
    )
      .bind(
        id,
        postDate,
        mode,
        step,
        status,
        message
      )
      .run();

    return json({
      ok: true,
      id,
      step,
      status,
      message,
    });
  } catch (err) {
    return json(
      {
        error: String(err),
      },
      500
    );
  }
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    },
  });
}