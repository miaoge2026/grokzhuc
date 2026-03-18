"""
Grok 自动注册主脚本 - 优化版本
基于 DrissionPage 实现 x.ai 账号自动注册，支持环境变量配置、结构化日志和健壮性增强
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError

from email_register import get_email_and_token, get_oai_code, DuckMailConfig

# ============================================================
# 配置管理 - 支持环境变量和 config.json
# ============================================================


@dataclass
class AppConfig:
    """应用配置数据类"""
    # 运行配置
    run_count: int = 10
    log_level: str = "INFO"
    output_dir: str = "sso"

    # 浏览器配置
    browser_proxy: str = ""
    headless: bool = True
    user_data_dir: str = "./chrome_data"
    chromium_path: Optional[str] = None

    # DuckMail 配置（委托给 email_register 模块）
    duckmail_config: DuckMailConfig = field(default_factory=DuckMailConfig)

    # Grok2API 配置
    api_endpoint: str = ""
    api_token: str = ""
    api_append: bool = True

    # 超时配置（秒）
    email_timeout: int = 15
    code_timeout: int = 180
    profile_timeout: int = 120
    sso_timeout: int = 120
    page_timeout: int = 60

    # 重试配置
    max_retries: int = 3
    retry_delay: float = 2.0

    @classmethod
    def load(cls) -> AppConfig:
        """从环境变量和 config.json 加载配置"""
        config = cls()

        # 从环境变量加载
        if os.getenv("RUN_COUNT"):
            config.run_count = int(os.getenv("RUN_COUNT"))
        if os.getenv("LOG_LEVEL"):
            config.log_level = os.getenv("LOG_LEVEL")
        if os.getenv("BROWSER_PROXY"):
            config.browser_proxy = os.getenv("BROWSER_PROXY")
        if os.getenv("HEADLESS"):
            config.headless = os.getenv("HEADLESS").lower() == "true"
        if os.getenv("GROK2API_ENDPOINT"):
            config.api_endpoint = os.getenv("GROK2API_ENDPOINT")
        if os.getenv("GROK2API_TOKEN"):
            config.api_token = os.getenv("GROK2API_TOKEN")
        if os.getenv("GROK2API_APPEND"):
            config.api_append = os.getenv("GROK2API_APPEND").lower() == "true"

        # 从 config.json 加载（环境变量未设置时）
        config_path = Path(__file__).parent / "config.json"
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as f:
                conf = json.load(f)

            if not os.getenv("RUN_COUNT"):
                config.run_count = conf.get("run", {}).get("count", 10)
            if not os.getenv("BROWSER_PROXY"):
                config.browser_proxy = conf.get("browser_proxy", "")
            if not os.getenv("GROK2API_ENDPOINT"):
                config.api_endpoint = conf.get("api", {}).get("endpoint", "")
            if not os.getenv("GROK2API_TOKEN"):
                config.api_token = conf.get("api", {}).get("token", "")
            if not os.getenv("GROK2API_APPEND"):
                config.api_append = conf.get("api", {}).get("append", True)

        return config


# ============================================================
# 结构化日志配置
# ============================================================


@dataclass
class LogContext:
    """日志上下文"""
    email: Optional[str] = None
    round_num: Optional[int] = None
    sso: Optional[str] = None
    error: Optional[str] = None


def setup_logger(config: AppConfig) -> logging.Logger:
    """
    设置结构化日志器

    Args:
        config: 应用配置

    Returns:
        logging.Logger: 配置好的日志器
    """
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"run_{ts}.log"

    logger = logging.getLogger("grok_register")
    logger.setLevel(log_level)
    logger.handlers.clear()

    # 结构化日志格式
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(log_level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(log_level)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("日志初始化完成", extra={"log_path": str(log_path), "level": config.log_level})
    return logger


# ============================================================
# 全局状态
# ============================================================


@dataclass
class BrowserState:
    """浏览器状态"""
    browser: Optional[Chromium] = None
    page: Optional[Any] = None
    user_data_dir: Path = field(default_factory=lambda: Path("./chrome_data"))


class RegistrationResult:
    """注册结果数据类"""

    def __init__(
        self,
        email: str,
        password: str,
        given_name: str,
        family_name: str,
        sso: str,
        success: bool = True,
        error: Optional[str] = None
    ):
        self.email = email
        self.password = password
        self.given_name = given_name
        self.family_name = family_name
        self.sso = sso
        self.success = success
        self.error = error
        self.timestamp = datetime.datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "password": self.password,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "sso": self.sso,
            "success": self.success,
            "error": self.error,
            "timestamp": self.timestamp,
        }


# ============================================================
# Python 运行时检查
# ============================================================


def ensure_stable_python_runtime() -> None:
    """
    确保使用稳定的 Python 版本（3.12/3.13）
    避免 3.14 下 Mail.tm 偶发 TLS/兼容问题
    """
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器：{candidate}")
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility() -> None:
    """Python 3.14+ 兼容性警告"""
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。")


# ============================================================
# 浏览器管理
# ============================================================


def setup_browser_options(config: AppConfig) -> ChromiumOptions:
    """
    配置 Chromium 选项

    Args:
        config: 应用配置

    Returns:
        ChromiumOptions: 配置好的浏览器选项
    """
    co = ChromiumOptions()
    co.auto_port()
    
    # 不强制 headless - 在 Xvfb 环境下会自动使用虚拟显示
    # co.set_argument("--headless=new")
    
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-software-rasterizer")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    
    # Turnstile 兼容性：禁用自动化特征
    co.set_argument("--disable-features=IsolateOrigins,site-per-process")
    
    # 增加页面加载超时
    co.set_timeouts(base=5, page_load=30, script=30)

    # 代理配置
    if config.browser_proxy:
        co.set_proxy(config.browser_proxy)
        print(f"[*] 浏览器代理：{config.browser_proxy}")

    # Linux 自动检测 Chromium 路径
    if sys.platform == "linux":
        import glob
        import platform

        # 优先用 playwright 装的 chromium
        pw_chromes = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
        if pw_chromes:
            co.set_browser_path(pw_chromes[0])
        else:
            for candidate in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
                if os.path.isfile(candidate):
                    co.set_browser_path(candidate)
                    break

        # 独立用户数据目录
        user_data = Path(__file__).parent / config.user_data_dir
        user_data.mkdir(exist_ok=True)
        co.set_user_data_path(str(user_data))

    co.set_timeouts(base=1)

    # 加载 Turnstile 修复扩展
    ext_path = Path(__file__).parent / "turnstilePatch"
    if ext_path.exists():
        co.add_extension(str(ext_path))
        print(f"[*] 已加载 Turnstile 扩展：{ext_path}")

    return co


def start_browser(options: ChromiumOptions, state: BrowserState) -> None:
    """
    启动浏览器

    Args:
        options: 浏览器选项
        state: 浏览器状态（更新）
    """
    # 清理旧用户数据
    if state.user_data_dir.exists():
        shutil.rmtree(state.user_data_dir, ignore_errors=True)
    state.user_data_dir.mkdir(exist_ok=True)

    state.browser = Chromium(options)
    tabs = state.browser.get_tabs()
    state.page = tabs[-1] if tabs else state.browser.new_tab()
    
    # 等待浏览器完全初始化
    time.sleep(1)
    print("[*] 浏览器启动成功")


def stop_browser(state: BrowserState) -> None:
    """
    停止浏览器

    Args:
        state: 浏览器状态
    """
    if state.browser is not None:
        try:
            state.browser.quit()
        except Exception:
            pass
    state.browser = None
    state.page = None


def restart_browser(options: ChromiumOptions, state: BrowserState) -> None:
    """
    重启浏览器

    Args:
        options: 浏览器选项
        state: 浏览器状态
    """
    stop_browser(state)
    start_browser(options, state)


def refresh_active_page(state: BrowserState) -> None:
    """
    刷新活动页面

    Args:
        state: 浏览器状态
    """
    if state.browser is None:
        start_browser(options=setup_browser_options(AppConfig.load()), state=state)
        return

    try:
        tabs = state.browser.get_tabs()
        if tabs:
            state.page = tabs[-1]
        else:
            state.page = state.browser.new_tab()
    except Exception:
        restart_browser(options=setup_browser_options(AppConfig.load()), state=state)


# ============================================================
# 页面操作
# ============================================================

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


def open_signup_page(state: BrowserState) -> None:
    """打开注册页面"""
    refresh_active_page(state)
    try:
        assert state.page is not None
        state.page.get(SIGNUP_URL)
        # 等待页面加载完成
        state.page.wait.load_start()
        state.page.wait(3)  # 额外等待确保 Turnstile 渲染
    except Exception:
        refresh_active_page(state)
        if state.page is None:
            raise RuntimeError("页面对象未初始化")
        state.page = state.browser.new_tab(SIGNUP_URL)  # type: ignore
        state.page.wait.load_start()
        state.page.wait(3)  # 额外等待确保 Turnstile 渲染
    click_email_signup_button(state)


def click_email_signup_button(state: BrowserState, timeout: int = 15) -> bool:
    """
    点击"使用邮箱注册"按钮

    Args:
        state: 浏览器状态
        timeout: 超时时间（秒）

    Returns:
        bool: 是否成功点击

    Raises:
        Exception: 未找到按钮时抛出
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        assert state.page is not None
        try:
            # 尝试用多种方式点击按钮
            # 方式 1: JavaScript 点击
            clicked = state.page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('使用邮箱注册') || text.includes('signupwithemail') || text.includes('signupemail') || text.includes('continuewith email') || text.includes('email');
});

