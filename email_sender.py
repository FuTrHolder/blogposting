"""
이메일 발송 모듈
Gmail SMTP를 사용해 완성된 블로그 원고를 이메일로 전송합니다.
Gmail 앱 비밀번호 사용 (2단계 인증 필요)

mode:
  morning : 오전 마감 리뷰 → 파란 계열 헤더
  evening : 저녁 프리마켓 & 이슈 → 보라/남색 계열 헤더
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import os
import markdown

logger = logging.getLogger(__name__)

# 모드별 UI 설정
MODE_CONFIG = {
    "morning": {
        "label": "📊 전일 마감 리뷰",
        "accent": "#1a73e8",       # 파란색
        "accent_light": "#e8f0fe",
        "th_bg": "#1a73e8",
        "subtitle": "미국 전일 증시 마감 분석 | 티스토리 원고",
        "emoji": "📈",
    },
    "evening": {
        "label": "🌙 프리마켓 & 당일 이슈",
        "accent": "#5c35d9",       # 보라색
        "accent_light": "#ede7f6",
        "th_bg": "#5c35d9",
        "subtitle": "오늘 밤 미국 증시 프리뷰 & 이슈 정리 | 티스토리 원고",
        "emoji": "🔔",
    },
}


def _md_to_html(md_content: str, mode: str = "morning") -> str:
    """마크다운을 모드별 스타일이 적용된 이메일 HTML로 변환합니다."""
    cfg = MODE_CONFIG.get(mode, MODE_CONFIG["morning"])
    html_body = markdown.markdown(
        md_content,
        extensions=["extra", "nl2br", "sane_lists"],
    )
    accent = cfg["accent"]
    accent_light = cfg["accent_light"]
    th_bg = cfg["th_bg"]

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    line-height: 1.8;
    color: #333;
    max-width: 720px;
    margin: 0 auto;
    padding: 24px;
    background: #f9f9f9;
  }}
  .container {{
    background: #fff;
    border-radius: 12px;
    padding: 36px 40px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
  }}
  .header {{
    border-bottom: 3px solid {accent};
    padding-bottom: 16px;
    margin-bottom: 28px;
  }}
  .header h1 {{
    color: {accent};
    font-size: 22px;
    margin: 0 0 6px;
  }}
  .header p {{
    color: #888;
    font-size: 13px;
    margin: 0;
  }}
  .mode-badge {{
    display: inline-block;
    background: {accent_light};
    color: {accent};
    padding: 3px 12px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    margin-bottom: 12px;
  }}
  h2 {{
    color: {accent};
    border-bottom: 2px solid {accent_light};
    padding-bottom: 8px;
    margin-top: 32px;
    font-size: 18px;
  }}
  h3 {{
    color: #34495e;
    margin-top: 20px;
    font-size: 16px;
  }}
  blockquote {{
    background: {accent_light};
    border-left: 4px solid {accent};
    padding: 12px 16px;
    margin: 16px 0;
    border-radius: 4px;
    color: #555;
  }}
  table {{
    border-collapse: collapse;
    width: 100%;
    margin: 16px 0;
  }}
  td, th {{
    border: 1px solid #ddd;
    padding: 10px 14px;
    text-align: left;
  }}
  th {{
    background: {th_bg};
    color: white;
  }}
  tr:nth-child(even) {{
    background: {accent_light};
  }}
  strong {{ color: #222; }}
  .footer {{
    margin-top: 36px;
    padding-top: 16px;
    border-top: 1px solid #eee;
    font-size: 12px;
    color: #aaa;
    text-align: center;
  }}
  .tags {{ margin-top: 20px; }}
  .tag {{
    display: inline-block;
    background: {accent_light};
    color: {accent};
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 12px;
    margin: 2px;
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="mode-badge">{cfg['label']}</div>
    <h1>{cfg['emoji']} 미국 증시 블로그 원고</h1>
    <p>{cfg['subtitle']}</p>
  </div>
  {html_body}
  <div class="footer">
    이 이메일은 자동으로 생성되었습니다. © 미국 증시 블로그 자동화
  </div>
</div>
</body>
</html>"""


class EmailSender:
    def __init__(
        self,
        gmail_address: str,
        gmail_app_password: str,
        recipient_email: str,
    ):
        self.sender = gmail_address
        self.password = gmail_app_password
        self.recipient = recipient_email

    def send(
        self,
        title: str,
        content: str,
        tags: list[str],
        image_path: str | None = None,
        mode: str = "morning",
    ) -> dict:
        """완성된 블로그 원고를 이메일로 발송합니다."""
        cfg = MODE_CONFIG.get(mode, MODE_CONFIG["morning"])

        msg = MIMEMultipart("related")
        msg["Subject"] = f"[{cfg['label']}] {title}"
        msg["From"] = self.sender
        msg["To"] = self.recipient

        # 마크다운 → HTML 변환 (태그 포함)
        tags_md = " ".join(f"#{t}" for t in tags)
        html_content = _md_to_html(
            content + f"\n\n---\n**태그:** {tags_md}",
            mode=mode,
        )

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(content, "plain", "utf-8"))
        alt.attach(MIMEText(html_content, "html", "utf-8"))
        msg.attach(alt)

        # 이미지 첨부
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                img = MIMEImage(f.read())
                img.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=os.path.basename(image_path),
                )
                img.add_header("Content-ID", "<thumbnail>")
            msg.attach(img)
            logger.info(f"썸네일 첨부: {image_path}")

        logger.info(f"이메일 발송 중 [{mode}]: {self.recipient}")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(self.sender, self.password)
            server.sendmail(self.sender, self.recipient, msg.as_string())

        logger.info(f"이메일 발송 완료: [{title}]")
        return {"status": "sent", "to": self.recipient, "subject": msg["Subject"]}
