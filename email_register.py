"""
DuckMail 临时邮箱模块 - 优化版本
提供邮箱创建、验证码获取功能，支持环境变量配置和结构化日志
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# 配置管理 - 支持环境变量和 config.json
# ============================================================


@dataclass
class DuckMailConfig:
    """DuckMail 配置数据类"""
    api_base: str = "https://api.duckmail.sbs"
    bearer_token: str = ""
    proxy: str = ""
    request_timeout: int = 15
    max_retries: int = 3
    retry_backoff: float = 1.0

    @classmethod
    def load(cls) -> DuckMailConfig:
        """从环境变量和 config.json 加载配置"""
        # 优先使用环境变量
        api_base = os.getenv("DUCKMAIL_API_BASE", "").strip()
        bearer = os.getenv("DUCKMAIL_BEARER", "").strip()
        proxy = os.getenv("HTTP_PROXY", os.getenv("HTTPS_PROXY", "")).strip()

        # 环境变量未设置时从 config.json 读取
        if not api_base or not bearer:
            config_path = Path(__file__).parent / "config.json"
            if config_path.exists():
                with config_path.open("r", encoding="utf-8") as f:
                    conf = json.load(f)
                if not api_base:
                    api_base = conf.get("duckmail_api_base", "https://api.duckmail.sbs")
                if not bearer:
                    bearer = conf.get("duckmail_bearer", "")
                if not proxy:
                    proxy = conf.get("proxy", "")

        return cls(
            api_base=api_base,
            bearer_token=bearer,
            proxy=proxy,
        )


# ============================================================
# 结构化日志配置
# ============================================================


def setup_logger(name: str = "duckmail") -> logging.Logger:
    """设置结构化日志器"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # 结构化日志格式
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 文件处理器
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"duckmail_{time.strftime('%Y%m%d')}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 控制台处理器
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


logger = setup_logger("duckmail")

# ============================================================
# 适配层接口
# ============================================================

_temp_email_cache: Dict[str, str] = {}


def get_email_and_token() -> Tuple[Optional[str], Optional[str]]:
    """
    创建 DuckMail 临时邮箱并返回 (email, mail_token)

    Returns:
        Tuple[Optional[str], Optional[str]]: (邮箱地址，邮件 token) 或 (None, None)
    """
    try:
        email, _password, mail_token = create_temp_email()
        if email and mail_token:
            _temp_email_cache[email] = mail_token
            logger.info("邮箱创建成功", extra={"email": email})
            return email, mail_token
        logger.error("邮箱创建失败：返回值为空")
        return None, None
    except Exception as e:
        logger.exception("创建邮箱时发生异常", extra={"error": str(e)})
        return None, None


def get_oai_code(dev_token: str, email: str, timeout: int = 120) -> Optional[str]:
    """
    轮询 DuckMail 获取 OTP 验证码

    Args:
        dev_token: 邮件访问 token
        email: 邮箱地址（用于日志）
        timeout: 超时时间（秒）

    Returns:
        Optional[str]: 验证码字符串（去除连字符）或 None
    """
    try:
        code = wait_for_verification_code(mail_token=dev_token, timeout=timeout)
        if code:
            code = code.replace("-", "")
            logger.info("验证码获取成功", extra={"email": email, "code": code})
            return code
        logger.warning("验证码获取超时", extra={"email": email, "timeout": timeout})
        return None
    except Exception as e:
        logger.exception("获取验证码时发生异常", extra={"email": email, "error": str(e)})
        return None


# ============================================================
# HTTP 会话管理
# ============================================================


def _create_duckmail_session(config: DuckMailConfig) -> Tuple[Any, bool]:
    """
    创建 DuckMail 请求会话

    Args:
        config: DuckMail 配置

    Returns:
        Tuple[Any, bool]: (会话对象，是否使用 curl_cffi)
    """
    if CURL_CFFI_AVAILABLE:
        session = curl_requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if config.proxy:
            session.proxies = {"http": config.proxy, "https": config.proxy}
        logger.debug("使用 curl_cffi 会话（TLS 指纹伪装）")
        return session, True

    # fallback to requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    retry = Retry(
        total=config.max_retries,
        backoff_factor=config.retry_backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    if config.proxy:
        s.proxies = {"http": config.proxy, "https": config.proxy}
    logger.debug("使用 requests 会话（fallback 模式）")
    return s, False


def _do_request(
    session: Any,
    use_cffi: bool,
    method: str,
    url: str,
    config: DuckMailConfig,
    **kwargs
) -> Any:
    """
    统一请求执行

    Args:
        session: 会话对象
        use_cffi: 是否使用 curl_cffi
        method: HTTP 方法
        url: 请求 URL
        config: 配置对象
        **kwargs: 额外参数

    Returns:
        响应对象
    """
    if use_cffi:
        kwargs.setdefault("impersonate", "chrome131")
    kwargs.setdefault("timeout", config.request_timeout)
    logger.debug(f"发送 {method.upper()} 请求: {url}")
    return getattr(session, method)(url, **kwargs)