if (!target) {
    return false;
}

target.click();
return true;
            """, timeout=5)

            if clicked:
                print("[*] 已点击邮箱注册按钮")
                state.page.wait(1)
                return True

            # 方式 2: DrissionPage 原生点击
            btn = state.page.ele('xpath://button[contains(text(), "邮箱") or contains(text(), "email")]', timeout=2)
            if btn:
                btn.click()
                print("[*] 已通过原生方式点击邮箱注册按钮")
                state.page.wait(1)
                return True

            # 方式 3: 检查页面 URL 是否正确
            current_url = state.page.url
            if "accounts.x.ai" not in current_url:
                print(f"[!] 页面 URL 不正确：{current_url}")
                return False

        except Exception as e:
            print(f"[*] 点击尝试失败：{str(e)[:50]}")
            time.sleep(1)
            continue

    print("[!] 未找到邮箱注册按钮")
    return False

    raise Exception('未找到"使用邮箱注册"按钮')


def fill_email_and_submit(state: BrowserState, timeout: int = 15) -> Tuple[str, str]:
    """
    填写邮箱并提交

    Args:
        state: 浏览器状态
        timeout: 超时时间（秒）

    Returns:
        Tuple[str, str]: (邮箱，dev_token)

    Raises:
        Exception: 填写失败时抛出
    """
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        assert state.page is not None
        filled = state.page.run_js(
            """
