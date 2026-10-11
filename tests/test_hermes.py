import time


# ---- Markdown to Telegram HTML -------------------------------------------------------------

def test_markdown_bold_code_and_escaping(hermes):
    out = hermes.md_to_html("**Hi** `x<1` & <tag>")
    assert "<b>Hi</b>" in out
    assert "<code>x&lt;1</code>" in out
    assert "&amp;" in out and "&lt;tag&gt;" in out


def test_markdown_headers_and_checkboxes(hermes):
    out = hermes.md_to_html("# Title\n[ ] todo\n[x] done")
    assert "<b>Title</b>" in out and "☐" in out and "✅" in out


def test_markdown_code_block_becomes_pre(hermes):
    assert "<pre>" in hermes.md_to_html("```python\nprint(1)\n```")


# ---- Quota-aware model routing --------------------------------------------------------------

def test_cooldown_uses_the_api_retry_delay(hermes):
    assert hermes._cooldown_secs("429 retry_delay { seconds: 17 }") == 19.0


def test_cooldown_backs_off_an_hour_for_daily_quota(hermes):
    assert hermes._cooldown_secs("Quota exceeded: requests per day") == 3600.0
    assert hermes._cooldown_secs("something else") == 60.0


def test_quota_and_unavailable_errors_are_told_apart(hermes):
    assert hermes._is_quota_error("429 RESOURCE_EXHAUSTED")
    assert not hermes._is_quota_error("500 internal error")
    assert hermes._is_model_unavailable("404 model is not found for API version v1beta")
    assert not hermes._is_model_unavailable("429 quota")


def test_auto_mode_skips_a_model_that_is_cooling_down(hermes, monkeypatch):
    monkeypatch.setattr(hermes, "_MODEL_COOLDOWN", {})
    monkeypatch.setattr(hermes, "_DEAD_MODELS", set())
    best = hermes.TEXT_MODEL_FALLBACKS[0]
    hermes._mark_exhausted(best, "429 retry_delay { seconds: 30 }")
    order = hermes._models_to_try("auto")
    assert best not in order and order[0] == hermes.TEXT_MODEL_FALLBACKS[1]
    assert hermes._models_to_try("smart")[0] == best  # "smart" ignores cooldowns


def test_easy_mode_starts_at_the_high_volume_model(hermes, monkeypatch):
    monkeypatch.setattr(hermes, "_MODEL_COOLDOWN", {})
    monkeypatch.setattr(hermes, "_DEAD_MODELS", set())
    order = hermes._models_to_try("easy")
    assert order[0] == hermes.MODEL_LITE and hermes.TEXT_MODEL_FALLBACKS[0] not in order


def test_dead_models_are_dropped_and_a_full_outage_retries_everything(hermes, monkeypatch):
    monkeypatch.setattr(hermes, "_MODEL_COOLDOWN", {})
    monkeypatch.setattr(hermes, "_DEAD_MODELS", {hermes.TEXT_MODEL_FALLBACKS[0]})
    assert hermes.TEXT_MODEL_FALLBACKS[0] not in hermes._models_to_try("auto")
    monkeypatch.setattr(hermes, "_DEAD_MODELS", set(hermes.TEXT_MODEL_FALLBACKS))
    assert hermes._models_to_try("auto") == list(hermes.TEXT_MODEL_FALLBACKS)


def test_everything_cooling_down_still_returns_models(hermes, monkeypatch):
    monkeypatch.setattr(hermes, "_DEAD_MODELS", set())
    monkeypatch.setattr(hermes, "_MODEL_COOLDOWN", {m: time.time() + 999 for m in hermes.TEXT_MODEL_FALLBACKS})
    assert hermes._models_to_try("auto") == list(hermes.TEXT_MODEL_FALLBACKS)


def test_trivial_messages_are_routed_to_the_cheap_tier(hermes):
    assert hermes._is_trivial("  Thanks!! ")
    assert not hermes._is_trivial("Summarise this PDF about tenancy law")


# ---- Vector helpers --------------------------------------------------------------------------

def test_vectors_survive_packing_into_a_blob(hermes):
    vec = [0.5, -1.25, 3.0]
    assert list(hermes._unpack(hermes._pack(vec))) == vec


def test_cosine_similarity(hermes):
    assert abs(hermes._cosine([1, 0], [1, 0]) - 1.0) < 1e-6
    assert abs(hermes._cosine([1, 0], [0, 1])) < 1e-6
    assert hermes._cosine([0, 0], [1, 1]) == 0.0  # zero vector does not divide by zero


# ---- Memory (SQLite) -------------------------------------------------------------------------

def test_recent_messages_come_back_oldest_first(memory):
    memory.store_message("user", "first")
    memory.store_message("assistant", "second")
    recent = memory.get_recent(10)
    assert len(recent) == 2
    assert "first" in str(recent[0]) and "second" in str(recent[1])


def test_semantic_recall_finds_the_related_older_message(memory):
    memory.store_message("user", "the heater in my room is broken")
    memory.store_message("user", "football practice is on thursday")
    found = memory.get_semantic("when will the heater be fixed", exclude_ids=set(), k=1)
    assert "heater" in found and "football" not in found


def test_semantic_recall_respects_excluded_ids(memory):
    memory.store_message("user", "the heater in my room is broken")
    ids = memory.get_recent_ids(10)
    assert "heater" not in memory.get_semantic("heater", exclude_ids=ids, k=3)


def test_clear_memory_empties_the_store(memory):
    memory.store_message("user", "remember this")
    memory.clear_memory()
    assert memory.get_recent(10) == []


# ---- Formatters ------------------------------------------------------------------------------

def test_email_sender_and_date_formatting(hermes):
    assert hermes._fmt_sender('"Jane Doe" <jane@example.com>') == "Jane Doe"
    assert hermes._fmt_sender("plain@example.com") == "plain@example.com"
    assert hermes._fmt_date("Fri, 22 May 2026 20:35:00 +0000") == "May 22, 20:35"
    assert hermes._fmt_date("not a date") == "not a date"


def test_match_time_is_shown_in_new_york_time(hermes):
    match = {
        "opponents": [{"opponent": {"name": "NAVI"}}, {"opponent": {"name": "G2"}}],
        "begin_at": "2026-09-17T18:00:00Z",
        "tournament": {"name": "Major"},
    }
    text = hermes._fmt_navi_match(match, "csgo")
    assert "NAVI vs G2" in text and "14:00 NYC" in text and "[CS2]" in text and "Major" in text


def test_match_with_missing_fields_does_not_crash(hermes):
    text = hermes._fmt_navi_match({}, "dota2")
    assert "TBD" in text and "Dota 2" in text and "Unknown tournament" in text
