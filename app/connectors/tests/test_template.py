import pytest

from connectors.template import UnknownVariable, referenced_variables, render


def test_plain_text_passes_through_untouched():
    assert render("hello world", {}) == "hello world"


def test_adversarial_caption_is_not_corrupted():
    # The old replace_keys() did undelimited str.replace() on serialized
    # JSON, so a caption containing "TEXT", "POST_ID", or a stray quote would
    # corrupt the outbound request. None of that applies here: only
    # {{ TOKENS }} are ever substituted.
    caption = 'My "TEXT" post about POST_ID numbers and {curly} braces'
    node = {"payload": {"status": "{{ CAPTION }}", "note": caption}}
    rendered = render(node, {"CAPTION": caption})
    assert rendered["payload"]["note"] == caption
    assert rendered["payload"]["status"] == caption


def test_embedded_token_interpolates_as_string():
    rendered = render("Bearer {{ ACCESS_TOKEN }}", {"ACCESS_TOKEN": "abc123"})
    assert rendered == "Bearer abc123"


def test_whole_value_token_preserves_type():
    rendered = render({"ids": "{{ MEDIA_IDS }}"}, {"MEDIA_IDS": [1, 2, 3]})
    assert rendered["ids"] == [1, 2, 3]


def test_unknown_variable_raises():
    with pytest.raises(UnknownVariable):
        render("{{ NOPE }}", {})


def test_unknown_variable_in_embedded_position_raises():
    with pytest.raises(UnknownVariable):
        render("Bearer {{ NOPE }}", {})


def test_nested_structure_is_fully_rendered():
    node = {
        "headers": {"Authorization": "Bearer {{ TOKEN }}"},
        "payload": {"items": [{"caption": "{{ CAPTION }}"}, "{{ EXTRA }}"]},
    }
    context = {"TOKEN": "t", "CAPTION": "c", "EXTRA": "e"}
    rendered = render(node, context)
    assert rendered == {
        "headers": {"Authorization": "Bearer t"},
        "payload": {"items": [{"caption": "c"}, "e"]},
    }


def test_referenced_variables_collects_all_tokens():
    node = {"a": "{{ X }}", "b": ["{{ Y }}", "plain"]}
    assert referenced_variables(node) == {"X", "Y"}


def test_credential_value_containing_json_metacharacters_is_safe():
    # A credential (or user text) containing a double-quote or backslash must
    # never be able to break the surrounding JSON structure, because we never
    # serialize-then-substitute — we substitute into the parsed tree.
    tricky = 'value with "quotes" and \\ backslash'
    rendered = render({"payload": {"caption": "{{ CAPTION }}"}}, {"CAPTION": tricky})
    assert rendered["payload"]["caption"] == tricky