const email = arguments[0];

function isVisible(node) {
    if (!node) { return false; }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') { return false; }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) { return 'not-ready'; }

input.focus();
input.click();

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) { tracker.setValue(''); }
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) { return false; }

input.blur();
return 'filled';
            """,
            email,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 邮箱输入框已出现，但写入失败：{filled}")
            time.sleep(0.5)
            continue

        time.sleep(0.8)
        clicked = state.page.run_js(r"""
function isVisible(node) {
    if (!node) { return false; }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') { return false; }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) { return false; }

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '注册' || text.includes('注册') || t === 'signup' || t === 'sign up' || t.includes('sign up');
});

if (!submitButton || submitButton.disabled) { return false; }

submitButton.click();
return true;
        """)

        if clicked:
            print(f"[*] 已填写邮箱并点击注册：{email}")
            return email, dev_token

        time.sleep(0.5)

    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(state: BrowserState, email: str, dev_token: str, timeout: int = 180) -> str:
    """
    填写验证码并提交

    Args:
        state: 浏览器状态
        email: 邮箱地址
        dev_token: 邮件 token
        timeout: 超时时间（秒）

    Returns:
        str: 验证码

    Raises:
        Exception: 填写失败时抛出
    """
    code = get_oai_code(dev_token, email, timeout=timeout)
    if not code:
        raise Exception("获取验证码失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            assert state.page is not None
            filled = state.page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) { return false; }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') { return false; }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) { tracker.setValue(''); }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) { return false; }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) { return 'not-ready'; }

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const expectedLength = Number(input.maxLength || code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) { return 'aggregate-mismatch'; }
    if (expectedLength > 0 && normalizedValue.length !== expectedLength) { return 'aggregate-length-mismatch'; }
    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) { return 'aggregate-slot-mismatch'; }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except PageDisconnectedError:
            refresh_active_page(state)
            if has_profile_form(state):
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(1)
            continue

        if filled == 'not-ready':
            if has_profile_form(state):
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 验证码输入框已出现，但写入失败：{filled}")
            time.sleep(0.5)
            continue

        time.sleep(1.2)
        try:
            assert state.page is not None
            clicked = state.page.run_js(r"""