# ============================================================
# 工具函数
# ============================================================


def _generate_password(length: int = 14) -> str:
    """
    生成随机密码（包含大小写、数字和特殊字符）

    Args:
        length: 密码长度

    Returns:
        生成的密码字符串
    """
    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%"

    # 确保包含至少一个每种字符
    pwd = [
        random.choice(lower),
        random.choice(upper),
        random.choice(digits),
        random.choice(special)
    ]

    # 填充剩余长度
    all_chars = lower + upper + digits + special
    pwd += [random.choice(all_chars) for _ in range(length - 4)]
    random.shuffle(pwd)
    return "".join(pwd)


def _validate_email_format(email: str) -> bool:
    """
    验证邮箱格式

    Args:
        email: 邮箱地址

    Returns:
        bool: 格式是否有效
    """
    pattern = r'^[a-z0-9]{8,13}@duckmail\.sbs$'
    return bool(re.match(pattern, email, re.IGNORECASE))


# ============================================================
# 核心功能
# ============================================================


def create_temp_email(config: Optional[DuckMailConfig] = None) -> Tuple[str, str, str]:
    """
    创建 DuckMail 临时邮箱

    Args:
        config: 配置对象（None 时自动加载）

    Returns:
        Tuple[str, str, str]: (邮箱，密码，mail_token)

    Raises:
        Exception: 创建失败时抛出异常
    """
    if config is None:
        config = DuckMailConfig.load()

    if not config.bearer_token:
        raise Exception("duckmail_bearer 未设置，无法创建临时邮箱")

    # 生成随机邮箱
    chars = string.ascii_lowercase + string.digits
    length = random.randint(8, 13)
    email_local = "".join(random.choice(chars) for _ in range(length))
    email = f"{email_local}@duckmail.sbs"
    password = _generate_password()

    if not _validate_email_format(email):
        raise ValueError(f"生成的邮箱格式无效：{email}")

    api_base = config.api_base.rstrip("/")
    bearer_headers = {"Authorization": f"Bearer {config.bearer_token}"}
    session, use_cffi = _create_duckmail_session(config)

    try:
        # 1. 创建账号
        logger.info("正在创建邮箱账号", extra={"email": email})
        res = _do_request(
            session, use_cffi, "post",
            f"{api_base}/accounts",
            config=config,
            json={"address": email, "password": password},
            headers=bearer_headers
        )

        if res.status_code not in (200, 201):
            error_msg = f"创建邮箱失败：HTTP {res.status_code} - {res.text[:200]}"
            logger.error(error_msg)
            raise Exception(error_msg)

        # 2. 获取 mail token
        time.sleep(0.5)
        logger.info("正在获取邮件 Token")
        token_res = _do_request(
            session, use_cffi, "post",
            f"{api_base}/token",
            config=config,
            json={"address": email, "password": password}
        )

        if token_res.status_code == 200:
            mail_token = token_res.json().get("token")
            if mail_token:
                logger.info("邮箱创建成功", extra={"email": email})
                return email, password, mail_token

        error_msg = f"获取邮件 Token 失败：HTTP {token_res.status_code}"
        logger.error(error_msg)
        raise Exception(error_msg)

    except Exception as e:
        logger.exception("创建邮箱过程中发生异常", extra={"email": email})
        raise Exception(f"DuckMail 创建邮箱失败：{e}")


def fetch_emails(
    mail_token: str,
    config: Optional[DuckMailConfig] = None
) -> List[Dict[str, Any]]:
    """
    获取 DuckMail 邮件列表

    Args:
        mail_token: 邮件访问 token
        config: 配置对象

    Returns:
        List[Dict[str, Any]]: 邮件列表
    """
    if config is None:
        config = DuckMailConfig.load()

    try:
        api_base = config.api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {mail_token}"}
        session, use_cffi = _create_duckmail_session(config)

        res = _do_request(
            session, use_cffi, "get",
            f"{api_base}/messages",
            config=config,
            headers=headers
        )

        if res.status_code == 200:
            data = res.json()
            messages = data.get("hydra:member") or data.get("member") or data.get("data") or []
            logger.debug(f"获取到 {len(messages)} 封邮件")
            return messages

        logger.warning(f"获取邮件列表失败：HTTP {res.status_code}")
        return []

    except Exception as e:
        logger.exception("获取邮件列表时发生异常", extra={"error": str(e)})
        return []


