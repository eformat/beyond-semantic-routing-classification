//! T-U3 (prompt extraction), T-U4 (char-boundary truncation) and the
//! conversation-details extraction (session observability).

use beyond_routing_filter::prompt::{PROMPT_PREVIEW_CHARS, conversation_details, extract_prompt, truncate_chars};

/// Extract from a JSON literal.
fn extract(json: &str) -> Option<String> {
    extract_prompt(json.as_bytes())
}

// -----------------------------------------------------------------------------
// T-U3 — extraction
// -----------------------------------------------------------------------------

#[test]
fn t_u3_chat_string_content() {
    let got = extract(r#"{"messages":[{"role":"user","content":"What is the capital of France?"}]}"#);
    assert_eq!(got.as_deref(), Some("What is the capital of France?"));
}

#[test]
fn t_u3_chat_array_of_parts_concatenates_text_parts() {
    let got = extract(
        r#"{"messages":[{"role":"user","content":[
             {"type":"text","text":"first"},
             {"type":"image_url","image_url":{"url":"http://example/x.png"}},
             {"type":"text","text":"second"}]}]}"#,
    );
    assert_eq!(
        got.as_deref(),
        Some("first\nsecond"),
        "only type=text parts, joined by newline"
    );
}

#[test]
fn t_u3_multiple_messages_picks_the_last_user_message() {
    let got = extract(
        r#"{"messages":[
             {"role":"system","content":"you are helpful"},
             {"role":"user","content":"first question"},
             {"role":"assistant","content":"an answer"},
             {"role":"user","content":"second question"}]}"#,
    );
    assert_eq!(
        got.as_deref(),
        Some("second question"),
        "the newest user turn is what should be classified"
    );
}

