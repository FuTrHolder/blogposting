"""
티스토리 Playwright 자동 업로드 모듈
"""

import logging
import os
import random
import time
import markdown

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

TISTORY_LOGIN_URL = "https://www.tistory.com/auth/login"


def human_delay(min_sec=1.5, max_sec=3.5):
    time.sleep(random.uniform(min_sec, max_sec))


class TistoryUploader:
    def __init__(
        self,
        kakao_email: str,
        kakao_password: str,
        blog_name: str,
        category: str = "미국",
    ):
        self.email = kakao_email
        self.password = kakao_password
        self.blog_name = blog_name
        self.category = category
        self.write_url = f"https://{blog_name}.tistory.com/manage/post/"

    def _md_to_html(self, md_content: str) -> str:
        html = markdown.markdown(
            md_content,
            extensions=["extra", "nl2br", "sane_lists"],
        )
        style = (
            "<style>"
            ".stock-post h2{color:#1a73e8;border-bottom:2px solid #e8f0fe;"
            "padding-bottom:8px;margin-top:32px}"
            ".stock-post h3{color:#34495e;margin-top:20px}"
            ".stock-post blockquote{background:#f8f9fa;border-left:4px solid #1a73e8;"
            "padding:12px 16px;margin:16px 0;border-radius:4px}"
            ".stock-post table{border-collapse:collapse;width:100%}"
            ".stock-post td,.stock-post th{border:1px solid #ddd;padding:8px 12px}"
            ".stock-post th{background:#1a73e8;color:white}"
            "</style>"
        )
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
                # 1) 로그인
                self._login(page)

                # 2) 글쓰기 페이지 이동
                logger.info(f"글쓰기 페이지로 이동: {self.write_url}")
                page.goto(self.write_url, wait_until="networkidle", timeout=30000)
                human_delay(2, 4)

                # 임시저장 글 복원 팝업 → 취소 (새 글 작성)
                try:
                    cancel_btn = page.wait_for_selector(
                        "button:has-text('취소'), .btn_cancel",
                        timeout=5000,
                        state="visible",
                    )
                    cancel_btn.click()
                    human_delay(1, 2)
                    logger.info("임시저장 복원 팝업 → 취소 클릭")
                except PWTimeout:
                    pass

                logger.info(f"글쓰기 페이지 진입: {page.url}")

                # 3) 카테고리 선택
                self._select_category(page)

                # 4) 제목 입력
                self._enter_title(page, title)

                # 5) 본문 입력 (기본모드 → 내용 붙여넣기)
                self._enter_content(page, html_content)

                # 6) 태그 입력
                self._enter_tags(page, tags)

                # 7) 완료(발행 패널 열기) → 공개 발행
                post_url = self._publish(page, title)
                logger.info(f"발행 완료: {post_url}")
                return {"url": post_url}

            except Exception as e:
                screenshot_path = "error_screenshot.png"
                page.screenshot(path=screenshot_path)
                logger.error(f"업로드 실패. 스크린샷: {screenshot_path} | 오류: {e}")
                raise
            finally:
                browser.close()

    # ── 로그인 ────────────────────────────────────────────────────────────
    def _login(self, page):
        logger.info(f"티스토리 로그인 페이지 접근: {TISTORY_LOGIN_URL}")
        page.goto(TISTORY_LOGIN_URL, wait_until="networkidle", timeout=30000)
        human_delay(2, 3)
        logger.info(f"로그인 페이지 URL: {page.url}")

        # React 앱 렌더링 후 카카오 버튼 대기
        try:
            kakao_btn = page.wait_for_selector(
                "a.link_kakao_id",
                timeout=15000,
                state="visible",
            )
            logger.info("카카오 버튼 발견! 클릭 중...")
            kakao_btn.click()
            human_delay(2, 3)
        except PWTimeout:
            html = page.evaluate("document.body?.innerHTML?.slice(0,2000)")
            logger.error(f"카카오 버튼 없음. HTML:\n{html}")
            raise RuntimeError("카카오 로그인 버튼을 찾지 못했습니다.")

        # accounts.kakao.com 로그인 폼
        if "accounts.kakao.com" in page.url:
            self._fill_kakao_form(page)
        else:
            raise RuntimeError(f"카카오 로그인 페이지 진입 실패. URL: {page.url}")

        # 동의 화면 처리
        try:
            agree_btn = page.wait_for_selector(
                "button:has-text('동의'), button:has-text('허용'), .btn_agree",
                timeout=4000,
                state="visible",
            )
            agree_btn.click()
            human_delay(1.5, 2.5)
        except PWTimeout:
            pass

        logger.info(f"로그인 완료. URL: {page.url}")

    def _fill_kakao_form(self, page):
        email_input = page.wait_for_selector(
            "#loginId, input[name='loginId'], input[autocomplete='username']",
            timeout=15000,
            state="visible",
        )
        email_input.click()
        human_delay(0.5, 1.0)
        for char in self.email:
            email_input.type(char, delay=random.randint(50, 130))
        human_delay(0.8, 1.5)

        pw_input = page.wait_for_selector(
            "#password, input[name='password'], input[autocomplete='current-password']",
            timeout=10000,
            state="visible",
        )
        pw_input.click()
        human_delay(0.3, 0.8)
        for char in self.password:
            pw_input.type(char, delay=random.randint(60, 140))
        human_delay(0.5, 1.0)

        login_btn = page.wait_for_selector(
            "button[type='submit']", timeout=10000, state="visible"
        )
        login_btn.click()

        try:
            page.wait_for_function(
                "() => !window.location.href.includes('accounts.kakao.com')",
                timeout=20000,
            )
        except PWTimeout:
            logger.warning("카카오 로그인 완료 대기 타임아웃")

        human_delay(2, 3)
        logger.info(f"카카오 폼 로그인 완료. URL: {page.url}")

    # ── 카테고리 선택 ─────────────────────────────────────────────────────
    def _select_category(self, page):
        try:
            # 카테고리 드롭다운 클릭
            cat_btn = page.wait_for_selector(
                ".category-selector, [data-role='category'], "
                "button:has-text('카테고리'), .tt_category",
                timeout=8000,
                state="visible",
            )
            cat_btn.click()
            human_delay(0.8, 1.5)

            # 카테고리 목록에서 선택
            cat_option = page.wait_for_selector(
                f"li:has-text('{self.category}'), "
                f"option:has-text('{self.category}'), "
                f"a:has-text('{self.category}')",
                timeout=5000,
                state="visible",
            )
            cat_option.click()
            human_delay(0.8, 1.5)
            logger.info(f"카테고리 선택 완료: {self.category}")
        except PWTimeout:
            logger.warning(f"카테고리 선택 실패 ({self.category}). 건너뜁니다.")

    # ── 제목 입력 ─────────────────────────────────────────────────────────
    def _enter_title(self, page, title: str):
        human_delay(1, 2)

        # 새 에디터: div[contenteditable] placeholder="제목을 입력하세요."
        selectors = [
            "div[contenteditable='true'][data-placeholder='제목을 입력하세요.']",
            "div[contenteditable='true'][data-placeholder*='제목']",
            "[data-placeholder*='제목']",
            "input[placeholder='제목을 입력하세요.']",
            "input[placeholder*='제목']",
            ".tt_editor_top input",
            "#post-title-inp",
            "input[name='title']",
        ]

        title_el = None
        for selector in selectors:
            try:
                title_el = page.wait_for_selector(
                    selector, timeout=4000, state="visible"
                )
                if title_el:
                    logger.info(f"제목 셀렉터 발견: {selector}")
                    break
            except PWTimeout:
                continue

        if not title_el:
            html_snippet = page.evaluate("document.body?.innerHTML?.slice(0,3000)")
            logger.error(f"제목 입력창 HTML:\n{html_snippet}")
            raise RuntimeError("제목 입력창을 찾지 못했습니다.")

        title_el.click()
        human_delay(0.3, 0.6)
        title_el.fill(title)
        human_delay(0.8, 1.5)
        logger.info(f"제목 입력 완료: {title}")

    # ── 본문 입력 ─────────────────────────────────────────────────────────
    def _enter_content(self, page, html_content: str):
        """
        티스토리 새 에디터(TinyMCE 기반)에 내용을 입력합니다.
        기본모드(블록에디터)는 iframe 내부의 TinyMCE body에 직접 주입합니다.
        """
        human_delay(1, 2)

        # TinyMCE iframe 찾기
        try:
            # iframe이 로딩될 때까지 대기
            page.wait_for_selector(
                "iframe.tox-edit-area__iframe, iframe[id*='tiny'], "
                "iframe[title*='편집'], .tox-tinymce iframe",
                timeout=10000,
            )
            human_delay(1, 1.5)

            # JavaScript로 TinyMCE에 직접 내용 주입
            page.evaluate(
                """(content) => {
                    // TinyMCE 전역 객체를 통해 직접 주입
                    if (window.tinymce && window.tinymce.activeEditor) {
                        window.tinymce.activeEditor.setContent(content);
                        window.tinymce.activeEditor.fire('change');
                        return 'tinymce_ok';
                    }
                    // iframe body에 직접 주입
                    const iframes = document.querySelectorAll(
                        'iframe.tox-edit-area__iframe, iframe[id*="tiny"]'
                    );
                    for (const iframe of iframes) {
                        try {
                            const doc = iframe.contentDocument || iframe.contentWindow.document;
                            if (doc && doc.body) {
                                doc.body.innerHTML = content;
                                return 'iframe_body_ok';
                            }
                        } catch(e) {}
                    }
                    return 'failed';
                }""",
                html_content,
            )
            logger.info("TinyMCE 본문 주입 완료")

        except PWTimeout:
            logger.warning("TinyMCE iframe을 찾지 못했습니다. 클립보드 방식 시도...")
            # 클립보드로 붙여넣기 (fallback)
            self._paste_content_via_clipboard(page, html_content)

        human_delay(1.5, 2.5)
        logger.info(f"본문 입력 완료 ({len(html_content)}자)")

    def _paste_content_via_clipboard(self, page, html_content: str):
        """TinyMCE 주입 실패 시 클립보드로 텍스트를 붙여넣습니다."""
        # 본문 영역 클릭
        content_selectors = [
            ".tox-edit-area",
            "div[contenteditable='true']:not([data-placeholder*='제목'])",
            ".ProseMirror",
        ]
        for selector in content_selectors:
            try:
                el = page.wait_for_selector(selector, timeout=3000, state="visible")
                if el:
                    el.click()
                    human_delay(0.5, 1.0)
                    # 마크다운 원본 텍스트로 붙여넣기 (HTML 태그 없이)
                    import html as html_module
                    plain = html_module.unescape(
                        html_content.replace("<br>", "\n")
                                   .replace("</p>", "\n")
                                   .replace("</div>", "\n")
                    )
                    # 태그 제거
                    import re
                    plain = re.sub(r"<[^>]+>", "", plain).strip()
                    page.keyboard.type(plain[:500])  # 앞 500자만 (타임아웃 방지)
                    logger.info("클립보드 방식으로 일부 내용 입력 완료")
                    break
            except PWTimeout:
                continue

    # ── 태그 입력 ────────────────────────────────────────────────────────
    def _enter_tags(self, page, tags: list[str]):
        try:
            tag_input = page.wait_for_selector(
                "input[placeholder*='태그'], .tag-input, #tagInput, "
                "[data-placeholder*='태그']",
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

    # ── 발행 ─────────────────────────────────────────────────────────────
    def _publish(self, page, title: str) -> str:
        """
        '완료' 버튼 클릭 → 발행 패널 열림 →
        공개 선택 확인 → '공개 발행' 버튼 클릭
        """
        human_delay(1, 2)

        # '완료' 버튼 클릭 (발행 패널 열기)
        done_btn = page.wait_for_selector(
            "button:has-text('완료'), .btn_publish, #publish-layer-btn, "
            "button.publish",
            timeout=10000,
            state="visible",
        )
        done_btn.click()
        human_delay(1.5, 2.5)
        logger.info("완료 버튼 클릭 → 발행 패널 열림")

        # 발행 패널: 공개 선택 확인
        try:
            public_radio = page.wait_for_selector(
                "input[type='radio'][value='public'], "
                "label:has-text('공개') input",
                timeout=5000,
                state="visible",
            )
            if not public_radio.is_checked():
                public_radio.click()
                human_delay(0.5, 1.0)
                logger.info("공개 라디오 선택 완료")
            else:
                logger.info("이미 공개 선택됨")
        except PWTimeout:
            # 레이블 클릭으로 재시도
            try:
                public_label = page.wait_for_selector(
                    "label:has-text('공개')",
                    timeout=4000,
                    state="visible",
                )
                public_label.click()
                human_delay(0.5, 1.0)
            except PWTimeout:
                logger.warning("공개 라디오 버튼을 찾지 못했습니다.")

        # '공개 발행' 버튼 클릭
        try:
            publish_btn = page.wait_for_selector(
                "button:has-text('공개 발행'), button:has-text('발행하기'), "
                ".btn-publish-ok, button[type='submit']:has-text('발행')",
                timeout=8000,
                state="visible",
            )
            publish_btn.click()
            human_delay(2, 3)
            logger.info("공개 발행 버튼 클릭 완료")
        except PWTimeout:
            logger.warning("공개 발행 버튼을 찾지 못했습니다.")

        # 발행 후 URL 확인
        try:
            page.wait_for_url(
                f"**{self.blog_name}.tistory.com/**",
                timeout=15000,
            )
            human_delay(1.5, 2.5)
            post_url = page.url
            logger.info(f"발행 완료 URL: {post_url}")
        except PWTimeout:
            post_url = f"https://{self.blog_name}.tistory.com/"
            logger.warning(f"URL 변경 감지 실패. 기본 URL 사용: {post_url}")

        return post_url