function isVisible(node) {
    if (!node) { return false; }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') { return false; }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) { return false; }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) { return false; }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) { return false; }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) { return false; }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || t.includes('confirm') || t.includes('continue') || t.includes('next') || t.includes('verify');
});

if (!confirmButton) { return 'no-button'; }

confirmButton.focus();
confirmButton.click();
return 'clicked';
            """)
        except PageDisconnectedError:
            refresh_active_page(state)
            if has_profile_form(state):
                print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                return code
            clicked = 'disconnected'

        if clicked == 'clicked':
            print(f"[*] 已填写验证码并点击确认邮箱：{code}")
            time.sleep(2)
            refresh_active_page(state)
            if has_profile_form(state):
                print("[*] 验证码确认完成，最终注册页已就绪。")
            return code

        if clicked == 'no-button':
            current_url = state.page.url
            if 'sign-up' in current_url or 'signup' in current_url:
                print(f"[*] 已填写验证码，页面已自动跳转到下一步：{current_url}")
                return code

        if clicked == 'disconnected':
            time.sleep(1)
            continue

        time.sleep(0.5)

    # 调试快照
    assert state.page is not None
    debug_snapshot = state.page.run_js(r"""
function isVisible(node) {
    if (!node) { return false; }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') { return false; }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
    """)
    print(f"[Debug] 验证码页 DOM 摘要：{debug_snapshot}")
    raise Exception("未找到验证码输入框或确认邮箱按钮")


def has_profile_form(state: BrowserState) -> bool:
    """
    检查是否显示个人资料表单

    Args:
        state: 浏览器状态

    Returns:
        bool: 是否显示表单
    """
    refresh_active_page(state)
    try:
        assert state.page is not None
        return bool(state.page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False


def get_turnstile_token(state: BrowserState) -> str:
    """
    获取 Turnstile token

    Args:
        state: 浏览器状态

    Returns:
        str: Turnstile token

    Raises:
        Exception: 获取失败时抛出
    """
    assert state.page is not None
    state.page.run_js("try { turnstile.reset() } catch(e) { }")

    start_time = time.time()
    turnstile_response = None
    for i in range(15):
        try:
            # 方法 1: 尝试直接获取 turnstile 响应
            turnstile_response = state.page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
            if turnstile_response and len(turnstile_response) > 20:
                print(f"[*] Turnstile 验证成功 (直接获取): {turnstile_response[:20]}...")
                return turnstile_response

            # 方法 2: 查找 challenge iframe
            iframe_found = False
            challenge_iframe = None
            
            # 尝试多种方式查找 iframe
            for selector in ["tag:iframe", "xpath://iframe[contains(@src, 'turnstile')]", "xpath://iframe[contains(@src, 'cloudflare')]"]:
                try:
                    challenge_iframe = state.page.ele(selector)
                    if challenge_iframe:
                        iframe_found = True
                        print("[*] 找到 Turnstile iframe")
                        break
                except Exception:
                    continue
            
            if iframe_found and challenge_iframe:
                # 等待 iframe 加载完成
                time.sleep(1)
                
                try:
                    # 注入鼠标事件修复
                    challenge_iframe.run_js("""
window.dtp = 1
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);

Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                    """)
                    print("[*] 已注入鼠标事件修复")
                except Exception as e:
                    print(f"[*] 注入失败：{str(e)[:40]}")
                
                # 尝试点击验证按钮 - 使用 JavaScript 直接点击
                try:
                    # 方法 1: 通过 JS 点击 checkbox - 支持多个选择器
                    clicked = challenge_iframe.run_js("""
