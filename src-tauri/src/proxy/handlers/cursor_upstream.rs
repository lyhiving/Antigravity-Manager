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

/// 只有显式 `cursor-` 前缀的模型走 relay；`auto` 等泛名仍归 Antigravity 的别名映射。
pub fn is_cursor_model(model: &str) -> bool {
    model.trim().to_ascii_lowercase().starts_with("cursor-")
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
    let key = std::env::var("CURSOR_UPSTREAM_KEY").unwrap_or_else(|_| "sk-kya-cursor".to_string());
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(TIMEOUT_S))
        .build()
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("cursor upstream client: {e}")))?;

    let mut req = client
        .post(format!("{UPSTREAM}{path}"))
        .header(header::AUTHORIZATION, format!("Bearer {key}"))
        .header(header::CONTENT_TYPE, "application/json")
        .json(body);

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