def fetch_email_detail(
    mail_token: str,
    msg_id: str,
    config: Optional[DuckMailConfig] = None
) -> Optional[Dict]:
    """
    获取 DuckMail 单封邮件详情

    Args:
        mail_token: 邮件访问 token
        msg_id: 邮件 ID
        config: 配置对象

    Returns:
        Optional[Dict]: 邮件详情或 None
    """
    if config is None:
        config = DuckMailConfig.load()

    try:
        api_base = config.api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {mail_token}"}
        session, use_cffi = _create_duckmail_session(config)

        # 标准化 msg_id
        if isinstance(msg_id, str) and msg_id.startswith("/messages/"):
            msg_id = msg_id.split("/")[-1]

        res = _do_request(
            session, use_cffi, "get",
            f"{api_base}/messages/{msg_id}",
            config=config,
            headers=headers
        )

        if res.status_code == 200:
            logger.debug(f"获取邮件详情成功：{msg_id}")
            return res.json()

        logger.warning(f"获取邮件详情失败：HTTP {res.status_code}")
        return None

    except Exception as e:
        logger.exception("获取邮件详情时发生异常", extra={"msg_id": msg_id})
        return None


def wait_for_verification_code(
    mail_token: str,
    timeout: int = 120,
    poll_interval: float = 3.0,
    config: Optional[DuckMailConfig] = None
) -> Optional[str]:
    """
    轮询 DuckMail 等待验证码邮件

    Args:
        mail_token: 邮件访问 token
        timeout: 超时时间（秒）
        poll_interval: 轮询间隔（秒）
        config: 配置对象

    Returns:
        Optional[str]: 验证码或 None
    """
    start_time = time.time()
    seen_ids: set = set()
    poll_count = 0

    logger.info(f"开始轮询验证码，超时={timeout}s", extra={"mail_token": mail_token[:8]})

    while time.time() - start_time < timeout:
        poll_count += 1
        messages = fetch_emails(mail_token, config)

        for msg in messages:
            if not isinstance(msg, dict):
                continue

            msg_id = msg.get("id") or msg.get("@id")
            if not msg_id or msg_id in seen_ids:
                continue

            seen_ids.add(msg_id)
            logger.debug(f"检查新邮件：{msg_id}")

            detail = fetch_email_detail(mail_token, str(msg_id), config)
            if detail:
                content = detail.get("text") or detail.get("html") or ""
                code = extract_verification_code(content)
                if code:
                    elapsed = time.time() - start_time
                    logger.info(f"验证码提取成功：{code}", extra={"elapsed": elapsed})
                    return code

        if poll_count % 5 == 0:
            elapsed = time.time() - start_time
            logger.debug(f"轮询中... {poll_count}次，已耗时{elapsed:.1f}s")

        time.sleep(poll_interval)

    logger.warning(f"验证码轮询超时，共轮询{poll_count}次")
    return None


def extract_verification_code(content: str) -> Optional[str]:
    """
    从邮件内容提取验证码

    支持格式:
    - Grok 格式：XXX-XXX（3 位 -3 位字母数字混合）
    - 6 位纯数字

    Args:
        content: 邮件内容

    Returns:
        Optional[str]: 验证码或 None
    """
    if not content:
        return None

    # 模式 1: Grok 格式 XXX-XXX
    match = re.search(r"(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])", content)
    if match:
        return match.group(1)

    # 模式 2: 带标签的验证码
    match = re.search(
        r"(?:verification code|验证码|your code)[:\s]*[<>\s]*([A-Z0-9]{3}-[A-Z0-9]{3})\b",
        content,
        re.IGNORECASE
    )
    if match:
        return match.group(1)

    # 模式 3: HTML 样式包裹
    match = re.search(
        r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?([A-Z0-9]{3}-[A-Z0-9]{3})[\s\S]*?</p>",
        content
    )
    if match:
        return match.group(1)

    # 模式 4: Subject 行 6 位数字
    match = re.search(r"Subject:.*?(\d{6})", content)
    if match and match.group(1) != "177010":
        return match.group(1)

    # 模式 5: HTML 标签内 6 位数字
    for code in re.findall(r">\s*(\d{6})\s*<", content):
        if code != "177010":
            return code

    # 模式 6: 独立 6 位数字
    for code in re.findall(r"(?<![&#\d])(\d{6})(?![&#\d])", content):
        if code != "177010":
            return code

    logger.debug("未找到匹配的验证码格式")
    return None


# ============================================================
# 模块入口
# ============================================================

if __name__ == "__main__":
    # 测试入口
    config = DuckMailConfig.load()
    print(f"配置加载：API={config.api_base}, Token={config.bearer_token[:8] if config.bearer_token else '未设置'}")

    try:
        email, password, token = create_temp_email(config)
        print(f"邮箱创建：{email}")
        code = wait_for_verification_code(token)
        print(f"验证码：{code}")
    except Exception as e:
        print(f"测试失败：{e}")
