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
            "h2{color:#1a73e8;border-bottom:2px solid #e8f0fe;padding-bottom:8px;margin-top:32px}"
            "h3{color:#34495e;margin-top:20px}"
            "blockquote{background:#f8f9fa;border-left:4px solid #1a73e8;padding:12px 16px;margin:16px 0;border-radius:4px}"
            "table{border-collapse:collapse;width:100%}"
            "td,th{border:1px solid #ddd;padding:8px 12px}"
            "th{background:#1a73e8;color:white}"
            "</style>"
        )
        return f"{style}\n{html}"

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
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ko-KR",
            )
            page = context.new_page()

            try:
                self._login(page)

                logger.info(f"글쓰기 페이지로 이동: {self.write_url}")
                page.goto(self.write_url, wait_until="networkidle", timeout=30000)
                human_delay(3, 5)

                # 임시저장 복원 팝업 → 취소
                try:
                    cancel = page.wait_for_selector(
                        "button:has-text('취소')", timeout=5000, state="visible"
                    )
                    cancel.click()
                    human_delay(1, 2)
                    logger.info("임시저장 팝업 취소")
                except PWTimeout:
                    pass

                self._select_category(page)
                self._enter_title(page, title)
                self._enter_content(page, html_content)
                self._enter_tags(page, tags)
                post_url = self._publish(page)
                logger.info(f"발행 완료: {post_url}")
                return {"url": post_url}

            except Exception as e:
                page.screenshot(path="error_screenshot.png")
                logger.error(f"업로드 실패: {e}")
                raise
            finally:
                browser.close()

    # ── 로그인 ────────────────────────────────────────────────────────────
    def _login(self, page):
        logger.info("티스토리 로그인 시작...")
        page.goto(TISTORY_LOGIN_URL, wait_until="networkidle", timeout=30000)
        human_delay(2, 3)

        # 카카오 로그인 버튼 클릭
        try:
            kakao_btn = page.wait_for_selector(
                "a.link_kakao_id", timeout=15000, state="visible"
            )
            kakao_btn.click()
            human_delay(2, 3)
        except PWTimeout:
            raise RuntimeError("카카오 로그인 버튼을 찾지 못했습니다.")

        logger.info(f"카카오 버튼 클릭 후 URL: {page.url}")

        # ── 계정 선택 화면 처리 (select_account) ─────────────────────────
        # prompt=select_account 파라미터가 있으면 계정 목록이 표시됨
        # 이메일 주소로 계정을 찾아 클릭하거나, 다른 계정으로 로그인 선택
        if "select_account" in page.url or "kauth.kakao.com" in page.url:
            self._handle_kakao_page(page)

        # 최종적으로 tistory.com인지 확인
        logger.info(f"로그인 최종 URL: {page.url}")
        if "tistory.com" not in page.url:
            page.screenshot(path="login_failed.png")
            raise RuntimeError(f"티스토리 세션 확립 실패. URL: {page.url}")

        logger.info("로그인 완료!")

    def _handle_kakao_page(self, page):
        """
        카카오 페이지에서 상황에 따라 처리:
        1. 계정 선택 화면 → 이메일 계정 클릭
        2. 로그인 폼 화면 → 이메일/비밀번호 입력
        3. tistory.com으로 리다이렉트될 때까지 반복
        """
        for attempt in range(5):
            current_url = page.url
            logger.info(f"카카오 처리 시도 {attempt+1}, URL: {current_url[:80]}...")

            # tistory로 이동 완료
            if "tistory.com" in current_url and "kauth" not in current_url:
                logger.info("티스토리 세션 확립!")
                return

            # ── 계정 선택 화면 ──────────────────────────────────────────
            # 로그인된 계정 목록이 뜨는 경우
            try:
                # 계정 목록에서 이메일이 포함된 항목 클릭
                account_item = page.wait_for_selector(
                    f"[data-email='{self.email}'], "
                    f"button:has-text('{self.email}'), "
                    f"div:has-text('{self.email.split('@')[0]}')",
                    timeout=3000,
                    state="visible",
                )
                account_item.click()
                logger.info(f"계정 선택: {self.email}")
                human_delay(2, 3)
                continue
            except PWTimeout:
                pass

            # "다른 계정으로 로그인" 또는 "로그인" 버튼
            try:
                other_btn = page.wait_for_selector(
                    "button:has-text('다른 카카오계정으로 로그인'), "
                    "a:has-text('다른 카카오계정'), "
                    "button:has-text('새 계정으로 로그인')",
                    timeout=2000,
                    state="visible",
                )
                other_btn.click()
                logger.info("다른 계정으로 로그인 클릭")
                human_delay(2, 3)
                continue
            except PWTimeout:
                pass

            # ── 로그인 폼 화면 ───────────────────────────────────────────
            try:
                email_el = page.wait_for_selector(
                    "#loginId, input[name='loginId']",
                    timeout=3000,
                    state="visible",
                )
                # 이메일 입력
                email_el.click()
                human_delay(0.5, 1.0)
                email_el.triple_click()
                email_el.fill("")
                for c in self.email:
                    email_el.type(c, delay=random.randint(50, 120))
                human_delay(0.8, 1.5)

                # 비밀번호 입력
                pw_el = page.wait_for_selector(
                    "#password, input[name='password']",
                    timeout=5000,
                    state="visible",
                )
                pw_el.click()
                human_delay(0.3, 0.7)
                pw_el.triple_click()
                pw_el.fill("")
                for c in self.password:
                    pw_el.type(c, delay=random.randint(60, 130))
                human_delay(0.5, 1.0)

                # 로그인 버튼
                page.wait_for_selector(
                    "button[type='submit']", timeout=5000
                ).click()
                logger.info("로그인 폼 제출")
                human_delay(3, 5)
                continue

            except PWTimeout:
                pass

            # 아무것도 못 찾으면 잠시 대기 후 재시도
            logger.info("대기 중...")
            human_delay(3, 5)

        raise RuntimeError("카카오 로그인 처리 실패 (5회 시도)")

    # ── 카테고리 ──────────────────────────────────────────────────────────
    def _select_category(self, page):
        try:
            cat_btn = page.wait_for_selector(
                ".category-selector, [data-role='category'], "
                "button:has-text('카테고리'), .tt_category",
                timeout=8000, state="visible",
            )
            cat_btn.click()
            human_delay(0.8, 1.5)
            cat_opt = page.wait_for_selector(
                f"li:has-text('{self.category}'), "
                f"option:has-text('{self.category}'), "
                f"a:has-text('{self.category}')",
                timeout=5000, state="visible",
            )
            cat_opt.click()
            human_delay(0.8, 1.2)
            logger.info(f"카테고리 선택: {self.category}")
        except PWTimeout:
            logger.warning("카테고리 선택 실패. 건너뜁니다.")

    # ── 제목 ─────────────────────────────────────────────────────────────
    def _enter_title(self, page, title: str):
        human_delay(1, 2)
        selectors = [
            "div[contenteditable='true'][data-placeholder='제목을 입력하세요.']",
            "div[contenteditable='true'][data-placeholder*='제목']",
            "[data-placeholder*='제목']",
            "input[placeholder*='제목']",
            ".tt_editor_top input",
            "#post-title-inp",
        ]
        el = None
        for s in selectors:
            try:
                el = page.wait_for_selector(s, timeout=4000, state="visible")
                if el:
                    logger.info(f"제목 셀렉터: {s}")
                    break
            except PWTimeout:
                continue

        if not el:
            logger.error(f"현재 URL: {page.url}")
            raise RuntimeError("제목 입력창을 찾지 못했습니다.")

        el.click()
        human_delay(0.3, 0.6)
        el.fill(title)
        human_delay(1, 1.5)
        logger.info(f"제목 입력: {title}")

    # ── 본문 ─────────────────────────────────────────────────────────────
    def _enter_content(self, page, html_content: str):
        human_delay(2, 3)
        logger.info("본문 입력 시작...")

        # 전략 1: iframe body innerHTML 직접 주입
        injected = page.evaluate(
            """(html) => {
                const iframes = document.querySelectorAll('iframe');
                for (const iframe of iframes) {
                    try {
                        const doc = iframe.contentDocument
                                 || iframe.contentWindow?.document;
                        if (!doc) continue;
                        if (doc.body && doc.body.contentEditable === 'true') {
                            doc.body.innerHTML = html;
                            doc.body.dispatchEvent(new Event('input', {bubbles:true}));
                            return 'iframe_body_ok';
                        }
                        const editable = doc.querySelector('[contenteditable="true"]');
                        if (editable) {
                            editable.innerHTML = html;
                            editable.dispatchEvent(new Event('input', {bubbles:true}));
                            return 'iframe_editable_ok';
                        }
                    } catch(e) { continue; }
                }
                return 'not_found';
            }""",
            html_content,
        )
        logger.info(f"iframe 주입 결과: {injected}")

        if injected != "not_found":
            human_delay(1, 2)
            logger.info(f"본문 입력 완료 ({len(html_content)}자)")
            return

        # 전략 2: Playwright frame_locator
        logger.info("frame_locator 방식 시도...")
        try:
            frame = page.frame_locator("iframe").first
            body = frame.locator("body[contenteditable='true']")
            body.wait_for(timeout=8000, state="visible")
            body.evaluate(
                "(el, html) => { el.innerHTML = html; "
                "el.dispatchEvent(new Event('input', {bubbles:true})); }",
                html_content,
            )
            human_delay(1, 2)
            logger.info("frame_locator 본문 주입 완료")
            return
        except Exception as e:
            logger.warning(f"frame_locator 실패: {e}")

        # 전략 3: 클립보드 붙여넣기
        logger.info("클립보드 붙여넣기 방식 시도...")
        try:
            editor_area = page.wait_for_selector(
                ".tox-edit-area, .ProseMirror, "
                "div[contenteditable='true']:not([data-placeholder*='제목'])",
                timeout=8000, state="visible",
            )
            editor_area.click()
            human_delay(0.5, 1.0)
            page.evaluate(
                """async (html) => {
                    try {
                        await navigator.clipboard.writeText(html);
                    } catch(e) {
                        const ta = document.createElement('textarea');
                        ta.value = html;
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        document.body.removeChild(ta);
                    }
                }""",
                html_content,
            )
            page.keyboard.press("Control+v")
            human_delay(2, 3)
            logger.info("클립보드 붙여넣기 완료")
        except Exception as e:
            logger.error(f"모든 본문 입력 방식 실패: {e}")

    # ── 태그 ─────────────────────────────────────────────────────────────
    def _enter_tags(self, page, tags: list[str]):
        try:
            tag_input = page.wait_for_selector(
                "input[placeholder*='태그'], #tagInput, [data-placeholder*='태그']",
                timeout=8000,
            )
            tag_input.click()
            human_delay(0.5, 1.0)
            for tag in tags:
                tag_input.type(tag, delay=80)
                tag_input.press("Enter")
                human_delay(0.3, 0.6)
            logger.info(f"태그 입력: {tags}")
        except PWTimeout:
            logger.warning("태그 입력란 없음. 건너뜁니다.")

    # ── 발행 ─────────────────────────────────────────────────────────────
    def _publish(self, page) -> str:
        human_delay(1, 2)

        done_btn = page.wait_for_selector(
            "button:has-text('완료'), .btn_publish, #publish-layer-btn, button.publish",
            timeout=10000, state="visible",
        )
        done_btn.click()
        human_delay(2, 3)
        logger.info("완료 버튼 클릭")

        try:
            radio = page.wait_for_selector(
                "input[type='radio'][value='public']",
                timeout=5000, state="visible",
            )
            if not radio.is_checked():
                radio.click()
                human_delay(0.5, 1.0)
        except PWTimeout:
            try:
                page.wait_for_selector(
                    "label:has-text('공개')", timeout=4000
                ).click()
                human_delay(0.5, 1.0)
            except PWTimeout:
                logger.warning("공개 라디오 버튼 없음")

        try:
            pub_btn = page.wait_for_selector(
                "button:has-text('공개 발행'), button:has-text('발행하기'), .btn-publish-ok",
                timeout=8000, state="visible",
            )
            pub_btn.click()
            human_delay(2, 3)
            logger.info("공개 발행 클릭")
        except PWTimeout:
            logger.warning("공개 발행 버튼 없음")

        try:
            page.wait_for_url(f"**{self.blog_name}.tistory.com/**", timeout=15000)
            human_delay(1.5, 2.5)
        except PWTimeout:
            pass

        return page.url
