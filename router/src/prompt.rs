//! Prompt extraction from an OpenAI-shaped request body (SPEC §4.4).
//!
//! Deliberately narrow and entirely pure: no I/O, no config, no context. A
//! classifier filter must never become a request validator, so every
//! unrecognised shape — including invalid JSON — yields `None` rather than an
//! error.

use serde_json::Value;

/// Extract the prompt to classify from a raw request body.
///
/// Resolution order (first match wins):
/// 1. `messages[]` — the **last** element whose `role == "user"`; its `content`
///    is either a string, or an array of parts whose `type == "text"` joined
///    by newlines.
/// 2. `prompt` — a string, or an array of strings joined by newlines.
/// 3. `input` — a string, or an array of strings joined by newlines.
///
/// Returns `None` for a non-JSON body, a body that is not a JSON object, or a
/// body with none of the above (or whose extraction is blank after trimming).
#[must_use]
pub fn extract_prompt(body: &[u8]) -> Option<String> {
    let root: Value = serde_json::from_slice(body).ok()?;
    let object = root.as_object()?;

    let candidate = object
        .get("messages")
        .and_then(|m| from_messages(m.as_array()?))
        .or_else(|| object.get("prompt").and_then(text_or_array))
        .or_else(|| object.get("input").and_then(text_or_array))?;

    let trimmed = candidate.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(trimmed.to_owned())
}

/// Truncate `text` to at most `max_chars` characters on a **char boundary**.
///
/// A byte slice would panic in the middle of a multi-byte code point, which is
/// reachable from any request body, so the cut is counted in `char_indices`.
#[must_use]
pub fn truncate_chars(text: &str, max_chars: usize) -> &str {
    match text.char_indices().nth(max_chars) {
        Some((byte_index, _)) => &text[..byte_index],
        None => text,
    }
}

/// The `content` of the last `role == "user"` message.
fn from_messages(messages: &[Value]) -> Option<String> {
    messages
        .iter()
        .rev()
        .find(|m| m.get("role").and_then(Value::as_str) == Some("user"))
        .and_then(|m| m.get("content"))
        .and_then(content_text)
}

/// A message `content`: a plain string, or an array of `{type, text}` parts.
fn content_text(content: &Value) -> Option<String> {
    match content {
        Value::String(s) => Some(s.clone()),
        Value::Array(parts) => {
            let joined = parts
                .iter()
                .filter(|p| p.get("type").and_then(Value::as_str) == Some("text"))
                .filter_map(|p| p.get("text").and_then(Value::as_str))
                .collect::<Vec<_>>()
                .join("\n");
            (!joined.is_empty()).then_some(joined)
        },
        _ => None,
    }
}

/// A `prompt` / `input` field: a string, or an array of strings.
fn text_or_array(value: &Value) -> Option<String> {
    match value {
        Value::String(s) => Some(s.clone()),
        Value::Array(items) => {
            let joined = items.iter().filter_map(Value::as_str).collect::<Vec<_>>().join("\n");
            (!joined.is_empty()).then_some(joined)
        },
        _ => None,
    }
}

// -----------------------------------------------------------------------------
// Conversation details (session observability)
// -----------------------------------------------------------------------------

/// The maximum prompt-preview length published as filter metadata.
///
/// praxis drops `filter_metadata` values over 256 bytes; a preview well under
/// that survives the metadata path AND keeps the access-log record legible.
pub const PROMPT_PREVIEW_CHARS: usize = 120;

/// Conversation identity + current-turn context, extracted from the request
/// body for session observability (the session module's first piece).
///
/// - `fingerprint`: SHA-256 over the **first** message's serialized content,
///   hex-truncated to 16 chars. `messages[0]` never changes across the turns
///   of one multi-turn conversation, so this is the stable conversation key:
///   every turn of the same conversation shares one fingerprint, distinct
///   conversations stay distinct.
/// - `prompt_preview`: the current turn's prompt (what the classifier saw),
///   flattened to one line and truncated.
/// - `turns`: the message count of the conversation so far.
#[derive(Clone, Debug, PartialEq)]
pub struct ConversationDetails {
    /// SHA-256 over the first message's serialized content, hex-truncated to
    /// 16 chars — the stable conversation key (see the struct docs).
    pub fingerprint: String,
    /// The current turn's prompt, flattened to one line and truncated to
    /// [`PROMPT_PREVIEW_CHARS`].
    pub prompt_preview: String,
    /// The message count of the conversation so far.
    pub turns: usize,
}

/// Extract [`ConversationDetails`] from a request body.
///
/// Returns `None` for a non-JSON body, a body without a `messages` array, or
/// an empty one. (Completions without `messages` — a bare `prompt`/`input` —
/// are single-shot by shape: no multi-turn identity to track, and the
/// prompt itself is already the classifier's input.)
#[must_use]
pub fn conversation_details(body: &[u8]) -> Option<ConversationDetails> {
    let root: Value = serde_json::from_slice(body).ok()?;
    let messages = root.as_object()?.get("messages")?.as_array()?;
    if messages.is_empty() {
        return None;
    }

    // Fingerprint over the first message's serialized content. Serializing the
    // Value (rather than hashing raw body bytes) keeps the fingerprint stable
    // across key-order and whitespace differences in the request envelope.
    let first = serde_json::to_vec(&messages[0]).ok()?;
    let fingerprint = hex_prefix(&first, 16);

    let prompt_preview = from_messages(messages)
        .map(|p| flatten_line(&p))
        .map(|p| truncate_chars(&p, PROMPT_PREVIEW_CHARS).to_owned())
        .unwrap_or_default();

    Some(ConversationDetails {
        fingerprint,
        prompt_preview,
        turns: messages.len(),
    })
}

/// Lowercase hex of the SHA-256 of `data`, truncated to `chars` characters.
fn hex_prefix(data: &[u8], chars: usize) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(data);
    digest.iter().map(|b| format!("{b:02x}")).take(chars).collect()
}

/// Collapse control characters and whitespace runs to single spaces, so the
/// preview renders as one line in the access-log record and the flow graphic.
fn flatten_line(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pending_space = false;
    for ch in text.chars() {
        if ch.is_whitespace() || ch.is_control() {
            pending_space = !out.is_empty();
        } else {
            if pending_space {
                out.push(' ');
                pending_space = false;
            }
            out.push(ch);
        }
    }
    out
}
