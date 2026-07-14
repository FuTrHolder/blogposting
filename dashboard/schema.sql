-- Cloudflare D1 스키마: seedsup 블로그 대시보드
-- 적용 방법: wrangler d1 execute blogposting-db --remote --file=./dashboard/schema.sql

CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,              -- `${post_date}_${mode}` 형식 (예: 2026-07-14_morning)
  post_date TEXT NOT NULL,          -- 'YYYY-MM-DD' (KST 기준 작성일)
  mode TEXT NOT NULL,               -- 'morning' | 'evening'
  title TEXT,
  content TEXT,                     -- 마크다운 본문 전체
  tags TEXT,                        -- JSON 배열 문자열
  thumbnail_key TEXT,               -- R2 오브젝트 키 (예: thumbnails/2026-07-14_morning.jpg)
  blog_url TEXT,                    -- 티스토리에 실제로 올린 후 수동 기록용 (선택)
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_posts_date_mode ON posts(post_date, mode);

CREATE TABLE IF NOT EXISTS marketing_results (
  id TEXT PRIMARY KEY,              -- `${post_date}_${mode}_${platform}`
  post_date TEXT NOT NULL,
  mode TEXT NOT NULL,
  platform TEXT NOT NULL,           -- youtube | facebook | instagram | threads | kakao
  status TEXT,                      -- ok | skip | error
  message TEXT,
  url TEXT,                         -- 실제 발행된 게시물 링크
  thumbnail_key TEXT,               -- R2 오브젝트 키
  video_key TEXT,                   -- R2 오브젝트 키 (영상, 주로 youtube)
  content_text TEXT,                -- 해당 플랫폼용 캡션/카피
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_marketing_date_mode ON marketing_results(post_date, mode);
