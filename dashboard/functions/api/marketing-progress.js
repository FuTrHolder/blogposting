// GET /api/marketing-progress?date=YYYY-MM-DD&mode=morning
//
// 현재 마케팅 워크플로우의 진행 상태를 반환합니다.

export async function onRequestGet(context) {
  const { request, env } = context;

  const url = new URL(request.url);

  const postDate = url.searchParams.get("date");
  const mode = url.searchParams.get("mode");

  if (!postDate || !mode) {
    return json(
      {
        error: "date, mode는 필수입니다.",
      },
      400
    );
  }

  try {
    if (!env.DB) {
      return json(
        {
          error:
            "D1 바인딩(DB)이 설정되지 않았습니다.",
        },
        500
      );
    }

    const row = await env.DB.prepare(
      `SELECT
         id,
         post_date,
         mode,
         step,
         status,
         message,
         updated_at
       FROM marketing_progress
       WHERE post_date = ?1
         AND mode = ?2
       LIMIT 1`
    )
      .bind(postDate, mode)
      .first();

    if (!row) {
      return json({
        exists: false,
        post_date: postDate,
        mode,
        step: 0,
        status: "idle",
        message: "",
      });
    }

    return json({
      exists: true,
      ...row,
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