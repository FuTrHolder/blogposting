"""
이메일 발송 모듈
Gmail SMTP를 사용해 완성된 블로그 원고를 이메일로 전송합니다.
Gmail 앱 비밀번호 사용 (2단계 인증 필요)
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import os
import markdown

logger = logging.getLogger(__name__)


def _md_to_html(md_content: str) -> str:
    """마크다운을 이메일용 HTML로 변환합니다."""
    html_body = markdown.markdown(
        md_content,
        extensions=["extra", "nl2br", "sane_lists"],
    )
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
    border-bottom: 3px solid #1a73e8;
    padding-bottom: 16px;
    margin-bottom: 28px;
  }}
  .header h1 {{
    color: #1a73e8;
    font-size: 22px;
    margin: 0 0 6px;
  }}
  .header p {{
    color: #888;
    font-size: 13px;
    margin: 0;
  }}
  h2 {{
    color: #1a73e8;
    border-bottom: 2px solid #e8f0fe;
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
    background: #f8f9fa;
    border-left: 4px solid #1a73e8;
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
    background: #1a73e8;
    color: white;
  }}
  tr:nth-child(even) {{
    background: #f8f9fa;
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
  .tags {{
    margin-top: 20px;
  }}
  .tag {{
    display: inline-block;
    background: #e8f0fe;
    color: #1a73e8;
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
    <h1>📈 미국 증시 일일 분석 리포트</h1>
    <p>자동 생성된 블로그 원고 | 티스토리에 직접 붙여넣기 하세요</p>
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
    ) -> dict:
        """완성된 블로그 원고를 이메일로 발송합니다."""

        msg = MIMEMultipart("related")
        msg["Subject"] = f"[블로그 원고] {title}"
        msg["From"] = self.sender
        msg["To"] = self.recipient

        # 태그 HTML
        tags_html = "".join(f'<span class="tag">#{t}</span>' for t in tags)
        tags_section = f'<div class="tags"><strong>태그:</strong> {tags_html}</div>'

        # 마크다운 → HTML 변환 (태그 포함)
        html_content = _md_to_html(content + f"\n\n---\n**태그:** {' '.join('#'+t for t in tags)}")

        # HTML 파트
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(content, "plain", "utf-8"))
        alt.attach(MIMEText(html_content, "html", "utf-8"))
        msg.attach(alt)

        # 이미지 첨부 (있을 경우)
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
            logger.info(f"썸네일 이미지 첨부: {image_path}")

        # Gmail SMTP 발송
        logger.info(f"이메일 발송 중: {self.recipient}")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(self.sender, self.password)
            server.sendmail(self.sender, self.recipient, msg.as_string())

        logger.info(f"이메일 발송 완료: [{title}]")
        return {"status": "sent", "to": self.recipient, "subject": msg["Subject"]}
