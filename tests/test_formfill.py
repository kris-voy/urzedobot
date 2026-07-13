import logging

from formfill import (
    build_filled_properties,
    fuzzy_match_field,
    normalize_label,
    strip_diacritics,
)


# ---------------------------------------------------------------------------
# strip_diacritics / normalize_label
# ---------------------------------------------------------------------------

def test_strip_diacritics_removes_combining_based_diacritics():
    # a, o, c, e, s, z, z each have an NFKD decomposition into base + combining
    # mark, so the combining mark gets dropped.
    assert strip_diacritics("Zażółć gęślą jaźń") == "Zazołc gesla jazn"


def test_strip_diacritics_leaves_plain_ascii_untouched():
    assert strip_diacritics("Lukasz") == "Lukasz"


def test_strip_diacritics_does_not_fold_case():
    # Ł/ł has no NFKD decomposition (it isn't base-letter + combining mark), so
    # it survives strip_diacritics untouched; this also demonstrates that
    # case-folding is normalize_label's job, not strip_diacritics's.
    assert strip_diacritics("ŁUKASZ") == "ŁUKASZ"


def test_normalize_label_lowercases_and_strips_surrounding_whitespace():
    assert normalize_label("  Numer Sprawy  ") == "numer sprawy"


def test_normalize_label_combines_diacritic_strip_and_case_fold():
    assert normalize_label("  Łukasz  ") == "łukasz"


def test_normalize_label_full_polish_pangram_style_input():
    assert normalize_label("Zażółć Gęślą Jaźń") == "zazołc gesla jazn"


# ---------------------------------------------------------------------------
# fuzzy_match_field
# ---------------------------------------------------------------------------

def test_fuzzy_match_exact_normalized_match_wins(caplog):
    form_data = {"FORM_IMIE": "Jan", "FORM_NUMER_SPRAWY": "123"}
    with caplog.at_level(logging.WARNING, logger="bezkolejki_bot"):
        result = fuzzy_match_field("  form_imie  ", form_data)
    assert result == "Jan"
    # a single exact match is unambiguous, no warning expected
    assert not caplog.records


def test_fuzzy_match_substring_key_is_substring_of_label():
    # config key is short/generic, the site's field label is long/descriptive
    form_data = {"imie": "Jan"}
    assert fuzzy_match_field("Podaj imię wnioskodawcy", form_data) == "Jan"


def test_fuzzy_match_substring_label_is_substring_of_key():
    # site's field label is short/generic, config key is long/descriptive
    form_data = {"numer dokumentu tozsamosci": "ABC123"}
    assert fuzzy_match_field("numer dokumentu", form_data) == "ABC123"


def test_fuzzy_match_ambiguous_picks_longest_most_specific_key(caplog):
    # Both keys share the "numer" token and both match the generic field
    # label "Numer" via substring (label-in-key), but they have different
    # lengths, so the longer/more specific one must win.
    form_data = {
        "numer sprawy": "SPR-1",
        "numer paszportu": "PASS-2",
    }
    with caplog.at_level(logging.WARNING, logger="bezkolejki_bot"):
        result = fuzzy_match_field("Numer", form_data)
    assert result == "PASS-2"  # "numer paszportu" (15 chars) > "numer sprawy" (12 chars)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "fuzzy-matched 2 config keys" in message
    assert "numer paszportu" in message


def test_fuzzy_match_no_candidates_returns_none():
    form_data = {"imie": "Jan"}
    assert fuzzy_match_field("data urodzenia", form_data) is None


# ---------------------------------------------------------------------------
# build_filled_properties
# ---------------------------------------------------------------------------

def test_build_filled_properties_overwrites_existing_value_key():
    field_defs = [{"name": "Imię", "value": "OLD"}]
    form_data = {"imie": "Jan"}
    result = build_filled_properties(field_defs, form_data)
    assert result == [{"name": "Imię", "value": "Jan"}]
    # original list/dict must not be mutated in place
    assert field_defs[0]["value"] == "OLD"


def test_build_filled_properties_updates_all_pre_existing_value_keys():
    field_defs = [{"name": "Imię", "value": "OLD1", "propertyValue": "OLD2"}]
    form_data = {"imie": "Jan"}
    result = build_filled_properties(field_defs, form_data)
    assert result[0]["value"] == "Jan"
    assert result[0]["propertyValue"] == "Jan"
    assert "fieldValue" not in result[0]


def test_build_filled_properties_injects_value_when_no_known_value_key_present():
    field_defs = [{"name": "Imię"}]
    form_data = {"imie": "Jan"}
    result = build_filled_properties(field_defs, form_data)
    assert result == [{"name": "Imię", "value": "Jan"}]


def test_build_filled_properties_no_match_leaves_field_untouched():
    field_defs = [{"name": "Nieznane pole"}]
    form_data = {"imie": "Jan"}
    result = build_filled_properties(field_defs, form_data)
    assert result == [{"name": "Nieznane pole"}]
    assert "value" not in result[0]


def test_build_filled_properties_passes_through_non_dict_items():
    field_defs = ["not-a-dict", 42, None]
    result = build_filled_properties(field_defs, {})
    assert result == ["not-a-dict", 42, None]


def test_build_filled_properties_non_list_input_returns_empty_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="bezkolejki_bot"):
        result = build_filled_properties(None, {})
    assert result == []
    assert len(caplog.records) == 1
    assert "did not return a list" in caplog.records[0].getMessage()


def test_build_filled_properties_dict_input_returns_empty_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="bezkolejki_bot"):
        result = build_filled_properties({"not": "a list"}, {})
    assert result == []
    assert len(caplog.records) == 1
    assert "did not return a list" in caplog.records[0].getMessage()
