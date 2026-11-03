"""Offline OpenAI-compatible stub used only to verify the Dify workflow wiring.

It is not part of the submitted app. Start it on port 8010 and configure the
Dify OpenAI-API-compatible provider to point at http://host.docker.internal:8010/v1.
"""

from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8010
MODEL = "gpt-4o-mini"


def build_card(prompt: str) -> str:
    match = re.search(r"<capsule_data>(.*?)</capsule_data>", prompt, re.S)
    if not match:
        return "【记忆罐头铺】收到一段记忆，但这次没有找到可用的罐头数据。"
    try:
        data = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return "【记忆罐头铺】罐头数据没有解析成功，请检查 companion 服务的返回。"

    capsule = data.get("capsule", {})
    echoes = data.get("echoes", [])
    stats = data.get("stats", {})

    lines = [
        f"【记忆罐头 #{int(capsule.get('can_no', 0)):04d}】",
        f"口味：{capsule.get('flavor', '未知')} · 心情：{capsule.get('mood', '未知')}",
        f"封存时间：{capsule.get('created_at_label', '未知')}",
        "",
        f"「{capsule.get('text', '')}」",
        "",
        "—— 从旧罐头里听见的回声 ——",
    ]
    if echoes:
        for echo in echoes[:3]:
            score = float(echo.get("score", 0.0))
            lines.append(
                f"· #{int(echo.get('can_no', 0)):04d}（{echo.get('created_at_label', '')}）"
                f" {echo.get('text', '')}〔相似度 {score:.2f}〕"
            )
    else:
        lines.append("· 这次货架上很安静，还没有相似的旧罐头。")

    lines.extend(
        [
            "",
            f"货架现在共有 {stats.get('pantry_total', '?')} 罐，"
            f"其中 {stats.get('user_total', '?')} 罐来自客人。",
            "",
            "今日小任务：把这罐记忆的某一个细节，讲给一个今天还没说过话的人听。",
        ]
    )
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print("[mock-llm]", fmt % args, flush=True)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/v1/models"):
            self._json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL,
                            "object": "model",
                            "created": 1700000000,
                            "owned_by": "mock",
                        }
                    ],
                }
            )
            return
        self._json({"error": {"message": "not found"}}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": {"message": "invalid json"}}, status=400)
            return

        messages = request.get("messages", [])
        prompt = "\n".join(str(item.get("content", "")) for item in messages)
        content = build_card(prompt)

        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for piece in (content[:120], content[120:280], content[280:]):
                chunk = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": MODEL,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            end = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(content), "total_tokens": len(prompt) + len(content)},
            }
            self.wfile.write(f"data: {json.dumps(end, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return

        self._json(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(prompt),
                    "completion_tokens": len(content),
                    "total_tokens": len(prompt) + len(content),
                },
            }
        )


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"mock OpenAI-compatible server on {HOST}:{PORT}", flush=True)
    server.serve_forever()
