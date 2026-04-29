"""
LM Studio Chat - FastAPI アプリケーション
"""

import httpx
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config

# ─── アプリケーション設定 ───────────────────────────────────────────────

app = FastAPI(title="LM Studio Chat")

# テンプレート・静的ファイルの設定
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# チャット履歴の保存先
HISTORY_FILE = BASE_DIR / "chat_history.json"

# ─── モデル定義 ─────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 2048


class LoginRequest(BaseModel):
    username: str
    password: str


class SettingsUpdate(BaseModel):
    lm_studio_host: str
    lm_studio_port: int
    app_username: str
    app_password: str
    lm_studio_api_key: str


# ─── チャット履歴管理 ───────────────────────────────────────────────────


def load_history() -> dict:
    """チャット履歴を読み込む"""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_history(history: dict) -> None:
    """チャット履歴を保存する"""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_session_history(session_id: str) -> list[dict]:
    """セッションの履歴を取得"""
    history = load_history()
    return history.get(session_id, [])


def append_to_history(session_id: str, role: str, content: str) -> None:
    """履歴に追加"""
    history = load_history()
    if session_id not in history:
        history[session_id] = []

    history[session_id].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat()
    })

    # 履歴は最新100件に制限
    if len(history[session_id]) > 100:
        history[session_id] = history[session_id][-100:]

    save_history(history)


def clear_session_history(session_id: str) -> None:
    """セッション履歴をクリア"""
    history = load_history()
    if session_id in history:
        del history[session_id]
    save_history(history)


# ─── LM Studio API 通信 ─────────────────────────────────────────────────


async def fetch_models() -> list[dict]:
    """LM Studio から利用可能なモデル一覧を取得"""
    try:
        api_key = config.Config.get_api_key()
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                config.Config.get_models_endpoint(),
                headers=headers
            )
            response.raise_for_status()
            data = response.json()
            return data.get("data", [])
    except Exception as e:
        print(f"モデル取得エラー: {e}")
        return []


async def send_to_lm_studio(
    messages: list[dict],
    model: str = "",
    temperature: float = 0.7,
    max_tokens: int = 2048
) -> str:
    """LM Studio にチャットリクエストを送信"""
    api_key = config.Config.get_api_key()
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if model:
        payload["model"] = model

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                config.Config.get_api_endpoint(),
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except httpx.TimeoutException:
        return "⚠️ タイムアウトしました。LM Studio サーバーが実行中か確認してください。"
    except httpx.HTTPStatusError as e:
        return f"⚠️ HTTPエラー: {e.response.status_code} - {e.response.text}"
    except Exception as e:
        return f"⚠️ エラーが発生しました: {str(e)}"


# ─── 認証ミドルウェア ───────────────────────────────────────────────────


def check_auth(request: Request) -> Optional[str]:
    """セッション認証をチェック"""
    session = request.cookies.get("session_token")
    if not session:
        return None
    return session  # 簡易的にトークン自体をユーザーIDとして使用


# ─── ルート ─────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """ログインページ / チャットページ"""
    session = request.cookies.get("session_token")
    
    if session:
        # ✅ request を context 外に明示的に指定
        return templates.TemplateResponse(
            name="chat.html",
            context={
                "authenticated": True,
                "session_id": session,
                "history": get_session_history(session),
                "models": await fetch_models(),
                "lm_studio_url": config.Config.get_lm_studio_url(),
            },
            request=request
        )
    
    return templates.TemplateResponse(
        name="login.html",
        context={
            "authenticated": False,
            "error": "ユーザー名またはパスワードが異なります。",
        },
        request=request
    )

@app.post("/login")
async def login(request: Request):
    """ログイン処理"""
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")

    if config.Config.is_authenticated(username, password):
        response = HTMLResponse(
            """<script>window.location.href='/';</script>"""
        )
        response.set_cookie(
            key="session_token",
            value=f"user_{username}_{datetime.now().timestamp()}",
            httponly=True,
            max_age=86400,
        )
        return response

    return templates.TemplateResponse("login.html", {
        "request": request,
        "authenticated": False,
        "error": "ユーザー名またはパスワードが異なります。",
    })


@app.get("/logout")
async def logout(request: Request):
    """ログアウト"""
    response = HTMLResponse("""<script>window.location.href='/';</script>""")
    response.delete_cookie(key="session_token")
    return response


@app.get("/api/models")
async def api_models(request: Request):
    """モデル一覧取得 API"""
    session = check_auth(request)
    if not session:
        raise HTTPException(status_code=401, detail="認証が必要です")

    models = await fetch_models()
    return JSONResponse({"models": models})


@app.post("/api/chat")
async def api_chat(request: Request):
    """チャットリクエスト API"""
    session = check_auth(request)
    if not session:
        raise HTTPException(status_code=401, detail="認証が必要です")

    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "")
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens", 2048)

    if not messages:
        return JSONResponse(
            {"error": "メッセージが空です"}, status_code=400
        )

    # LM Studio に送信
    assistant_reply = await send_to_lm_studio(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    # 履歴に保存
    append_to_history(session, "user", messages[-1]["content"])
    append_to_history(session, "assistant", assistant_reply)

    return JSONResponse({
        "reply": assistant_reply,
        "history": get_session_history(session),
    })


@app.get("/api/history/{session_id}")
async def api_get_history(session_id: str, request: Request):
    """履歴取得 API"""
    _ = check_auth(request)  # 認証チェック（トークンベース）
    return JSONResponse({"history": get_session_history(session_id)})


@app.post("/api/clear-history")
async def api_clear_history(request: Request):
    """履歴クリア API"""
    session = check_auth(request)
    if not session:
        raise HTTPException(status_code=401, detail="認証が必要です")

    clear_session_history(session)
    return JSONResponse({"status": "cleared"})


@app.get("/api/settings")
async def api_get_settings(request: Request):
    """設定取得 API"""
    session = check_auth(request)
    if not session:
        raise HTTPException(status_code=401, detail="認証が必要です")

    return JSONResponse({
        "lm_studio_host": os.getenv("LM_STUDIO_HOST", "127.0.0.1"),
        "lm_studio_port": os.getenv("LM_STUDIO_PORT", "1234"),
        "has_api_key": bool(os.getenv("LM_STUDIO_API_KEY", "")),
        "app_username": os.getenv("APP_USERNAME", "admin"),
    })


@app.post("/api/update-settings")
async def api_update_settings(request: Request):
    """設定更新 API（.env ファイルを書き換え）"""
    session = check_auth(request)
    if not session:
        raise HTTPException(status_code=401, detail="認証が必要です")

    body = await request.json()
    env_path = BASE_DIR / ".env"

    env_content = f"""# LM Studio サーバー接続設定
LM_STUDIO_HOST={body.get('lm_studio_host', '127.0.0.1')}
LM_STUDIO_PORT={body.get('lm_studio_port', '1234')}

# LM Studio の API キー（設定していない場合は空白）
LM_STUDIO_API_KEY={body.get('lm_studio_api_key', '')}

# アプリ認証設定
APP_USERNAME={body.get('app_username', 'admin')}
APP_PASSWORD={body.get('app_password', 'changeme')}

# サーバー設定
APP_HOST=0.0.0.0
APP_PORT=8000
"""

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_content)

    # 環境変数を再読み込み
    load_dotenv(env_path, override=True)

    return JSONResponse({"status": "updated", "message": "設定を更新しました。サーバーを再起動してください。"})


# ─── メイン ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=config.Config.get_app_host(),
        port=config.Config.get_app_port(),
        reload=True,
    )
