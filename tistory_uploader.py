def _kakao_login(self, page):
    """카카오 계정으로 티스토리에 로그인합니다."""
    page.goto(TISTORY_WRITE_URL, wait_until="networkidle", timeout=30000)
    human_delay(2, 3)

    # 현재 카카오 로그인 페이지 URL인지 확인
    if "accounts.kakao.com" not in page.url and "tistory.com" in page.url:
        logger.info("이미 로그인된 상태입니다.")
        return

    logger.info(f"현재 페이지: {page.url}")

    # 카카오 로그인 페이지 대기 (accounts.kakao.com)
    page.wait_for_url("**/accounts.kakao.com/**", timeout=15000)
    human_delay(1.5, 2.5)

    # 이메일 입력 — 개편된 카카오 로그인 셀렉터
    email_input = page.wait_for_selector(
        "input#loginId, input[autocomplete='username']",
        timeout=15000,
        state="visible",
    )
    email_input.click()
    human_delay(0.5, 1.0)
    for char in self.email:
        email_input.type(char, delay=random.randint(50, 150))
    human_delay(0.8, 1.5)

    # 비밀번호 입력
    pw_input = page.wait_for_selector(
        "input#password, input[autocomplete='current-password']",
        timeout=10000,
        state="visible",
    )
    pw_input.click()
    human_delay(0.3, 0.8)
    for char in self.password:
        pw_input.type(char, delay=random.randint(60, 160))
    human_delay(0.5, 1.0)

    # 로그인 버튼
    login_btn = page.wait_for_selector(
        "button[type='submit']",
        timeout=10000,
        state="visible",
    )
    login_btn.click()

    # 로그인 완료 후 티스토리로 리다이렉트 대기
    page.wait_for_url("**/tistory.com/**", timeout=25000)
    human_delay(2, 3)
    logger.info("카카오 로그인 성공")
