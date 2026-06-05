"""
QQ机器人服务 — Webhook回调 + 消息推送

功能:
  1. QQPusher:  向用户/群/频道推送消息（可在任何脚本中导入使用）
  2. Webhook:   接收QQ机器人回调消息（Flask路由）

配置方式:
  在 data/qq_config.json 中配置（首次运行时自动生成模板）:

{
  "qq_bot": {
    "enabled": true,
    "app_id": "102870731",
    "client_secret": "your_secret",
    "sandbox": false,
    "push_targets": [
      {"type": "user", "id": "USER_OPENID"},
      {"type": "group", "id": "GROUP_OPENID"}
    ]
  }
}

使用示例:
  from models.qq_webhook import push_message, push_markdown

  # 推送到所有配置的目标
  push_message("Hello from b_quant!")

  # 推送 Markdown 格式
  push_markdown("## 每日指导报告\\n今天发现3只候选股")

  # 使用 Pusher 实例
  from models.qq_webhook import QQPusher
  pusher = QQPusher()
  pusher.send_user_message("USER_OPENID", "Hello!")
"""
import json
import time
import hashlib
import hmac
import os
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, request, jsonify

# ============ 配置加载 ============

CONFIG_DIR = Path(__file__).resolve().parents[1] / "data"
CONFIG_PATH = CONFIG_DIR / "qq_config.json"
DEFAULT_CONFIG = {
    "qq_bot": {
        "enabled": False,
        "app_id": "",
        "client_secret": "",
        "sandbox": False,
        "push_targets": []
    }
}