#[test]
fn t_u3_system_only_yields_no_prompt() {
    let got = extract(r#"{"messages":[{"role":"system","content":"you are helpful"}]}"#);
    assert_eq!(got, None, "no user turn means nothing to classify");
}

#[test]
fn t_u3_prompt_string() {
    let got = extract(r#"{"prompt":"legacy completions prompt"}"#);
    assert_eq!(got.as_deref(), Some("legacy completions prompt"));
}

#[test]
fn t_u3_prompt_array_joined_by_newline() {
    let got = extract(r#"{"prompt":["one","two"]}"#);
    assert_eq!(got.as_deref(), Some("one\ntwo"));
}

#[test]
fn t_u3_input_string_and_array() {
    assert_eq!(extract(r#"{"input":"embed me"}"#).as_deref(), Some("embed me"));
    assert_eq!(extract(r#"{"input":["a","b"]}"#).as_deref(), Some("a\nb"));
}

#[test]
fn t_u3_messages_take_precedence_over_prompt_and_input() {
    let got = extract(r#"{"messages":[{"role":"user","content":"chat"}],"prompt":"legacy","input":"embed"}"#);
    assert_eq!(got.as_deref(), Some("chat"));
}

#[test]
fn t_u3_prompt_takes_precedence_over_input() {
    assert_eq!(
        extract(r#"{"prompt":"legacy","input":"embed"}"#).as_deref(),
        Some("legacy")
    );
}

#[test]
fn t_u3_malformed_json_yields_none_not_an_error() {
    assert_eq!(extract("{not json at all"), None);
    assert_eq!(extract_prompt(b"\xff\xfe\x00binary"), None);
}

#[test]
fn t_u3_empty_body_yields_none() {
    assert_eq!(extract_prompt(b""), None);
}

#[test]
fn t_u3_json_that_is_not_an_object_yields_none() {
    assert_eq!(extract("[1,2,3]"), None, "a JSON array is not a chat request");
    assert_eq!(extract("\"just a string\""), None);
    assert_eq!(extract("null"), None);
}

#[test]
fn t_u3_object_without_any_recognised_field_yields_none() {
    assert_eq!(extract(r#"{"model":"gpt-4","temperature":0.7}"#), None);
}

#[test]
fn t_u3_whitespace_only_prompt_yields_none() {
    assert_eq!(extract(r#"{"prompt":"   \n\t "}"#), None, "blank is not a prompt");
}

#[test]
fn t_u3_extracted_text_is_trimmed() {
    assert_eq!(extract(r#"{"prompt":"  padded  "}"#).as_deref(), Some("padded"));
}

#[test]
fn t_u3_non_string_content_yields_none() {
    assert_eq!(extract(r#"{"messages":[{"role":"user","content":42}]}"#), None);
}

// -----------------------------------------------------------------------------
// T-U4 — truncation
// -----------------------------------------------------------------------------

#[test]
fn t_u4_truncation_counts_characters_not_bytes() {
    // Each of these is 4 bytes but 1 char; a byte slice at 2 would panic.
    let emoji = "🙂🙂🙂🙂";
    assert_eq!(truncate_chars(emoji, 2), "🙂🙂");
    assert_eq!(truncate_chars(emoji, 0), "");
    assert_eq!(truncate_chars(emoji, 4), emoji);
    assert_eq!(
        truncate_chars(emoji, 99),
        emoji,
        "a ceiling above the length is a no-op"
    );
}

#[test]
fn t_u4_truncation_never_panics_on_a_multibyte_boundary() {
    let cjk = "日本語のテキストです";
    for limit in 0..=cjk.chars().count() + 5 {
        let cut = truncate_chars(cjk, limit);
        assert!(
            cjk.starts_with(cut),
            "truncation must be a prefix of the input at limit {limit}"
        );
        assert!(cut.chars().count() <= limit.min(cjk.chars().count()));
    }
}

#[test]
fn t_u4_mixed_width_text_cuts_on_a_char_boundary() {
    let mixed = "ab🙂日cd";
    assert_eq!(truncate_chars(mixed, 3), "ab🙂");
    assert_eq!(truncate_chars(mixed, 4), "ab🙂日");
}

#[test]
fn t_u4_combining_sequences_are_cut_by_scalar_value() {
    // `é` here is `e` + U+0301. Truncation is defined on chars (scalar
    // values), so cutting at 1 keeps the base letter and drops the mark —
    // still valid UTF-8, which is the property that matters for the RPC.
    let text = "e\u{301}xyz";
    let cut = truncate_chars(text, 1);
    assert_eq!(cut, "e");
    assert!(text.starts_with(cut));
}

// -----------------------------------------------------------------------------
// Conversation details — the session module's stable conversation key
// -----------------------------------------------------------------------------

const FIRST_TURN: &str = r#"{"messages":[{"role":"system","content":"You are a coding agent."},{"role":"user","content":"Design a cache eviction policy"}]}"#;

#[test]
fn conversation_fingerprint_is_stable_across_turns() {
    // Turn 2 of the same conversation: messages[0] is unchanged, the user
    // turn is new. The fingerprint must hold; the preview must move.
    let second_turn = r#"{"messages":[{"role":"system","content":"You are a coding agent."},{"role":"user","content":"Design a cache eviction policy"},{"role":"assistant","content":"Use LRU with..."},{"role":"user","content":"Now compare with LFU"}]}"#;

    let t1 = conversation_details(FIRST_TURN.as_bytes()).expect("messages body");
    let t2 = conversation_details(second_turn.as_bytes()).expect("messages body");

    assert_eq!(
        t1.fingerprint, t2.fingerprint,
        "messages[0] is unchanged across turns: one conversation, one fingerprint"
    );
    assert_eq!(t1.prompt_preview, "Design a cache eviction policy");
    assert_eq!(t2.prompt_preview, "Now compare with LFU");
    assert_eq!(t1.turns, 2);
    assert_eq!(t2.turns, 4, "the message count grows per turn");
}

#[test]
fn conversation_fingerprint_separates_conversations() {
    let other = r#"{"messages":[{"role":"system","content":"You are a different agent."},{"role":"user","content":"Design a cache eviction policy"}]}"#;
    let a = conversation_details(FIRST_TURN.as_bytes()).expect("messages body");
    let b = conversation_details(other.as_bytes()).expect("messages body");
    assert_ne!(a.fingerprint, b.fingerprint, "different first message: different conversation");
}

#[test]
fn conversation_details_flatten_and_truncate_the_preview() {
    let long = format!("line one\n\n  line two{}", " x".repeat(200));
    let body = serde_json::json!({"messages": [{"role": "user", "content": long}]}).to_string();
    let details = conversation_details(body.as_bytes()).expect("messages body");
    assert!(!details.prompt_preview.contains('\n'), "flattened to one line");
    assert!(
        details.prompt_preview.chars().count() <= PROMPT_PREVIEW_CHARS,
        "preview is truncated to the metadata-safe length"
    );
}

#[test]
fn conversation_details_reject_non_messages_bodies() {
    assert!(conversation_details(b"not json").is_none());
    assert!(conversation_details(br#"{"prompt":"single-shot"}"#).is_none());
    assert!(conversation_details(br#"{"messages":[]}"#).is_none());
}