const selectors = [
    'input[type="checkbox"]',
    'input[name="cf-turnstile-response"]',
    'div[role="checkbox"]',
    '.turnstile-widget',
    'input#turnstile-widget'
];
let found = false;
for (const selector of selectors) {
    const element = document.querySelector(selector);
    if (element) {
        // 创建鼠标事件并设置坐标
        const event = new MouseEvent('click', {
            bubbles: true,
            cancelable: true,
            view: window,
            screenX: Math.floor(Math.random() * 400) + 800,
            screenY: Math.floor(Math.random() * 200) + 400
        });
        element.dispatchEvent(event);
        found = true;
        break;
    }
}
return found;
                    """)
                    if clicked:
                        print("[*] 已通过 JS 点击 Turnstile 验证按钮")
                    else:
                        print("[!] 未找到 checkbox 元素")
                    
                    # 等待 turnstile 处理
                    time.sleep(5)
                    
                    # 方法 2: 尝试直接获取 turnstile token
                    token = challenge_iframe.run_js("""
try {
    return turnstile.getResponse ? turnstile.getResponse() : null;
} catch(e) {
    return null;
}
                    """)
                    if token and len(token) > 20:
                        print(f"[*] Turnstile 验证成功 (iframe 获取): {token[:20]}...")
                        return token
                    
                except Exception as e:
                    print(f"[*] 点击失败：{str(e)[:40]}")
                
                # 再次尝试从主页面获取响应
                turnstile_response = state.page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
                if turnstile_response and len(turnstile_response) > 20:
                    print(f"[*] Turnstile 验证成功 (点击后): {turnstile_response[:20]}...")
                    return turnstile_response
                    
        except Exception as e:
            print(f"[*] Turnstile 处理异常：{str(e)[:50]}")
        
        print(f"[*] Turnstile 等待中... ({int(time.time() - start_time)}s)")
        time.sleep(3)

    raise Exception("failed to solve turnstile")


def build_profile() -> Tuple[str, str, str]:
    """
    生成注册资料

    Returns:
        Tuple[str, str, str]: (given_name, family_name, password)
    """
    given_name = "Neo"
    family_name = "Lin"
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(state: BrowserState, timeout: int = 120) -> Dict[str, str]:
    """
    填写个人资料并提交

    Args:
        state: 浏览器状态
        timeout: 超时时间（秒）

    Returns:
        Dict[str, str]: 注册资料字典

    Raises:
        Exception: 填写失败时抛出
    """
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""

    while time.time() < deadline:
        assert state.page is not None
        filled = state.page.run_js(
            """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) { return false; }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') { return false; }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) { return false; }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) { tracker.setValue(''); }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) { return 'not-ready'; }

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) { return 'filled-failed'; }

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
            """,
            given_name,
            family_name,
            password,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 最终注册页输入框已出现，但姓名/密码写入失败：{filled}")
            time.sleep(0.5)
            continue

        values_ok = state.page.run_js(
            """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) { return false; }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') { return false; }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) { return false; }

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
            """,
            given_name,
            family_name,
            password,
        )
        if not values_ok:
            print("[Debug] 最终注册页字段值校验失败，继续重试填写。")
            time.sleep(0.5)
            continue

        turnstile_state = state.page.run_js(
            """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) { return 'not-found'; }
const value = String(challengeInput.value || '').trim();
return value ? 'ready' : 'pending';
            """
        )

        if turnstile_state == "pending" and not turnstile_token:
            print("[*] 检测到最终注册页存在 Turnstile，开始使用现有真人化点击逻辑。")
            turnstile_token = get_turnstile_token(state)
            if turnstile_token:
                synced = state.page.run_js(
                    """
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) { return false; }
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim() === String(token || '').trim();
                    """,
                    turnstile_token,
                )
                if synced:
                    print("[*] Turnstile 响应已同步到最终注册表单。")

        time.sleep(1.2)

        try:
            submit_button = state.page.ele('tag:button@@text()=完成注册') or state.page.ele('tag:button@@text():Create Account') or state.page.ele('tag:button@@text():Sign up')
        except Exception:
            submit_button = None

        if not submit_button:
            clicked = state.page.run_js(r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) { return false; }
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '完成注册' || text.includes('完成注册') || t.includes('create account') || t.includes('sign up') || t.includes('complete');
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') { return false; }
submitButton.focus();
submitButton.click();
return true;
            """)
        else:
            challenge_value = state.page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
