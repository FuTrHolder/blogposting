"""
티스토리 Playwright 자동 업로드 모듈
Open API 종료(2024.02) 이후 브라우저 자동화로 대체한 버전입니다.
"""

import logging
import os
import random
import time
import markdown

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

TISTORY_WRITE_URL = "https://www.tistory.com/auth/kakao"


def human_delay(min_sec=1.5, max_sec=3.5):
    time.sleep(random.uniform(min_sec, max_sec))


class TistoryUploader:
    def __init__(self, kakao_email: str, kakao_password: str, blog_name: str):
        self.email = kakao_email
        self.password = kakao_password
        self.blog_name = blog_name
        self.write_url = f"https://{blog_name}.tistory.com/manage/post/"

    def _md_to_html(self, md_content: str) -> str:
        html = markdown.markdown(
            md_content,
            extensions=["extra", "nl2br", "sane_lists"],
        )
        style = """<style>
.stock-post h2{color:#1a73e8;border-bottom:2px solid #e8f0fe;padding-bottom:8px;margin-top:32px}
.stock-post h3{color:#34495e;margin-top:20px}
.stock-post blockquote{background:#f8f9fa;border-left:4px solid #1a73e8;padding:12px 16px;margin:16px 0;border-radius:4px}
.stock-post table{border-collapse:collapse;width:100%}
.stock-post td,.stock-post th{border:1px solid #ddd;padding:8px 12px}
.stock-post th{background:#1a73e8;color:white}
</style>"""
        return f'{style}\n<div class="stock-post">\n{html}\n</div>'

    def upload(
        self,
        title: str,
        content: str,
        tags: list[str],
        image_path: str | None = None,
    ) -> dict:
        html_content = self._md_to_html(content)

        with sync_playwright() as p:
            headless = os.environ.get("CI", "false").lower() == "true"
            browser = p.chromium.launch(
                headless=headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ko-KR",
            )
            page = context.new_page()

            try:
                logger.info("카카오 로그인 시도 중...")
                self._kakao_login(page)

                logger.info(f"글쓰기 페이지로 이동: {self.write_url}")
                page.goto(self.write_url, wait_until="networkidle", timeout=30000)
                human_delay(2, 4)

                self._enter_title(page, title)
                self._enter_content_html(page, html_content)

                if image_path and os.path.exists(image_path):
                    self._upload_thumbnail(page, image_path)

                self._enter_tags(page, tags)
                post_url = self._publish(page)
                logger.info(f"발행 완료: {post_url}")
                return {"url": post_url}

            except Exception as e:
                screenshot_path = "error_screenshot.png"
                page.screenshot(path=screenshot_path)
                logger.error(f"업로드 실패. 스크린샷: {screenshot_path} | 오류: {e}")
                raise
            finally:
                browser.close()

    def _kakao_login(self, page):
        """카카오 계정으로 티스토리에 로그인합니다 (2025년 로그인 페이지 기준)."""
        page.goto(TISTORY_WRITE_URL, wait_until="networkidle", timeout=30000)
        human_delay(2, 3)

        # 이미 로그인된 경우 건너뜀
        if "tistory.com" in page.url and "accounts.kakao.com" not in page.url:
            logger.info("이미 로그인된 상태입니다.")
            return

        logger.info(f"현재 페이지: {page.url}")

        # 카카오 로그인 페이지 진입 대기
        page.wait_for_url("**/accounts.kakao.com/**", timeout=15000)
        human_delay(1.5, 2.5)

        # ── 이메일 입력 ──────────────────────────────────────────────────
        # 카카오는 2024년 말 로그인 페이지 개편 후 id="loginId" 사용
        email_input = page.wait_for_selector(
            "#loginId, input[autocomplete='username'], input[name='loginId']",
            timeout=15000,
            state="visible",
        )
        email_input.click()
        human_delay(0.5, 1.0)
        for char in self.email:
            email_input.type(char, delay=random.randint(50, 150))
        human_delay(0.8, 1.5)

        # ── 비밀번호 입력 ────────────────────────────────────────────────
        pw_input = page.wait_for_selector(
            "#password, input[autocomplete='current-password'], input[name='password']",
            timeout=10000,
            state="visible",
        )
        pw_input.click()
        human_delay(0.3, 0.8)
        for char in self.password:
            pw_input.type(char, delay=random.randint(60, 160))
        human_delay(0.5, 1.0)

        # ── 로그인 버튼 클릭 ─────────────────────────────────────────────
        login_btn = page.wait_for_selector(
            "button[type='submit']",
            timeout=10000,
            state="visible",
        )
        login_btn.click()

        # 티스토리로 리다이렉트 대기
        page.wait_for_url("**/tistory.com/**", timeout=25000)
        human_delay(2, 3)
        logger.info("카카오 로그인 성공")

    def _enter_title(self, page, title: str):
        title_input = page.wait_for_selector(
            ".tt_editor_top input, input[placeholder*='제목'], #post-title-inp",
            timeout=15000,
        )
        title_input.click()
        human_delay(0.5, 1.0)
        title_input.fill(title)
        human_delay(1, 2)
        logger.info(f"제목 입력 완료: {title}")

    def _enter_content_html(self, page, html_content: str):
        try:
            mode_btn = page.wait_for_selector(
                "button.editor-mode, .editor-switch, [data-name='editor-type']",
                timeout=10000,
            )
            mode_btn.click()
            human_delay(0.8, 1.5)

            html_option = page.wait_for_selector(
                "li[data-value='html'], .editor-mode-html, button:has-text('HTML')",
                timeout=8000,
            )
            html_option.click()
            human_delay(1, 2)
            logger.info("HTML 모드로 전환 완료")
        except PWTimeout:
            logger.warning("에디터 모드 전환 버튼을 찾지 못했습니다. 계속 진행합니다.")

        try:
            page.evaluate(
                """(args) => {
                    const el = document.querySelector(args.selector);
                    if (el) {
                        el.value = args.content;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                {
                    "selector": "textarea.html-editor, .CodeMirror textarea, #html-editor",
                    "content": html_content,
                },
            )
        except Exception:
            page.evaluate(
                """(content) => {
                    const cm = document.querySelector('.CodeMirror')?.CodeMirror;
                    if (cm) cm.setValue(content);
                }""",
                html_content,
            )
        human_delay(1.5, 2.5)
        logger.info(f"본문 입력 완료 ({len(html_content)}자)")

    def _upload_thumbnail(self, page, image_path: str):
        try:
            thumb_btn = page.wait_for_selector(
                "button.thumbnail, .btn-thumbnail, [data-name='thumbnail']",
                timeout=8000,
            )
            thumb_btn.click()
            human_delay(1, 2)

            file_input = page.wait_for_selector("input[type='file']", timeout=8000)
            file_input.set_input_files(os.path.abspath(image_path))
            human_delay(2, 4)

            try:
                confirm_btn = page.wait_for_selector(
                    "button:has-text('적용'), button:has-text('확인'), .btn-apply",
                    timeout=8000,
                )
                confirm_btn.click()
                human_delay(1, 2)
            except PWTimeout:
                pass
            logger.info(f"썸네일 업로드 완료: {image_path}")
        except PWTimeout:
            logger.warning("썸네일 업로드 버튼을 찾지 못했습니다. 건너뜁니다.")

    def _enter_tags(self, page, tags: list[str]):
        try:
            tag_input = page.wait_for_selector(
                "input[placeholder*='태그'], .tag-input, #tagInput",
                timeout=8000,
            )
            tag_input.click()
            human_delay(0.5, 1.0)
            for tag in tags:
                tag_input.type(tag, delay=80)
                tag_input.press("Enter")
                human_delay(0.3, 0.6)
            logger.info(f"태그 입력 완료: {tags}")
        except PWTimeout:
            logger.warning("태그 입력란을 찾지 못했습니다. 건너뜁니다.")

    def _publish(self, page) -> str:
        human_delay(1, 2)
        publish_btn = page.wait_for_selector(
            "button.publish, .btn-publish, button:has-text('발행'), "
            "button:has-text('완료'), #publish-layer-btn",
            timeout=10000,
        )
        publish_btn.click()
        human_delay(1.5, 2.5)

        try:
            public_btn = page.wait_for_selector(
                "label:has-text('공개'), input[value='public']",
                timeout=5000,
            )
            public_btn.click()
            human_delay(0.5, 1.0)
        except PWTimeout:
            pass

        try:
            confirm_publish = page.wait_for_selector(
                "button.btn-publish-ok, button:has-text('발행하기'), "
                ".layer-publish button[type='submit']",
                timeout=8000,
            )
            confirm_publish.click()
        except PWTimeout:
            logger.warning("최종 발행 확인 버튼을 찾지 못했습니다.")

        try:
            page.wait_for_url(f"**{self.blog_name}.tistory.com/**", timeout=15000)
            human_delay(1.5, 2.5)
            post_url = page.url
        except PWTimeout:
            post_url = f"https://{self.blog_name}.tistory.com/"

        return post_url
