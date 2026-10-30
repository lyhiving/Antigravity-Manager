//! Cursor 上游：`cursor-*` 模型透明转发到本地 cursor-relay(8317)。
//!
//! relay 本身已是 OpenAI(`/v1/chat/completions`) 与 Anthropic(`/v1/messages`)
//! 双协议兼容端点，因此这里只做原样转发，不解析也不改写报文。

use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::Value;
use std::time::Duration;

const UPSTREAM: &str = "http://127.0.0.1:8317";
const TIMEOUT_S: u64 = 900;

/// 仅当模型名为 `cursor-auto` 时走 relay；其它名字一律走 Antigravity 正常管线。
/// 鉴权（管理 key / 用户 key）在路由层已完成，这里只做模型分发。
pub fn is_cursor_model(model: &str) -> bool {
    model.trim().eq_ignore_ascii_case("cursor-auto")
}

pub async fn forward(path: &str, headers: &HeaderMap, body: &Value) -> Response {
    match try_forward(path, headers, body).await {
        Ok(resp) => resp,
        Err((status, msg)) => (status, msg).into_response(),
    }
}

async fn try_forward(
    path: &str,
    headers: &HeaderMap,
    body: &Value,
) -> Result<Response, (StatusCode, String)> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(TIMEOUT_S))
        .build()
        .map_err(|e| {
            (
                StatusCode::BAD_GATEWAY,
                format!("cursor upstream client: {e}"),
            )
        })?;

    let mut req = client
        .post(format!("{UPSTREAM}{path}"))
        .header(header::CONTENT_TYPE, "application/json")
        .json(body);

    // 鉴权沿用 Antigravity 管理端的 key（路由层已校验过客户端），不引入第二套密钥。
    // relay 与本进程共用同一个 API_KEY，因此这里直取即可。
    // 管理 key / 用户 key 的校验逻辑不在这里改，本文件只负责按模型转发。
    match std::env::var("API_KEY") {
        Ok(key) if !key.is_empty() => {
            req = req.header(header::AUTHORIZATION, format!("Bearer {key}"));
        }
        _ => {
            if let Some(v) = headers
                .get(header::AUTHORIZATION)
                .cloned()
                .or_else(|| headers.get("x-api-key").cloned())
            {
                req = req.header(header::AUTHORIZATION, v);
            }
        }
    }

    // 客户端 UA 决定 relay 的 dialect(Claude/Cursor/OpenAI)，版本头决定 Anthropic 特性协商。
    for name in [
        header::USER_AGENT.as_str(),
        "anthropic-version",
        "anthropic-beta",
    ] {
        if let Some(v) = headers.get(name) {
            req = req.header(name, v.clone());
        }
    }

    let resp = req
        .send()
        .await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("cursor upstream: {e}")))?;

    let status = resp.status();
    let content_type = resp
        .headers()
        .get(header::CONTENT_TYPE)
        .cloned()
        .unwrap_or_else(|| HeaderValue::from_static("application/json"));

    // bytes_stream 直通，SSE 增量不缓冲。
    let mut out = Response::new(Body::from_stream(resp.bytes_stream()));
    *out.status_mut() = status;
    out.headers_mut().insert(header::CONTENT_TYPE, content_type);
    Ok(out)
}