return challengeInput ? String(challengeInput.value || '').trim() : 'not-found';
                """
            )
            if challenge_value not in ('not-found', ''):
                submit_button.click()
                clicked = True
            else:
                clicked = False

        if clicked:
            print(f"[*] 已填写注册资料并点击完成注册：{given_name} {family_name} / {password}")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

        time.sleep(0.5)

    raise Exception("未找到最终注册表单或完成注册按钮")


# ============================================================
# SSO Cookie 处理
# ============================================================


def wait_for_sso_cookie(state: BrowserState, timeout: int = 120) -> str:
    """
    等待 SSO cookie 出现

    Args:
        state: 浏览器状态
        timeout: 超时时间（秒）

    Returns:
        str: SSO token

    Raises:
        Exception: 超时未找到时抛出
    """
    deadline = time.time() + timeout
    last_seen_names: set = set()

    while time.time() < deadline:
        try:
            refresh_active_page(state)
            if state.page is None:
                time.sleep(1)
                continue

            cookies = state.page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    print("[*] 注册完成后已获取到 sso cookie。")
                    return value

        except PageDisconnectedError:
            refresh_active_page(state)
        except Exception:
            pass

        time.sleep(1)

    raise Exception(f"注册完成后未获取到 sso cookie，当前已见 cookie: {sorted(last_seen_names)}")


def append_sso_to_txt(sso_value: str, output_path: Path) -> None:
    """
    追加 SSO 到文件

    Args:
        sso_value: SSO token
        output_path: 输出文件路径

    Raises:
        Exception: SSO 为空时抛出
    """
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("待写入的 sso 为空")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(normalized + "\n")

    print(f"[*] 已追加写入 sso 到文件：{output_path}")


# ============================================================
# Grok2API 推送
# ============================================================


def push_sso_to_api(tokens: List[str], config: AppConfig) -> None:
    """
    推送 SSO token 到 grok2api

    Args:
        tokens: token 列表
        config: 应用配置
    """
    if not config.api_endpoint or not config.api_token:
        print("[*] API 配置为空，跳过推送")
        return

    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = {
        "Authorization": f"Bearer {config.api_token}",
        "Content-Type": "application/json",
    }

    tokens_to_push = [t for t in tokens if t]

    if config.api_append:
        try:
            get_resp = requests.get(config.api_endpoint, headers=headers, timeout=15, verify=False)
            if get_resp.status_code == 200:
                existing = get_resp.json().get("ssoBasic", [])
                existing_tokens = [
                    item["token"] if isinstance(item, dict) else str(item)
                    for item in existing if item
                ]
                seen: set = set()
                deduped: List[str] = []
                for t in existing_tokens + tokens_to_push:
                    if t not in seen:
                        seen.add(t)
                        deduped.append(t)
                tokens_to_push = deduped
                print(f"[*] 查询到线上 {len(existing_tokens)} 个 token，合并本次 {len(tokens)} 个，共 {len(deduped)} 个")
            else:
                print(f"[Warn] 查询线上 token 失败：HTTP {get_resp.status_code}，仅推送本次 token")
        except Exception as e:
            print(f"[Warn] 查询线上 token 异常：{e}，仅推送本次 token")

    try:
        resp = requests.post(
            config.api_endpoint,
            json={"ssoBasic": tokens_to_push},
            headers=headers,
            timeout=60,
            verify=False,
        )
        if resp.status_code == 200:
            print(f"[*] SSO token 已推送到 API（共 {len(tokens_to_push)} 个）：{config.api_endpoint}")
        else:
            print(f"[Warn] 推送 API 返回异常：HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[Warn] 推送 API 失败：{e}")


# ============================================================
# 单轮注册流程
# ============================================================


def run_single_registration(
    state: BrowserState,
    output_path: Path,
    config: AppConfig,
    logger: logging.Logger,
    round_num: int,
    extract_numbers: bool = False
) -> RegistrationResult:
    """
    执行单轮注册流程

    Args:
        state: 浏览器状态
        output_path: 输出文件路径
        config: 应用配置
        logger: 日志器
        round_num: 当前轮次
        extract_numbers: 是否提取数字文本

    Returns:
        RegistrationResult: 注册结果
    """
    try:
        open_signup_page(state)
        email, dev_token = fill_email_and_submit(state, timeout=config.email_timeout)
        code = fill_code_and_submit(state, email, dev_token, timeout=config.code_timeout)
        profile = fill_profile_and_submit(state, timeout=config.profile_timeout)
        sso_value = wait_for_sso_cookie(state, timeout=config.sso_timeout)
        append_sso_to_txt(sso_value, output_path)

        if extract_numbers:
            print("[*] 跳过数字提取（功能已简化）")

        result = RegistrationResult(
            email=email,
            password=profile["password"],
            given_name=profile["given_name"],
            family_name=profile["family_name"],
            sso=sso_value,
            success=True
        )

        logger.info(
            "注册成功",
            extra={
                "round": round_num,
                "email": email,
                "given": profile["given_name"],
                "family": profile["family_name"],
            }
        )

        print(f"[*] 本轮注册完成，邮箱：{email}")
        return result

    except Exception as e:
        logger.exception(f"第{round_num}轮注册失败", extra={"error": str(e)})
        return RegistrationResult(
            email="",
            password="",
            given_name="",
            family_name="",
            sso="",
            success=False,
            error=str(e)
        )


# ============================================================
# 主入口
# ============================================================


def main() -> None:
    """主函数入口"""
    # 运行时检查
    ensure_stable_python_runtime()
    warn_runtime_compatibility()

    # 加载配置
    config = AppConfig.load()

    # 初始化日志
    logger = setup_logger(config)

    # 解析命令行
    parser = argparse.ArgumentParser(description="xAI 自动注册并采集 sso（优化版）")
    parser.add_argument("--count", type=int, default=config.run_count, help=f"执行轮数，0 表示无限循环（默认 {config.run_count}）")
    parser.add_argument("--output", default="sso/sso.txt", help="sso 输出 txt 路径")
    parser.add_argument("--extract-numbers", action="store_true", help="注册完成后额外提取页面数字文本（已简化）")
    parser.add_argument("--config", type=str, help="自定义配置文件路径")
    args = parser.parse_args()

    # 输出路径
    output_path = Path(args.output)
    if args.config:
        config = AppConfig.load()  # 可扩展为加载自定义配置

    # 初始化浏览器
    options = setup_browser_options(config)
    state = BrowserState()
    state.user_data_dir = Path(__file__).parent / config.user_data_dir

    current_round = 0
    collected_sso: List[str] = []

    try:
        start_browser(options, state)

        while True:
            if config.run_count > 0 and current_round >= config.run_count:
                break

            current_round += 1
            print(f"\n[*] 开始第 {current_round} 轮注册")

            try:
                result = run_single_registration(
                    state=state,
                    output_path=output_path,
                    config=config,
                    logger=logger,
                    round_num=current_round,
                    extract_numbers=args.extract_numbers
                )

                if result.success and result.sso:
                    collected_sso.append(result.sso)

            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {current_round} 轮失败：{error}")
                logger.exception("轮次执行异常", extra={"round": current_round})
            finally:
                restart_browser(options, state)

            if config.run_count == 0 or current_round < config.run_count:
                time.sleep(config.retry_delay)

    finally:
        if collected_sso:
            print(f"\n[*] 注册完成，推送 {len(collected_sso)} 个 token 到 API...")
            push_sso_to_api(collected_sso, config)

        stop_browser(state)
        logger.info("程序执行完成", extra={"total_rounds": current_round, "success_count": len(collected_sso)})


if __name__ == "__main__":
    main()
