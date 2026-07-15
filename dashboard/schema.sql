-- Cloudflare D1 스키마: seedsup 블로그 대시보드
-- 저장 방식: 이미지/영상은 GitHub Release에 업로드되고, 여기에는 그 공개 URL만 저장합니다.
-- (R2를 사용하지 않으므로 *_key 대신 *_url 컬럼을 씁니다)
-- 적용 방법: D1 데이터베이스 > Console 탭에 이 파일 내용을 붙여넣고 실행

CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,              -- `${post_date}_${mode}` 형식 (예: 2026-07-14_morning)
  post_date TEXT NOT NULL,          -- 'YYYY-MM-DD' (KST 기준 작성일)
  mode TEXT NOT NULL,               -- 'morning' | 'evening'
  title TEXT,
  content TEXT,                     -- 마크다운 본문 전체
  tags TEXT,                        -- JSON 배열 문자열
  thumbnail_url TEXT,               -- GitHub Release 공개 다운로드 URL
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
  thumbnail_url TEXT,               -- GitHub Release 공개 다운로드 URL
  video_url TEXT,                   -- GitHub Release 공개 다운로드 URL (영상, 주로 youtube)
  content_text TEXT,                -- 해당 플랫폼용 캡션/카피
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_marketing_date_mode ON marketing_results(post_date, mode);