def _load_config() -> dict:
    """加载 QQ 配置，不存在则创建模板"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[qq_webhook] 配置模板已创建: {CONFIG_PATH}")
        return DEFAULT_CONFIG
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_qq_config() -> dict:
    """获取 QQ Bot 配置"""
    return _load_config().get("qq_bot", DEFAULT_CONFIG["qq_bot"])


# ============ QQPusher 推送类 ============

class QQPusher:
    """QQ机器人消息推送器"""

    # QQ Bot API 端点
    API_SANDBOX = "https://sandbox.api.sgroup.qq.com"
    API_PROD = "https://api.sgroup.qq.com"
    TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

    def __init__(self, config: dict = None):
        """
        初始化推送器。

        参数:
            config: QQ Bot 配置 dict，含 app_id, client_secret, sandbox, push_targets
                    传 None 则从 data/qq_config.json 自动加载
        """
        if config is None:
            config = get_qq_config()
        self.app_id = str(config.get("app_id", ""))
        self.client_secret = config.get("client_secret", "")
        self.sandbox = config.get("sandbox", False)
        self.push_targets = config.get("push_targets", [])
        self.enabled = config.get("enabled", False) and bool(self.app_id) and bool(self.client_secret)
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._api_base = self.API_SANDBOX if self.sandbox else self.API_PROD

    # ---- Token 管理 ----

    def _get_access_token(self) -> Optional[str]:
        """获取/刷新 access_token"""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        try:
            resp = requests.post(self.TOKEN_URL, json={
                "appId": self.app_id,
                "clientSecret": self.client_secret,
            }, timeout=10)
            data = resp.json()
            self._access_token = data.get("access_token", "")
            expires_in = int(data.get("expires_in", 7200))
            self._token_expires_at = time.time() + expires_in
            return self._access_token
        except Exception as e:
            print(f"[QQPusher] 获取token失败: {e}")
            return None

    # ---- 发送消息 ----

    def _send(self, method: str, url: str, payload: dict) -> bool:
        """发送 HTTP 请求到 QQ API"""
        token = self._get_access_token()
        if not token:
            print("[QQPusher] 无有效token，发送失败")
            return False

        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.request(method, url, json=payload, headers=headers, timeout=10)
            if resp.status_code != 200:
                print(f"[QQPusher] 发送失败 HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            print(f"[QQPusher] 发送异常: {e}")
            return False

    def send_user_message(self, openid: str, content: str, msg_type: int = 0) -> bool:
        """
        发送私聊消息 (C2C)。

        参数:
            openid:   用户 openid
            content:  消息内容
            msg_type: 0=文本, 2=Markdown
        """
        if msg_type == 2:
            payload = {
                "msg_type": 2,
                "markdown": {"content": content},
            }
        else:
            payload = {
                "msg_type": 0,
                "content": content,
            }
        url = f"{self._api_base}/v2/users/{openid}/messages"
        return self._send("POST", url, payload)

    def send_group_message(self, group_openid: str, content: str, msg_type: int = 0) -> bool:
        """发送群消息"""
        if msg_type == 2:
            payload = {
                "msg_type": 2,
                "markdown": {"content": content},
            }
        else:
            payload = {
                "msg_type": 0,
                "content": content,
            }
        url = f"{self._api_base}/v2/groups/{group_openid}/messages"
        return self._send("POST", url, payload)

    def send_channel_message(self, channel_id: str, content: str, msg_type: int = 0) -> bool:
        """发送频道消息"""
        if msg_type == 2:
            payload = {
                "msg_type": 2,
                "markdown": {"content": content},
            }
        else:
            payload = {
                "msg_type": 0,
                "content": content,
            }
        url = f"{self._api_base}/v2/channels/{channel_id}/messages"
        return self._send("POST", url, payload)

    def push_to_all(self, content: str, msg_type: int = 0) -> dict:
        """
        向所有配置的 push_targets 推送消息。

        返回:
            {"success": 3, "fail": 0, "total": 3}
        """
        if not self.enabled:
            return {"success": 0, "fail": 0, "total": 0, "message": "QQ推送未启用"}

        success = 0
        fail = 0
        for target in self.push_targets:
            ttype = target.get("type", "user")
            tid = target.get("id", "")
            if not tid:
                continue
            if ttype == "group":
                ok = self.send_group_message(tid, content, msg_type)
            elif ttype == "channel":
                ok = self.send_channel_message(tid, content, msg_type)
            else:  # user (default)
                ok = self.send_user_message(tid, content, msg_type)
            if ok:
                success += 1
            else:
                fail += 1

        return {"success": success, "fail": fail, "total": success + fail}

    # ---- 分段推送（长文本自动拆分） ----

    def push_long_text(self, text: str, max_len: int = 1900, msg_type: int = 0) -> dict:
        """
        推送长文本，自动分段发送。

        参数:
            text:     要发送的完整文本
            max_len:  每段最大字符数（QQ限制约2000）
            msg_type: 0=文本, 2=Markdown
        """
        if not self.enabled:
            return {"success": 0, "fail": 0, "total": 0, "message": "QQ推送未启用"}

        # 按换行分块，尽量在段落边界断开
        lines = text.split("\n")
        chunks = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > max_len:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            chunks.append(current)

        total_ok = 0
        total_fail = 0
        for i, chunk in enumerate(chunks):
            prefix = f"({i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
            result = self.push_to_all(prefix + chunk, msg_type)
            total_ok += result["success"]
            total_fail += result["fail"]
            if i < len(chunks) - 1:
                time.sleep(0.5)  # 避免频率限制

        return {"success": total_ok, "fail": total_fail, "total": total_ok + total_fail}


# ============ 便捷函数 ============

_pusher_instance: Optional[QQPusher] = None


def _get_pusher() -> QQPusher:
    """获取全局推送器实例（懒加载）"""
    global _pusher_instance
    if _pusher_instance is None:
        _pusher_instance = QQPusher()
    return _pusher_instance


def push_message(content: str) -> dict:
    """
    推送文本消息到所有配置目标。
    可在任何脚本中直接调用: from models.qq_webhook import push_message
    """
    return _get_pusher().push_to_all(content, msg_type=0)


def push_markdown(content: str) -> dict:
    """
    推送 Markdown 消息到所有配置目标。
    """
    return _get_pusher().push_to_all(content, msg_type=2)


def push_long_message(text: str) -> dict:
    """
    推送长文本消息，自动分段。
    """
    return _get_pusher().push_long_text(text, msg_type=0)


def is_push_enabled() -> bool:
    """检查QQ推送是否已启用"""
    return _get_pusher().enabled


# ============ Flask Webhook（保留原有回调功能）============

app = Flask(__name__)


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """验证QQ机器人回调签名"""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.route("/qq/callback", methods=["POST"])
def qq_callback():
    """QQ机器人消息回调接口"""
    cfg = get_qq_config()
    secret = cfg.get("client_secret", "")

    payload = request.get_data()
    data = request.get_json() or {}

    # 处理验证请求（首次配置回调地址时）
    if data.get("op") == 13:  # VERIFY
        challenge = data.get("d", {}).get("plain_token", "")
        event_ts = data.get("d", {}).get("event_ts", "")
        sign_content = f"{event_ts}{challenge}"
        signature = hmac.new(secret.encode(), sign_content.encode(), hashlib.sha256).hexdigest()
        return jsonify({
            "plain_token": challenge,
            "signature": signature
        })

    # 处理消息事件
    event_type = data.get("t", "")
    event_data = data.get("d", {})

    if event_type in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE", "AT_MESSAGE_CREATE"):
        author = event_data.get("author", {})
        user_openid = (
            author.get("user_openid")
            or author.get("id")
            or event_data.get("author", {}).get("member_openid")
        )
        group_openid = event_data.get("group_openid")
        guild_id = event_data.get("guild_id")
        channel_id = event_data.get("channel_id")
        content = event_data.get("content", "")

        print("\n" + "=" * 60)
        print("【QQ机器人 - 收到消息】")
        print(f"  内容: {content}")
        print(f"  用户openid: {user_openid}")
        if group_openid:
            print(f"  群openid: {group_openid}")
        if channel_id:
            print(f"  频道ID: {channel_id}, 频道组: {guild_id}")
        print("=" * 60 + "\n")

    return jsonify({"code": 0})


def run_webhook(host: str = "0.0.0.0", port: int = 8080):
    """启动 Webhook 服务"""
    print("\n" + "=" * 60)
    print("QQ机器人 Webhook 服务启动")
    print(f"回调地址: http://<你的公网IP>:{port}/qq/callback")
    print("请在QQ开放平台配置此回调地址")
    print("=" * 60 + "\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run_webhook()
